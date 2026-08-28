"""Durability contracts at the drafting worker/outbox boundary."""

import draft_worker


class FakeWorkerService:
    def __init__(self):
        self.failed = []
        self.marked = []
        self.pending = []

    def claim_generation(self, worker_id, lease_seconds):
        return {
            "id": 31, "lease_token": "lease", "system_prompt": "system",
            "user_prompt": "user", "project": {"id": 7, "user_id": 91, "title": "Tool"},
        }

    def complete_generation(self, job_id, lease_token, generated, model_name):
        return {"project_id": 7, "version_no": 2, "published": True}

    def fail_generation(self, *args, **kwargs):
        self.failed.append((args, kwargs))
        return {"status": "failed"}

    def heartbeat_generation(self, *args, **kwargs):
        return None

    def pending_version_notifications(self, limit=20):
        return list(self.pending)

    def mark_version_notification(self, project_id, version_no, status):
        self.marked.append((project_id, version_no, status))
        return {"notification_status": status}


def test_successful_version_is_not_failed_when_outbox_handoff_temporarily_breaks(monkeypatch):
    service = FakeWorkerService()
    draft_worker.configure(lambda: service)
    monkeypatch.setattr(draft_worker, "_generate", lambda system, user: {"ok": True})
    monkeypatch.setattr(draft_worker.accounts, "get_user", lambda user_id: {
        "id": user_id, "email": "qa@example.invalid", "full_name": "QA",
        "is_active": True, "email_on_completion": True,
    })
    monkeypatch.setattr(
        draft_worker, "_queue_version_notification",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("outbox unavailable")))

    result = draft_worker.process_one()

    assert result["published"] is True
    assert service.failed == []
    assert "Draft notification" in (draft_worker.status()["last_error"] or "")


def test_pending_version_notification_reconciles_idempotently(monkeypatch):
    service = FakeWorkerService()
    service.pending = [{
        "project_id": 7, "version_no": 2, "title": "Tool", "user_id": 91,
        "email": "qa@example.invalid", "full_name": "QA", "is_active": True,
        "email_on_completion": True,
    }]
    monkeypatch.setattr(draft_worker.notifications, "queue_draft_completion",
                        lambda user, project, version: {"id": 55, "status": "pending"})

    assert draft_worker._reconcile_notifications(service) == 1
    assert service.marked == [(7, 2, "queued")]
