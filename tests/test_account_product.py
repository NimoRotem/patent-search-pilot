"""Named-account UI contracts without sending mail or mutating the real account tables."""
import re

import pytest

import accounts
import auth
import webapp


USER = {"id": 71, "email": "analyst@example.test", "full_name": "Patent Analyst",
        "is_admin": False, "is_active": True, "email_on_completion": True}


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


def test_legacy_login_is_an_admin_bootstrap(account_client, monkeypatch):
    monkeypatch.setattr(accounts, "list_users", lambda: [])
    monkeypatch.setattr(accounts, "mail_stats", lambda: {})
    response = account_client.post("/login", data={"password": "legacy-admin-password"})
    assert response.status_code == 302
    admin = account_client.get("/admin/users")
    assert admin.status_code == 200
    assert "User administration" in admin.get_data(as_text=True)


def test_saved_report_toggle_requires_csrf(account_client, monkeypatch):
    with account_client.session_transaction() as session:
        session["user_id"] = USER["id"]
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
