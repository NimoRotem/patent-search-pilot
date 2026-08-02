"""Named-account UI contracts without sending mail or mutating the real account tables."""
import re

import pytest

import accounts
import auth
import webapp

USER = {"id": 71, "email": "analyst@example.test", "full_name": "Patent Analyst",
        "is_admin": False, "is_active": True, "email_on_completion": True,
        "session_version": 3}


@pytest.fixture()
def account_client(monkeypatch):
    webapp.app.config.update(TESTING=True, FORCE_AUTH=True, FORCE_ACCOUNTS=True,
                             APP_PASSWORD="legacy-admin-password")
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    monkeypatch.setattr(accounts, "get_user", lambda uid: dict(USER) if int(uid) == USER["id"] else None)
    auth.reset_limits()
    try:
        yield webapp.app.test_client()
    finally:
        for key in ("FORCE_AUTH", "FORCE_ACCOUNTS", "APP_PASSWORD"):
            webapp.app.config.pop(key, None)
        auth.reset_limits()


def _csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match, response.get_data(as_text=True)[:500]
    return match.group(1)


def test_registration_creates_named_session(account_client, monkeypatch):
    monkeypatch.setattr(accounts, "create_user", lambda email, name, password: dict(USER))
    page = account_client.get("/register")
    assert page.status_code == 200
    response = account_client.post("/register", data={
        "csrf_token": _csrf(page), "full_name": USER["full_name"], "email": USER["email"],
        "password": "long-enough-password", "password_confirm": "long-enough-password",
    })
    assert response.status_code == 302
    with account_client.session_transaction() as session:
        assert session["user_id"] == USER["id"]
        assert session["session_version"] == USER["session_version"]
        assert session.get("auth") is not True


def test_named_login_and_account_navigation(account_client, monkeypatch):
    monkeypatch.setattr(accounts, "authenticate", lambda email, password: dict(USER))
    response = account_client.post("/login", data={"email": USER["email"],
                                                    "password": "long-enough-password"})
    assert response.status_code == 302
    home = account_client.get("/")
    assert home.status_code == 200
    body = home.get_data(as_text=True)
    assert "Patent Analyst" in body and "/account" in body and "/logout" in body
    assert 'name="search_focus"' in body and 'value="claims"' in body
    assert 'name="notify_email"' in body


