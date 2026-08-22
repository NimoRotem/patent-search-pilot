from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from acquire import niche_handoff
from acquire.manifest import NicheDatabaseReader, open_reader
from corpus.niche.batch import (
    AmbiguousSubmission,
    GCSBatchStore,
    VertexBatchClient,
    project_embedding,
    vertex_job_body,
)
from corpus.niche.chunks import build_chunks
from corpus.niche.embed import BatchController
from corpus.niche.embedding import (
    EmbeddingSettings,
    batch_request_line,
    embedding_key,
    embedding_rows,
    submission_key,
)
from corpus.niche.manifest import PublicationRecord
from corpus.niche.parse import parse_source
from corpus.niche.publish import PostgresVectorPublisher
from corpus.niche.repository import local_parse_jobs
from corpus.niche.stream import ParseJob, PostgresParseQueue, StreamingParseWorker
from corpus.niche.tantivy_build import TantivyIndexer, TantivyRepository


def test_legacy_json_preserves_distinct_claim_and_description_languages():
    parsed = parse_source(
        b'{"publication_number":"DE1A1","claims":"1. Vorrichtung.",'
        b'"description":"Beschreibung eins.\\n\\nBeschreibung zwei.",'
        b'"claims_lang":"de","desc_lang":"en"}',
        "application/json",
        "DE1A1",
        "corpus_family",
    )

    assert parsed["claims"][0]["language"] == "de"
    assert [row["language"] for row in parsed["description_paragraphs"]] == ["en", "en"]
    assert [row["text"] for row in parsed["description_paragraphs"]] == [
        "Beschreibung eins.",
        "Beschreibung zwei.",
    ]


def test_acquisition_json_accepts_provider_string_and_groups_claim_continuations():
    parsed = parse_source(
        b'{"publication_number":"US1A1","source":"serp_self",'
        b'"claims":"Claims (2)\\nTranslated from German\\n1. A tool comprising\\n'
        b'a suction cup and a valve.\\n2. The tool of claim 1 with a pump.",'
        b'"description":"Description\\nFirst paragraph.\\nSecond paragraph.",'
        b'"claims_lang":"en","desc_lang":"en","date":"20250102"}',
        "application/json",
        "US1A1",
        "fulltext_gcs",
    )

    assert [claim["number"] for claim in parsed["claims"]] == [1, 2]
    assert parsed["claims"][0]["text"] == "A tool comprising a suction cup and a valve."
    assert parsed["claims"][1]["depends_on"] == [1]
    assert parsed["source"]["upstream_provider"] == "serp_self"
    assert parsed["dates"]["publication_date"] == "20250102"


def test_embedding_identity_includes_model_dimension_and_task():
    content_hash = hashlib.sha256(b"vacuum gripper").hexdigest()
    baseline = embedding_key(content_hash, "gemini-embedding-001", 768, "RETRIEVAL_DOCUMENT")

    assert baseline != embedding_key(content_hash, "gemini-embedding-001", 3072, "RETRIEVAL_DOCUMENT")
    assert baseline != embedding_key(content_hash, "gemini-embedding-001", 768, "CLUSTERING")
    assert baseline != embedding_key(content_hash, "another-model", 768, "RETRIEVAL_DOCUMENT")


def test_batch_request_uses_current_vertex_embedding_shape_and_key():
    settings = EmbeddingSettings.from_env({
        "GEMINI_EMBED_MODEL": "gemini-embedding-001",
        "GEMINI_EMBED_DIMENSION": "768",
        "GEMINI_EMBED_TASK_TYPE": "RETRIEVAL_DOCUMENT",
        "NICHE_CORPUS_RELEASE": "niche_full_v1",
        "MAX_GEMINI_EMBED_USD_TOTAL": "49",
        "GEMINI_EMBED_PRICE_USD_PER_MTOK": "0.12",
        "GEMINI_BATCH_BUCKET": "nimo-patents-v3",
        "NICHE_EXPECTED_DATABASE": "niche_full_v1",
        "NICHE_DATABASE_FINGERPRINT": "niche-full-v1-test",
    })

    assert batch_request_line("item-1", "vacuum gripper", settings) == {
        "key": "item-1",
        "request": {
            "content": {"parts": [{"text": "vacuum gripper"}]},
            "embed_content_config": {
                "output_dimensionality": 768,
                "task_type": "RETRIEVAL_DOCUMENT",
            },
        },
    }


def test_embedding_paid_limits_fail_closed():
    minimum = {
        "GEMINI_EMBED_MODEL": "gemini-embedding-001",
        "GEMINI_EMBED_DIMENSION": "768",
        "GEMINI_EMBED_TASK_TYPE": "RETRIEVAL_DOCUMENT",
        "NICHE_CORPUS_RELEASE": "niche_full_v1",
        "GEMINI_BATCH_BUCKET": "nimo-patents-v3",
        "NICHE_EXPECTED_DATABASE": "niche_full_v1",
        "NICHE_DATABASE_FINGERPRINT": "niche-full-v1-test",
    }
    with pytest.raises(RuntimeError, match="MAX_GEMINI_EMBED_USD_TOTAL"):
        EmbeddingSettings.from_env(minimum)
    with pytest.raises(RuntimeError, match="positive"):
        EmbeddingSettings.from_env({
            **minimum,
            "MAX_GEMINI_EMBED_USD_TOTAL": "0",
            "GEMINI_EMBED_PRICE_USD_PER_MTOK": "0.12",
        })
    with pytest.raises(RuntimeError, match="GEMINI_EMBED_PRICE_USD_PER_MTOK"):
        EmbeddingSettings.from_env({**minimum, "MAX_GEMINI_EMBED_USD_TOTAL": "49"})


