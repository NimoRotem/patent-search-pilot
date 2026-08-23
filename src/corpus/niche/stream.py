"""Durable streaming queue and staging helpers for parsed niche documents."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from .chunks import build_chunks
from .embedding import EmbeddingSettings, embedding_rows
from .identifiers import authority_of, normalize_publication_number
from .manifest import PublicationRecord
from .models import Completeness, FetchRequest
from .parse import parse_source
from .providers.local import LocalCorpusProvider


@dataclass(frozen=True)
class ParseJob:
    job_id: int
    publication_id: str
    source_kind: str
    source_uri: str
    source_generation: str
    status: str
    worker_id: str | None
    lease_until: datetime | None
    attempt: int


class ParseLeaseLost(RuntimeError):
    """Raised when a worker no longer owns the durable parse lease."""


class ParseWorkerCancelled(RuntimeError):
    """Raised when the worker pool is shutting down between durable steps."""


def _job(row) -> ParseJob | None:
    if not row:
        return None
    values = dict(row)
    return ParseJob(**{name: values.get(name) for name in ParseJob.__dataclass_fields__})


class PostgresParseQueue:
    CLAIM_SQL = """
    WITH candidate AS (
        SELECT job_id
          FROM niche_corpus.niche_parse_jobs
         WHERE status = 'pending'
           AND next_attempt_at <= now()
         ORDER BY next_attempt_at, job_id
         FOR UPDATE SKIP LOCKED
         LIMIT 1
    )
    UPDATE niche_corpus.niche_parse_jobs AS jobs
       SET status = 'leased', worker_id = %s,
           lease_until = now() + (%s * interval '1 second'),
           heartbeat_at = now(), attempt = jobs.attempt + 1
      FROM candidate
     WHERE jobs.job_id = candidate.job_id
    RETURNING jobs.*
    """

    def __init__(self, connection_factory, *, max_attempts: int = 5):
        self.connection_factory = connection_factory
        self.max_attempts = max(1, int(max_attempts))

    def enqueue(
        self,
        publication_id: str,
        source_kind: str,
        source_uri: str,
        source_generation: str = "",
    ) -> bool:
        if source_kind not in {"local", "gcs"}:
            raise ValueError("parse source kind must be local or gcs")
        sql = """
        INSERT INTO niche_corpus.niche_parse_jobs
            (publication_id, source_kind, source_uri, source_generation)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (source_uri, source_generation) DO UPDATE
           SET publication_id = EXCLUDED.publication_id
        RETURNING (xmax = 0) AS inserted
        """
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql,
                (str(publication_id), source_kind, str(source_uri), str(source_generation or "")),
            )
            row = cursor.fetchone() or {}
            return bool(row.get("inserted"))

    def enqueue_many(self, rows) -> int:
        values = [(
            str(publication_id), str(source_kind), str(source_uri),
            str(source_generation or ""),
        ) for publication_id, source_kind, source_uri, source_generation in rows]
        if not values:
            return 0
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO niche_corpus.niche_parse_jobs
                    (publication_id, source_kind, source_uri, source_generation)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (source_uri, source_generation) DO UPDATE
                   SET publication_id=EXCLUDED.publication_id
                """,
                values,
            )
        return len(values)

    def claim(self, worker_id: str, lease_seconds: int) -> ParseJob | None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(self.CLAIM_SQL, (worker_id, max(5, int(lease_seconds))))
            return _job(cursor.fetchone())

    def heartbeat(self, job_id: int, worker_id: str, lease_seconds: int) -> bool:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE niche_corpus.niche_parse_jobs SET heartbeat_at=now(), "
                "lease_until=now() + (%s * interval '1 second') "
                "WHERE job_id=%s AND status='leased' AND worker_id=%s "
                "AND lease_until > now()",
                (max(5, int(lease_seconds)), int(job_id), worker_id),
            )
            return cursor.rowcount == 1

    def complete(self, job_id: int, worker_id: str) -> bool:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE niche_corpus.niche_parse_jobs SET status='completed', "
                "completed_at=now(), lease_until=NULL, heartbeat_at=now() "
                "WHERE job_id=%s AND status='leased' AND worker_id=%s "
                "AND lease_until > now()",
                (int(job_id), worker_id),
            )
            return cursor.rowcount == 1

    def fail(self, job: ParseJob, worker_id: str, error: str) -> bool:
        terminal = int(job.attempt) >= self.max_attempts
        delay = min(3600.0, 30.0 * (2 ** max(0, int(job.attempt) - 1)))
        delay *= 0.8 + (0.4 * random.random())
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE niche_corpus.niche_parse_jobs SET status=%s, worker_id=NULL, "
                "lease_until=NULL, next_attempt_at=CASE WHEN %s THEN next_attempt_at "
                "ELSE now() + (%s * interval '1 second') END, "
                "completed_at=CASE WHEN %s THEN now() ELSE NULL END, last_error=%s "
                "WHERE job_id=%s AND status='leased' AND worker_id=%s",
                (
                    "failed" if terminal else "pending",
                    terminal,
                    delay,
                    terminal,
                    str(error)[:2000],
                    int(job.job_id),
                    worker_id,
                ),
            )
            return cursor.rowcount == 1

    def cancel(self, job: ParseJob, worker_id: str, reason: str = "worker shutdown") -> bool:
        """Return an owned job immediately without consuming a retry attempt."""
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE niche_corpus.niche_parse_jobs SET status='pending', worker_id=NULL, "
                "lease_until=NULL, heartbeat_at=now(), next_attempt_at=now(), "
                "attempt=GREATEST(0, attempt - 1), last_error=%s "
                "WHERE job_id=%s AND status='leased' AND worker_id=%s "
                "AND lease_until > now()",
                (str(reason)[:2000], int(job.job_id), worker_id),
            )
            return cursor.rowcount == 1

    def reclaim_expired(self) -> int:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE niche_corpus.niche_parse_jobs SET "
                "status=CASE WHEN attempt >= %s THEN 'failed' ELSE 'pending' END, "
                "worker_id=NULL, lease_until=NULL, next_attempt_at=now(), "
                "completed_at=CASE WHEN attempt >= %s THEN now() ELSE NULL END, "
                "last_error=COALESCE(last_error, 'lease expired') "
                "WHERE status='leased' AND lease_until <= now()",
                (self.max_attempts, self.max_attempts),
            )
            return int(cursor.rowcount or 0)


