"""Continuous niche corpus completeness and worker status reporting."""
from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc

DATABASE_SUMMARY_SQL = """
SELECT count(*) AS total_publications,
       count(*) FILTER (WHERE has_title) AS title_complete,
       count(*) FILTER (WHERE has_abstract) AS abstract_complete,
       count(*) FILTER (WHERE has_complete_claims) AS claims_complete,
       count(*) FILTER (WHERE has_complete_description) AS description_complete,
       count(*) FILTER (WHERE has_citations) AS citations_complete,
       count(*) FILTER (WHERE NOT has_complete_claims) AS missing_claims,
       count(*) FILTER (WHERE NOT has_complete_description) AS missing_descriptions,
       count(*) FILTER (
           WHERE NOT (has_complete_claims AND has_complete_description)
       ) AS missing_full_text
  FROM niche_corpus.niche_publications
"""

DATABASE_FAMILY_SQL = """
SELECT count(*) AS total_families,
       count(*) FILTER (WHERE has_title) AS title_complete,
       count(*) FILTER (WHERE has_abstract) AS abstract_complete,
       count(*) FILTER (WHERE has_complete_claims) AS claims_complete,
       count(*) FILTER (WHERE has_complete_description) AS description_complete,
       count(*) FILTER (WHERE has_citations) AS citations_complete
  FROM (
      SELECT COALESCE(NULLIF(family_id, ''), 'publication:' || publication_id) AS family_key,
             bool_or(has_title) AS has_title,
             bool_or(has_abstract) AS has_abstract,
             bool_or(has_complete_claims) AS has_complete_claims,
             bool_or(has_complete_description) AS has_complete_description,
             bool_or(has_citations) AS has_citations
        FROM niche_corpus.niche_publications
       GROUP BY COALESCE(NULLIF(family_id, ''), 'publication:' || publication_id)
  ) AS families
"""


def _percentage(numerator: int, denominator: int) -> float:
    return round((100.0 * numerator / denominator), 2) if denominator else 0.0


def _counts(rows: Iterable[dict], key: str) -> dict[str, int]:
    values = Counter(str(row.get(key) or "unknown") for row in rows)
    return dict(sorted(values.items()))


def _code_counts(rows: Iterable[dict]) -> dict[str, int]:
    values = Counter()
    for row in rows:
        for code in row.get("cpc_codes") or []:
            values[str(code)] += 1
    return dict(sorted(values.items()))