def test_embedding_dimension_must_match_the_768_dimension_vector_tables():
    env = {
        "GEMINI_EMBED_MODEL": "gemini-embedding-001",
        "GEMINI_EMBED_DIMENSION": "3072",
        "GEMINI_EMBED_TASK_TYPE": "RETRIEVAL_DOCUMENT",
        "NICHE_CORPUS_RELEASE": "niche_full_v1",
        "MAX_GEMINI_EMBED_USD_TOTAL": "49",
        "GEMINI_EMBED_PRICE_USD_PER_MTOK": "0.12",
        "GEMINI_BATCH_BUCKET": "nimo-patents-v3",
        "NICHE_EXPECTED_DATABASE": "niche_full_v1",
        "NICHE_DATABASE_FINGERPRINT": "niche-full-v1-test",
    }

    with pytest.raises(RuntimeError, match="768"):
        EmbeddingSettings.from_env(env)


def test_embedding_model_id_is_release_configuration_not_source_code_policy():
    env = {
        "GEMINI_EMBED_MODEL": "gemini-embedding-evaluated-next",
        "GEMINI_EMBED_DIMENSION": "768",
        "GEMINI_EMBED_TASK_TYPE": "RETRIEVAL_DOCUMENT",
        "NICHE_CORPUS_RELEASE": "niche_full_v1_next",
        "MAX_GEMINI_EMBED_USD_TOTAL": "10",
        "GEMINI_EMBED_PRICE_USD_PER_MTOK": "0.12",
        "GEMINI_BATCH_BUCKET": "test-bucket",
        "NICHE_EXPECTED_DATABASE": "niche_full_v1_next",
        "NICHE_DATABASE_FINGERPRINT": "test-only",
    }

    settings = EmbeddingSettings.from_env(env)

    assert settings.model == "gemini-embedding-evaluated-next"


def test_submission_identity_is_stable_and_order_independent():
    settings = EmbeddingSettings.from_env({
        "GEMINI_EMBED_MODEL": "gemini-embedding-001",
        "GEMINI_EMBED_DIMENSION": "768",
        "GEMINI_EMBED_TASK_TYPE": "RETRIEVAL_DOCUMENT",
        "NICHE_CORPUS_RELEASE": "niche_full_v1",
        "MAX_GEMINI_EMBED_USD_TOTAL": "49",
        "GEMINI_EMBED_PRICE_USD_PER_MTOK": "0.12",
        "GEMINI_BATCH_BUCKET": "nimo-patents-v3",
        "NICHE_EXPECTED_DATABASE": "niche_full_v1",
        "NICHE_DATABASE_FINGERPRINT": "niche-full-v1-test",
    })

    assert submission_key(["b", "a"], settings) == submission_key(["a", "b"], settings)


def test_identical_chunk_content_uses_one_embedding_cache_entry():
    settings = EmbeddingSettings.from_env({
        "GEMINI_EMBED_MODEL": "gemini-embedding-001",
        "GEMINI_EMBED_DIMENSION": "768",
        "GEMINI_EMBED_TASK_TYPE": "RETRIEVAL_DOCUMENT",
        "NICHE_CORPUS_RELEASE": "niche_full_v1",
        "MAX_GEMINI_EMBED_USD_TOTAL": "49",
        "GEMINI_EMBED_PRICE_USD_PER_MTOK": "0.12",
        "GEMINI_BATCH_BUCKET": "nimo-patents-v3",
        "NICHE_EXPECTED_DATABASE": "niche_full_v1",
        "NICHE_DATABASE_FINGERPRINT": "niche-full-v1-test",
    })
    content_hash = hashlib.sha256(b"same text").hexdigest()
    chunks = [
        {"chunk_id": "one", "content_hash": content_hash, "text": "same text"},
        {"chunk_id": "two", "content_hash": content_hash, "text": "same text"},
    ]

    cache, stage = embedding_rows(chunks, settings)

    assert len(cache) == 1
    assert len(stage) == 2
    assert {row["embedding_key"] for row in stage} == {cache[0]["embedding_key"]}


def test_parse_queue_claim_is_skip_locked_and_schema_qualified():
    normalized = " ".join(PostgresParseQueue.CLAIM_SQL.upper().split())

    assert "FOR UPDATE SKIP LOCKED" in normalized
    assert "NICHE_CORPUS.NICHE_PARSE_JOBS" in normalized
    assert " PUBLIC." not in normalized


def test_parse_worker_stops_publishing_after_heartbeat_loses_lease():
    heartbeat_failed = threading.Event()
    events = []

    class Queue:
        def heartbeat(self, *_args):
            heartbeat_failed.set()
            return False

        def complete(self, *_args):
            raise AssertionError("a lost lease must not be completed")

        def fail(self, *_args):
            raise AssertionError("a lost lease belongs to another worker")

    class Repository:
        def publication_lock(self, _publication):
            return nullcontext()

        def get_publication(self, _publication):
            raise AssertionError("a lost lease must not enter publication writes")

    worker = StreamingParseWorker(
        queue=Queue(),
        repository=Repository(),
        source_connection_factory=lambda: None,
        canonical_store=object(),
        settings=object(),
        worker_id="worker-a",
        lease_seconds=30,
        heartbeat_seconds=5,
        gcs_client=object(),
        logger=events.append,
    )
    worker.heartbeat_seconds = 0.01

    def parsed(_job):
        assert heartbeat_failed.wait(1)
        return {"publication_id": "US1A1"}, 10

    worker._parse = parsed
    job = ParseJob(
        job_id=1,
        publication_id="US1A1",
        source_kind="local",
        source_uri="local://US1A1",
        source_generation="",
        status="leased",
        worker_id="worker-a",
        lease_until=None,
        attempt=1,
    )

    assert worker.run_job(job) is False
    assert events[-1]["result"] == "lease_lost"


