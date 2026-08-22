"""Opt-in PostgreSQL integration checks for the isolated niche factory.

Set ``NICHE_TEST_DATABASE_URL`` to a disposable database whose name begins with
``niche_full_v1_qa``. These tests never connect to the production corpus.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest


def _factory():
    dsn = str(os.environ.get("NICHE_TEST_DATABASE_URL") or "").strip()
    if not dsn:
        pytest.skip("NICHE_TEST_DATABASE_URL is not configured")
    from corpus.niche.database import connection_factory

    factory = connection_factory(dsn, application_name="niche-postgres-integration-test")
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_database() AS database_name")
        database_name = str(cursor.fetchone()["database_name"])
    if not database_name.startswith("niche_full_v1_qa"):
        raise RuntimeError("integration tests require a disposable niche_full_v1_qa database")
    return factory, database_name


def _settings():
    from corpus.niche.embedding import EmbeddingSettings

    return EmbeddingSettings.from_env({
        "GEMINI_EMBED_MODEL": "gemini-embedding-001",
        "GEMINI_EMBED_DIMENSION": "768",
        "GEMINI_EMBED_TASK_TYPE": "RETRIEVAL_DOCUMENT",
        "NICHE_CORPUS_RELEASE": "niche_full_v1_qa",
        "GEMINI_EMBED_BUDGET_KEY": "niche_full_v1_qa",
        "MAX_GEMINI_EMBED_USD_TOTAL": "1",
        "GEMINI_EMBED_PRICE_USD_PER_MTOK": "0.12",
        "GEMINI_BATCH_BUCKET": "qa-never-used",
        "GCP_PROJECT": "nimo-gpt",
        "NICHE_EXPECTED_DATABASE": "niche_full_v1_qa",
        "NICHE_DATABASE_FINGERPRINT": "qa-only",
    })


def _publication(factory, publication_id: str, family_id: str) -> None:
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO niche_corpus.niche_publications "
            "(publication_id,publication_number,family_id,authority) "
            "VALUES (%s,%s,%s,'US') ON CONFLICT (publication_id) DO NOTHING",
            (publication_id, publication_id, family_id),
        )


def _reset_publications(factory, publication_ids: list[str]) -> None:
    """Delete only this disposable test's rows so the integration check is rerunnable."""
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM niche_corpus.niche_vector_documents WHERE publication_id=ANY(%s)",
            (publication_ids,),
        )
        cursor.execute(
            "DELETE FROM niche_corpus.niche_embedding_stage AS stage USING "
            "niche_corpus.niche_chunks AS chunks WHERE stage.chunk_id=chunks.chunk_id "
            "AND chunks.publication_id=ANY(%s)",
            (publication_ids,),
        )
        cursor.execute(
            "DELETE FROM niche_corpus.niche_chunks WHERE publication_id=ANY(%s)",
            (publication_ids,),
        )
        cursor.execute(
            "DELETE FROM niche_corpus.niche_publications WHERE publication_id=ANY(%s)",
            (publication_ids,),
        )


def _complete_cache(factory, embedding_key: str) -> None:
    vector = "[" + ",".join(["0.03608439"] * 768) + "]"
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE niche_corpus.niche_embedding_cache "
            "SET status='complete',vector=%s::vector WHERE embedding_key=%s",
            (vector, embedding_key),
        )
        cursor.execute(
            "UPDATE niche_corpus.niche_embedding_stage AS stage SET status='complete' "
            "FROM niche_corpus.niche_embedding_cache AS cache "
            "WHERE stage.embedding_key=cache.embedding_key AND cache.embedding_key=%s",
            (embedding_key,),
        )