def _parse_time(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_status_report(publications, attempts, jobs, *, watermarks=None) -> dict:
    publications = [dict(row) for row in publications]
    attempts = [dict(row) for row in attempts]
    jobs = [dict(row) for row in jobs]
    total = len(publications)
    completeness_fields = {
        "title_complete_pct": "has_title",
        "abstract_complete_pct": "has_abstract",
        "claims_complete_pct": "has_complete_claims",
        "description_complete_pct": "has_complete_description",
        "citations_complete_pct": "has_citations",
    }
    publication_completeness = {
        label: _percentage(sum(bool(row.get(field)) for row in publications), total)
        for label, field in completeness_fields.items()
    }

    families = defaultdict(list)
    for row in publications:
        key = str(row.get("family_id") or f"publication:{row.get('publication_number', '')}")
        families[key].append(row)
    family_completeness = {
        label: _percentage(
            sum(any(bool(member.get(field)) for member in members) for members in families.values()),
            len(families),
        )
        for label, field in completeness_fields.items()
    }

    provider_attempts = defaultdict(list)
    credits = Counter()
    for attempt in attempts:
        provider = str(attempt.get("provider") or "unknown")
        provider_attempts[provider].append(attempt)
        credits[provider] += int(attempt.get("credits_used") or 0)
    success_rates = {
        provider: _percentage(
            sum(attempt.get("status") == "success" for attempt in rows), len(rows)
        )
        for provider, rows in sorted(provider_attempts.items())
    }
    failure_rates = {
        provider: _percentage(
            sum(attempt.get("status") == "error" for attempt in rows), len(rows)
        )
        for provider, rows in sorted(provider_attempts.items())
    }

    times = sorted(time for time in (_parse_time(row.get("attempted_at")) for row in attempts) if time)
    successful_fetches = sum(row.get("status") == "success" for row in attempts)
    if times:
        minutes = max(1.0, (times[-1] - times[0]).total_seconds() / 60.0)
        fetches_per_minute = round(successful_fetches / minutes, 3)
    else:
        fetches_per_minute = 0.0
    heartbeats = [
        (row.get("heartbeat_at"), _parse_time(row.get("heartbeat_at"))) for row in jobs
        if row.get("heartbeat_at")
    ]
    last_heartbeat = max(heartbeats, key=lambda pair: pair[1])[0] if heartbeats else None
    queue_counts = Counter(str(row.get("status") or "unknown") for row in jobs)
    queue = {state: int(queue_counts.get(state, 0)) for state in (
        "pending", "leased", "completed", "failed"
    )}
    watermarks = {
        str(key): int(value) for key, value in dict(watermarks or {}).items()
    }
    scan_position = watermarks.get("publication_id", 0)
    source_max = watermarks.get("source_max_publication_id", 0)
    discovery = {
        "watermarks": watermarks,
        "source_scan_progress_pct": _percentage(
            min(scan_position, source_max), source_max
        ),
        "source_scan_complete": bool(source_max and scan_position >= source_max),
    }

    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "total_publications": total,
        "total_families": len(families),
        "publication_completeness": publication_completeness,
        "family_completeness": family_completeness,
        "counts_by_authority": _counts(publications, "authority"),
        "counts_by_cpc": _code_counts(publications),
        "counts_by_language": _counts(publications, "language"),
        "counts_by_provider": _counts(publications, "preferred_source"),
        "counts_by_fetch_status": _counts(publications, "fetch_status"),
        "remaining_missing_claims": sum(not bool(row.get("has_complete_claims")) for row in publications),
        "remaining_missing_descriptions": sum(
            not bool(row.get("has_complete_description")) for row in publications
        ),
        "remaining_missing_full_text": sum(
            not (
                bool(row.get("has_complete_claims"))
                and bool(row.get("has_complete_description"))
            )
            for row in publications
        ),
        "credits_spent_by_provider": dict(sorted(credits.items())),
        "provider_success_rates": success_rates,
        "failure_rate_by_provider": failure_rates,
        "fetches_per_minute": fetches_per_minute,
        "fetch_rate_per_hour": round(fetches_per_minute * 60.0, 3),
        "queue": queue,
        "last_heartbeat": last_heartbeat,
        "discovery": discovery,
    }


def _database_group(cursor, expression: str) -> dict[str, int]:
    cursor.execute(
        f"SELECT {expression} AS key, count(*) AS n "
        "FROM niche_corpus.niche_publications GROUP BY 1 ORDER BY 1"
    )
    return {str(row["key"] or "unknown"): int(row["n"]) for row in cursor.fetchall()}


def _discovery_progress(watermarks: dict[str, int]) -> dict:
    assigned_intervals = []
    covered_intervals = []
    for key, last_value in watermarks.items():
        if not key.startswith("publication_id:"):
            continue
        try:
            start, end = (int(value) for value in key.split(":", 1)[1].split(":", 1))
        except ValueError:
            continue
        if end <= start:
            continue
        assigned_intervals.append((start, end))
        covered_end = min(end, max(start, int(last_value)))
        if covered_end > start:
            covered_intervals.append((start, covered_end))

    def union_length(intervals) -> int:
        merged = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return sum(end - start for start, end in merged)

    assigned = union_length(assigned_intervals)
    covered = union_length(covered_intervals)
    if not assigned:
        scan = int(watermarks.get("publication_id", 0))
        maximum = int(watermarks.get("source_max_publication_id", 0))
        covered, assigned = min(scan, maximum), maximum
    return {
        "watermarks": watermarks,
        "source_scan_progress_pct": _percentage(covered, assigned),
        "source_scan_complete": bool(assigned and covered >= assigned),
    }