def test_inline_reauthentication_mints_new_csrf_and_named_session(account_client, monkeypatch):
    monkeypatch.setattr(accounts, "authenticate", lambda email, password: dict(USER))
    with account_client.session_transaction() as session:
        session["csrf_token"] = "expired-page-token"
    response = account_client.post(
        "/login", data={"email": USER["email"], "password": "long-enough-password"},
        headers={"Accept": "application/json", "X-Reauth": "1"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True and payload["csrf_token"] != "expired-page-token"
    with account_client.session_transaction() as session:
        assert session["user_id"] == USER["id"]
        assert session["session_version"] == USER["session_version"]
        assert session["csrf_token"] == payload["csrf_token"]


def test_legacy_login_is_an_admin_bootstrap(account_client, monkeypatch):
    monkeypatch.setattr(accounts, "list_users", list)
    monkeypatch.setattr(accounts, "mail_stats", dict)
    response = account_client.post("/login", data={"password": "legacy-admin-password"})
    assert response.status_code == 302
    admin = account_client.get("/admin/users")
    assert admin.status_code == 200
    assert "User administration" in admin.get_data(as_text=True)


def test_saved_report_toggle_requires_csrf(account_client, monkeypatch):
    with account_client.session_transaction() as session:
        session["user_id"] = USER["id"]
        session["session_version"] = USER["session_version"]
        session["csrf_token"] = "csrf-test"
    monkeypatch.setattr(accounts, "get_search", lambda uid, slug: {"saved": True, "title": None})
    monkeypatch.setattr(accounts, "set_search_saved",
                        lambda uid, slug, saved, title=None: {"saved": saved, "title": title})
    denied = account_client.post("/api/searches/grabo_gripper_novelty",
                                 json={"saved": False})
    assert denied.status_code == 400
    ok = account_client.post("/api/searches/grabo_gripper_novelty", json={"saved": False},
                             headers={"X-CSRF-Token": "csrf-test"})
    assert ok.status_code == 200 and ok.get_json()["saved"] is False


def test_trusted_loopback_automation_can_open_ad_hoc_reports(monkeypatch):
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", True)
    monkeypatch.setattr(auth, "accounts_enabled", lambda app=None: True)
    with webapp.app.test_request_context("/report/adhoc-test",
                                         environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert webapp._can_access_report("adhoc-test") is True


def test_running_search_offers_wait_email_or_history(account_client, monkeypatch):
    with account_client.session_transaction() as session:
        session["user_id"] = USER["id"]
        session["session_version"] = USER["session_version"]
        session["csrf_token"] = "csrf-test"
    row = {"slug": "adhoc-running", "notify_email": False,
           "notification_status": "not_requested", "status": "running"}
    monkeypatch.setattr(accounts, "can_access_search", lambda uid, slug: True)
    monkeypatch.setattr(accounts, "get_search", lambda uid, slug: dict(row))
    monkeypatch.setattr(webapp, "ensure_report", lambda *a, **k: ("running", None))
    response = account_client.get("/report/adhoc-running")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Email me when ready" in body
    assert "Leave and view history" in body
    assert "/notification" in body


def test_notification_toggle_does_not_email_a_partial_report(account_client, monkeypatch):
    with account_client.session_transaction() as session:
        session["user_id"] = USER["id"]
        session["session_version"] = USER["session_version"]
        session["csrf_token"] = "csrf-test"
    row = {"slug": "adhoc-running", "notify_email": True,
           "notification_status": "pending", "status": "running"}
    monkeypatch.setattr(accounts, "get_search", lambda uid, slug: dict(row))
    monkeypatch.setattr(accounts, "set_search_notification", lambda uid, slug, enabled: dict(row))
    monkeypatch.setattr(webapp, "_load_report", lambda slug: {"partial": True})
    queued = []
    monkeypatch.setattr(webapp.notifications, "queue_search_completion", lambda slug: queued.append(slug))
    response = account_client.post(
        "/api/searches/adhoc-running/notification", json={"enabled": True},
        headers={"X-CSRF-Token": "csrf-test"})
    assert response.status_code == 200
    assert response.get_json()["enabled"] is True
    assert queued == []


def test_notification_toggle_queues_if_report_already_finished(account_client, monkeypatch):
    with account_client.session_transaction() as session:
        session["user_id"] = USER["id"]
        session["session_version"] = USER["session_version"]
        session["csrf_token"] = "csrf-test"
    before = {"notify_email": False, "notification_status": "not_requested", "status": "running"}
    enabled = {"notify_email": True, "notification_status": "pending", "status": "running"}
    after = {"notify_email": True, "notification_status": "queued", "status": "complete"}
    calls = {"get": 0, "queued": []}
    def get_search(uid, slug):
        calls["get"] += 1
        return dict(before if calls["get"] == 1 else after)
    monkeypatch.setattr(accounts, "get_search", get_search)
    monkeypatch.setattr(accounts, "set_search_notification", lambda uid, slug, on: dict(enabled))
    monkeypatch.setattr(webapp, "_load_report", lambda slug: {"partial": False})
    monkeypatch.setattr(webapp.notifications, "queue_search_completion",
                        lambda slug: calls["queued"].append(slug))
    response = account_client.post(
        "/api/searches/adhoc-finished/notification", json={"enabled": True},
        headers={"X-CSRF-Token": "csrf-test"})
    assert response.status_code == 200
    assert calls["queued"] == ["adhoc-finished"]
    assert response.get_json()["status"] == "queued"


def test_batch_reference_preview_is_cache_only_and_scoped(account_client, monkeypatch, tmp_path):
    with account_client.session_transaction() as session:
        session["user_id"] = USER["id"]
        session["session_version"] = USER["session_version"]
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    card = {"pub": "US-123-A", "title": "Fast text", "abstract": "Ready now",
            "claims": [{"claim_no": 1, "text": "A device."}],
            "description": [{"para_no": "0001", "text": "Description."}],
            "cpc": [{"code": "B66C1/02"}], "images": [], "n_images": 0}
    webapp._write_detail_preview("adhoc-preview", {"partial": True, "cards": [card]})
    response = account_client.get("/api/ref-batch/adhoc-preview?pubs=US-123-A")
    assert response.status_code == 200
    data = response.get_json()
    assert data["partial"] is True
    assert data["items"]["US-123-A"]["sections"]["claims"][0]["text"] == "A device."
    assert data["items"]["US-123-A"]["_preview"] is True
    claim_only = account_client.get(
        "/api/ref-batch/adhoc-preview?pubs=US-123-A&section=claims").get_json()["items"]["US-123-A"]
    assert "claims" in claim_only["sections"]
    assert "paragraphs" not in claim_only["sections"] and "abstract" not in claim_only["display"]

    source = data["items"]["US-123-A"] | {"rationale": {"summary": "Specific overlap."}}
    why_only = webapp._detail_preview_section(source, "why")
    assert why_only["rationale"]["summary"] == "Specific overlap."
    assert "abstract" not in why_only["display"] and why_only["sections"] == {}


def test_search_cache_identity_includes_subject_and_uploaded_document():
    base = webapp.search_slug("same query", "novelty", wide=True, search_focus="claims")
    anchored = webapp.search_slug(
        "same query", "novelty", wide=True, search_focus="claims", subject="US-123-A")
    uploaded = webapp.search_slug(
        "same query", "novelty", wide=True, search_focus="claims", doc_token="document-1")
    assert len({base, anchored, uploaded}) == 3
    assert uploaded == webapp.search_slug(
        "same query", "novelty", wide=True, search_focus="claims", doc_token="document-1")


def test_password_session_version_rejects_unversioned_and_older_signed_sessions(account_client):
    with account_client.session_transaction() as session:
        session["user_id"] = USER["id"]
#  "/" is a PUBLIC landing page now (see tests/test_public_shell.py): a visitor has to be
#  able to read what the product does before being asked to sign in. The canary for "am I
#  authenticated" therefore has to be a route that is still gated.
    missing = account_client.get("/history", follow_redirects=False)
    assert missing.status_code == 302 and "/login" in missing.headers["Location"]
    with account_client.session_transaction() as session:
        session["user_id"] = USER["id"]
        session["session_version"] = USER["session_version"] - 1
    response = account_client.get("/history", follow_redirects=False)
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_unknown_browser_page_is_branded_but_unknown_api_stays_json(account_client):
    with account_client.session_transaction() as session:
        session["user_id"] = USER["id"]
        session["session_version"] = USER["session_version"]
    page = account_client.get("/qa-page-that-does-not-exist")
    assert page.status_code == 404
    assert "That page is not available" in page.get_data(as_text=True)
    api = account_client.get("/api/qa-page-that-does-not-exist")
    assert api.status_code == 404 and api.is_json and api.get_json()["error"] == "not found"
