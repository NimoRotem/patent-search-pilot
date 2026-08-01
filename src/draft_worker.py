"""Small durable worker for queued patent-drafting jobs."""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable

import accounts
import drafting
import llm
import notifications

POLL_SECONDS = max(2.0, float(os.environ.get("DRAFT_POLL_SECONDS", "6")))
LEASE_SECONDS = max(120, min(int(os.environ.get("DRAFT_LEASE_SECONDS", "900")), 1800))

_START_LOCK = threading.Lock()
_THREAD = None
_STOP = threading.Event()
_WAKE = threading.Event()
_SERVICE_FACTORY: Callable[[], drafting.DraftingService] | None = None
_STATE = {"running": False, "last_job_id": None, "last_result": None,
          "last_error": None, "updated_at": None}


def configure(service_factory: Callable[[], drafting.DraftingService]) -> None:
    global _SERVICE_FACTORY
    _SERVICE_FACTORY = service_factory


def _stamp(**values) -> None:
    _STATE.update(values, updated_at=time.time())


def _missing_sections(generated) -> list[str]:
    return [key for key in drafting.SECTION_KEYS
            if not isinstance((generated or {}).get(key), str)
            or not str((generated or {}).get(key)).strip()]


def _generate(system_prompt: str, user_prompt: str):
    """One model call, plus ONE corrective call for any section it left out.

    Measured, and it reproduced every time: asked for nine sections with the project title
    already present in SOURCE_DATA, the model returned eight and silently omitted `title` —
    apparently treating a title it had been given as one it need not restate. The validator is
    strict and rightly refuses an incomplete draft, so every attempt failed and the whole
    generation (a 21,000-character prompt, three times over) was thrown away over one short
    string.

    The fix is a second, narrow request for exactly the missing keys rather than substituting a
    value: a draft missing `claims` must NOT be quietly patched from somewhere else, and asking
    the model to supply what it skipped keeps the validator's contract intact.
    """
    generated = llm.chat_json(system_prompt, user_prompt, max_tokens=16_000)
    if not generated:
        raise RuntimeError("The drafting model returned no valid JSON.")
    missing = _missing_sections(generated)
    if not missing:
        return generated
    headings = dict(drafting.SECTION_ORDER)
    retry = llm.chat_json(
        system_prompt,
        user_prompt +
        "\n\nYou omitted the following required section(s) from your JSON: " +
        ", ".join(f"{k} ({headings.get(k, k)})" for k in missing) +
        ". Return ONLY a JSON object containing exactly those keys, drafted to the same "
        "requirements and guardrails. Do not restate the sections you already produced.",
        max_tokens=8_000) or {}
    for key in missing:
        value = retry.get(key)
        if isinstance(value, str) and value.strip():
            generated[key] = value
    still = _missing_sections(generated)
    if still:
        raise RuntimeError("The drafting model omitted required section(s): " + ", ".join(still))
    return generated


def _renew_lease(service: drafting.DraftingService, job_id: int, lease_token: str,
                 stop: threading.Event) -> None:
    """Keep a long model request from being reclaimed by a second gunicorn worker."""
    interval = max(15.0, min(float(LEASE_SECONDS) / 3.0, 60.0))
    while not stop.wait(interval):
        try:
            service.heartbeat_generation(job_id, lease_token, lease_seconds=LEASE_SECONDS)
        except drafting.DraftingError:
            # Cancellation, archival, or another revision intentionally revokes the capability.
            return
        except Exception as exc:  # noqa: BLE001 - retry a transient database/transport failure
            # A transient DB failure should not terminate the model request. The next interval can
            # renew the lease, and completion still verifies ownership transactionally.
            _stamp(last_error=f"Lease heartbeat: {type(exc).__name__}: {str(exc)[:350]}")


def _queue_version_notification(service: drafting.DraftingService, item: dict) -> bool:
    user = {
        "id": item.get("user_id"), "email": item.get("email"),
        "full_name": item.get("full_name"), "is_active": item.get("is_active"),
        "email_on_completion": item.get("email_on_completion"),
    }
    project = {"id": item.get("project_id"), "title": item.get("title")}
    version = {"version_no": item.get("version_no")}
    mail = notifications.queue_draft_completion(user, project, version)
    service.mark_version_notification(
        item["project_id"], item["version_no"], "queued" if mail else "not_requested")
    return bool(mail)


