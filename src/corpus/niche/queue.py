"""Durable fetch jobs with PostgreSQL leases and deterministic retry policy."""
from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def retry_delay_seconds(
    attempt: int,
    *,
    base: float = 30,
    cap: float = 3600,
    jitter_fraction: float = 0.2,
    random_value: float | None = None,
) -> float:
    """Exponential backoff with bounded symmetric jitter."""
    exponent = max(0, int(attempt) - 1)
    unjittered = min(float(cap), float(base) * (2 ** exponent))
    if unjittered >= float(cap):
        return float(cap)
    value = random.random() if random_value is None else min(1.0, max(0.0, random_value))
    factor = 1.0 + float(jitter_fraction) * ((2.0 * value) - 1.0)
    return min(float(cap), max(0.0, unjittered * factor))


@dataclass
class FetchJob:
    job_id: int
    publication_id: str
    priority: int
    status: str
    worker_id: str = ""
    lease_until: datetime | None = None
    attempt: int = 0
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    next_attempt_at: datetime | None = None
    heartbeat_at: datetime | None = None
    last_error: str = ""


class InMemoryFetchQueue:
    """Thread-safe semantic model of the PostgreSQL queue for deterministic tests."""

    def __init__(self, *, max_attempts: int, backoff_base: float = 30, backoff_cap: float = 3600):
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.max_attempts = int(max_attempts)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self._lock = threading.Lock()
        self._by_id: dict[int, FetchJob] = {}
        self._by_publication: dict[str, int] = {}
        self._next_id = 1

    def enqueue(self, publication_id: str, priority: int = 4, *, now=None) -> FetchJob:
        now = now or utcnow()
        with self._lock:
            old_id = self._by_publication.get(publication_id)
            if old_id is not None:
                job = self._by_id[old_id]
                job.priority = min(job.priority, int(priority))
                return replace(job)
            job = FetchJob(
                self._next_id,
                publication_id,
                int(priority),
                "pending",
                created_at=now,
                next_attempt_at=now,
            )
            self._next_id += 1
            self._by_id[job.job_id] = job
            self._by_publication[publication_id] = job.job_id
            return replace(job)

    def claim(self, worker_id: str, lease_seconds: int, *, now=None) -> FetchJob | None:
        now = now or utcnow()
        with self._lock:
            eligible = []
            for job in self._by_id.values():
                pending = job.status == "pending" and (job.next_attempt_at is None or job.next_attempt_at <= now)
                expired = (
                    job.status == "leased"
                    and job.lease_until is not None
                    and job.lease_until <= now
                )
                if (pending or expired) and job.attempt < self.max_attempts:
                    eligible.append(job)
                elif (pending or expired) and job.attempt >= self.max_attempts:
                    job.status = "failed"
                    job.completed_at = now
            if not eligible:
                return None
            job = min(eligible, key=lambda item: (item.priority, item.created_at, item.job_id))
            job.status = "leased"
            job.worker_id = worker_id
            job.lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
            job.heartbeat_at = now
            job.started_at = now
            job.attempt += 1
            return replace(job)

    def heartbeat(self, job_id: int, worker_id: str, lease_seconds: int, *, now=None) -> bool:
        now = now or utcnow()
        with self._lock:
            job = self._by_id.get(job_id)
            if (
                not job
                or job.status != "leased"
                or job.worker_id != worker_id
                or job.lease_until is None
                or job.lease_until <= now
            ):
                return False
            job.heartbeat_at = now
            job.lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
            return True

    def complete(self, job_id: int, worker_id: str, *, now=None) -> bool:
        now = now or utcnow()
        with self._lock:
            job = self._by_id.get(job_id)
            if (
                not job
                or job.status != "leased"
                or job.worker_id != worker_id
                or job.lease_until is None
                or job.lease_until <= now
            ):
                return False
            job.status = "completed"
            job.completed_at = now
            job.lease_until = None
            return True

    def cancel(
        self,
        job: FetchJob,
        worker_id: str,
        reason: str = "worker shutdown",
        *,
        now=None,
    ) -> bool:
        """Return an owned lease immediately without consuming a retry attempt."""
        now = now or utcnow()
        with self._lock:
            current = self._by_id.get(int(job.job_id))
            if (
                not current
                or current.status != "leased"
                or current.worker_id != worker_id
                or current.lease_until is None
                or current.lease_until <= now
            ):
                return False
            current.status = "pending"
            current.worker_id = ""
            current.lease_until = None
            current.heartbeat_at = now
            current.next_attempt_at = now
            current.attempt = max(0, int(current.attempt) - 1)
            current.last_error = str(reason)[:2000]
            return True

    def fail(self, job_id: int, worker_id: str, error: str, *, now=None) -> bool:
        now = now or utcnow()
        with self._lock:
            job = self._by_id.get(job_id)
            if (
                not job
                or job.status != "leased"
                or job.worker_id != worker_id
                or job.lease_until is None
                or job.lease_until <= now
            ):
                return False
            job.last_error = str(error)[:2000]
            job.lease_until = None
            if job.attempt >= self.max_attempts:
                job.status = "failed"
                job.completed_at = now
            else:
                delay = retry_delay_seconds(
                    job.attempt, base=self.backoff_base, cap=self.backoff_cap
                )
                job.status = "pending"
                job.worker_id = ""
                job.next_attempt_at = now + timedelta(seconds=delay)
            return True

    def get(self, job_id: int) -> FetchJob | None:
        with self._lock:
            job = self._by_id.get(job_id)
            return replace(job) if job else None

    def jobs(self) -> list[FetchJob]:
        with self._lock:
            return [replace(job) for job in sorted(self._by_id.values(), key=lambda value: value.job_id)]


