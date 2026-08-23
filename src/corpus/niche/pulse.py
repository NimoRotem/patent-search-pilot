"""A small, cheap factory snapshot, published once a minute.

WHY THIS EXISTS SEPARATELY FROM `status.py`

`status.py` is the completeness REPORT: it groups every publication by CPC, by language and by
authority, and the artefact it writes is 3.2 MB. That is the right thing to read when deciding
what to acquire next, and the wrong thing to poll: it runs every five minutes and it cannot be
made to answer "is the factory moving right now".

The pulse answers exactly that, in under 10 KB, from counts that are index-only or bounded:

  * where discovery has reached, and whether it is still finding anything
  * the acquisition pool, and how many documents arrived in the last minute
  * the parse queue, including how many jobs are stuck behind their retry ceiling
  * embedding progress and the money spent against its cap
  * how many vectors are searchable, which is the number the search actually feels

Every entry carries `generated_at`, and the reader decides staleness from it rather than from a
process claiming to be alive. A publisher that dies leaves a snapshot that ages, which is the
honest failure mode: the page says stale, it does not say zero.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

UTC = timezone.utc

#  Keep two hours at one a minute. The page draws its per-minute deltas from this, so the history
#  travels with the snapshot and the reader needs no state of its own.
HISTORY = int(os.environ.get("NICHE_PULSE_HISTORY", "120"))

NICHE_SQL = {
    "manifest": """
        SELECT count(*) AS publications,
               count(*) FILTER (WHERE has_complete_claims) AS claims_complete,
               count(*) FILTER (WHERE has_complete_description) AS description_complete,
               count(*) FILTER (WHERE fetch_status = 'completed') AS fetched
          FROM niche_corpus.niche_publications
    """,
    "families": """
        SELECT count(DISTINCT COALESCE(NULLIF(family_id, ''), 'p:' || publication_id)) AS families
          FROM niche_corpus.niche_publications
    """,
    "parse_queue": """
        SELECT status,
               count(*) AS n,
               count(*) FILTER (WHERE attempt >= 3) AS retried
          FROM niche_corpus.niche_parse_jobs GROUP BY status
    """,
    "chunks": "SELECT count(*) AS chunks FROM niche_corpus.niche_chunks",
    "embedding": """
        SELECT status, count(*) AS n FROM niche_corpus.niche_embedding_stage GROUP BY status
    """,
    "vectors": """
        SELECT count(*) AS vectors,
               count(*) FILTER (WHERE tantivy_indexed_at IS NOT NULL) AS lexical
          FROM niche_corpus.niche_vector_documents
    """,
    "budget": """
        SELECT limit_usd, spent_usd, reserved_usd FROM niche_corpus.embedding_budget
         WHERE budget_key = %s
    """,
    "watermarks": """
        SELECT max(last_value) AS reached
          FROM niche_corpus.niche_discovery_watermarks WHERE source = 'local'
    """,
    "batches": """
        SELECT status, count(*) AS n FROM niche_corpus.niche_embedding_batches GROUP BY status
    """,
}

#  The acquisition pool lives in the active corpus database, and this reads it with the same
#  read-only DSN every other niche process uses. Never write here.
POOL_SQL = """
    SELECT status, count(*) AS n FROM fulltext_fetch_task GROUP BY status
"""
POOL_RATE_SQL = """
    SELECT count(*) AS n FROM fulltext_fetch_event
     WHERE attempted_at > now() - interval '5 minutes' AND outcome = 'hit'