def test_postgres_stage_publish_late_reuse_and_source_replacement():
    from corpus.niche.chunks import build_chunks
    from corpus.niche.publish import PostgresVectorPublisher
    from corpus.niche.stream import stage_chunks

    factory, _database_name = _factory()
    settings = _settings()
    first_publication = "USQA1001A1"
    second_publication = "USQA1002A1"
    _reset_publications(factory, [first_publication, second_publication])
    _publication(factory, first_publication, "QA-FAMILY-1")
    _publication(factory, second_publication, "QA-FAMILY-2")

    first_chunks = build_chunks({
        "publication_id": first_publication,
        "family_id": "QA-FAMILY-1",
        "language": "en",
        "description_paragraphs": [{
            "id": "p1",
            "text": "A reusable vacuum gripper paragraph.",
            "source_location": "paragraph:p1",
        }],
    })
    stage_chunks(
        factory,
        first_chunks,
        settings,
        source_uri="gs://qa/source-one.json",
        source_generation="sha256:first",
        publication_id=first_publication,
    )
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT embedding_key FROM niche_corpus.niche_embedding_stage "
            "WHERE chunk_id=%s",
            (first_chunks[0]["chunk_id"],),
        )
        embedding_key = str(cursor.fetchone()["embedding_key"])
    _complete_cache(factory, embedding_key)
    assert PostgresVectorPublisher(factory).publish_batch(10) == 1

    duplicate_chunks = build_chunks({
        "publication_id": second_publication,
        "family_id": "QA-FAMILY-2",
        "language": "en",
        "description_paragraphs": [{
            "id": "p1",
            "text": "A reusable vacuum gripper paragraph.",
            "source_location": "paragraph:p1",
        }],
    })
    stage_chunks(
        factory,
        duplicate_chunks,
        settings,
        source_uri="gs://qa/source-two.json",
        source_generation="sha256:second",
        publication_id=second_publication,
    )

    replacement_chunks = build_chunks({
        "publication_id": first_publication,
        "family_id": "QA-FAMILY-1",
        "language": "en",
        "description_paragraphs": [{
            "id": "p1",
            "text": "A corrected vacuum lifting paragraph.",
            "source_location": "paragraph:p1",
        }],
    })
    stage_chunks(
        factory,
        replacement_chunks,
        settings,
        source_uri="gs://qa/source-one.json",
        source_generation="sha256:replacement",
        publication_id=first_publication,
    )

    with factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT status FROM niche_corpus.niche_embedding_stage WHERE chunk_id=%s",
            (duplicate_chunks[0]["chunk_id"],),
        )
        assert cursor.fetchone()["status"] == "complete"
        cursor.execute(
            "SELECT active FROM niche_corpus.niche_vector_documents "
            "WHERE chunk_id=%s AND corpus_release=%s",
            (first_chunks[0]["chunk_id"], settings.corpus_release),
        )
        assert cursor.fetchone()["active"] is False
        cursor.execute(
            "SELECT count(*) AS n FROM niche_corpus.niche_tantivy_deletions"
        )
        assert int(cursor.fetchone()["n"]) >= 1
        cursor.execute(
            "SELECT count(*) AS n FROM pg_indexes "
            "WHERE schemaname='niche_corpus' "
            "AND indexname='niche_vector_documents_active_hnsw_idx'"
        )
        assert int(cursor.fetchone()["n"]) == 1


def test_postgres_two_workers_claim_one_parse_job():
    from corpus.niche.stream import PostgresParseQueue

    factory, _database_name = _factory()
    publication = "USQA2001A1"
    _reset_publications(factory, [publication])
    _publication(factory, publication, "QA-FAMILY-3")
    queue = PostgresParseQueue(factory)
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM niche_corpus.niche_parse_jobs WHERE source_uri=%s",
            ("gs://qa/two-worker-source.json",),
        )
    queue.enqueue(
        publication,
        "gcs",
        "gs://qa/two-worker-source.json",
        "sha256:two-worker",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda worker: queue.claim(worker, 60), ("worker-a", "worker-b")))

    claimed = [job for job in claims if job and job.publication_id == publication]
    assert len(claimed) == 1
    first = claimed[0]
    assert queue.cancel(first, first.worker_id, "integration shutdown") is True

    reclaimed = queue.claim("worker-c", 60)
    assert reclaimed is not None
    assert reclaimed.job_id == first.job_id
    assert reclaimed.publication_id == publication
    assert reclaimed.attempt == 1
    assert queue.complete(reclaimed.job_id, "worker-c") is True


def test_postgres_fetch_cancel_requeues_without_consuming_attempt():
    from corpus.niche.queue import PostgresFetchQueue

    factory, _database_name = _factory()
    publication = "USQA2002A1"
    _reset_publications(factory, [publication])
    _publication(factory, publication, "QA-FAMILY-4")
    queue = PostgresFetchQueue(factory, max_attempts=3)
    queued = queue.enqueue(publication, priority=1)

    leased = queue.claim("fetch-worker-a", 60)
    assert leased is not None
    assert leased.job_id == queued.job_id
    assert leased.attempt == 1
    assert queue.cancel(leased, "fetch-worker-a", "integration shutdown") is True

    reclaimed = queue.claim("fetch-worker-b", 60)
    assert reclaimed is not None
    assert reclaimed.job_id == queued.job_id
    assert reclaimed.attempt == 1
    assert queue.complete(reclaimed.job_id, "fetch-worker-b") is True