def test_parse_worker_returns_owned_lease_on_graceful_shutdown():
    stopped = threading.Event()
    stopped.set()
    events = []

    class Queue:
        def heartbeat(self, *_args):
            raise AssertionError("shutdown must not extend the lease")

        def cancel(self, job, worker_id):
            assert (job.job_id, worker_id) == (2, "worker-b")
            return True

    class Repository:
        def publication_lock(self, _publication):
            raise AssertionError("shutdown must stop before publication work")

    worker = StreamingParseWorker(
        queue=Queue(),
        repository=Repository(),
        source_connection_factory=lambda: None,
        canonical_store=object(),
        settings=object(),
        worker_id="worker-b",
        lease_seconds=30,
        heartbeat_seconds=5,
        gcs_client=object(),
        logger=events.append,
    )
    job = ParseJob(
        job_id=2,
        publication_id="US2A1",
        source_kind="gcs",
        source_uri="gs://bucket/US2A1.json",
        source_generation="gcs:1",
        status="leased",
        worker_id="worker-b",
        lease_until=None,
        attempt=1,
    )

    assert worker.run_job(job, cancel_event=stopped) is False
    assert events[-1]["result"] == "cancelled"


def test_only_locally_complete_publications_become_parse_work():
    complete = PublicationRecord(
        publication_number="US1234567A1",
        has_claims=True,
        has_complete_claims=True,
        has_description=True,
        has_complete_description=True,
    )
    partial = PublicationRecord(
        publication_number="EP1234567A1",
        has_claims=True,
        has_complete_claims=True,
    )

    assert local_parse_jobs([complete, partial]) == [
        (complete.publication_id, "local", f"local://{complete.publication_number}", "")
    ]


def test_fetch_handoff_writes_only_to_isolated_staging_schema():
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            statements.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {"database_name": "niche_full_v1", "fingerprint": "safe-test"}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    worked = niche_handoff.enqueue(
        publication_number="US1234567A1",
        family_id="42",
        authority="US",
        source_uri="gs://bucket/raw/US1234567A1/hash.xml",
        source_generation="hash",
        expected_database="niche_full_v1",
        fingerprint="safe-test",
        connection_factory=lambda: Connection(),
    )

    assert worked is True
    mutations = [sql.upper() for sql, _params in statements if sql.upper().startswith("INSERT")]
    assert mutations
    assert all("NICHE_CORPUS." in sql for sql in mutations)
    assert all("PUBLIC." not in sql for sql in mutations)


def test_niche_database_manifest_requires_explicit_staging_identity(monkeypatch):
    for name in (
        "NICHE_PARSE_DATABASE_URL",
        "NICHE_EXPECTED_DATABASE",
        "NICHE_DATABASE_FINGERPRINT",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="NICHE_PARSE_DATABASE_URL"):
        open_reader("niche-db")


def test_niche_database_manifest_pages_by_primary_key_and_returns_one_family_target():
    calls = []

    class Cursor:
        sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            calls.append((self.sql, params))

        def fetchone(self):
            return {"database_name": "niche_full_v1", "fingerprint": "safe-test"}

        def fetchall(self):
            if "ORDER BY updated_at, publication_id LIMIT" in self.sql:
                return [
                    {"publication_id": "EP1A1", "publication_number": "EP1A1",
                     "family_id": "F1", "authority": "EP", "priority": 2,
                     "updated_at": datetime(2026, 8, 22, 18, 0, tzinfo=timezone.utc),
                     "has_complete_claims": False, "has_complete_description": False},
                    {"publication_id": "US2A1", "publication_number": "US2A1",
                     "family_id": None, "authority": "US", "priority": 1,
                     "updated_at": datetime(2026, 8, 22, 18, 1, tzinfo=timezone.utc),
                     "has_complete_claims": False, "has_complete_description": False},
                ]
            return [
                {"publication_number": "US1A1", "family_id": "F1", "authority": "US",
                 "family_priority": 2}
            ]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            calls.append((sql, None))

        def cursor(self):
            return Cursor()

    reader = NicheDatabaseReader(
        connection_factory=lambda: Connection(),
        expected_database="niche_full_v1",
        fingerprint="safe-test",
    )
    entries, cursor, exhausted = reader.read("", 2)

    assert [entry.publication_number for entry in entries] == ["US1A1", "US2A1"]
    assert cursor == "2026-08-22T18:01:00+00:00|US2A1"
    assert exhausted is False
    assert any("SET TRANSACTION READ ONLY" in sql for sql, _params in calls)


def test_long_description_paragraph_is_split_without_dropping_text():
    original = " ".join(f"word{index}" for index in range(1800))
    chunks = build_chunks({
        "publication_id": "US1A1",
        "publication_number": "US1A1",
        "family_id": "family:1",
        "language": "en",
        "description_paragraphs": [{
            "id": "p1",
            "text": original,
            "source_location": "paragraph:p1",
            "language": "en",
        }],
    })
    descriptions = [row for row in chunks if row["chunk_kind"] == "description"]

    assert len(descriptions) > 1
    assert " ".join(row["text"] for row in descriptions) == original
    assert all(len(row["text"].encode("utf-8")) <= 6000 for row in descriptions)
    assert descriptions[0]["source_location"].endswith(";part:1")