def _acquisition_status(source_factory) -> dict:
    with source_factory() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '5s'")
        connection.execute("SET LOCAL lock_timeout = '1s'")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT provider, count(*) FILTER (WHERE outcome IN "
                "('hit','miss','error','timeout')) AS attempts, "
                "count(*) FILTER (WHERE outcome='hit') AS successes, "
                "count(*) FILTER (WHERE outcome IN ('error','timeout')) AS failures, "
                "COALESCE(sum(credits),0) AS credits "
                "FROM fulltext_fetch_event "
                "WHERE at >= now() - interval '24 hours' "
                "GROUP BY provider ORDER BY provider"
            )
            providers = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT provider,spent FROM fulltext_budget "
                "WHERE period=to_char(now(),'YYYY-MM') ORDER BY provider"
            )
            budgets = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT count(*) FILTER (WHERE state='pending') AS pending, "
                "count(*) FILTER (WHERE state='leased') AS leased, "
                "count(*) FILTER (WHERE state='done') AS completed, "
                "count(*) FILTER (WHERE state IN ('missing','failed')) AS failed, "
                "max(updated_at) FILTER (WHERE state='leased') AS last_heartbeat "
                "FROM fulltext_fetch_task"
            )
            queue = dict(cursor.fetchone() or {})
            cursor.execute(
                "SELECT count(*) AS hits FROM fulltext_fetch_event "
                "WHERE outcome='hit' AND at >= now() - interval '1 hour'"
            )
            hits = int((cursor.fetchone() or {}).get("hits") or 0)
    success = {
        str(row["provider"]): _percentage(int(row["successes"]), int(row["attempts"]))
        for row in providers
        if int(row["attempts"])
    }
    failure = {
        str(row["provider"]): _percentage(int(row["failures"]), int(row["attempts"]))
        for row in providers
        if int(row["attempts"])
    }
    credits = {str(row["provider"]): float(row["spent"] or 0) for row in budgets}
    for row in providers:
        credits.setdefault(str(row["provider"]), float(row["credits"] or 0))
    return {
        "provider_success_rates": success,
        "failure_rate_by_provider": failure,
        "credits_spent_by_provider": credits,
        "fetches_per_minute": round(hits / 60.0, 3),
        "fetch_rate_per_hour": hits,
        "queue": {
            key: int(queue.get(key) or 0)
            for key in ("pending", "leased", "completed", "failed")
        },
        "last_heartbeat": (
            queue["last_heartbeat"].isoformat()
            if queue.get("last_heartbeat") else None
        ),
    }


