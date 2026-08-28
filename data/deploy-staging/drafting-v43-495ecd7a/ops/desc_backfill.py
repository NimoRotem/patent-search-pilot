"""Chunk and embed the stored description paragraphs that search cannot see.

Measured 2026-08-22 on the live corpus: `paragraphs` holds 14,379,018 rows across 432,056
publications, but only 34,350 publications have any `kind='paragraph'` chunk. So the full
description of roughly 398,000 publications is sitting in Postgres and is invisible to both the
dense and the lexical channel. Closing that is the cheapest recall win available: the text is
already fetched, parsed and stored, and only the embedding is missing.

Two rules shape the design.

**Nothing is written to the active corpus.** Search time writes into the retrieval database have
blocked live searches behind index maintenance before, and adding 12.7M vectors to a 94 GB HNSW
index that already does not fit in the 62 GB of the corpus box would make every dense query worse,
not better. So the output lands in `chunks_stage_v3`, a new table with no vector index. Which of
these rows go into the hot tier and which go to a cold shard is a corpus release decision, made
later against the sizing budget, not a side effect of this job.

**Every vector carries its version.** `chunks` has no model, dimension, chunker or release column,
which is why a model change today means rewriting 26.7M rows and rebuilding the index in place.
The stage table carries all four from the first row.

Resumable: each shard keeps its own paragraph id watermark, so a kill and restart costs one batch.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

BACKFILL_ENV_FILE = os.environ.get("BACKFILL_ENV_FILE")
if BACKFILL_ENV_FILE:
    load_dotenv(BACKFILL_ENV_FILE, override=False)

MAX_CHARS = 8000                      # same clip as src/chunker.py, so stage rows match live rows
MIN_CHARS = 20                        # a paragraph shorter than this is a numbering artefact
SUB = 200                             # inputs per Vertex call
FETCH = 4000                          # paragraphs pulled per DB round
WORKERS = int(os.environ.get("EMBED_WORKERS", "12"))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))
TASK_TYPE = os.environ.get("EMBED_TASK_TYPE", "RETRIEVAL_DOCUMENT")
MAX_ATTEMPTS = int(os.environ.get("EMBED_MAX_ATTEMPTS", "3"))
CHUNKER_VERSION = "chunker-2026.08.22"

# Namespace for the advisory lock. Any constant works; it only has to be stable and not collide
# with another user of pg_try_advisory_lock on this database.
LEASE_NAMESPACE = 0x64657363          # "desc"
CORPUS_RELEASE = os.environ.get("CORPUS_RELEASE", "v3-desc-backfill-20260822")

PG = dict(host=os.environ.get("PGHOST", "10.128.0.53"),
          port=int(os.environ.get("PGPORT", "5433")),
          dbname=os.environ.get("PGDATABASE", "patents"),
          user=os.environ.get("PGUSER", "patents"),
          password=os.environ.get("PGPASSWORD"))

DDL = """
CREATE TABLE IF NOT EXISTS chunks_stage_v3 (
  id               bigserial PRIMARY KEY,
  publication_id   bigint NOT NULL,
  kind             text   NOT NULL,
  ref_id           bigint,
  coord            jsonb,
  lang             text,
  text             text   NOT NULL,
  token_count      int,
  embedding        vector(768),
  embed_model      text   NOT NULL,
  embed_dim        int    NOT NULL,
  task_type        text   NOT NULL,
  chunker_version  text   NOT NULL,
  corpus_release   text   NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_stage_v3_kind_ref
  ON chunks_stage_v3 (kind, ref_id) WHERE ref_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_stage_v3_pub ON chunks_stage_v3 (publication_id);

CREATE TABLE IF NOT EXISTS chunks_stage_v3_progress (
  shard        int PRIMARY KEY,
  shards       int NOT NULL,
  pass_name    text NOT NULL,
  watermark    bigint NOT NULL DEFAULT 0,
  rows_done    bigint NOT NULL DEFAULT 0,
  chars_done   bigint NOT NULL DEFAULT 0,
  api_calls    bigint NOT NULL DEFAULT 0,
  started_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
"""

_local = threading.local()


CALL_TIMEOUT_MS = int(os.environ.get("EMBED_CALL_TIMEOUT_MS", "120000"))


def _client():
    """A timeout here is not optional, it is the difference between a retry and a wedge.

    Measured 2026-08-22: run 58597 stopped producing rows at 08:00:29 and sat in
    `futex_wait_queue` with two of its seven threads in `wait_woken` on ESTAB sockets to
    172.217.118.4:443. The retry loop below only catches EXCEPTIONS, and a hung socket never
    raises one, so `ThreadPoolExecutor.map` blocked for ever and the whole process stalled while
    looking perfectly alive. Bounding the call turns that silent wedge into an ordinary retry."""
    if not hasattr(_local, "c"):
        from google import genai
        from google.genai.types import HttpOptions
        _local.c = genai.Client(vertexai=True, project="nimo-gpt", location="us-central1",
                                http_options=HttpOptions(timeout=CALL_TIMEOUT_MS))
    return _local.c


def _connect():
    return psycopg.connect(row_factory=dict_row, connect_timeout=30, **PG)


def _clip(s):
    s = (s or "").strip()
    return s[:MAX_CHARS] if len(s) > MAX_CHARS else s


def _tok(s):
    return max(1, len(s) // 4)


def _vec(e):
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


def _log(event, **kv):
    """One structured line per event. Greppable, and parseable without a log shipper."""
    kv["event"] = event
    kv["ts"] = round(time.time(), 3)
    print(json.dumps(kv, sort_keys=True), flush=True)


def retryable(exc):
    """Transport and capacity failures are worth another attempt; a bad request is not.

    Split out so it can be tested without a network."""
    s = str(exc).lower()
    return any(t in s for t in ("429", "resource_exhausted", "quota", "503", "500",
                                "deadline", "timeout", "timed out", "unavailable",
                                "connection reset", "broken pipe"))


def backoff_delay(attempt, base=0.7, cap=20.0, rand=None):
    """Exponential with full jitter. Without jitter every worker that hit the same 429 retries in
    the same millisecond and reproduces the burst that caused it."""
    rand = rand if rand is not None else random.random
    return min(base * (2 ** attempt), cap) * (0.5 + 0.5 * rand())


def _embed(texts):
    """One Vertex call, bounded by MAX_ATTEMPTS and by the client deadline in _client()."""
    from google.genai.types import EmbedContentConfig
    for attempt in range(MAX_ATTEMPTS):
        t0 = time.time()
        try:
            r = _client().models.embed_content(
                model=EMBED_MODEL, contents=list(texts),
                config=EmbedContentConfig(output_dimensionality=EMBED_DIM, task_type=TASK_TYPE))
            out = [list(e.values) for e in r.embeddings]
            _log("embed_call", n=len(texts), attempt=attempt, ms=int((time.time() - t0) * 1000),
                 ok=True)
            return out
        except Exception as exc:                                   # noqa: BLE001
            ms = int((time.time() - t0) * 1000)
            last = attempt == MAX_ATTEMPTS - 1
            _log("embed_call", n=len(texts), attempt=attempt, ms=ms, ok=False,
                 retryable=retryable(exc), err=str(exc)[:200])
            if last or not retryable(exc):
                raise
            time.sleep(backoff_delay(attempt))
    raise RuntimeError("unreachable")


def lease_key(pass_name, shard):
    """Stable 32 bit key for one (pass, shard). zlib.crc32 rather than hash(), because hash() is
    randomised per process and would hand two workers different keys for the same work."""
    return zlib.crc32(f"{pass_name}:{shard}".encode()) & 0x7FFFFFFF


def _take_lease(conn, pass_name, shard):
    """Session level advisory lock, held for the life of this process on its own connection.

    Two workers on the same (pass, shard) is not a data hazard, the unique index and ON CONFLICT
    see to that, but it is duplicate paid embedding calls and two writers fighting over one
    watermark row. It happened on 2026-08-22. The lock is released automatically when the
    connection drops, so a killed worker never leaves the lease stuck."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s, %s) AS got",
                    (LEASE_NAMESPACE, lease_key(pass_name, shard)))
        return bool(cur.fetchone()["got"])


def _filter_staged(conn, rows):
    """Drop paragraphs already in the stage table BEFORE paying to embed them.

    ON CONFLICT DO NOTHING already made a rerun correct, but correct is not the same as cheap: it
    embedded the row, sent it to Vertex, then threw the result away at insert time. This uses the
    unique index on (kind, ref_id) and turns a rerun from full price into free."""
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    with conn.cursor() as cur:
        cur.execute("SELECT ref_id FROM chunks_stage_v3 "
                    "WHERE kind = 'paragraph' AND ref_id = ANY(%s)", (ids,))
        have = {r["ref_id"] for r in cur.fetchall()}
    return [r for r in rows if r["id"] not in have]


def _already_chunked_pubs(conn):
    """The 34,350 publications whose paragraphs are already live chunks. Held in memory because
    `chunks` has no index on ref_id, so a per batch anti-join there is a sequential scan of a
    290 GB table."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT publication_id AS p FROM chunks WHERE kind = 'paragraph'")
        return {r["p"] for r in cur.fetchall()}


def _pass_sql(pass_name):
    """Which paragraphs this pass covers. The niche goes first: its recall is the product.

    `publications.tier` is the corpus tier assigned at ingest. Pass `core` takes the seeded core,
    pass `rest` takes everything else, so an interrupted run has already delivered the part that
    matters most."""
    if pass_name == "core":
        return ("JOIN publications pub ON pub.id = p.publication_id",
                "AND pub.tier = 'core'")
    if pass_name == "rest":
        return ("JOIN publications pub ON pub.id = p.publication_id",
                "AND (pub.tier IS DISTINCT FROM 'core')")
    return ("", "")


def _range_for(shard, shards, conn):
    """Contiguous id range per shard, NOT `id %% shards = shard`.

    Modulo cannot use an index, so every shard would scan all 14.4M rows of a 16 GB table: with
    N shards that is N full scans of the same table on a corpus box that is already serving live
    searches and measured 20 to 23 percent iowait. A contiguous range is an index range scan on
    `paragraphs_pkey`, so the shards touch disjoint pages and the total work is one pass, not N."""
    with conn.cursor() as cur:
        cur.execute("SELECT min(id) lo, max(id) hi FROM paragraphs")
        r = cur.fetchone()
    lo, hi = r["lo"] or 0, r["hi"] or 0
    span = (hi - lo + 1 + shards - 1) // shards
    return lo + shard * span, lo + (shard + 1) * span


def run(shard, shards, pass_name):
    conn = _connect()
    conn.execute("SET lock_timeout = '15s'")
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()

    # Exit 0, not an error: another worker legitimately holds this pass. Supervisor is configured
    # autorestart=unexpected with exitcodes=0, so a clean refusal does not become a restart loop.
    if not _take_lease(conn, pass_name, shard):
        _log("lease_refused", pass_name=pass_name, shard=shard,
             key=lease_key(pass_name, shard))
        conn.close()
        sys.exit(0)
    _log("lease_taken", pass_name=pass_name, shard=shard, key=lease_key(pass_name, shard))

    with conn.cursor() as cur:
        cur.execute("""INSERT INTO chunks_stage_v3_progress (shard, shards, pass_name)
                       VALUES (%s, %s, %s) ON CONFLICT (shard) DO NOTHING""",
                    (shard, shards, pass_name))
        conn.commit()
        cur.execute("""SELECT watermark, rows_done, chars_done, api_calls, pass_name, shards
                       FROM chunks_stage_v3_progress WHERE shard = %s""", (shard,))
        st = cur.fetchone()
    # A watermark only means anything within one (pass, shard geometry). Change either and it
    # points into a range this process no longer owns, which silently skips the work instead of
    # doing it. Reset on both.
    if st["pass_name"] != pass_name or st["shards"] != shards:
        with conn.cursor() as cur:
            cur.execute("""UPDATE chunks_stage_v3_progress
                           SET pass_name = %s, shards = %s, watermark = 0 WHERE shard = %s""",
                        (pass_name, shards, shard))
            conn.commit()
        st = dict(st, watermark=0)

    skip = _already_chunked_pubs(conn)
    join, filt = _pass_sql(pass_name)
    lo, hi = _range_for(shard, shards, conn)
    watermark = max(st["watermark"], lo - 1)
    rows_done, chars_done, api_calls = st["rows_done"], st["chars_done"], st["api_calls"]
    t0 = time.time()
    print(f"[backfill] shard {shard}/{shards} ids [{lo:,},{hi:,}) pass={pass_name} "
          f"watermark={watermark:,} skip_pubs={len(skip):,} workers={WORKERS}", flush=True)

    def work(sub):
        embs = _embed([r["ctext"] for r in sub])
        return list(zip(sub, embs))

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        while True:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT p.id, p.publication_id, p.para_no, p.heading, p.page_no, p.lang, p.text
                    FROM paragraphs p {join}
                    WHERE p.id > %s AND p.id < %s {filt}
                    ORDER BY p.id LIMIT %s""", (watermark, hi, FETCH))
                batch = cur.fetchall()
            if not batch:
                break
            watermark = batch[-1]["id"]

            todo = []
            for r in batch:
                if r["publication_id"] in skip:
                    continue
                t = _clip(r["text"])
                if len(t) < MIN_CHARS:
                    continue
                todo.append(dict(r, ctext=t))

            before = len(todo)
            todo = _filter_staged(conn, todo)
            if before != len(todo):
                _log("skipped_already_staged", n=before - len(todo), batch=before)

            if todo:
                subs = [todo[i:i + SUB] for i in range(0, len(todo), SUB)]
                pairs = [p for s in ex.map(work, subs) for p in s]
                api_calls += len(subs)
                # COPY straight into the table would abort the whole batch on the first unique
                # violation, and a restart after a reset watermark produces exactly that. Land in
                # a temp table, then insert conflict-free, so a rerun is idempotent.
                cols = ("publication_id, kind, ref_id, coord, lang, text, token_count, "
                        "embedding, embed_model, embed_dim, task_type, chunker_version, "
                        "corpus_release")
                with conn.cursor() as cur:
                    cur.execute("CREATE TEMP TABLE IF NOT EXISTS _stage_in "
                                "(LIKE chunks_stage_v3 INCLUDING DEFAULTS) ON COMMIT DROP")
                    cur.execute("TRUNCATE _stage_in")
                    with cur.copy(f"COPY _stage_in ({cols}) FROM STDIN") as cp:
                        for r, e in pairs:
                            coord = json.dumps({"para_no": r["para_no"], "heading": r["heading"],
                                                "page_no": r["page_no"]})
                            cp.write_row((r["publication_id"], "paragraph", r["id"], coord,
                                          r["lang"] or "en", r["ctext"], _tok(r["ctext"]), _vec(e),
                                          EMBED_MODEL, EMBED_DIM, TASK_TYPE, CHUNKER_VERSION,
                                          CORPUS_RELEASE))
                    cur.execute(f"INSERT INTO chunks_stage_v3 ({cols}) SELECT {cols} "
                                f"FROM _stage_in ON CONFLICT DO NOTHING")
                rows_done += len(pairs)
                chars_done += sum(len(r["ctext"]) for r, _ in pairs)

            with conn.cursor() as cur:
                cur.execute("""UPDATE chunks_stage_v3_progress
                               SET watermark = %s, rows_done = %s, chars_done = %s,
                                   api_calls = %s, updated_at = now()
                               WHERE shard = %s""",
                            (watermark, rows_done, chars_done, api_calls, shard))
            conn.commit()

            el = time.time() - t0
            _log("progress", shard=shard, pass_name=pass_name, rows=rows_done,
                 chars=chars_done, api_calls=api_calls, watermark=watermark,
                 rows_this_process=rows_done - st["rows_done"],
                 rate_per_s=round((rows_done - st["rows_done"]) / max(el, 1), 1))

    print(f"[backfill] shard {shard} pass={pass_name} COMPLETE rows={rows_done:,} "
          f"chars={chars_done:,} in {time.time() - t0:.0f}s", flush=True)
    conn.close()


def status():
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) c, sum(length(text)) ch FROM chunks_stage_v3")
        print("stage rows:", cur.fetchone())
        cur.execute("""SELECT shard, pass_name, watermark, rows_done, chars_done, api_calls,
                              updated_at FROM chunks_stage_v3_progress ORDER BY shard""")
        for r in cur.fetchall():
            print(r)
    conn.close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["status"]:
        status()
    else:
        run(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "core")