def _row_job(row) -> FetchJob | None:
    if not row:
        return None
    if not isinstance(row, dict):
        raise TypeError("queue connections must use dictionary rows")
    return FetchJob(**{key: row.get(key) for key in FetchJob.__dataclass_fields__})


class PostgresFetchQueue:
    """Atomic multi-VM queue. Every claim is one PostgreSQL transaction."""

    CLAIM_SQL = """
    WITH exhausted AS (
        UPDATE niche_corpus.corpus_fetch_jobs
           SET status = 'failed',
               worker_id = NULL,
               lease_until = NULL,
               completed_at = now(),
               last_error = COALESCE(last_error, 'maximum fetch attempts reached')
         WHERE attempt >= %s
           AND ((status = 'pending' AND next_attempt_at <= now())
                OR (status = 'leased' AND lease_until <= now()))
        RETURNING publication_id, last_error
    ), synchronized AS (
        UPDATE niche_corpus.niche_publications AS publications
           SET fetch_status = CASE WHEN publications.fetch_status = 'completed'
                                   THEN 'completed' ELSE 'failed' END,
               last_error = CASE WHEN publications.fetch_status = 'completed'
                                 THEN publications.last_error ELSE exhausted.last_error END,
               updated_at = now()
          FROM exhausted
         WHERE publications.publication_id = exhausted.publication_id
        RETURNING publications.publication_id
    ), candidate AS (
        SELECT job_id
        FROM niche_corpus.corpus_fetch_jobs
        WHERE attempt < %s
          AND (
              (status = 'pending' AND next_attempt_at <= now())
              OR (status = 'leased' AND lease_until <= now())
          )
        ORDER BY priority ASC, created_at ASC, job_id ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE niche_corpus.corpus_fetch_jobs AS jobs
       SET status = 'leased',
           worker_id = %s,
           lease_until = now() + (%s * interval '1 second'),
           attempt = jobs.attempt + 1,
           started_at = now(),
           heartbeat_at = now(),
           last_error = NULL
      FROM candidate
     WHERE jobs.job_id = candidate.job_id
    RETURNING jobs.*
    """

    def __init__(
        self,
        connection_factory: Callable,
        *,
        max_attempts: int,
        backoff_base: float = 30,
        backoff_cap: float = 3600,
    ):
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.connection_factory = connection_factory
        self.max_attempts = int(max_attempts)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)

    def enqueue(self, publication_id: str, priority: int = 4) -> FetchJob:
        sql = """
        INSERT INTO niche_corpus.corpus_fetch_jobs
            (publication_id, priority, status, next_attempt_at)
        VALUES (%s, %s, 'pending', now())
        ON CONFLICT (publication_id) DO UPDATE
           SET priority = LEAST(niche_corpus.corpus_fetch_jobs.priority, EXCLUDED.priority)
        RETURNING *
        """
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(sql, (publication_id, int(priority)))
            return _row_job(cursor.fetchone())

    def claim(self, worker_id: str, lease_seconds: int) -> FetchJob | None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                self.CLAIM_SQL,
                (
                    self.max_attempts,
                    self.max_attempts,
                    worker_id,
                    max(1, int(lease_seconds)),
                ),
            )
            return _row_job(cursor.fetchone())

    def heartbeat(self, job_id: int, worker_id: str, lease_seconds: int) -> bool:
        sql = """
        UPDATE niche_corpus.corpus_fetch_jobs
           SET heartbeat_at = now(), lease_until = now() + (%s * interval '1 second')
         WHERE job_id = %s AND status = 'leased' AND worker_id = %s
           AND lease_until > now()
        """
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(sql, (max(1, int(lease_seconds)), job_id, worker_id))
            return cursor.rowcount == 1

    def complete(self, job_id: int, worker_id: str) -> bool:
        sql = """
        UPDATE niche_corpus.corpus_fetch_jobs
           SET status = 'completed', completed_at = now(), lease_until = NULL,
               heartbeat_at = now()
         WHERE job_id = %s AND status = 'leased' AND worker_id = %s
           AND lease_until > now()
        """
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(sql, (job_id, worker_id))
            return cursor.rowcount == 1

    def cancel(
        self,
        job: FetchJob,
        worker_id: str,
        reason: str = "worker shutdown",
    ) -> bool:
        """Return an owned job immediately and synchronize its manifest status."""
        sql = """
        WITH cancelled AS (
            UPDATE niche_corpus.corpus_fetch_jobs
               SET status='pending', worker_id=NULL, lease_until=NULL,
                   heartbeat_at=now(), next_attempt_at=now(),
                   attempt=GREATEST(0, attempt - 1), last_error=%s
             WHERE job_id=%s AND status='leased' AND worker_id=%s
               AND lease_until > now()
            RETURNING publication_id
        ), synchronized AS (
            UPDATE niche_corpus.niche_publications AS publications
               SET fetch_status=CASE WHEN publications.fetch_status='completed'
                                     THEN 'completed' ELSE 'pending' END,
                   last_error=CASE WHEN publications.fetch_status='completed'
                                   THEN publications.last_error ELSE %s END,
                   updated_at=now()
              FROM cancelled
             WHERE publications.publication_id=cancelled.publication_id
            RETURNING publications.publication_id
        )
        SELECT EXISTS(SELECT 1 FROM cancelled) AS cancelled
        """
        message = str(reason)[:2000]
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(sql, (message, int(job.job_id), worker_id, message))
            row = cursor.fetchone() or {}
            return bool(row.get("cancelled"))

    def fail(self, job_id: int, worker_id: str, attempt: int, error: str) -> bool:
        terminal = int(attempt) >= self.max_attempts
        delay = retry_delay_seconds(attempt, base=self.backoff_base, cap=self.backoff_cap)
        sql = """
        UPDATE niche_corpus.corpus_fetch_jobs
           SET status = %s,
               worker_id = CASE WHEN %s THEN worker_id ELSE NULL END,
               lease_until = NULL,
               next_attempt_at = CASE WHEN %s THEN next_attempt_at
                                      ELSE now() + (%s * interval '1 second') END,
               completed_at = CASE WHEN %s THEN now() ELSE NULL END,
               last_error = %s
         WHERE job_id = %s AND status = 'leased' AND worker_id = %s
           AND lease_until > now()
        """
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    "failed" if terminal else "pending",
                    terminal,
                    terminal,
                    delay,
                    terminal,
                    str(error)[:2000],
                    job_id,
                    worker_id,
                ),
            )
            return cursor.rowcount == 1

    def reclaim_expired(self) -> int:
        sql = """
        WITH reclaimed AS (
            UPDATE niche_corpus.corpus_fetch_jobs
               SET status = CASE WHEN attempt >= %s THEN 'failed' ELSE 'pending' END,
                   worker_id = NULL,
                   lease_until = NULL,
                   next_attempt_at = now(),
                   completed_at = CASE WHEN attempt >= %s THEN now() ELSE NULL END,
                   last_error = COALESCE(last_error, 'lease expired')
             WHERE status = 'leased' AND lease_until <= now()
            RETURNING publication_id, status, last_error
        ), synchronized AS (
            UPDATE niche_corpus.niche_publications AS publications
               SET fetch_status = CASE WHEN publications.fetch_status = 'completed'
                                       THEN 'completed' ELSE reclaimed.status END,
                   last_error = CASE WHEN publications.fetch_status = 'completed'
                                     THEN publications.last_error ELSE reclaimed.last_error END,
                   updated_at = now()
              FROM reclaimed
             WHERE publications.publication_id = reclaimed.publication_id
            RETURNING publications.publication_id
        )
        SELECT count(*) AS reclaimed_count FROM reclaimed
        """
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(sql, (self.max_attempts, self.max_attempts))
            row = cursor.fetchone() or {}
            return int(row.get("reclaimed_count") or 0)
