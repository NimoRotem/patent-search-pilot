from __future__ import annotations

import gzip
import subprocess
import sys
from pathlib import Path

from corpus.niche.fetch import FetchWorker
from corpus.niche.limits import PaidBudget
from corpus.niche.manifest import PublicationRecord
from corpus.niche.models import Completeness, FetchRequest, ProviderResult
from corpus.niche.providers.base import BaseProvider
from corpus.niche.providers.marec import MarecProvider
from corpus.niche.providers.registry import build_default_providers
from corpus.niche.queue import FetchJob
from corpus.niche.storage import FileObjectStore
from corpus.niche.waterfall import ProviderWaterfall


def test_required_provider_modules_are_registered_in_waterfall_order():
    providers = build_default_providers(local_connection_factory=None)

    assert [provider.name for provider in providers] == [
        "local",
        "marec",
        "uspto",
        "epo",
        "google_patents",
        "self_serp",
        "firecrawl",
        "scrapingbee",
        "serpapi",
    ]


def test_marec_reads_deterministic_gzip_path_without_scanning(tmp_path):
    path = tmp_path / "US" / "US1234567A1.xml.gz"
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wb") as handle:
        handle.write(b"<patent-document><claims><claim num='1'>A claim</claim></claims></patent-document>")
    provider = MarecProvider(root=tmp_path)
    request = FetchRequest(
        publication_id="US1234567A1",
        publication_number="US1234567A1",
        authority="US",
        missing_fields=frozenset({"claims"}),
    )

    result = provider.fetch(request)

    assert result.media_type == "application/xml"
    assert result.content.startswith(b"<patent-document>")
    assert result.credits_used == 0


class _NeverProvider(BaseProvider):
    name = "never"

    def __init__(self):
        self.calls = 0

    def fetch(self, request):
        self.calls += 1
        raise AssertionError("cached raw source must be parsed before any provider call")


class _Queue:
    def __init__(self, job, max_attempts=5, complete_result=True):
        self.job = job
        self.max_attempts = max_attempts
        self.complete_result = complete_result
        self.claimed = False
        self.completed = []
        self.failed = []

    def claim(self, worker_id, lease_seconds):
        if self.claimed:
            return None
        self.claimed = True
        return self.job

    def heartbeat(self, *args):
        return True

    def complete(self, job_id, worker_id):
        self.completed.append((job_id, worker_id))
        return self.complete_result

    def fail(self, job_id, worker_id, attempt, error):
        self.failed.append((job_id, error))
        return True


class _Repository:
    def __init__(self, record, source):
        self.record = record
        self.source = source
        self.attempts = []
        self.parsed = []
        self.statuses = []

    def get_publication(self, publication_id):
        return self.record

    def cached_sources(self, publication_id):
        return [self.source] if self.source else []

    def record_attempt(self, publication_id, attempt):
        self.attempts.append((publication_id, attempt))

    def record_source(self, publication_id, result, stored):
        raise AssertionError("cached raw source must not be stored again")

    def save_parsed(self, publication_id, parsed, parsed_object, chunks_object):
        self.parsed.append((publication_id, parsed, parsed_object, chunks_object))

    def mark_fetch_status(self, publication_id, status, **fields):
        self.statuses.append((status, fields))