def test_vertex_job_is_reconcilable_by_deterministic_labels_and_uris():
    body = vertex_job_body({
        "submission_key": "a" * 64,
        "display_name": "niche-full-v1-aaaaaaaaaaaaaaaaaaaaaaaa",
        "input_uri": "gs://batch/in.jsonl",
        "output_prefix": "gs://batch/out/",
        "model": "gemini-embedding-001",
    })

    assert body["labels"] == {"pipeline": "niche_full_v1", "submission": "a" * 32}
    assert body["inputConfig"]["gcsSource"]["uris"] == ["gs://batch/in.jsonl"]
    assert body["outputConfig"]["gcsDestination"]["outputUriPrefix"] == "gs://batch/out/"
    assert body["model"] == "publishers/google/models/gemini-embedding-001"


def test_vertex_submission_adopts_one_existing_job_without_posting():
    batch = {
        "submission_key": "b" * 64,
        "display_name": "niche-full-v1-bbbbbbbbbbbbbbbbbbbbbbbb",
        "input_uri": "gs://batch/in.jsonl",
        "output_prefix": "gs://batch/out/",
        "model": "gemini-embedding-001",
    }
    existing = {
        "name": "projects/p/locations/us-central1/batchPredictionJobs/1",
        "displayName": batch["display_name"],
        "labels": {"pipeline": "niche_full_v1", "submission": "b" * 32},
        "model": "publishers/google/models/gemini-embedding-001",
        "inputConfig": {"gcsSource": {"uris": [batch["input_uri"]]}},
        "outputConfig": {"gcsDestination": {"outputUriPrefix": batch["output_prefix"]}},
        "state": "JOB_STATE_RUNNING",
    }

    class Transport:
        def get(self, _url, params=None):
            assert params and params["filter"] == f"labels.submission={'b' * 32}"
            return {"batchPredictionJobs": [existing]}

        def post(self, *_args, **_kwargs):
            raise AssertionError("an existing Vertex job must be adopted, not submitted again")

    assert VertexBatchClient(project="p", transport=Transport()).submit(batch) == existing


def test_multiple_matching_vertex_jobs_fail_closed():
    batch = {
        "submission_key": "c" * 64,
        "display_name": "niche-full-v1-cccccccccccccccccccccccc",
        "input_uri": "gs://batch/in.jsonl",
        "output_prefix": "gs://batch/out/",
        "model": "gemini-embedding-001",
    }
    matching = {
        "name": "job/1",
        "displayName": batch["display_name"],
        "labels": {"pipeline": "niche_full_v1", "submission": "c" * 32},
        "model": "publishers/google/models/gemini-embedding-001",
        "inputConfig": {"gcsSource": {"uris": [batch["input_uri"]]}},
        "outputConfig": {"gcsDestination": {"outputUriPrefix": batch["output_prefix"]}},
    }

    class Transport:
        def get(self, _url, params=None):
            return {"batchPredictionJobs": [matching, {**matching, "name": "job/2"}]}

    with pytest.raises(AmbiguousSubmission):
        VertexBatchClient(project="p", transport=Transport()).submit(batch)


def test_gemini_full_vector_is_truncated_and_normalized_for_768_index():
    vector = project_embedding([3.0, 4.0, 12.0], 2)

    assert vector == pytest.approx([0.6, 0.8])
    assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_batch_output_reads_usage_metadata_token_count():
    line = (
        b'{"key":"k1","response":{"embedding":{"values":[0.1,0.2]},'
        b'"usageMetadata":{"promptTokenCount":17}},"status":""}\n'
    )

    class Blob:
        name = "prefix/predictions.jsonl"

        def download_as_bytes(self):
            return line

    class Client:
        def list_blobs(self, bucket, prefix):
            assert (bucket, prefix) == ("bucket", "prefix/")
            return [Blob()]

    vectors, errors = GCSBatchStore(Client()).collect("gs://bucket/prefix", ["k1"])

    assert errors == {}
    assert vectors["k1"] == {"values": [0.1, 0.2], "token_count": 17}


def test_embedding_controller_adopts_uncertain_submission_without_posting():
    batch = {
        "batch_id": 7,
        "submission_key": "d" * 64,
        "updated_at": datetime.now(timezone.utc),
    }
    job = {"name": "projects/p/locations/us-central1/batchPredictionJobs/7",
           "state": "JOB_STATE_RUNNING"}

    class Repository:
        def batches(self, statuses):
            return [batch] if statuses == ("submitting", "ambiguous") else []

        def load(self, batch_id):
            assert batch_id == 7
            return {**batch, "input_bytes": b"{}\n"}

        def adopt(self, batch_id, adopted):
            assert (batch_id, adopted) == (7, job)
            self.adopted = True

    class Store:
        def ensure_input(self, loaded, content):
            assert loaded["batch_id"] == 7
            assert content == b"{}\n"

    class Client:
        def find_matches(self, loaded):
            assert loaded["batch_id"] == 7
            return [job]

        def submit(self, _loaded):
            raise AssertionError("a restart must reconcile instead of posting again")

    repository = Repository()
    controller = BatchController(repository, Store(), Client(), ambiguity_grace_seconds=600)

    assert controller.reconcile_submitting() == {"adopted": 1, "ambiguous": 0, "waiting": 0}
    assert repository.adopted is True