def build_database_status(connection_factory, *, source_factory=None) -> dict:
    """Aggregate in the isolated database without loading every manifest row into Python."""
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(DATABASE_SUMMARY_SQL)
        summary = dict(cursor.fetchone() or {})
        cursor.execute(DATABASE_FAMILY_SQL)
        families = dict(cursor.fetchone() or {})
        authority = _database_group(
            cursor, "COALESCE(NULLIF(authority, ''), 'unknown')"
        )
        language = _database_group(
            cursor, "COALESCE(NULLIF(language, ''), 'unknown')"
        )
        providers = _database_group(
            cursor, "COALESCE(NULLIF(preferred_source, ''), 'unknown')"
        )
        fetch_status = _database_group(cursor, "fetch_status")
        cursor.execute(
            "SELECT code, count(*) AS n FROM niche_corpus.niche_publications, "
            "LATERAL unnest(cpc_codes) AS code GROUP BY code ORDER BY code"
        )
        cpc = {str(row["code"]): int(row["n"]) for row in cursor.fetchall()}
        cursor.execute(
            "SELECT scope_key, last_value FROM niche_corpus.niche_discovery_watermarks "
            "WHERE source='local' ORDER BY scope_key"
        )
        watermarks = {
            str(row["scope_key"]): int(row["last_value"]) for row in cursor.fetchall()
        }
        cursor.execute(
            "SELECT status, count(*) AS n FROM niche_corpus.niche_parse_jobs "
            "GROUP BY status ORDER BY status"
        )
        parse_queue = {str(row["status"]): int(row["n"]) for row in cursor.fetchall()}
        cursor.execute(
            "SELECT status, count(*) AS n FROM niche_corpus.niche_embedding_cache "
            "GROUP BY status ORDER BY status"
        )
        embedding_cache = {
            str(row["status"]): int(row["n"]) for row in cursor.fetchall()
        }
        cursor.execute(
            "SELECT status, count(*) AS n FROM niche_corpus.niche_embedding_batches "
            "GROUP BY status ORDER BY status"
        )
        embedding_batches = {
            str(row["status"]): int(row["n"]) for row in cursor.fetchall()
        }
        cursor.execute(
            "SELECT limit_usd,reserved_usd,spent_usd "
            "FROM niche_corpus.embedding_budget ORDER BY budget_key LIMIT 1"
        )
        budget_row = dict(cursor.fetchone() or {})
        cursor.execute(
            "SELECT count(*) AS chunks FROM niche_corpus.niche_chunks"
        )
        chunks = int((cursor.fetchone() or {}).get("chunks") or 0)
        cursor.execute(
            "SELECT count(*) AS vectors, "
            "count(*) FILTER (WHERE tantivy_indexed_at IS NOT NULL) AS tantivy "
            "FROM niche_corpus.niche_vector_documents"
        )
        search = dict(cursor.fetchone() or {})

    total = int(summary.get("total_publications") or 0)
    total_families = int(families.get("total_families") or 0)
    labels = {
        "title_complete_pct": "title_complete",
        "abstract_complete_pct": "abstract_complete",
        "claims_complete_pct": "claims_complete",
        "description_complete_pct": "description_complete",
        "citations_complete_pct": "citations_complete",
    }
    acquisition = {
        "provider_success_rates": {},
        "failure_rate_by_provider": {},
        "credits_spent_by_provider": {},
        "fetches_per_minute": 0.0,
        "fetch_rate_per_hour": 0,
        "queue": {key: 0 for key in ("pending", "leased", "completed", "failed")},
        "last_heartbeat": None,
    }
    if source_factory is not None:
        acquisition = _acquisition_status(source_factory)
    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "total_publications": total,
        "total_families": total_families,
        "publication_completeness": {
            label: _percentage(int(summary.get(field) or 0), total)
            for label, field in labels.items()
        },
        "family_completeness": {
            label: _percentage(int(families.get(field) or 0), total_families)
            for label, field in labels.items()
        },
        "counts_by_authority": authority,
        "counts_by_cpc": cpc,
        "counts_by_language": language,
        "counts_by_provider": providers,
        "counts_by_fetch_status": fetch_status,
        "remaining_missing_claims": int(summary.get("missing_claims") or 0),
        "remaining_missing_descriptions": int(summary.get("missing_descriptions") or 0),
        "remaining_missing_full_text": int(summary.get("missing_full_text") or 0),
        **acquisition,
        "discovery": _discovery_progress(watermarks),
        "parse_queue": parse_queue,
        "embedding_cache": embedding_cache,
        "embedding_batches": embedding_batches,
        "embedding_budget": {key: str(value) for key, value in budget_row.items()},
        "search_build": {
            "chunks": chunks,
            "vectors": int(search.get("vectors") or 0),
            "tantivy_documents": int(search.get("tantivy") or 0),
        },
    }


def _flatten(report: dict):
    for key, value in report.items():
        if isinstance(value, dict):
            for child, child_value in sorted(value.items()):
                yield {"metric": f"{key}.{child}", "value": child_value}
        else:
            yield {"metric": key, "value": "" if value is None else value}


def write_status_artifacts(report: dict, directory: str | os.PathLike) -> tuple[Path, Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "niche_corpus_status.json"
    csv_path = root / "niche_corpus_status.csv"
    json_tmp = root / f".{json_path.name}.{os.getpid()}.tmp"
    csv_tmp = root / f".{csv_path.name}.{os.getpid()}.tmp"
    json_tmp.write_text(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2) + "\n")
    with csv_tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["metric", "value"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(_flatten(report))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(json_tmp, json_path)
    os.replace(csv_tmp, csv_path)
    return json_path, csv_path


def main(argv=None) -> int:
    from .cli import run_status
    return run_status(argv)


if __name__ == "__main__":
    raise SystemExit(main())