SYNC_STAGE_STATUS_SQL = """
UPDATE niche_corpus.niche_embedding_stage AS stage
   SET status = CASE
           WHEN cache.status = 'complete' THEN 'complete'
           WHEN cache.status = 'failed' THEN 'failed'
           ELSE 'pending'
       END,
       active = true,
       retired_at = NULL,
       updated_at = now()
  FROM niche_corpus.niche_embedding_cache AS cache
 WHERE stage.embedding_key = cache.embedding_key
   AND stage.chunk_id = ANY(%s)
   AND stage.model = %s
   AND stage.dimension = %s
   AND stage.task_type = %s
   AND stage.corpus_release = %s
"""


def stage_chunks(
    connection_factory,
    chunks,
    settings: EmbeddingSettings,
    *,
    source_uri: str,
    source_generation: str = "",
    publication_id: str = "",
) -> int:
    """Idempotently stage chunk occurrences and their deduplicated embedding work."""
    rows = list(chunks)
    publication = str(
        publication_id or (rows[0].get("publication_id") if rows else "") or ""
    )
    if not publication:
        raise ValueError("publication_id is required when staging chunks")
    chunk_ids = [str(row["chunk_id"]) for row in rows]
    cache, stage = embedding_rows(rows, settings)
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO niche_corpus.niche_embedding_releases
                (corpus_release,model,dimension,task_type)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (corpus_release) DO NOTHING
            """,
            (
                settings.corpus_release,
                settings.model,
                settings.dimension,
                settings.task_type,
            ),
        )
        cursor.execute(
            "SELECT corpus_release FROM niche_corpus.niche_embedding_releases "
            "WHERE corpus_release=%s AND model=%s AND dimension=%s AND task_type=%s",
            (
                settings.corpus_release,
                settings.model,
                settings.dimension,
                settings.task_type,
            ),
        )
        if not cursor.fetchone():
            raise RuntimeError("corpus release already has a different embedding configuration")
        cursor.execute(
            """
            INSERT INTO niche_corpus.niche_tantivy_deletions(document_key)
            SELECT vectors.corpus_release || chr(31) || vectors.chunk_id
              FROM niche_corpus.niche_vector_documents AS vectors
             WHERE vectors.publication_id=%s AND vectors.active
               AND NOT (vectors.chunk_id=ANY(%s))
            ON CONFLICT (document_key) DO NOTHING
            """,
            (publication, chunk_ids),
        )
        cursor.execute(
            "UPDATE niche_corpus.niche_vector_documents "
            "SET active=false, tantivy_index_generation=NULL, "
            "tantivy_indexed_at=NULL, updated_at=now() "
            "WHERE publication_id=%s AND active AND NOT (chunk_id=ANY(%s))",
            (publication, chunk_ids),
        )
        cursor.execute(
            "UPDATE niche_corpus.niche_embedding_stage "
            "SET active=false, retired_at=now(), published_at=NULL, updated_at=now() "
            "WHERE active AND chunk_id IN ("
            "SELECT chunk_id FROM niche_corpus.niche_chunks WHERE publication_id=%s"
            ") AND NOT (chunk_id=ANY(%s))",
            (publication, chunk_ids),
        )
        cursor.execute(
            "UPDATE niche_corpus.niche_chunks "
            "SET active=false, retired_at=now(), updated_at=now() "
            "WHERE publication_id=%s AND active AND NOT (chunk_id=ANY(%s))",
            (publication, chunk_ids),
        )
        cursor.executemany(
            """
            INSERT INTO niche_corpus.niche_chunks
                (chunk_id, publication_id, family_id, chunk_kind, claim_number, language,
                 text, source_location, content_hash, source_uri, source_generation)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (chunk_id) DO UPDATE SET
                publication_id=EXCLUDED.publication_id,
                family_id=EXCLUDED.family_id, chunk_kind=EXCLUDED.chunk_kind,
                claim_number=EXCLUDED.claim_number, language=EXCLUDED.language,
                text=EXCLUDED.text, source_location=EXCLUDED.source_location,
                content_hash=EXCLUDED.content_hash,
                source_uri=EXCLUDED.source_uri, source_generation=EXCLUDED.source_generation,
                active=true, retired_at=NULL,
                updated_at=now()
            """,
            [(
                row["chunk_id"], row["publication_id"], row["family_id"], row["chunk_kind"],
                row.get("claim_number"), row.get("language") or None, row["text"],
                row["source_location"], row["content_hash"], source_uri,
                str(source_generation or ""),
            ) for row in rows],
        )
        cursor.executemany(
            """
            INSERT INTO niche_corpus.niche_embedding_cache
                (embedding_key, content_hash, model, dimension, task_type, text, token_estimate)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (embedding_key) DO NOTHING
            """,
            [(
                row["embedding_key"], row["content_hash"], row["model"], row["dimension"],
                row["task_type"], row["text"], row["token_estimate"],
            ) for row in cache],
        )
        cursor.executemany(
            """
            INSERT INTO niche_corpus.niche_embedding_stage
                (chunk_id, embedding_key, model, dimension, task_type, corpus_release)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (chunk_id, model, dimension, task_type, corpus_release) DO NOTHING
            """,
            [(
                row["chunk_id"], row["embedding_key"], row["model"], row["dimension"],
                row["task_type"], row["corpus_release"],
            ) for row in stage],
        )
        cursor.execute(
            SYNC_STAGE_STATUS_SQL,
            (
                chunk_ids,
                settings.model,
                settings.dimension,
                settings.task_type,
                settings.corpus_release,
            ),
        )
    return len(rows)


def _watermark(connection_factory, source: str) -> str:
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT cursor FROM niche_corpus.niche_input_watermarks WHERE source=%s",
            (source,),
        )
        row = cursor.fetchone()
        return str(row["cursor"] if row else "")


def _save_watermark(connection_factory, source: str, cursor_value: str) -> None:
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO niche_corpus.niche_input_watermarks(source,cursor) VALUES (%s,%s) "
            "ON CONFLICT (source) DO UPDATE SET cursor=EXCLUDED.cursor, updated_at=now()",
            (source, str(cursor_value)),
        )


def _publication_from_gcs_name(name: str, prefix: str) -> str:
    rest = name.removeprefix(prefix)
    return normalize_publication_number(rest.split("/", 1)[0])


def enqueue_gcs_backfill(
    connection_factory,
    queue: PostgresParseQueue,
    *,
    bucket_name: str,
    prefix: str = "parsed/",
    page_size: int = 1000,
    max_objects: int = 0,
    client=None,
) -> dict:
    """One resumable historical listing. Live fetches use the direct handoff instead."""
    if client is None:
        from google.cloud import storage
        client = storage.Client()
    prefix = str(prefix).lstrip("/")
    source = f"gcs:{bucket_name}/{prefix}"
    cursor_value = _watermark(connection_factory, source)
    pending = []
    seen = enqueued = 0
    listing = client.list_blobs(
        bucket_name,
        prefix=prefix,
        start_offset=cursor_value or None,
        page_size=max(1, int(page_size)),
    )
    for blob in listing:
        name = str(blob.name)
        if cursor_value and name <= cursor_value:
            continue
        cursor_value = name
        seen += 1
        if name.endswith(".json"):
            publication = _publication_from_gcs_name(name, prefix)
            if publication:
                pending.append((
                    publication,
                    "gcs",
                    f"gs://{bucket_name}/{name}",
                    f"gcs:{blob.generation or ''}",
                ))
        if len(pending) >= page_size:
            enqueued += queue.enqueue_many(pending)
            pending = []
            _save_watermark(connection_factory, source, cursor_value)
        if max_objects and seen >= int(max_objects):
            break
    if pending:
        enqueued += queue.enqueue_many(pending)
    if cursor_value:
        _save_watermark(connection_factory, source, cursor_value)
    return {"seen": seen, "enqueued": enqueued, "cursor": cursor_value}


def enqueue_local_backfill(
    connection_factory,
    queue: PostgresParseQueue,
    *,
    page_size: int = 1000,
    max_publications: int = 0,
) -> dict:
    """Backfill manifest rows created before synchronous local parse handoff was deployed."""
    source = "manifest:local-complete"
    cursor_value = _watermark(connection_factory, source)
    seen = enqueued = 0
    while True:
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT publication_id, publication_number
                  FROM niche_corpus.niche_publications
                 WHERE publication_id > %s
                   AND has_complete_claims AND has_complete_description
                 ORDER BY publication_id
                 LIMIT %s
                """,
                (cursor_value, max(1, int(page_size))),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        if not rows:
            break
        if max_publications:
            rows = rows[:max(0, int(max_publications) - seen)]
        enqueued += queue.enqueue_many([
            (
                row["publication_id"],
                "local",
                f"local://{row['publication_number']}",
                "",
            )
            for row in rows
        ])
        seen += len(rows)
        cursor_value = str(rows[-1]["publication_id"])
        _save_watermark(connection_factory, source, cursor_value)
        if max_publications and seen >= int(max_publications):
            break
        if len(rows) < page_size:
            break
    return {"seen": seen, "enqueued": enqueued, "cursor": cursor_value}