"""


def _rows(cursor, sql, params=None) -> list[dict]:
    cursor.execute(sql, params or ())
    return [dict(row) for row in cursor.fetchall() or []]


def _one(cursor, sql, params=None) -> dict:
    rows = _rows(cursor, sql, params)
    return rows[0] if rows else {}


def _by_status(rows, key="n") -> dict:
    return {str(row.get("status") or "unknown"): int(row.get(key) or 0) for row in rows}


def collect(connection_factory, *, source_factory=None, budget_key: str = "") -> dict:
    """One snapshot. Bounded work: no sequential scan of chunks, no per-CPC grouping."""
    budget_key = budget_key or os.environ.get("GEMINI_EMBED_BUDGET_KEY", "niche_full_v1")
    started = time.monotonic()
    snapshot: dict = {"generated_at": datetime.now(UTC).isoformat()}
    with connection_factory() as connection, connection.cursor() as cursor:
        manifest = _one(cursor, NICHE_SQL["manifest"])
        snapshot["publications"] = int(manifest.get("publications") or 0)
        snapshot["claims_complete"] = int(manifest.get("claims_complete") or 0)
        snapshot["description_complete"] = int(manifest.get("description_complete") or 0)
        snapshot["fetched"] = int(manifest.get("fetched") or 0)
        snapshot["families"] = int(_one(cursor, NICHE_SQL["families"]).get("families") or 0)
        parse_rows = _rows(cursor, NICHE_SQL["parse_queue"])
        snapshot["parse"] = _by_status(parse_rows)
        snapshot["parse_stuck"] = sum(int(row.get("retried") or 0) for row in parse_rows)
        snapshot["chunks"] = int(_one(cursor, NICHE_SQL["chunks"]).get("chunks") or 0)
        snapshot["embedding"] = _by_status(_rows(cursor, NICHE_SQL["embedding"]))
        vectors = _one(cursor, NICHE_SQL["vectors"])
        snapshot["vectors"] = int(vectors.get("vectors") or 0)
        snapshot["lexical_indexed"] = int(vectors.get("lexical") or 0)
        snapshot["batches"] = _by_status(_rows(cursor, NICHE_SQL["batches"]))
        budget = _one(cursor, NICHE_SQL["budget"], (budget_key,))
        snapshot["budget"] = {
            "limit_usd": float(budget.get("limit_usd") or 0),
            "spent_usd": float(budget.get("spent_usd") or 0),
            "reserved_usd": float(budget.get("reserved_usd") or 0),
        }
        snapshot["discovery_reached"] = int(
            _one(cursor, NICHE_SQL["watermarks"]).get("reached") or 0
        )
    if source_factory is not None:
        try:
            with source_factory() as connection, connection.cursor() as cursor:
                snapshot["pool"] = _by_status(_rows(cursor, POOL_SQL))
                snapshot["pool_hits_5m"] = int(_one(cursor, POOL_RATE_SQL).get("n") or 0)
        except Exception as exc:  # noqa: BLE001 - a pool read must never stop the pulse
            snapshot["pool_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    snapshot["collect_ms"] = int((time.monotonic() - started) * 1000)
    return snapshot


def _delta(current: dict, previous: dict | None, key: str) -> int:
    if not previous:
        return 0
    return int(current.get(key) or 0) - int(previous.get(key) or 0)


def with_rates(snapshot: dict, previous: dict | None) -> dict:
    """Per-minute movement, normalised by the real gap between the two snapshots.

    A rate computed against a nominal interval lies whenever the publisher was slow or restarted,
    and this number is the whole point of the page."""
    out = dict(snapshot)
    seconds = 0.0
    if previous and previous.get("generated_at"):
        try:
            before = datetime.fromisoformat(str(previous["generated_at"]))
            after = datetime.fromisoformat(str(snapshot["generated_at"]))
            seconds = max((after - before).total_seconds(), 0.0)
        except ValueError:
            seconds = 0.0
    out["interval_seconds"] = round(seconds, 1)
    if seconds >= 5:
        scale = 60.0 / seconds
        out["rates_per_minute"] = {
            key: round(_delta(snapshot, previous, key) * scale, 1)
            for key in ("publications", "fetched", "chunks", "vectors",
                        "claims_complete", "description_complete")
        }
        embedded_now = int((snapshot.get("embedding") or {}).get("complete") or 0)
        embedded_before = int((previous.get("embedding") or {}).get("complete") or 0)
        out["rates_per_minute"]["embedded"] = round((embedded_now - embedded_before) * scale, 1)
    else:
        out["rates_per_minute"] = {}
    return out


def publish(document: dict, *, uri: str = "", path: str = "") -> list[str]:
    """Write the pulse where the app can read it. GCS is authoritative, disk is the local copy."""
    written = []
    if path:
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        written.append(str(target))
    if uri:
        from urllib.parse import urlsplit

        from google.cloud import storage

        parsed = urlsplit(uri)
        blob = storage.Client().bucket(parsed.netloc).blob(parsed.path.lstrip("/"))
        blob.cache_control = "no-store"
        blob.upload_from_string(
            json.dumps(document, ensure_ascii=False), content_type="application/json"
        )
        written.append(uri)
    return written


def load(uri: str) -> dict:
    from urllib.parse import urlsplit

    from google.cloud import storage

    parsed = urlsplit(uri)
    blob = storage.Client().bucket(parsed.netloc).blob(parsed.path.lstrip("/"))
    if not blob.exists():
        return {}
    return json.loads(blob.download_as_bytes().decode("utf-8"))


def append_history(document: dict, snapshot: dict, limit: int = HISTORY) -> dict:
    history = list(document.get("history") or [])
    history.append({
        key: snapshot.get(key)
        for key in ("generated_at", "publications", "families", "fetched", "chunks", "vectors",
                    "claims_complete", "description_complete", "parse", "embedding", "pool")
    })
    return {"now": snapshot, "history": history[-limit:]}


def run(argv=None) -> int:
    """`python -m src.corpus.niche.pulse --loop 60`, one process, on the discovery VM."""
    import argparse

    from .database import connection_factory, require_dsn

    parser = argparse.ArgumentParser(prog="python -m src.corpus.niche.pulse")
    parser.add_argument("--niche-dsn", default="")
    parser.add_argument("--source-dsn", default="")
    parser.add_argument("--uri", default=os.environ.get("NICHE_PULSE_URI", ""))
    parser.add_argument("--path", default=os.environ.get("NICHE_PULSE_PATH", ""))
    parser.add_argument("--loop", type=int, default=0, help="seconds between snapshots")
    args = parser.parse_args(argv)

    niche = connection_factory(
        require_dsn(args.niche_dsn or os.environ.get("NICHE_DATABASE_URL"), "NICHE_DATABASE_URL"),
        application_name="niche-pulse",
    )
    source_dsn = args.source_dsn or os.environ.get("NICHE_SOURCE_DATABASE_URL") or ""
    source = connection_factory(source_dsn, application_name="niche-pulse-source") if source_dsn \
        else None

    document = {}
    if args.uri:
        try:
            document = load(args.uri)
        except Exception:  # noqa: BLE001 - a missing or unreadable object starts a new history
            document = {}
    while True:
        snapshot = with_rates(
            collect(niche, source_factory=source),
            (document.get("history") or [{}])[-1] if document.get("history") else None,
        )
        document = append_history(document, snapshot)
        for target in publish(document, uri=args.uri, path=args.path):
            print(json.dumps({"result": "published", "target": target,
                              "collect_ms": snapshot.get("collect_ms"),
                              "vectors": snapshot.get("vectors")}), flush=True)
        if args.loop <= 0:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(run())
