"""A search that fails owes an email to whoever asked to be told about it.

Reported 2026-08-19 as "emails are still not sent". Every search that SUCCEEDED had in fact been
delivered — the tenant log and Resend both said so. The one that failed said nothing at all: it
marked itself failed and queued no message, so notification_status sat at 'pending' for ever. From
the user's side that is indistinguishable from a search still running, and from mail being broken.
"""
import accounts
import notifications
import webapp


def test_a_failed_search_queues_a_message(monkeypatch):
    rows = [{"user_id": 7, "slug": "adhoc-x", "notify_email": True,
             "notification_status": "pending", "query": "a vacuum gripper with a sealing lip"}]
    sent = {}

    class _Cur:
        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE"):
                sent["status"] = params[0]

        def fetchall(self):
            return rows

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(accounts, "ensure_schema", lambda: None)
    monkeypatch.setattr(accounts.db, "cursor", lambda *a, **k: _Ctx())
    monkeypatch.setattr(accounts, "get_user",
                        lambda uid: {"id": uid, "email": "nimo@rotem.ai",
                                     "full_name": "Nimo", "is_active": True})
    queued = {}
    monkeypatch.setattr(accounts, "enqueue_mail",
                        lambda **kw: (queued.update(kw), {"status": "sent"})[1])

    out = accounts.queue_failure_notifications("adhoc-x", "https://x/report/adhoc-x",
                                               reason="RuntimeError: boom")
    assert len(out) == 1
    assert queued["kind"] == "search_failed"
    assert "did not finish" in queued["subject"]
    assert "RuntimeError: boom" in queued["body_text"]
    assert "https://x/report/adhoc-x" in queued["body_text"]
    assert sent["status"] == "sent"


def test_the_failure_message_has_its_own_dedupe_key(monkeypatch):
    """A search that fails, is re-run and then succeeds must send both, and neither may suppress
    the other — they answer different questions."""
    keys = []
    rows = [{"user_id": 7, "slug": "s", "notify_email": True, "notification_status": "pending",
             "query": "q"}]

    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(accounts, "ensure_schema", lambda: None)
    monkeypatch.setattr(accounts.db, "cursor", lambda *a, **k: _Ctx())
    monkeypatch.setattr(accounts, "get_user", lambda uid: {"id": uid, "email": "e",
                                                           "full_name": "N", "is_active": True})
    monkeypatch.setattr(accounts, "enqueue_mail",
                        lambda **kw: (keys.append(kw["dedupe_key"]), {"status": "sent"})[1])
    monkeypatch.setattr(accounts, "mark_search_complete", lambda slug: rows)

    accounts.queue_failure_notifications("s", "u")
    accounts.queue_completion_notifications("s", "u")
    assert len(keys) == 2 and keys[0] != keys[1]
    assert keys[0].startswith("search-failed:") and keys[1].startswith("search-complete:")


def test_a_user_who_did_not_ask_is_not_emailed_about_a_failure(monkeypatch):
    rows = [{"user_id": 7, "slug": "s", "notify_email": False,
             "notification_status": "not_requested", "query": "q"}]

    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(accounts, "ensure_schema", lambda: None)
    monkeypatch.setattr(accounts.db, "cursor", lambda *a, **k: _Ctx())
    called = []
    monkeypatch.setattr(accounts, "enqueue_mail", lambda **kw: called.append(1))
    assert accounts.queue_failure_notifications("s", "u") == []
    assert called == []


def test_notifications_exposes_the_failure_path():
    assert hasattr(notifications, "queue_search_failure")


def test_both_failure_paths_in_the_app_notify():
    """Guards the two places that mark a search failed. A third one added later without a
    notification would reintroduce exactly the reported silence."""
    src = open(webapp.__file__.replace(".pyc", ".py")).read()
    for marker in ("accounts.mark_search_failed(slug)",):
        assert src.count(marker) == 2, "unexpected number of failure sites"
    #  every mark_search_failed must sit near a queue_search_failure
    for chunk in src.split("accounts.mark_search_failed(slug)")[1:]:
        assert "queue_search_failure" in chunk[:400], \
            "a search is marked failed without telling the user"