def test_embedding_controller_fails_closed_after_submission_ambiguity_window():
    batch = {
        "batch_id": 8,
        "submission_key": "e" * 64,
        "updated_at": datetime.now(timezone.utc) - timedelta(minutes=20),
    }

    class Repository:
        def batches(self, statuses):
            return [batch] if statuses == ("submitting", "ambiguous") else []

        def load(self, _batch_id):
            return {**batch, "input_bytes": b"{}\n"}

        def ambiguous(self, batch_id, error):
            self.result = (batch_id, error)

    class Store:
        def ensure_input(self, _loaded, _content):
            return None

    class Client:
        def find_matches(self, _loaded):
            return []

        def submit(self, _loaded):
            raise AssertionError("an uncertain submission must never be posted again")

    repository = Repository()
    controller = BatchController(repository, Store(), Client(), ambiguity_grace_seconds=600)

    assert controller.reconcile_submitting() == {"adopted": 0, "ambiguous": 1, "waiting": 0}
    assert repository.result[0] == 8
    assert "no matching Vertex job" in repository.result[1]


def test_embedding_controller_repairs_previously_unread_completed_output():
    batch = {"batch_id": 9, "status": "succeeded", "last_error": "5000 item failures"}

    class Repository:
        def batches(self, statuses):
            return [batch] if statuses == ("succeeded",) else []

        def load(self, _batch_id):
            return {
                **batch,
                "output_prefix": "gs://bucket/out/",
                "items": [{"embedding_key": "k1"}],
            }

        def settle_success(self, batch_id, vectors, errors):
            self.result = (batch_id, vectors, errors)
            return {"ok": 1, "failed": 0}

    class Store:
        def collect(self, output, expected):
            assert output == "gs://bucket/job-output"
            assert expected == ["k1"]
            return {"k1": {"values": [1.0], "token_count": 3}}, {}

    class Client:
        def poll(self, _job_name, _batch):
            return {"outputInfo": {"gcsOutputDirectory": "gs://bucket/job-output"}}

    repository = Repository()
    controller = BatchController(repository, Store(), Client())

    assert controller.repair_completed_outputs() == {"repaired": 1, "errors": 0}
    assert repository.result[0] == 9


def test_streaming_migration_is_staging_only_and_durable():
    sql = (Path(__file__).parents[1] / "sql" / "niche" / "002_streaming_embedding.sql").read_text()
    normalized = " ".join(sql.upper().split())

    for table in (
        "NICHE_PARSE_JOBS",
        "NICHE_CHUNKS",
        "NICHE_EMBEDDING_CACHE",
        "NICHE_EMBEDDING_STAGE",
        "NICHE_EMBEDDING_BATCHES",
        "NICHE_EMBEDDING_BATCH_ITEMS",
        "EMBEDDING_BUDGET",
    ):
        assert f"NICHE_CORPUS.{table}" in normalized
    assert "FOR UPDATE SKIP LOCKED" not in normalized
    assert " PUBLIC.CHUNKS" not in normalized
    assert " PUBLIC.PUBLICATIONS" not in normalized
    assert "DELETE FROM" not in normalized


def test_vector_publish_claim_is_atomic_skip_locked_and_isolated():
    normalized = " ".join(PostgresVectorPublisher.PUBLISH_SQL.upper().split())

    assert "FOR UPDATE OF STAGE SKIP LOCKED" in normalized
    assert "NICHE_CORPUS.NICHE_VECTOR_DOCUMENTS" in normalized
    assert "NICHE_CORPUS.NICHE_EMBEDDING_STAGE" in normalized
    assert " PUBLIC.CHUNKS" not in normalized


def test_search_build_migration_has_isolated_hnsw_and_publish_watermark():
    sql = (Path(__file__).parents[1] / "sql" / "niche" / "004_search_build.sql").read_text()
    normalized = " ".join(sql.upper().split())

    assert "NICHE_CORPUS.NICHE_VECTOR_DOCUMENTS" in normalized
    assert "USING HNSW (EMBEDDING VECTOR_COSINE_OPS)" in normalized
    assert "TANTIVY_INDEXED_AT" in normalized
    assert " PUBLIC.CHUNKS" not in normalized
    assert "DELETE FROM" not in normalized


def test_tantivy_queue_is_bounded_and_reads_only_isolated_vector_table():
    normalized = " ".join(TantivyRepository.NEXT_SQL.upper().split())

    assert "NICHE_CORPUS.NICHE_VECTOR_DOCUMENTS" in normalized
    assert "TANTIVY_INDEX_GENERATION IS DISTINCT FROM %S" in normalized
    assert "ORDER BY CHUNK_ID, CORPUS_RELEASE" in normalized
    assert "LIMIT %S" in normalized
    assert " PUBLIC.CHUNKS" not in normalized


def test_tantivy_rerun_deletes_document_key_before_add_and_marks_after_commit():
    events = []
    rows = [{
        "chunk_id": "c1",
        "corpus_release": "niche_full_v1",
        "publication_id": "US1A1",
        "family_id": "F1",
        "chunk_kind": "claim_own",
        "claim_number": 1,
        "language": "en",
        "text": "vacuum suction gripper",
        "source_location": "claim:1",
        "content_hash": "abc",
    }]

    class Repository:
        def next_batch(self, limit):
            assert limit == 50
            return rows

        def mark_indexed(self, keys):
            events.append(("marked", keys))

    class Writer:
        def delete_documents_by_term(self, field, value):
            events.append(("delete", field, value))

        def add_document(self, document):
            events.append(("add", document))

        def commit(self):
            events.append(("commit",))

    class Index:
        def writer(self, **_kwargs):
            return Writer()

        def reload(self):
            events.append(("reload",))

    class Tantivy:
        @staticmethod
        def Document(**values):
            return values

    count = TantivyIndexer(Repository(), Index(), Tantivy()).index_batch(50)

    assert count == 1
    assert [event[0] for event in events] == ["delete", "add", "commit", "reload", "marked"]
    assert events[0][2] == "niche_full_v1\x1fc1"


