"""Exactly-once publication of completed embeddings into the isolated search table."""
from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time

from .database import connection_factory, require_dsn, validate_staging_database


class PostgresVectorPublisher:
    PUBLISH_SQL = """
    WITH selected AS MATERIALIZED (
        SELECT stage.chunk_id, stage.corpus_release,
               chunks.publication_id, chunks.family_id, chunks.chunk_kind,
               chunks.claim_number, chunks.language, chunks.text,
               chunks.source_location, chunks.content_hash,
               cache.embedding_key, stage.model, stage.dimension, stage.task_type,
               cache.vector, cache.updated_at AS embedded_at
          FROM niche_corpus.niche_embedding_stage AS stage
          JOIN niche_corpus.niche_chunks AS chunks
            ON chunks.chunk_id = stage.chunk_id
          JOIN niche_corpus.niche_embedding_cache AS cache
            ON cache.embedding_key = stage.embedding_key
          JOIN niche_corpus.niche_embedding_releases AS releases
            ON releases.corpus_release = stage.corpus_release
           AND releases.model = stage.model
           AND releases.dimension = stage.dimension
           AND releases.task_type = stage.task_type
         WHERE stage.status = 'complete'
           AND stage.active
           AND chunks.active
           AND stage.published_at IS NULL
           AND cache.status = 'complete'
           AND cache.vector IS NOT NULL
         ORDER BY stage.chunk_id, stage.corpus_release
         FOR UPDATE OF stage SKIP LOCKED
         LIMIT %s
    ), inserted AS (
        INSERT INTO niche_corpus.niche_vector_documents (
            chunk_id, corpus_release, publication_id, family_id, chunk_kind,
            claim_number, language, text, source_location, content_hash,
            embedding_key, model, dimension, task_type, embedding, embedded_at, active
        )
        SELECT chunk_id, corpus_release, publication_id, family_id, chunk_kind,
               claim_number, language, text, source_location, content_hash,
               embedding_key, model, dimension, task_type, vector, embedded_at, true
          FROM selected
        ON CONFLICT (chunk_id, corpus_release) DO UPDATE SET
            publication_id = EXCLUDED.publication_id,
            family_id = EXCLUDED.family_id,
            chunk_kind = EXCLUDED.chunk_kind,
            claim_number = EXCLUDED.claim_number,
            language = EXCLUDED.language,
            text = EXCLUDED.text,
            source_location = EXCLUDED.source_location,
            content_hash = EXCLUDED.content_hash,
            embedding_key = EXCLUDED.embedding_key,
            model = EXCLUDED.model,
            dimension = EXCLUDED.dimension,
            task_type = EXCLUDED.task_type,
            embedding = EXCLUDED.embedding,
            embedded_at = EXCLUDED.embedded_at,
            tantivy_indexed_at = NULL,
            tantivy_index_generation = NULL,
            active = true,
            updated_at = now()
        RETURNING chunk_id, corpus_release
    )
    UPDATE niche_corpus.niche_embedding_stage AS stage
       SET published_at = now(), updated_at = now()
      FROM selected
     WHERE stage.chunk_id = selected.chunk_id
       AND stage.corpus_release = selected.corpus_release
    RETURNING stage.chunk_id
    """

    def __init__(self, factory):
        self.factory = factory

    def publish_batch(self, limit: int = 5000) -> int:
        with self.factory() as connection, connection.cursor() as cursor:
            cursor.execute(self.PUBLISH_SQL, (min(50_000, max(1, int(limit))),))
            return len(cursor.fetchall())

    def status(self) -> dict:
        with self.factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FILTER (WHERE published_at IS NULL) AS pending, "
                "count(*) FILTER (WHERE published_at IS NOT NULL) AS published "
                "FROM niche_corpus.niche_embedding_stage WHERE status='complete'"
            )
            stage = dict(cursor.fetchone() or {})
            cursor.execute(
                "SELECT count(*) AS vectors, "
                "count(*) FILTER (WHERE tantivy_index_generation IS NOT NULL) AS tantivy "
                "FROM niche_corpus.niche_vector_documents WHERE active"
            )
            vectors = dict(cursor.fetchone() or {})
        return {**stage, **vectors}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.corpus.niche.publish",
        description="Publish completed niche vectors into the isolated search table.",
    )
    parser.add_argument(
        "--niche-dsn", default=os.environ.get("NICHE_DATABASE_URL", "")
    )
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    expected = str(os.environ.get("NICHE_EXPECTED_DATABASE") or "").strip()
    fingerprint = str(os.environ.get("NICHE_DATABASE_FINGERPRINT") or "").strip()
    factory = connection_factory(
        require_dsn(args.niche_dsn, "NICHE_DATABASE_URL"),
        application_name="niche-vector-publish",
    )
    validate_staging_database(factory, expected, fingerprint)
    publisher = PostgresVectorPublisher(factory)
    if args.status:
        print(json.dumps(publisher.status(), sort_keys=True, default=str))
        return 0
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    total = 0
    while not stop.is_set():
        count = publisher.publish_batch(args.batch_size)
        total += count
        print(json.dumps({"published": count, "total": total}), flush=True)
        if args.once:
            break
        if count == 0:
            stop.wait(max(1.0, float(args.poll_seconds)))
        else:
            time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