def test_idempotent_rerun_parses_cached_raw_and_skips_provider(tmp_path):
    store = FileObjectStore(tmp_path)
    xml = b"""<patent-document lang="en"><claims><claim num="1">1. A vacuum gripper.</claim></claims>
    <description><p id="p1">A preserved paragraph.</p></description></patent-document>"""
    stored = store.put_raw(
        authority="US",
        publication_number="US1234567A1",
        provider="marec",
        content=xml,
        media_type="application/xml",
        http_status=200,
        headers={},
    )
    record = PublicationRecord(publication_number="US1234567A1", priority=1)
    repository = _Repository(record, {
        "raw_object_uri": stored.uri,
        "provider": "marec",
        "media_type": "application/xml",
    })
    job = FetchJob(1, record.publication_id, 1, "leased", worker_id="worker", attempt=1)
    queue = _Queue(job)
    provider = _NeverProvider()
    waterfall = ProviderWaterfall([provider], PaidBudget({}))
    worker = FetchWorker(
        queue=queue,
        repository=repository,
        waterfall=waterfall,
        object_store=store,
        worker_id="worker",
        lease_seconds=60,
        heartbeat_seconds=5,
        chunks_format="jsonl",
    )

    worked = worker.run_once()

    assert worked is True
    assert provider.calls == 0
    assert queue.completed == [(1, "worker")]
    assert not queue.failed
    assert repository.parsed[0][1]["completeness"]["has_complete_description"] is True
    assert repository.statuses[-1][0] == "completed"


def test_terminal_fetch_attempt_marks_manifest_failed(tmp_path):
    record = PublicationRecord(publication_number="US1234567A1", priority=1)
    repository = _Repository(record, None)
    job = FetchJob(1, record.publication_id, 1, "leased", worker_id="worker", attempt=2)
    queue = _Queue(job, max_attempts=2)
    worker = FetchWorker(
        queue=queue,
        repository=repository,
        waterfall=ProviderWaterfall([], PaidBudget({})),
        object_store=FileObjectStore(tmp_path),
        worker_id="worker",
        lease_seconds=60,
        heartbeat_seconds=5,
        chunks_format="jsonl",
    )

    assert worker.run_once() is True
    assert queue.failed == [(1, "all providers unavailable")]
    assert repository.statuses[-1][0] == "failed"


def test_worker_that_lost_its_lease_cannot_mark_manifest_completed(tmp_path):
    store = FileObjectStore(tmp_path)
    stored = store.put_raw(
        authority="US",
        publication_number="US1234567A1",
        provider="marec",
        content=(
            b"<patent-document><claims><claim num='1'>A sufficiently complete claim "
            b"with structured limitations.</claim></claims><description><p>Complete "
            b"description.</p></description></patent-document>"
        ),
        media_type="application/xml",
        http_status=200,
        headers={},
    )
    record = PublicationRecord(publication_number="US1234567A1", priority=1)
    repository = _Repository(record, {
        "raw_object_uri": stored.uri,
        "provider": "marec",
        "media_type": "application/xml",
    })
    job = FetchJob(1, record.publication_id, 1, "leased", worker_id="stale", attempt=1)
    queue = _Queue(job, complete_result=False)
    worker = FetchWorker(
        queue=queue,
        repository=repository,
        waterfall=ProviderWaterfall([], PaidBudget({})),
        object_store=store,
        worker_id="stale",
        lease_seconds=60,
        heartbeat_seconds=5,
        chunks_format="jsonl",
        logger=lambda _event: None,
    )

    assert worker.run_once() is True
    assert not any(status == "completed" for status, _fields in repository.statuses)


def test_unreadable_cached_raw_stops_before_paid_refetch(tmp_path):
    class BrokenReadStore(FileObjectStore):
        def read(self, _uri):
            raise OSError("object store unavailable")

    paid = _NeverProvider()
    paid.paid = True
    paid.name = "firecrawl"
    paid.estimated_credits = lambda _request: 1
    record = PublicationRecord(publication_number="US1234567A1", priority=1)
    repository = _Repository(record, {
        "raw_object_uri": "patents/raw/US/missing.xml",
        "provider": "marec",
        "media_type": "application/xml",
    })
    job = FetchJob(1, record.publication_id, 1, "leased", worker_id="worker", attempt=1)
    queue = _Queue(job)
    worker = FetchWorker(
        queue=queue,
        repository=repository,
        waterfall=ProviderWaterfall([paid], PaidBudget({"firecrawl": 10})),
        object_store=BrokenReadStore(tmp_path),
        worker_id="worker",
        lease_seconds=60,
        heartbeat_seconds=5,
        chunks_format="jsonl",
        logger=lambda _event: None,
    )

    assert worker.run_once() is True
    assert paid.calls == 0
    assert queue.failed