def test_staging_target_is_checked_before_schema_ddl():
    from corpus.niche.database import validate_database_target

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            assert "CURRENT_DATABASE" in sql.upper()

        def fetchone(self):
            return {"database_name": "patents"}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    with pytest.raises(RuntimeError, match="database name mismatch"):
        validate_database_target(lambda: Connection(), "niche_full_v1")


def test_schema_bootstrap_refuses_a_matching_active_corpus_database_name():
    from corpus.niche.database import validate_database_target

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql):
            return None

        def fetchone(self):
            return {"database_name": "patents"}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    with pytest.raises(RuntimeError, match="approved niche staging name"):
        validate_database_target(lambda: Connection(), "patents")


def test_schema_initialization_requires_fingerprint_before_any_ddl(monkeypatch):
    from corpus.niche import cli, database

    monkeypatch.setenv("NICHE_EXPECTED_DATABASE", "niche_full_v1")
    monkeypatch.delenv("NICHE_DATABASE_FINGERPRINT", raising=False)
    applied = []
    monkeypatch.setattr(database, "validate_database_target", lambda *_args: None)
    monkeypatch.setattr(database, "apply_schema", lambda *_args: applied.append(True))

    with pytest.raises(RuntimeError, match="NICHE_DATABASE_FINGERPRINT"):
        cli._initialize_if_requested(SimpleNamespace(init_schema=True), lambda: None)

    assert applied == []


def test_every_niche_cli_writer_requires_the_identity_guard():
    import inspect

    from corpus.niche import cli

    for function in (
        cli.run_discover,
        cli.run_fetch,
        cli.run_parse,
        cli.run_status,
    ):
        assert "_require_staging(" in inspect.getsource(function)
    assert [path.name for path in cli.MIGRATIONS] == [
        "001_fetch_queue.sql",
        "002_streaming_embedding.sql",
        "003_manifest_stream.sql",
        "004_search_build.sql",
    ]


def test_acquisition_handoff_hashes_the_exact_uploaded_bytes():
    import gzip
    import json

    from acquire.blobstore import parsed_bytes, raw_bytes

    record = {"publication_number": "US1A1", "fetched_at": 123.5, "claims": "claim"}
    assert parsed_bytes(record) == json.dumps(
        record, ensure_ascii=False, default=str
    ).encode("utf-8")
    compressed = raw_bytes(b"<xml>source</xml>")
    assert gzip.decompress(compressed) == b"<xml>source</xml>"
    assert compressed == raw_bytes(b"<xml>source</xml>")


def test_integrated_raw_source_key_is_content_addressed_and_write_once(monkeypatch):
    from acquire import blobstore

    captured = []

    async def put(_client, name, data, content_type, **kwargs):
        captured.append({
            "name": name,
            "data": data,
            "content_type": content_type,
            "kwargs": kwargs,
        })
        return f"gs://bucket/{name}"

    monkeypatch.setattr(blobstore, "put", put)
    stored = blobstore.raw_bytes(b"<xml>source</xml>")
    digest = hashlib.sha256(stored).hexdigest()

    uri = asyncio.run(
        blobstore.put_raw(
            None,
            "US1A1",
            "epo",
            b"<xml>source</xml>",
            ext="xml",
            http_status=200,
            headers={"Content-Type": "application/xml", "Authorization": "secret"},
            source_url="https://example.test/patent?api_key=secret&lang=en",
        )
    )

    assert uri == f"gs://bucket/raw/US1A1/epo/{digest}.xml.gz"
    assert [item["name"] for item in captured] == [
        f"raw/US1A1/epo/{digest}.xml.gz",
        f"raw/US1A1/epo/{digest}.metadata.json",
    ]
    assert all(item["kwargs"]["write_once"] is True for item in captured)
    metadata = json.loads(captured[1]["data"])
    assert metadata["provider"] == "epo"
    assert metadata["stored_content_hash"] == digest
    assert metadata["http_headers"] == {"content-type": "application/xml"}
    assert metadata["source_url"] == "https://example.test/patent?lang=en"


