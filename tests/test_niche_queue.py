from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from corpus.niche.limits import RunJobBudget
from corpus.niche.queue import (
    InMemoryFetchQueue,
    PostgresFetchQueue,
    retry_delay_seconds,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)


def test_retry_backoff_is_exponential_bounded_and_jittered():
    assert retry_delay_seconds(1, base=10, cap=100, random_value=0.5) == 10
    assert retry_delay_seconds(3, base=10, cap=100, random_value=0.5) == 40
    assert retry_delay_seconds(9, base=10, cap=100, random_value=0.5) == 100
    assert retry_delay_seconds(2, base=10, cap=100, random_value=1.0) > 20


def test_run_job_budget_is_shared_and_releases_empty_claims():
    budget = RunJobBudget(2)

    assert budget.acquire() is True
    assert budget.acquire() is True
    assert budget.acquire() is False
    budget.release_unprocessed()
    assert budget.acquire() is True
    assert budget.processed == 2


def test_lease_expiration_allows_another_worker_to_reclaim():
    queue = InMemoryFetchQueue(max_attempts=4)
    queue.enqueue("US1", now=T0)

    first = queue.claim("worker-a", lease_seconds=30, now=T0)
    blocked = queue.claim("worker-b", lease_seconds=30, now=T0 + timedelta(seconds=20))
    reclaimed = queue.claim("worker-b", lease_seconds=30, now=T0 + timedelta(seconds=31))

    assert first.worker_id == "worker-a"
    assert blocked is None
    assert reclaimed.worker_id == "worker-b"
    assert reclaimed.attempt == 2


def test_two_worker_race_has_one_winner():
    queue = InMemoryFetchQueue(max_attempts=4)
    queue.enqueue("US1", now=T0)
    barrier = threading.Barrier(3)
    results = []

    def claim(worker):
        barrier.wait()
        results.append(queue.claim(worker, lease_seconds=30, now=T0))

    threads = [threading.Thread(target=claim, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sum(result is not None for result in results) == 1


def test_sigkill_reclaim_does_not_lose_the_publication():
    queue = InMemoryFetchQueue(max_attempts=4)
    queue.enqueue("US1", now=T0)
    killed_job = queue.claim("dead-vm", lease_seconds=15, now=T0)

    recovered = queue.claim("replacement-vm", lease_seconds=15, now=T0 + timedelta(seconds=16))

    assert recovered.job_id == killed_job.job_id
    assert recovered.publication_id == "US1"
    assert recovered.attempt == 2


def test_graceful_cancel_returns_owned_fetch_lease_without_consuming_attempt():
    queue = InMemoryFetchQueue(max_attempts=3)
    queued = queue.enqueue("US123A1", priority=1, now=T0)
    leased = queue.claim("worker-a", 60, now=T0)

    assert leased is not None
    assert leased.job_id == queued.job_id
    assert leased.attempt == 1
    assert queue.cancel(leased, "worker-a", "worker shutdown", now=T0) is True

    reclaimed = queue.claim("worker-b", 60, now=T0)
    assert reclaimed is not None
    assert reclaimed.job_id == queued.job_id
    assert reclaimed.attempt == 1


def test_heartbeat_extends_only_the_current_workers_lease():
    queue = InMemoryFetchQueue(max_attempts=4)
    queue.enqueue("US1", now=T0)
    job = queue.claim("worker-a", lease_seconds=10, now=T0)

    assert queue.heartbeat(job.job_id, "worker-b", 30, now=T0) is False
    assert queue.heartbeat(job.job_id, "worker-a", 30, now=T0) is True
    assert queue.claim("worker-b", lease_seconds=10, now=T0 + timedelta(seconds=11)) is None


def test_expired_worker_cannot_heartbeat_complete_or_fail_lease():
    queue = InMemoryFetchQueue(max_attempts=4)
    queue.enqueue("US1", now=T0)
    job = queue.claim("worker-a", lease_seconds=10, now=T0)
    expired = T0 + timedelta(seconds=11)

    assert queue.heartbeat(job.job_id, "worker-a", 30, now=expired) is False
    assert queue.complete(job.job_id, "worker-a", now=expired) is False
    assert queue.fail(job.job_id, "worker-a", "late result", now=expired) is False
    assert queue.get(job.job_id).status == "leased"


def test_bounded_attempts_end_in_failed_state():
    queue = InMemoryFetchQueue(max_attempts=2, backoff_base=1)
    queue.enqueue("US1", now=T0)
    first = queue.claim("worker", lease_seconds=10, now=T0)
    queue.fail(first.job_id, "worker", "temporary", now=T0)
    second = queue.claim("worker", lease_seconds=10, now=T0 + timedelta(seconds=2))
    queue.fail(second.job_id, "worker", "still broken", now=T0 + timedelta(seconds=2))

    assert queue.get(first.job_id).status == "failed"
    assert queue.claim("worker", lease_seconds=10, now=T0 + timedelta(days=1)) is None


def test_idempotent_rerun_enqueues_one_job_per_publication():
    queue = InMemoryFetchQueue(max_attempts=4)

    first = queue.enqueue("US1", priority=1, now=T0)
    second = queue.enqueue("US1", priority=4, now=T0 + timedelta(seconds=1))

    assert first.job_id == second.job_id
    assert len(queue.jobs()) == 1
    assert queue.get(first.job_id).priority == 1


def test_postgres_claim_uses_skip_locked_and_reclaims_expired_leases():
    sql = " ".join(PostgresFetchQueue.CLAIM_SQL.upper().split())

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "LEASE_UNTIL" in sql
    assert "STATUS = 'LEASED'" in sql
    assert "MAXIMUM FETCH ATTEMPTS REACHED" in sql
    assert "UPDATE NICHE_CORPUS.NICHE_PUBLICATIONS" in sql
