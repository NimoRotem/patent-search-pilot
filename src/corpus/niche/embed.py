"""Restart-safe Gemini Batch controller for the isolated niche corpus."""
from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from datetime import datetime, timezone

from .batch import (
    AmbiguousSubmission,
    BatchRepository,
    BudgetExhausted,
    GCSBatchStore,
    VertexBatchClient,
)
from .database import connection_factory, require_dsn, validate_staging_database
from .embedding import EmbeddingSettings


def _utc(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


class BatchController:
    """Coordinates durable database state, write-once GCS input, and Vertex jobs."""

    def __init__(
        self,
        repository,
        store,
        client,
        *,
        ambiguity_grace_seconds: int = 600,
        max_active_batches: int = 4,
        logger=None,
    ):
        self.repository = repository
        self.store = store
        self.client = client
        self.ambiguity_grace_seconds = max(60, int(ambiguity_grace_seconds))
        self.max_active_batches = max(1, int(max_active_batches))
        self.logger = logger or (
            lambda event: print(json.dumps(event, sort_keys=True, default=str), flush=True)
        )

    def _event(self, result: str, batch: dict, **values) -> None:
        self.logger({
            "pipeline": "niche_embedding_batch",
            "batch_id": batch.get("batch_id"),
            "submission_key": batch.get("submission_key"),
            "result": result,
            **values,
        })

    def reconcile_submitting(self, *, now: datetime | None = None) -> dict:
        """Resolve uncertain POST outcomes without ever blindly posting again."""
        now = now or datetime.now(timezone.utc)
        counts = {"adopted": 0, "ambiguous": 0, "waiting": 0}
        for summary in self.repository.batches(("submitting", "ambiguous")):
            batch = self.repository.load(summary["batch_id"])
            if not batch:
                continue
            try:
                self.store.ensure_input(batch, batch["input_bytes"])
                matches = self.client.find_matches(batch)
                if len(matches) == 1:
                    self.repository.adopt(batch["batch_id"], matches[0])
                    counts["adopted"] += 1
                    self._event("adopted", batch, provider_job=matches[0].get("name"))
                    continue
                if len(matches) > 1:
                    raise AmbiguousSubmission(
                        "multiple Vertex jobs match one submission key"
                    )
                age = (now - _utc(summary.get("updated_at") or now)).total_seconds()
                if (
                    summary.get("status") != "ambiguous"
                    and age >= self.ambiguity_grace_seconds
                ):
                    message = "no matching Vertex job after the submission ambiguity window"
                    self.repository.ambiguous(batch["batch_id"], message)
                    counts["ambiguous"] += 1
                    self._event("ambiguous", batch, error=message)
                else:
                    counts["waiting"] += 1
            except AmbiguousSubmission as exc:
                self.repository.ambiguous(batch["batch_id"], str(exc))
                counts["ambiguous"] += 1
                self._event("ambiguous", batch, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - one batch must not stop the controller
                counts["waiting"] += 1
                self._event("reconcile_error", batch, error_class=type(exc).__name__)
        return counts

    def poll_submitted(self) -> dict:
        counts = {"running": 0, "succeeded": 0, "failed": 0, "errors": 0}
        for batch in self.repository.batches(("submitted",)):
            try:
                job = self.client.poll(batch["provider_job_name"], batch)
                state = str(job.get("state") or "JOB_STATE_UNSPECIFIED")
                self.repository.update_provider_state(batch["batch_id"], state)
                if state not in self.client.TERMINAL:
                    counts["running"] += 1
                    continue
                if state in self.client.SUCCESS:
                    loaded = self.repository.load(batch["batch_id"])
                    expected = [item["embedding_key"] for item in loaded["items"]]
                    output_directory = str(
                        (job.get("outputInfo") or {}).get("gcsOutputDirectory")
                        or loaded["output_prefix"]
                    )
                    vectors, errors = self.store.collect(output_directory, expected)
                    result = self.repository.settle_success(
                        batch["batch_id"], vectors, errors
                    )
                    counts["succeeded"] += 1
                    self._event("succeeded", loaded, **result)
                else:
                    detail = str((job.get("error") or {}).get("message") or state)
                    self.repository.settle_failure(batch["batch_id"], state, detail)
                    counts["failed"] += 1
                    self._event("failed", batch, provider_state=state)
            except Exception as exc:  # noqa: BLE001 - provider errors are isolated by batch
                counts["errors"] += 1
                self._event("poll_error", batch, error_class=type(exc).__name__)
        return counts

    def repair_completed_outputs(self) -> dict:
        """Re-read durable provider output for batches whose prior collection was incomplete."""
        counts = {"repaired": 0, "errors": 0}
        for summary in self.repository.batches(("succeeded",)):
            if "item failures" not in str(summary.get("last_error") or ""):
                continue
            try:
                batch = self.repository.load(summary["batch_id"])
                expected = [item["embedding_key"] for item in batch["items"]]
                job = self.client.poll(batch.get("provider_job_name"), batch)
                output_directory = str(
                    (job.get("outputInfo") or {}).get("gcsOutputDirectory")
                    or batch["output_prefix"]
                )
                vectors, errors = self.store.collect(output_directory, expected)
                result = self.repository.settle_success(
                    batch["batch_id"], vectors, errors
                )
                counts["repaired"] += 1
                self._event("repaired", batch, **result)
            except Exception as exc:  # noqa: BLE001 - later cycles retry the durable output
                counts["errors"] += 1
                self._event("repair_error", summary, error_class=type(exc).__name__)
        return counts

    def _submit_one_prepared(self) -> bool:
        prepared = self.repository.batches(("prepared",))
        if not prepared:
            return False
        candidate = self.repository.load(prepared[0]["batch_id"])
        if not candidate:
            return False
        self.store.ensure_input(candidate, candidate["input_bytes"])
        batch = self.repository.begin_submission(candidate["batch_id"])
        if not batch:
            return True
        try:
            matches = self.client.find_matches(batch)
        except Exception as exc:  # noqa: BLE001 - no provider POST was attempted
            self.repository.retry_prepared(batch["batch_id"], str(exc))
            self._event("pre_submit_retry", batch, error_class=type(exc).__name__)
            return False
        try:
            if len(matches) > 1:
                raise AmbiguousSubmission(
                    "multiple Vertex jobs match one submission key"
                )
            job = matches[0] if matches else self.client.create(batch)
            self.repository.adopt(batch["batch_id"], job)
            self._event("submitted", batch, provider_job=job.get("name"))
        except AmbiguousSubmission as exc:
            self.repository.ambiguous(batch["batch_id"], str(exc))
            self._event("ambiguous", batch, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - state remains reconcilable after uncertain POST
            self._event("submit_uncertain", batch, error_class=type(exc).__name__)
        return True

    def run_once(self, *, force_small_batch: bool = False) -> dict:
        result = {
            "repair": self.repair_completed_outputs(),
            "reconcile": self.reconcile_submitting(),
            "poll": self.poll_submitted(),
            "prepared": 0,
            "submission_cycles": 0,
        }
        while True:
            active = len(
                self.repository.batches(("submitting", "submitted", "ambiguous"))
            )
            if active >= self.max_active_batches:
                break
            if self._submit_one_prepared():
                result["submission_cycles"] += 1
                continue
            try:
                batch = self.repository.prepare(force=force_small_batch)
            except BudgetExhausted:
                result["budget_exhausted"] = True
                break
            if not batch:
                break
            result["prepared"] += 1
        return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.corpus.niche.embed",
        description="Submit and collect restart-safe Gemini Batch embeddings.",
    )
    parser.add_argument("command", nargs="?", choices=("run", "status"), default="run")
    parser.add_argument(
        "--niche-dsn",
        default=os.environ.get("NICHE_DATABASE_URL", ""),
        help="isolated niche_full_v1 PostgreSQL DSN",
    )
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force-small-batch", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    settings = EmbeddingSettings.from_env(os.environ)
    factory = connection_factory(
        require_dsn(args.niche_dsn, "NICHE_DATABASE_URL"),
        application_name="niche-embed",
    )
    validate_staging_database(
        factory, settings.expected_database, settings.database_fingerprint
    )
    repository = BatchRepository(factory, settings)
    if args.command == "status":
        print(json.dumps(repository.status(), sort_keys=True, default=str))
        return 0

    controller = BatchController(
        repository,
        GCSBatchStore(),
        VertexBatchClient(project=settings.project, location=settings.location),
        ambiguity_grace_seconds=settings.ambiguity_grace_seconds,
        max_active_batches=settings.max_active_batches,
    )
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop.is_set():
        result = controller.run_once(force_small_batch=args.force_small_batch)
        print(json.dumps(result, sort_keys=True, default=str), flush=True)
        if args.once:
            break
        stop.wait(max(1.0, float(args.poll_seconds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