def test_integrated_fetch_isolated_mode_skips_active_corpus_stores(monkeypatch):
    import runstore
    from acquire import blobstore, niche_handoff, providers, worker
    from sources import docstore

    monkeypatch.setenv("NICHE_FACTORY_ISOLATED", "1")
    monkeypatch.setenv("NICHE_PARSE_DATABASE_URL", "postgresql://staging")
    monkeypatch.setenv("NICHE_EXPECTED_DATABASE", "niche_full_v1")
    monkeypatch.setenv("NICHE_DATABASE_FINGERPRINT", "safe-test")
    monkeypatch.setattr(blobstore, "enabled", lambda: True)

    async def put_raw(*_args, **_kwargs):
        return "gs://bucket/raw/US1A1/provider/hash.json.gz"

    async def put_parsed(*_args, **_kwargs):
        return "gs://bucket/parsed/US1A1/provider.json"

    monkeypatch.setattr(blobstore, "put_raw", put_raw)
    monkeypatch.setattr(blobstore, "put_parsed", put_parsed)
    handoffs = []
    monkeypatch.setattr(niche_handoff, "enqueue", lambda **kwargs: handoffs.append(kwargs) or True)
    monkeypatch.setattr(
        docstore,
        "_put_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("isolated fetch must not write sources_docstore")
        ),
    )
    monkeypatch.setattr(
        runstore,
        "queue_for_ingest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("isolated fetch must not write corpus_ingest_queue")
        ),
    )
    result = providers.FetchResult(
        provider="provider",
        claims="1. A vacuum gripper.",
        description="Description " * 100,
        raw=b'{"claims":"1. A vacuum gripper."}',
    )

    uris = asyncio.run(
        worker.Worker(0, 1, cascade=[]).store(
            "US1A1", result, None, [], {"partition_id": 0, "family_id": "F1"}
        )
    )

    assert uris["parsed_uri"] == "gs://bucket/parsed/US1A1/provider.json"
    assert handoffs[0]["publication_number"] == "US1A1"


def test_acquisition_worker_does_not_complete_after_heartbeat_loses_lease(monkeypatch):
    from acquire import worker

    fetcher = worker.Worker(0, 1, cascade=[])

    async def cascade(publication, *_args):
        fetcher.cancelled.add(publication)

    monkeypatch.setattr(fetcher, "cascade_for", cascade)
    monkeypatch.setattr(
        worker.tasks,
        "complete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a worker that lost its lease must not complete the job")
        ),
    )

    asyncio.run(
        fetcher.handle(
            {"publication_number": "US3A1", "partition_id": 0},
            None,
            [],
        )
    )


def test_acquisition_lease_loss_stops_before_the_next_provider():
    from acquire import providers, worker

    class First(providers.Provider):
        name = "first"

        async def fetch(self, publication, _client):
            fetcher.cancelled.add(publication)
            return providers.FetchResult(provider=self.name, reached=False)

    class Second(providers.Provider):
        name = "second"

        async def fetch(self, _publication, _client):
            raise AssertionError("lease loss must cancel the remaining provider waterfall")

    fetcher = worker.Worker(0, 1, cascade=[First(), Second()])
    result = asyncio.run(
        fetcher.cascade_for(
            "US4A1",
            {"publication_number": "US4A1", "partition_id": 0},
            None,
            [],
        )
    )

    assert result is None


def test_stream_reader_verifies_hash_before_decompressing_raw_fallback():
    import gzip
    import hashlib

    from corpus.niche.stream import decode_source_bytes

    stored = gzip.compress(b"<xml>source</xml>", compresslevel=6, mtime=0)
    generation = "sha256:" + hashlib.sha256(stored).hexdigest()
    assert decode_source_bytes(stored, "gs://bucket/raw/US1/provider.xml.gz", generation) == (
        b"<xml>source</xml>"
    )
    with pytest.raises(RuntimeError, match="content hash mismatch"):
        decode_source_bytes(stored + b"x", "gs://bucket/raw/US1/provider.xml.gz", generation)


def test_late_duplicate_stage_inherits_completed_embedding_state():
    from corpus.niche.stream import SYNC_STAGE_STATUS_SQL

    normalized = " ".join(SYNC_STAGE_STATUS_SQL.upper().split())
    assert "CACHE.STATUS = 'COMPLETE'" in normalized
    assert "THEN 'COMPLETE'" in normalized
    assert "STAGE.EMBEDDING_KEY = CACHE.EMBEDDING_KEY" in normalized


def test_streaming_schema_tracks_source_versions_and_active_chunk_replacements():
    sql = (
        Path(__file__).parents[1] / "sql" / "niche" / "002_streaming_embedding.sql"
    ).read_text()
    normalized = " ".join(sql.upper().split())

    assert "NICHE_CORPUS.NICHE_PARSED_SOURCES" in normalized
    assert "ACTIVE BOOLEAN" in normalized
    assert "RETIRED_AT" in normalized
    assert "NICHE_CORPUS.NICHE_TANTIVY_DELETIONS" in normalized


def test_embedding_batches_retain_their_own_accounting_configuration():
    sql = (
        Path(__file__).parents[1] / "sql" / "niche" / "002_streaming_embedding.sql"
    ).read_text()
    normalized = " ".join(sql.upper().split())

    for column in (
        "BUDGET_KEY",
        "PRICE_USD_PER_MILLION_TOKENS",
        "GCP_PROJECT",
        "GCP_LOCATION",
    ):
        assert column in normalized
    assert "ALTER COLUMN BUDGET_KEY SET NOT NULL" in normalized
    assert "ALTER COLUMN PRICE_USD_PER_MILLION_TOKENS SET NOT NULL" in normalized
    assert "ALTER COLUMN GCP_PROJECT SET NOT NULL" in normalized
    assert "ALTER COLUMN GCP_LOCATION SET NOT NULL" in normalized


def test_gemini_batch_size_is_capped_at_provider_limit():
    settings = EmbeddingSettings.from_env({
        "GEMINI_EMBED_MODEL": "gemini-embedding-001",
        "GEMINI_EMBED_DIMENSION": "768",
        "GEMINI_EMBED_TASK_TYPE": "RETRIEVAL_DOCUMENT",
        "NICHE_CORPUS_RELEASE": "niche_full_v1",
        "MAX_GEMINI_EMBED_USD_TOTAL": "49",
        "GEMINI_EMBED_PRICE_USD_PER_MTOK": "0.12",
        "GEMINI_BATCH_BUCKET": "nimo-patents-v3",
        "NICHE_EXPECTED_DATABASE": "niche_full_v1",
        "NICHE_DATABASE_FINGERPRINT": "niche-full-v1-test",
        "GEMINI_BATCH_SIZE": "50000",
    })

    assert settings.batch_size == 30_000