def _media_type(uri: str) -> str:
    path = str(uri).split("?", 1)[0].lower()
    path = path.removesuffix(".gz")
    if path.endswith(".json"):
        return "application/json"
    if path.endswith((".xml", ".xml.gz")):
        return "application/xml"
    if path.endswith((".html", ".htm")):
        return "text/html"
    if path.endswith(".pdf"):
        return "application/pdf"
    return "application/octet-stream"


def is_content_addressed(uri: str) -> bool:
    """Whether the object at this URI can never legitimately change.

    `raw/{publication}/{provider}/{sha256}.{ext}.gz` is written write-once under a key that
    contains its own digest, so a hash mismatch there means corruption and must fail.
    `parsed/{publication}/{provider}.json` is a MUTABLE key by design: a later, better fetch from
    the same provider overwrites it. A parse job enqueued against the older content then carries a
    hash that can never match again, which is why 4,775 publications were failing permanently on a
    handoff that was working exactly as intended."""
    return "/raw/" in str(uri).split("?", 1)[0]


def source_hash_state(content: bytes, source_generation: str) -> tuple[bool, str, str]:
    """(checked, expected, actual) for a sha256 handoff. `checked` is False when none was given."""
    generation = str(source_generation or "")
    if not generation.startswith("sha256:"):
        return False, "", ""
    expected = generation.split(":", 1)[1].lower()
    return bool(expected), expected, hashlib.sha256(bytes(content)).hexdigest()