def _reconcile_notifications(service: drafting.DraftingService) -> int:
    """Retry the post-commit outbox handoff until every published version is represented."""
    handled = 0
    for item in service.pending_version_notifications(limit=10):
        try:
            _queue_version_notification(service, item)
            handled += 1
        except Exception as exc:  # noqa: BLE001 - leave pending for a later durable retry
            _stamp(last_error=f"Draft notification: {type(exc).__name__}: {str(exc)[:350]}")
            break
    return handled


def process_one() -> dict | None:
    """Claim and process one job. Safe to call from a test or an operational command."""
    if not _SERVICE_FACTORY:
        return None
    service = _SERVICE_FACTORY()
    worker_id = f"patent-draft-{os.getpid()}-{threading.get_ident()}"
    claimed = service.claim_generation(worker_id, lease_seconds=LEASE_SECONDS)
    if not claimed:
        return None
    _stamp(running=True, last_job_id=claimed["id"], last_result="generating", last_error=None)
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_renew_lease,
        args=(service, claimed["id"], claimed["lease_token"], heartbeat_stop),
        name=f"patent-draft-lease-{claimed['id']}", daemon=True)
    heartbeat.start()
    try:
        generated = _generate(claimed["system_prompt"], claimed["user_prompt"])
        version = service.complete_generation(
            claimed["id"], claimed["lease_token"], generated, model_name=llm.AGENT_MODEL)
        if version.get("published"):
            project = claimed.get("project") or {}
            _stamp(running=False, last_result="published", last_error=None)
            # Publication is already committed. Notification handoff is deliberately isolated so
            # an SMTP/outbox problem cannot turn a successful version into a failed generation.
            try:
                user = accounts.get_user(project.get("user_id"))
                _queue_version_notification(service, {
                    **project, "project_id": project.get("id"),
                    "version_no": version.get("version_no"), **(user or {}),
                    "user_id": project.get("user_id"),
                })
            except Exception as exc:  # noqa: BLE001 - reconciliation retries the pending row
                _stamp(last_error=f"Draft notification: {type(exc).__name__}: {str(exc)[:350]}")
        else:
            _stamp(running=False, last_result="superseded", last_error=None)
        return version
    except drafting.DraftingValidationError as exc:
        # Model-shape and grounding failures are often transient. Preserve the validator's exact
        # reason and let the durable exponential retry policy make another bounded attempt.
        try:
            result = service.fail_generation(
                claimed["id"], claimed["lease_token"], str(exc), retryable=True)
            _stamp(running=False, last_result=result.get("status"), last_error=str(exc)[:500])
            return result
        except drafting.DraftingError:
            _stamp(running=False, last_result="superseded", last_error=str(exc)[:500])
            return None
    except Exception as exc:  # noqa: BLE001 - durable worker boundary records and retries failures
        try:
            result = service.fail_generation(
                claimed["id"], claimed["lease_token"],
                f"{type(exc).__name__}: {str(exc)[:3500]}", retryable=True)
            _stamp(running=False, last_result=result.get("status"), last_error=str(exc)[:500])
            return result
        except drafting.DraftingError:
            _stamp(running=False, last_result="superseded", last_error=str(exc)[:500])
            return None
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1.0)


def _worker() -> None:
    while not _STOP.is_set():
        try:
            # Reconcile before every generation claim so a busy drafting queue cannot starve an
            # already-published version's promised completion email.
            if _SERVICE_FACTORY:
                _reconcile_notifications(_SERVICE_FACTORY())
            worked = process_one()
            if worked is not None:
                continue
        except Exception as exc:  # noqa: BLE001 - keep the long-lived worker thread alive
            _stamp(running=False, last_result="worker-error", last_error=str(exc)[:500])
            traceback.print_exc()
        _WAKE.wait(POLL_SECONDS)
        _WAKE.clear()


def start_worker():
    global _THREAD
    with _START_LOCK:
        if _THREAD and _THREAD.is_alive():
            return _THREAD
        _STOP.clear()
        _THREAD = threading.Thread(target=_worker, name="patent-draft-worker", daemon=True)
        _THREAD.start()
        return _THREAD


def stop_worker() -> None:
    _STOP.set()
    _WAKE.set()


def kick() -> None:
    _WAKE.set()


def status() -> dict:
    return {**_STATE, "thread_alive": bool(_THREAD and _THREAD.is_alive()),
            "configured": _SERVICE_FACTORY is not None}


def init_app(app, service_factory: Callable[[], drafting.DraftingService]):
    configure(service_factory)
    if ("pytest" not in sys.modules and not app.config.get("TESTING")
            and os.environ.get("DRAFT_WORKER", "1").lower() not in ("0", "false", "no")):
        start_worker()
    return app


atexit.register(stop_worker)
