"""Mail alerts are durable/product-facing, while tests remain capture-only."""
from hashlib import sha256

import notifications


def test_capture_transport_builds_completion_message(monkeypatch):
    monkeypatch.setattr(notifications, "MAIL_TRANSPORT", "capture")
    notifications._CAPTURED.clear()
    row = {"to_email": "analyst@example.test", "subject": "Report ready",
           "body_text": "Open the saved report.", "kind": "search_complete"}
    notifications._deliver(row)
    assert notifications.captured_messages() == [{
        "to": "analyst@example.test", "subject": "Report ready",
        "body": "Open the saved report.",
    }]


def test_password_reset_queues_no_plaintext_token_in_durable_key(monkeypatch):
    token = "opaque-reset-token"
    user = {"id": 42, "email": "analyst@example.test", "full_name": "Patent Analyst"}
    queued = {}
    monkeypatch.setattr(notifications.accounts, "create_password_reset",
                        lambda email: (token, user))
    monkeypatch.setattr(notifications.accounts, "enqueue_mail",
                        lambda **kwargs: queued.update(kwargs) or kwargs)
    monkeypatch.setattr(notifications, "kick", lambda: None)

    assert notifications.queue_password_reset(
        user["email"], lambda value: f"https://rotem.ai/patents/reset-password/{value}") is True
    assert token not in queued["dedupe_key"]
    assert queued["dedupe_key"].endswith(sha256(token.encode()).hexdigest())
    assert f"/reset-password/{token}" in queued["body_text"]
    assert queued["kind"] == "password_reset"


def test_completion_queue_wakes_delivery_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.accounts, "queue_completion_notifications",
                        lambda slug, url: [{"slug": slug, "url": url}])
    monkeypatch.setattr(notifications, "kick", lambda: calls.append("wake"))
    rows = notifications.queue_search_completion("adhoc-test")
    assert rows and rows[0]["url"].endswith("/report/adhoc-test")
    assert calls == ["wake"]
