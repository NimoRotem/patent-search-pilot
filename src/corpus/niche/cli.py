"""Command-line entry points for discovery, fetching, parsing, and reporting."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "sql" / "niche" / "001_fetch_queue.sql"
DEFAULT_ARTIFACTS = ROOT / "artifacts"


def _common_database(parser):
    parser.add_argument(
        "--niche-dsn",
        default=os.environ.get("NICHE_DATABASE_URL", ""),
        help="PostgreSQL DSN for the niche_corpus staging schema (or NICHE_DATABASE_URL)",
    )
    parser.add_argument(
        "--init-schema",
        action="store_true",
        help="apply sql/niche/001_fetch_queue.sql to the isolated niche database before running",
    )


def _factory(dsn, variable, application_name):
    from .database import connection_factory, require_dsn
    return connection_factory(require_dsn(dsn, variable), application_name=application_name)


def _initialize_if_requested(args, factory):
    if args.init_schema:
        from .database import apply_schema
        apply_schema(factory, MIGRATION)


def _load_seed_records(paths):
    from .manifest import PublicationRecord

    records = []
    for path in paths or []:
        with Path(path).open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                data = json.loads(line)
                if "publication_number" not in data:
                    raise ValueError(f"{path}:{line_number} has no publication_number")
                data.setdefault("discovery_signals", ("known_result",))
                data.setdefault("priority", 1)
                records.append(PublicationRecord(**data))
    return records


def _discover_parser():
    parser = argparse.ArgumentParser(
        prog="python -m src.corpus.niche.discover",
        description="Discover and audit a bounded niche publication range.",
    )
    _common_database(parser)
    parser.add_argument(
        "--source-dsn",
        default=os.environ.get("NICHE_SOURCE_DATABASE_URL", ""),
        help="read-only active-corpus DSN (or NICHE_SOURCE_DATABASE_URL)",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument(
        "--max-batches",
        type=int,
        default=1,
        help="bounded source windows to process; 0 drains the assigned range",
    )
    parser.add_argument(
        "--id-start",
        type=int,
        default=0,
        help="exclusive lower publication id bound for this worker",
    )
    parser.add_argument(
        "--id-end",
        type=int,
        default=0,
        help="inclusive upper publication id bound; 0 follows the current source maximum",
    )
    parser.add_argument(
        "--db-read-delay",
        type=float,
        default=float(os.environ.get("NICHE_DB_READ_DELAY", "1.0")),
        help="minimum pause between bounded source-corpus ranges",
    )
    parser.add_argument("--seed-jsonl", action="append", default=[])
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="enqueue one preferred incomplete or unnormalized publication per family",
    )
    return parser


def run_discover(argv=None) -> int:
    args = _discover_parser().parse_args(argv)
    from .discover import DiscoveryEngine
    from .providers.local import LocalDiscoverySource
    from .queue import PostgresFetchQueue
    from .repository import PostgresNicheRepository

    niche_factory = _factory(args.niche_dsn, "NICHE_DATABASE_URL", "niche-discover")
    source_factory = _factory(args.source_dsn, "NICHE_SOURCE_DATABASE_URL", "niche-source-read")
    _initialize_if_requested(args, niche_factory)
    repository = PostgresNicheRepository(niche_factory)
    source = LocalDiscoverySource(
        source_factory,
        range_start=args.id_start,
        range_end=args.id_end or None,
    )
    extras = _load_seed_records(args.seed_jsonl)
    totals = {"publications_seen": 0, "families_seen": 0, "batches": 0}
    previous = None
    batch_limit = None if int(args.max_batches) == 0 else max(1, int(args.max_batches))
    batch_index = 0
    while batch_limit is None or batch_index < batch_limit:
        summary = DiscoveryEngine(
            source,
            repository,
            batch_size=max(1, args.batch_size),
            extra_records=extras,
        ).run()
        batch_index += 1
        totals["publications_seen"] += summary.publications_seen
        totals["families_seen"] = max(totals["families_seen"], summary.families_seen)
        totals["batches"] += 1
        current = summary.watermarks.get(source.watermark_key)
        if current == previous:
            break
        previous = current
        extras = ()
        if batch_limit is None or batch_index < batch_limit:
            time.sleep(max(0.0, float(args.db_read_delay)))
    if args.enqueue:
        max_attempts = _max_attempts()
        queue = PostgresFetchQueue(niche_factory, max_attempts=max_attempts)
        totals["jobs_enqueued"] = repository.enqueue_incomplete_families(queue)
    print(json.dumps(totals, sort_keys=True))
    return 0


def _max_attempts() -> int:
    raw = os.environ.get("MAX_FETCH_ATTEMPTS_PER_PUBLICATION", "5")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("MAX_FETCH_ATTEMPTS_PER_PUBLICATION must be a positive integer") from exc
    if not 1 <= value <= 20:
        raise RuntimeError("MAX_FETCH_ATTEMPTS_PER_PUBLICATION must be between 1 and 20")
    return value


def _workers_default() -> int:
    try:
        value = int(os.environ.get("FETCH_WORKERS", "8"))
    except ValueError:
        return 8
    return min(64, max(1, value))


def _object_store(path):
    from .storage import build_object_store
    location = (
        path
        or os.environ.get("NICHE_OBJECT_URI", "")
        or os.environ.get("NICHE_OBJECT_ROOT", "")
        or ROOT / "data" / "niche-objects"
    )
    return build_object_store(location)


def _fetch_parser():
    parser = argparse.ArgumentParser(
        prog="python -m src.corpus.niche.fetch",
        description="Run lease-based niche full-text fetch workers.",
    )
    _common_database(parser)
    parser.add_argument("--source-dsn", default=os.environ.get("NICHE_SOURCE_DATABASE_URL", ""))
    parser.add_argument(
        "--object-root",
        default=os.environ.get("NICHE_OBJECT_URI", "") or os.environ.get("NICHE_OBJECT_ROOT", ""),
        help="filesystem path or gs:// bucket prefix",
    )
    parser.add_argument("--workers", type=int, default=_workers_default())
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--heartbeat-seconds", type=int, default=30)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--chunk-format", choices=("jsonl", "parquet"), default="parquet")
    parser.add_argument("--once", action="store_true", help="drain currently eligible jobs and exit")
    parser.add_argument("--max-jobs", type=int, default=0)
    return parser


def run_fetch(argv=None) -> int:
    args = _fetch_parser().parse_args(argv)
    from .fetch import FetchWorker
    from .limits import PaidBudget, PaidLimits, RunJobBudget
    from .providers.registry import build_default_providers
    from .queue import PostgresFetchQueue
    from .repository import PostgresNicheRepository
    from .waterfall import ProviderWaterfall

    niche_factory = _factory(args.niche_dsn, "NICHE_DATABASE_URL", "niche-fetch")
    source_factory = (
        _factory(args.source_dsn, "NICHE_SOURCE_DATABASE_URL", "niche-source-read")
        if args.source_dsn else None
    )
    _initialize_if_requested(args, niche_factory)
    max_attempts = _max_attempts()
    queue = PostgresFetchQueue(niche_factory, max_attempts=max_attempts)
    queue.reclaim_expired()
    repository = PostgresNicheRepository(niche_factory)
    store = _object_store(args.object_root)
    paid_limits = PaidLimits.from_env(os.environ)
    budget = PaidBudget(paid_limits.caps)
    worker_count = min(64, max(1, int(args.workers)))
    job_budget = RunJobBudget(args.max_jobs)
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def run_slot(slot):
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{slot}"
        worker = FetchWorker(
            queue=queue,
            repository=repository,
            waterfall=ProviderWaterfall(
                build_default_providers(local_connection_factory=source_factory), budget
            ),
            object_store=store,
            worker_id=worker_id,
            lease_seconds=args.lease_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            chunks_format=args.chunk_format,
        )
        processed = 0
        while not stop.is_set() and job_budget.acquire():
            if worker.run_once():
                processed += 1
                continue
            job_budget.release_unprocessed()
            if args.once:
                break
            stop.wait(max(0.25, args.poll_seconds))
        return processed

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="niche-fetch") as pool:
        processed = sum(pool.map(run_slot, range(worker_count)))
    print(json.dumps({
        "workers": worker_count,
        "processed": processed,
        "credits": budget.snapshot(),
        "invalid_paid_limits": list(paid_limits.invalid),
    }, sort_keys=True))
    return 0


def _parse_parser():
    parser = argparse.ArgumentParser(
        prog="python -m src.corpus.niche.parse",
        description="Parse cached niche source objects and write embedding chunks.",
    )
    _common_database(parser)
    parser.add_argument(
        "--object-root",
        default=os.environ.get("NICHE_OBJECT_URI", "") or os.environ.get("NICHE_OBJECT_ROOT", ""),
        help="filesystem path or gs:// bucket prefix",
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--chunk-format", choices=("jsonl", "parquet"), default="parquet")
    return parser


def run_parse(argv=None) -> int:
    args = _parse_parser().parse_args(argv)
    from .fetch import parse_cached_record
    from .manifest import PublicationRecord
    from .repository import _RECORD_FIELDS, PostgresNicheRepository

    factory = _factory(args.niche_dsn, "NICHE_DATABASE_URL", "niche-parse")
    _initialize_if_requested(args, factory)
    repository = PostgresNicheRepository(factory)
    store = _object_store(args.object_root)
    processed = complete = errors = 0
    for row in repository.iter_publications():
        if processed >= max(1, args.limit):
            break
        if not repository.cached_sources(row["publication_id"]):
            continue
        values = {key: value for key, value in row.items() if key in _RECORD_FIELDS}
        values["cpc_codes"] = tuple(values.get("cpc_codes") or ())
        values["ipc_codes"] = tuple(values.get("ipc_codes") or ())
        values["discovery_signals"] = tuple(values.get("discovery_signals") or ())
        record = PublicationRecord(**values)
        processed += 1
        try:
            parsed = parse_cached_record(repository, store, record, args.chunk_format)
            if parsed and (parsed.get("completeness") or {}).get("has_complete_claims") \
                    and (parsed.get("completeness") or {}).get("has_complete_description"):
                repository.mark_fetch_status(record.publication_id, "completed")
                complete += 1
        except Exception as exc:  # noqa: BLE001 - one bad object must not stop the batch
            errors += 1
            repository.mark_fetch_status(
                record.publication_id, "partial", last_error=f"{type(exc).__name__}: {exc}"
            )
    print(json.dumps({"processed": processed, "complete": complete, "errors": errors}, sort_keys=True))
    return 0 if not errors else 1


def _status_parser():
    parser = argparse.ArgumentParser(
        prog="python -m src.corpus.niche.status",
        description="Write and display niche corpus completeness status.",
    )
    _common_database(parser)
    parser.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS))
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    return parser


def run_status(argv=None) -> int:
    args = _status_parser().parse_args(argv)
    from .repository import PostgresNicheRepository
    from .status import build_status_report, write_status_artifacts

    factory = _factory(args.niche_dsn, "NICHE_DATABASE_URL", "niche-status")
    _initialize_if_requested(args, factory)
    repository = PostgresNicheRepository(factory)
    report = build_status_report(
        list(repository.iter_publications()),
        list(repository.iter_attempts()),
        list(repository.iter_jobs()),
        watermarks=repository.load_watermarks(),
    )
    paths = write_status_artifacts(report, args.artifacts_dir)
    if args.json:
        print(json.dumps(report, sort_keys=True, indent=2, default=str))
    else:
        queue = report["queue"]
        print(
            f"pending={queue['pending']} leased={queue['leased']} "
            f"completed={queue['completed']} failed={queue['failed']} "
            f"fetches/min={report['fetches_per_minute']} "
            f"last_heartbeat={report['last_heartbeat'] or 'none'}"
        )
        for provider, rate in report["provider_success_rates"].items():
            credits = report["credits_spent_by_provider"].get(provider, 0)
            print(f"provider={provider} success={rate}% credits={credits}")
        print(f"json={paths[0]} csv={paths[1]}")
    return 0