def test_missing_provider_token_usage_remains_unknown():
    line = b'{"key":"k1","response":{"embedding":{"values":[0.1,0.2]}},"status":""}\n'

    class Blob:
        name = "prefix/predictions.jsonl"

        def download_as_bytes(self):
            return line

    class Client:
        def list_blobs(self, _bucket, prefix):
            assert prefix == "prefix/"
            return [Blob()]

    vectors, errors = GCSBatchStore(Client()).collect("gs://bucket/prefix", ["k1"])

    assert errors == {}
    assert vectors["k1"]["token_count"] is None


def test_ambiguous_vertex_submission_is_reconciled_on_later_cycles():
    batch = {
        "batch_id": 12,
        "submission_key": "f" * 64,
        "status": "ambiguous",
        "updated_at": datetime.now(timezone.utc) - timedelta(hours=1),
    }
    job = {"name": "jobs/12", "state": "JOB_STATE_RUNNING"}

    class Repository:
        def batches(self, statuses):
            return [batch] if statuses == ("submitting", "ambiguous") else []

        def load(self, _batch_id):
            return {**batch, "input_bytes": b"{}\n"}

        def adopt(self, batch_id, adopted):
            self.adopted = (batch_id, adopted)

    class Store:
        def ensure_input(self, _batch, _content):
            return None

    class Client:
        def find_matches(self, _batch):
            return [job]

    repository = Repository()
    controller = BatchController(repository, Store(), Client())

    assert controller.reconcile_submitting()["adopted"] == 1
    assert repository.adopted == (12, job)


def test_pre_provider_lookup_failure_waits_for_next_controller_cycle():
    batch = {"batch_id": 14, "input_bytes": b"{}\n"}

    class Repository:
        def batches(self, statuses):
            return [batch] if statuses == ("prepared",) else []

        def load(self, _batch_id):
            return batch

        def begin_submission(self, _batch_id):
            return batch

        def retry_prepared(self, batch_id, _error):
            assert batch_id == 14

    class Store:
        def ensure_input(self, _batch, _content):
            return None

    class Client:
        def find_matches(self, _batch):
            raise RuntimeError("temporary list failure")

    controller = BatchController(Repository(), Store(), Client())

    assert controller._submit_one_prepared() is False


def test_ambiguous_batches_count_toward_active_submission_limit():
    class Repository:
        def batches(self, statuses):
            if statuses == ("submitting", "submitted", "ambiguous"):
                return [{"batch_id": 15}]
            return []

        def prepare(self, **_kwargs):
            raise AssertionError("an unresolved ambiguous job must hold an active slot")

    controller = BatchController(
        Repository(), object(), object(), max_active_batches=1
    )
    controller._submit_one_prepared = lambda: (_ for _ in ()).throw(
        AssertionError("an unresolved ambiguous job must block another submission")
    )

    result = controller.run_once()

    assert result["submission_cycles"] == 0


def test_vector_release_has_one_embedding_configuration():
    migration = (
        Path(__file__).parents[1] / "sql" / "niche" / "004_search_build.sql"
    ).read_text()
    publish_sql = " ".join(PostgresVectorPublisher.PUBLISH_SQL.upper().split())
    normalized = " ".join(migration.upper().split())

    assert "NICHE_CORPUS.NICHE_EMBEDDING_RELEASES" in normalized
    assert "FOREIGN KEY (CORPUS_RELEASE, MODEL, DIMENSION, TASK_TYPE)" in normalized
    assert "NICHE_CORPUS.NICHE_EMBEDDING_RELEASES" in publish_sql
    for column in ("MODEL", "DIMENSION", "TASK_TYPE"):
        assert column in publish_sql


def test_tantivy_mark_is_bound_to_generation_and_content_hash():
    import inspect

    from corpus.niche.tantivy_build import TantivyRepository

    source = inspect.getsource(TantivyRepository.mark_indexed).upper()
    assert "TANTIVY_INDEX_GENERATION" in source
    assert "CONTENT_HASH" in source


def test_tantivy_index_generation_is_stable_only_for_the_same_path(tmp_path):
    from corpus.niche.tantivy_build import ensure_index_generation

    first = ensure_index_generation(tmp_path / "one")
    assert ensure_index_generation(tmp_path / "one") == first
    copied = tmp_path / "two"
    copied.mkdir()
    (copied / ".niche-index-generation").write_text(first)
    assert ensure_index_generation(copied) != first


def test_source_status_queries_have_a_bounded_event_window():
    import inspect

    from corpus.niche.status import _acquisition_status

    source = inspect.getsource(_acquisition_status).upper()
    assert "FULLTEXT_FETCH_EVENT" in source
    assert "AT >= NOW() -" in source
    assert "STATEMENT_TIMEOUT" in source


def test_source_status_rates_count_only_real_provider_requests():
    import inspect

    from corpus.niche.status import _acquisition_status

    source = " ".join(inspect.getsource(_acquisition_status).upper().split())
    assert "COUNT(*) FILTER (WHERE OUTCOME IN" in source
    assert "('HIT','MISS','ERROR','TIMEOUT')) AS ATTEMPTS" in source
    assert "IF INT(ROW[\"ATTEMPTS\"])" in source
