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
