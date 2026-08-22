"""Lease-based fetch worker. Fetching and embedding remain separate stages."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import replace

from .chunks import build_chunks
from .models import Completeness, FetchRequest
from .parse import merge_parsed, parse_source
from .waterfall import PipelineFatalError


def _record_completeness(record) -> Completeness:
    return Completeness(**{
        key: bool(getattr(record, key, False))
        for key in Completeness.__dataclass_fields__
    })


def _parsed_completeness(parsed: dict) -> Completeness:
    values = parsed.get("completeness") or {}
    return Completeness(**{
        key: bool(values.get(key, False)) for key in Completeness.__dataclass_fields__
    })


def parse_cached_record(repository, object_store, record, chunks_format="parquet"):
    """Parse every cached raw source for one publication, merging complementary fields."""
    accumulated = None
    for source in repository.cached_sources(record.publication_id):
        content = object_store.read(source["raw_object_uri"])
        parsed = parse_source(
            content, source["media_type"], record.publication_number, source["provider"]
        )
        parsed["family_id"] = parsed.get("family_id") or record.family_id
        accumulated = merge_parsed(accumulated, parsed)
    if not accumulated:
        return None
    parsed_object = object_store.put_parsed(record.authority, record.publication_number, accumulated)
    chunks_object = object_store.put_chunks(
        record.authority, record.publication_number, build_chunks(accumulated), chunks_format
    )
    repository.save_parsed(record.publication_id, accumulated, parsed_object, chunks_object)
    return accumulated


class FetchWorker:
    def __init__(
        self,
        *,
        queue,
        repository,
        waterfall,
        object_store,
        worker_id: str,
        lease_seconds: int = 300,
        heartbeat_seconds: int = 30,
        chunks_format: str = "parquet",
        logger: Callable[[dict], None] | None = None,
    ):
        self.queue = queue
        self.repository = repository
        self.waterfall = waterfall
        self.object_store = object_store
        self.worker_id = worker_id
        self.lease_seconds = max(5, int(lease_seconds))
        self.heartbeat_seconds = max(1, min(int(heartbeat_seconds), self.lease_seconds // 2))
        self.chunks_format = chunks_format
        self.logger = logger or (lambda event: print(json.dumps(event, sort_keys=True), flush=True))
        self._parsed_accumulator = None
        self._cached_providers: set[str] = set()

    def _failure_status(self, job) -> str:
        return (
            "failed"
            if int(job.attempt) >= int(getattr(self.queue, "max_attempts", job.attempt + 1))
            else "pending"
        )

    def _persist_parsed(self, record, parsed: dict):
        parsed_object = self.object_store.put_parsed(
            record.authority, record.publication_number, parsed
        )
        chunks_object = self.object_store.put_chunks(
            record.authority,
            record.publication_number,
            build_chunks(parsed),
            self.chunks_format,
        )
        self.repository.save_parsed(
            record.publication_id, parsed, parsed_object, chunks_object
        )
        return parsed

    def _parse_and_persist(self, record, content, media_type, provider):
        parsed = parse_source(content, media_type, record.publication_number, provider)
        parsed["family_id"] = parsed.get("family_id") or record.family_id
        self._parsed_accumulator = merge_parsed(self._parsed_accumulator, parsed)
        try:
            return self._persist_parsed(record, self._parsed_accumulator)
        except Exception as exc:
            raise PipelineFatalError(
                f"cached parse persistence failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _try_cached(self, record) -> dict | None:
        sources = self.repository.cached_sources(record.publication_id)
        readable_sources = 0
        for source in sources:
            try:
                content = self.object_store.read(source["raw_object_uri"])
                readable_sources += 1
            except Exception as exc:  # noqa: BLE001 - audit all cache entries before stopping
                self.logger({
                    "publication": record.publication_number,
                    "provider": source.get("provider") or "cached",
                    "attempt": 0,
                    "latency_ms": 0,
                    "credits": 0,
                    "bytes": 0,
                    "result": "cached_read_error",
                    "error_class": type(exc).__name__,
                })
                continue
            try:
                parsed = self._parse_and_persist(
                    record, content, source["media_type"], source["provider"]
                )
            except PipelineFatalError:
                raise
            except Exception as exc:  # noqa: BLE001 - continue through independently cached sources
                self.logger({
                    "publication": record.publication_number,
                    "provider": source.get("provider") or "cached",
                    "attempt": 0,
                    "latency_ms": 0,
                    "credits": 0,
                    "bytes": 0,
                    "result": "cached_parse_error",
                    "error_class": type(exc).__name__,
                })
                continue
            self._cached_providers.add(str(source["provider"]))
            if _parsed_completeness(parsed).full_text_complete:
                return parsed
        if sources and not readable_sources:
            raise PipelineFatalError("all registered raw source objects are unreadable")
        return None

    def _heartbeat_loop(self, job, stopped: threading.Event):
        while not stopped.wait(self.heartbeat_seconds):
            if not self.queue.heartbeat(
                job.job_id, self.worker_id, self.lease_seconds
            ):
                return

    def _process(self, job) -> bool:
        record = self.repository.get_publication(job.publication_id)
        if record is None:
            self.queue.fail(job.job_id, self.worker_id, job.attempt, "manifest row missing")
            return True
        self.repository.mark_fetch_status(record.publication_id, "leased")
        self._parsed_accumulator = None
        self._cached_providers = set()

        cached = self._try_cached(record)
        if cached and _parsed_completeness(cached).full_text_complete:
            lease_owned = self.queue.complete(job.job_id, self.worker_id)
            if lease_owned:
                self.repository.mark_fetch_status(record.publication_id, "completed")
            self.logger({
                "publication": record.publication_number,
                "provider": "cached_raw",
                "attempt": job.attempt,
                "latency_ms": 0,
                "credits": 0,
                "bytes": 0,
                "result": "success" if lease_owned else "lease_lost",
            })
            return True

        current = _record_completeness(record)
        if self._parsed_accumulator:
            current = _parsed_completeness(self._parsed_accumulator)
        request = FetchRequest(
            publication_id=record.publication_id,
            publication_number=record.publication_number,
            authority=record.authority,
            kind_code=record.kind_code,
            language=record.language,
            family_id=record.family_id,
            raw_object_uri=record.raw_object_uri,
            skip_providers=frozenset(self._cached_providers),
            require_artifact=not bool(record.parsed_object_uri),
            local_only=(
                _record_completeness(record).full_text_complete
                and not bool(record.parsed_object_uri)
            ),
            missing_fields=current.missing_text_fields(),
            completeness=current,
        )

        def store_result(_request, result):
            try:
                stored = self.object_store.put_raw(
                    authority=record.authority,
                    publication_number=record.publication_number,
                    provider=result.provider,
                    content=result.content,
                    media_type=result.media_type,
                    http_status=result.http_status,
                    headers=result.response_headers,
                    source_url=result.source_url,
                )
                self.repository.record_source(record.publication_id, result, stored)
            except Exception as exc:
                raise PipelineFatalError(
                    f"raw source persistence failed: {type(exc).__name__}: {exc}"
                ) from exc

        def validate(_request, result):
            parsed = parse_source(
                result.content, result.media_type, record.publication_number, result.provider
            )
            parsed["family_id"] = parsed.get("family_id") or record.family_id
            self._parsed_accumulator = merge_parsed(self._parsed_accumulator, parsed)
            try:
                persisted = self._persist_parsed(record, self._parsed_accumulator)
            except Exception as exc:
                raise PipelineFatalError(
                    f"parsed source persistence failed: {type(exc).__name__}: {exc}"
                ) from exc
            return replace(result, completeness=_parsed_completeness(persisted))

        self.waterfall.on_result = store_result
        self.waterfall.validator = validate
        outcome = self.waterfall.fetch(request)
        for attempt in outcome.attempts:
            self.repository.record_attempt(record.publication_id, attempt)
            self.logger({
                "publication": record.publication_number,
                "provider": attempt.provider,
                "attempt": job.attempt,
                "latency_ms": attempt.latency_ms,
                "credits": attempt.credits_used,
                "bytes": attempt.bytes_received,
                "result": attempt.status,
                **({"error_class": attempt.error_class} if attempt.error_class else {}),
            })
        if outcome.status in {"completed", "already_complete"}:
            if self.queue.complete(job.job_id, self.worker_id):
                self.repository.mark_fetch_status(
                    record.publication_id,
                    "completed",
                    last_provider=outcome.result.provider if outcome.result else None,
                )
            return True
        error = outcome.attempts[-1].error if outcome.attempts else "all providers unavailable"
        if self.queue.fail(job.job_id, self.worker_id, job.attempt, error):
            self.repository.mark_fetch_status(
                record.publication_id, self._failure_status(job), last_error=error
            )
        return True

    def run_once(self) -> bool:
        job = self.queue.claim(self.worker_id, self.lease_seconds)
        if job is None:
            return False
        stopped = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, stopped),
            name=f"niche-heartbeat-{self.worker_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            return self._process(job)
        except Exception as exc:  # noqa: BLE001 - worker boundary must return the lease to retry
            error = f"{type(exc).__name__}: {exc}"
            if self.queue.fail(
                job.job_id, self.worker_id, job.attempt, error
            ):
                self.repository.mark_fetch_status(
                    job.publication_id,
                    self._failure_status(job),
                    last_error=error,
                )
            self.logger({
                "publication": job.publication_id,
                "provider": "worker",
                "attempt": job.attempt,
                "latency_ms": 0,
                "credits": 0,
                "bytes": 0,
                "result": "error",
                "error_class": type(exc).__name__,
            })
            return True
        finally:
            stopped.set()
            heartbeat.join(timeout=max(1, self.heartbeat_seconds + 1))

    def run_until_empty(self, max_jobs: int = 0) -> int:
        completed = 0
        while not max_jobs or completed < max_jobs:
            if not self.run_once():
                break
            completed += 1
        return completed


def main(argv=None) -> int:
    from .cli import run_fetch
    return run_fetch(argv)


if __name__ == "__main__":
    raise SystemExit(main())