def test_valid_partial_cached_raw_skips_same_provider_on_retry(tmp_path):
    store = FileObjectStore(tmp_path)
    stored = store.put_raw(
        authority="US",
        publication_number="US1234567A1",
        provider="firecrawl",
        content=(
            b"<patent-document><claims><claim num='1'>"
            b"1. A vacuum tool.</claim></claims></patent-document>"
        ),
        media_type="application/xml",
        http_status=200,
        headers={},
    )
    paid = _NeverProvider()
    paid.paid = True
    paid.name = "firecrawl"
    paid.estimated_credits = lambda _request: 1
    record = PublicationRecord(publication_number="US1234567A1", priority=1)
    repository = _Repository(record, {
        "raw_object_uri": stored.uri,
        "provider": "firecrawl",
        "media_type": "application/xml",
    })
    job = FetchJob(1, record.publication_id, 1, "leased", worker_id="worker", attempt=1)
    queue = _Queue(job)
    worker = FetchWorker(
        queue=queue,
        repository=repository,
        waterfall=ProviderWaterfall([paid], PaidBudget({"firecrawl": 1})),
        object_store=store,
        worker_id="worker",
        lease_seconds=60,
        heartbeat_seconds=5,
        chunks_format="jsonl",
        logger=lambda _event: None,
    )

    assert worker.run_once() is True
    assert paid.calls == 0
    assert queue.failed


def test_waterfall_validator_can_reject_bad_provider_output_and_continue():
    class Provider(BaseProvider):
        def __init__(self, name, content):
            self.name, self.content = name, content

        def fetch(self, request):
            return ProviderResult(
                provider=self.name,
                content=self.content,
                media_type="application/xml",
                source_url="test",
                completeness=Completeness(
                    has_claims=True, has_complete_claims=True,
                    has_description=True, has_complete_description=True,
                ),
            )

    providers = [Provider("bad", b"not XML"), Provider("good", b"<patent-document/>")]

    def validator(_request, result):
        if result.content == b"not XML":
            raise ValueError("invalid source")
        return result

    waterfall = ProviderWaterfall(providers, PaidBudget({}), validator=validator)
    request = FetchRequest(
        "US1A1", "US1A1", "US", frozenset({"claims", "description"}), Completeness()
    )

    outcome = waterfall.fetch(request)

    assert outcome.status == "completed"
    assert [attempt.status for attempt in outcome.attempts] == ["error", "success"]


def test_migration_is_staging_only_and_has_all_durable_tables():
    root = Path(__file__).resolve().parents[1]
    sql = (root / "sql" / "niche" / "001_fetch_queue.sql").read_text()
    normalized = " ".join(sql.upper().split())

    assert "CREATE SCHEMA IF NOT EXISTS NICHE_CORPUS" in normalized
    assert "NICHE_CORPUS.NICHE_PUBLICATIONS" in normalized
    assert "NICHE_CORPUS.NICHE_FETCH_ATTEMPTS" in normalized
    assert "NICHE_CORPUS.CORPUS_FETCH_JOBS" in normalized
    assert "NICHE_CORPUS.NICHE_SOURCE_OBJECTS" in normalized
    assert "CREATE INDEX" in normalized
    assert "CREATE INDEX ON CHUNKS" not in normalized
    assert "UPDATE PUBLICATIONS" not in normalized
    assert "DELETE FROM" not in normalized


def test_all_four_cli_modules_expose_help_without_database_or_credentials():
    root = Path(__file__).resolve().parents[1]
    modules = [
        "src.corpus.niche.discover",
        "src.corpus.niche.fetch",
        "src.corpus.niche.parse",
        "src.corpus.niche.status",
    ]
    for module in modules:
        result = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, (module, result.stderr)
        assert "usage:" in result.stdout.lower()