def decode_source_bytes(
    content: bytes, uri: str, source_generation: str, *, strict: bool | None = None
) -> bytes:
    """Verify a content-addressed handoff before decoding its storage wrapper."""
    stored = bytes(content)
    checked, expected, actual = source_hash_state(stored, source_generation)
    if checked and actual != expected:
        if is_content_addressed(uri) if strict is None else strict:
            raise RuntimeError("GCS source content hash mismatch")
    if str(uri).split("?", 1)[0].lower().endswith(".gz"):
        try:
            return gzip.decompress(stored)
        except (OSError, EOFError) as exc:
            raise RuntimeError("GCS raw source is not valid gzip") from exc
    return stored


class StreamingParseWorker:
    def __init__(
        self,
        *,
        queue: PostgresParseQueue,
        repository,
        source_connection_factory,
        canonical_store,
        settings: EmbeddingSettings,
        worker_id: str,
        lease_seconds: int = 300,
        heartbeat_seconds: int = 30,
        gcs_client=None,
        logger=None,
    ):
        self.queue = queue
        self.repository = repository
        self.local = LocalCorpusProvider(source_connection_factory)
        self.canonical_store = canonical_store
        self.settings = settings
        self.worker_id = worker_id
        self.lease_seconds = max(30, int(lease_seconds))
        self.heartbeat_seconds = max(5, min(int(heartbeat_seconds), self.lease_seconds // 2))
        if gcs_client is None:
            from google.cloud import storage
            gcs_client = storage.Client()
        self.gcs_client = gcs_client
        self.logger = logger or (
            lambda event: print(json.dumps(event, sort_keys=True, default=str), flush=True)
        )

    def _read_gcs(self, job: ParseJob) -> bytes:
        from urllib.parse import urlsplit

        parsed = urlsplit(job.source_uri)
        if parsed.scheme != "gs" or not parsed.netloc:
            raise ValueError("parse job GCS URI is invalid")
        blob = self.gcs_client.bucket(parsed.netloc).blob(parsed.path.lstrip("/"))
        if str(job.source_generation).startswith("gcs:"):
            generation = str(job.source_generation).split(":", 1)[1]
            if generation.isdigit():
                content = blob.download_as_bytes(if_generation_match=int(generation))
                return decode_source_bytes(content, job.source_uri, job.source_generation)
        content = blob.download_as_bytes()
        checked, expected, actual = source_hash_state(content, job.source_generation)
        if checked and actual != expected and not is_content_addressed(job.source_uri):
            #  The object was rewritten by a later fetch. Read what is there now, and say so:
            #  a silent acceptance would leave no trace that the bytes are not the ones the job
            #  was created for.
            self.logger({"publication": job.publication_id, "provider": "gcs",
                         "result": "source_generation_drift", "uri": job.source_uri,
                         "expected_sha256": expected[:12], "actual_sha256": actual[:12]})
        return decode_source_bytes(content, job.source_uri, job.source_generation)

    def _record(self, publication: str, parsed: dict):
        record = self.repository.get_publication(publication)
        if record:
            return record
        self.repository.upsert_publications([PublicationRecord(
            publication_number=publication,
            family_id=str(parsed.get("family_id") or ""),
            authority=authority_of(publication),
            title=str(parsed.get("title") or ""),
            abstract=str(parsed.get("abstract") or ""),
            language=str(parsed.get("language") or ""),
            discovery_signals=("parse_handoff",),
        )])
        return self.repository.get_publication(publication)

    def _parse(self, job: ParseJob) -> tuple[dict, int]:
        publication = normalize_publication_number(job.publication_id)
        if job.source_kind == "local":
            record = self.repository.get_publication(publication)
            if record is None:
                raise LookupError("local parse manifest row is missing")
            result = self.local.fetch(FetchRequest(
                publication_id=record.publication_id,
                publication_number=record.publication_number,
                authority=record.authority,
                missing_fields=frozenset(),
                completeness=Completeness(),
                family_id=record.family_id,
                local_only=True,
            ))
            if result is None:
                raise LookupError("local source has no reusable patent text")
            content, media_type, provider = result.content, result.media_type, result.provider
        else:
            content = self._read_gcs(job)
            media_type, provider = _media_type(job.source_uri), "fulltext_gcs"
        parsed = parse_source(content, media_type, publication, provider)
        record = self._record(publication, parsed)
        if record is None:
            raise RuntimeError("manifest upsert did not create the publication")
        parsed["family_id"] = parsed.get("family_id") or record.family_id
        parsed["publication_id"] = record.publication_id
        return parsed, len(content)

    def run_job(self, job: ParseJob, *, cancel_event: threading.Event | None = None) -> bool:
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()

        def heartbeat():
            while not heartbeat_stop.wait(self.heartbeat_seconds):
                if cancel_event is not None and cancel_event.is_set():
                    return
                if not self.queue.heartbeat(job.job_id, self.worker_id, self.lease_seconds):
                    lease_lost.set()
                    return

        def check_cancelled() -> None:
            if lease_lost.is_set():
                raise ParseLeaseLost("parse lease was lost")
            if cancel_event is not None and cancel_event.is_set():
                raise ParseWorkerCancelled("parse worker is shutting down")

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        started = time.monotonic()
        publication = normalize_publication_number(job.publication_id)
        size = 0
        chunks = []
        try:
            check_cancelled()
            with self.repository.publication_lock(job.publication_id):
                parsed, size = self._parse(job)
                check_cancelled()
                record = self.repository.get_publication(parsed["publication_id"])
                parsed = self.repository.merge_parsed_source(
                    record.publication_id,
                    job.source_uri,
                    job.source_generation,
                    parsed,
                )
                check_cancelled()
                parsed["publication_id"] = record.publication_id
                parsed["family_id"] = parsed.get("family_id") or record.family_id
                parsed_object = self.canonical_store.put_parsed(
                    record.authority, record.publication_number, parsed
                )
                check_cancelled()
                chunks = build_chunks(parsed)
                chunks_object = self.canonical_store.put_chunks(
                    record.authority, record.publication_number, chunks, "parquet"
                )
                check_cancelled()
                self.repository.save_parsed(
                    record.publication_id, parsed, parsed_object, chunks_object
                )
                check_cancelled()
                stage_chunks(
                    self.repository.connection_factory,
                    chunks,
                    self.settings,
                    source_uri=job.source_uri,
                    source_generation=job.source_generation,
                    publication_id=record.publication_id,
                )
                check_cancelled()
            owned = self.queue.complete(job.job_id, self.worker_id)
            if not owned:
                raise ParseLeaseLost("parse lease was lost before completion")
            self.logger({
                "publication": record.publication_number,
                "provider": job.source_kind,
                "attempt": job.attempt,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "credits": 0,
                "bytes": size,
                "chunks": len(chunks),
                "result": "success",
            })
            return True
        except ParseLeaseLost:
            self.logger({
                "publication": publication,
                "provider": job.source_kind,
                "attempt": job.attempt,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "credits": 0,
                "bytes": size,
                "chunks": len(chunks),
                "result": "lease_lost",
            })
            return False
        except ParseWorkerCancelled:
            released = self.queue.cancel(job, self.worker_id)
            self.logger({
                "publication": publication,
                "provider": job.source_kind,
                "attempt": job.attempt,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "credits": 0,
                "bytes": size,
                "chunks": len(chunks),
                "result": "cancelled" if released else "lease_lost",
            })
            return False
        except Exception as exc:  # noqa: BLE001 - durable job boundary isolates one patent
            self.queue.fail(job, self.worker_id, f"{type(exc).__name__}: {exc}")
            self.logger({
                "publication": job.publication_id,
                "provider": job.source_kind,
                "attempt": job.attempt,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "credits": 0,
                "bytes": 0,
                "result": "error",
                "error_class": type(exc).__name__,
            })
            return False
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=self.heartbeat_seconds + 1)


def run_parse_pool(
    *,
    queue,
    repository,
    source_connection_factory,
    canonical_store,
    settings,
    workers: int = 4,
    lease_seconds: int = 300,
    heartbeat_seconds: int = 30,
    poll_seconds: float = 5.0,
    once: bool = False,
    max_jobs: int = 0,
) -> dict:
    stop = threading.Event()
    counter_lock = threading.Lock()
    claimed = 0

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    queue.reclaim_expired()

    def slot(index: int) -> int:
        nonlocal claimed
        worker = StreamingParseWorker(
            queue=queue,
            repository=repository,
            source_connection_factory=source_connection_factory,
            canonical_store=canonical_store,
            settings=settings,
            worker_id=f"{socket.gethostname()}:{os.getpid()}:{index}",
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )
        processed = 0
        while not stop.is_set():
            with counter_lock:
                if max_jobs and claimed >= int(max_jobs):
                    break
                job = queue.claim(worker.worker_id, lease_seconds)
                if job is not None:
                    claimed += 1
            if job is None:
                if once:
                    break
                stop.wait(max(0.25, float(poll_seconds)))
                continue
            worker.run_job(job, cancel_event=stop)
            processed += 1
        return processed

    count = min(32, max(1, int(workers)))
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="niche-parse") as pool:
        processed = sum(pool.map(slot, range(count)))
    return {"workers": count, "processed": processed}
