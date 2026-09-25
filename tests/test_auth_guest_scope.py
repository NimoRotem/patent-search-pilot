"""A guest account narrowed to ONE page (src/auth.py, src/observations.py).

Outside counsel is given the actions docket and nothing else: not the drafting studio, not the
search history, not the filing browser, and not their own profile page. The narrowing lives on
the account (`app_users.access_scope`) rather than on a link, so revoking it is one UPDATE and a
guest inherits the workbench's own session revocation, rate limits and password rules.

The default `app_client` fixture runs with TESTING=True, which disables the gate. These tests opt
it back on with FORCE_AUTH and turn off the loopback exemption, exactly as test_auth_extra does.
"""
import pytest

import accounts
import auth
import observations
import webapp

PASSWORD = "guest-scope-password-4c71"
GUEST = {"id": 902, "email": "counsel@example.test", "full_name": "Outside Counsel",
         "is_admin": False, "is_active": True, "session_version": 1,
         "access_scope": "actions", "docket_user_id": 903}
HOST = {"id": 903, "email": "owner@example.test", "full_name": "Docket Owner",
        "is_admin": True, "is_active": True, "session_version": 1,
        "access_scope": "", "docket_user_id": None}

_BY_ID = {GUEST["id"]: GUEST, HOST["id"]: HOST}


def _accounts(monkeypatch):
    monkeypatch.setattr(accounts, "get_user",
                        lambda uid: dict(_BY_ID[int(uid)]) if int(uid) in _BY_ID else None)
    monkeypatch.setattr(accounts, "authenticate", lambda email, password: next(
        (dict(u) for u in (GUEST, HOST)
         if u["email"] == email and password == PASSWORD), None))


@pytest.fixture()
def secured(monkeypatch):
    """A client with the auth gate actually enforced."""
    webapp.app.config["TESTING"] = True
    webapp.app.config["FORCE_AUTH"] = True
    webapp.app.config["FORCE_ACCOUNTS"] = True
    _accounts(monkeypatch)
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)   # test client looks like loopback
    monkeypatch.setattr(auth, "API_TOKEN", "")
    auth.reset_limits()
    try:
        yield webapp.app.test_client()
    finally:
        webapp.app.config.pop("FORCE_AUTH", None)
        webapp.app.config.pop("FORCE_ACCOUNTS", None)
        auth.reset_limits()


def _sign_in(client, user):
    r = client.post("/login", data={"email": user["email"], "password": PASSWORD})
    assert r.status_code in (200, 302), r.status_code
    return r


# ---- the gate ------------------------------------------------------------------------------
def test_guest_is_sent_to_its_one_page(secured):
    _sign_in(secured, GUEST)
    r = secured.get("/history")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/actions")


def test_guest_is_refused_even_on_an_open_endpoint(secured):
    """The scope check must NOT be keyed off _OPEN_ENDPOINTS.

    `/about` is readable by a signed-out stranger. A signed-in guest asking for it should be put
    back on their own page rather than shown the rest of the workbench's furniture.
    """
    _sign_in(secured, GUEST)
    r = secured.get("/about")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/actions")


def test_guest_may_not_load_the_profile_page(secured):
    """This one is a security boundary, not tidiness.

    /patents/sketch is a separate service that decides whether somebody is signed in by fetching
    THIS app's /account with their cookie and reading the status code. Letting a guest load
    /account would hand them the drawings app as well.
    """
    _sign_in(secured, GUEST)
    r = secured.get("/account")
    assert r.status_code != 200


def test_guest_json_call_outside_the_scope_is_refused_not_redirected(secured):
    """A fetch() must never read an HTML redirect as its answer."""
    _sign_in(secured, GUEST)
    r = secured.post("/api/actions/targets", json={"name": "x"})
    assert r.status_code == 403


def test_guest_keeps_the_endpoints_its_page_actually_calls(secured):
    """Every endpoint the docket page drives from the browser stays reachable."""
    _sign_in(secured, GUEST)
    with webapp.app.test_request_context("/actions"):
        for ep in ("observations.actions_page", "observations.api_action_case",
                   "observations.api_action_refresh", "observations.api_action_refresh_state",
                   "observations.action_image", "observations.action_package",
                   "observations.action_iptorch_zip", "static", "auth.logout"):
            assert ep in auth._SCOPE_ENDPOINTS["actions"] or ep in auth._SCOPE_ALWAYS, ep


def test_an_ordinary_account_is_untouched(secured):
    _sign_in(secured, HOST)
    assert secured.get("/history").status_code == 200
    assert secured.get("/account").status_code == 200


def test_the_landing_itself_is_never_bounced(monkeypatch):
    """A scope naming a page this build does not serve must not ping-pong the browser.

    Refused ON the landing it would otherwise redirect to, the gate says 403 once instead of
    sending the browser round the same two URLs for ever.
    """
    from werkzeug.exceptions import Forbidden
    _accounts(monkeypatch)
    monkeypatch.setattr(auth, "current_user", lambda: dict(GUEST))
    monkeypatch.setitem(auth._SCOPE_ENDPOINTS, "actions", set())
    with webapp.app.test_request_context("/actions"):
        with pytest.raises(Forbidden):
            auth._scope_denies("observations.actions_page")


# ---- where a guest lands after signing in --------------------------------------------------
def test_login_sends_a_guest_to_its_own_page(secured):
    r = secured.post("/login", data={"email": GUEST["email"], "password": PASSWORD,
                                     "next": "/drafts"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/actions")


def test_login_with_no_next_lands_a_guest_on_its_page_directly(secured):
    """Not on the root, which would bounce: one visible hop on the first screen an outsider
    ever sees."""
    r = secured.post("/login", data={"email": GUEST["email"], "password": PASSWORD})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/actions")


def test_login_with_no_next_lands_an_ordinary_account_on_the_root(secured):
    r = secured.post("/login", data={"email": HOST["email"], "password": PASSWORD})
    assert r.status_code == 302
    assert r.headers["Location"] == "/"


def test_login_honours_next_for_an_ordinary_account(secured):
    r = secured.post("/login", data={"email": HOST["email"], "password": PASSWORD,
                                     "next": "/drafts"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/drafts")


# ---- what the siblings are told ------------------------------------------------------------
def test_session_check_reports_the_scope(secured):
    """The three filing siblings admit on is_admin, which a guest can never have. The scope is
    reported so a future sibling that does not require an admin can still refuse."""
    _sign_in(secured, GUEST)
    body = secured.get("/api/session-check").get_json()
    assert body["authenticated"] is True
    assert body["is_admin"] is False
    assert body["access_scope"] == "actions"


def test_session_check_is_empty_for_an_ordinary_account(secured):
    _sign_in(secured, HOST)
    body = secured.get("/api/session-check").get_json()
    assert body["access_scope"] == ""


# ---- whose docket a guest works on ---------------------------------------------------------
def test_a_guest_reads_the_host_account_docket(monkeypatch):
    """The docket rows are keyed by user id. A guest owns none, so returning their own id would
    show an empty page, which is indistinguishable from a wiped docket."""
    _accounts(monkeypatch)
    monkeypatch.setattr(auth, "current_user", lambda: dict(GUEST))
    with webapp.app.test_request_context("/actions"):
        assert observations._user()["id"] == HOST["id"]
        assert auth.docket_owner_id() == HOST["id"]


def test_an_ordinary_account_reads_its_own_docket(monkeypatch):
    _accounts(monkeypatch)
    monkeypatch.setattr(auth, "current_user", lambda: dict(HOST))
    with webapp.app.test_request_context("/actions"):
        assert observations._user()["id"] == HOST["id"]
        assert auth.docket_owner_id() == HOST["id"]


def test_a_guest_whose_host_was_deactivated_is_refused(monkeypatch):
    """Deactivating the owner must take the guest's view of the docket with it."""
    dead = dict(HOST, is_active=False)
    monkeypatch.setattr(accounts, "get_user",
                        lambda uid: dict(dead) if int(uid) == HOST["id"] else dict(GUEST))
    monkeypatch.setattr(auth, "current_user", lambda: dict(GUEST))
    with webapp.app.test_request_context("/actions"):
        with pytest.raises(Exception):
            observations._user()


def test_guests_may_not_change_what_is_watched(monkeypatch):
    """A guest's writes land on the OWNER's rows, so a deletion here would be somebody else's
    docket disappearing."""
    monkeypatch.setattr(auth, "current_scope", lambda: "actions")
    with webapp.app.test_request_context("/api/actions/targets"):
        with pytest.raises(Exception):
            observations._no_guests()
    monkeypatch.setattr(auth, "current_scope", lambda: "")
    with webapp.app.test_request_context("/api/actions/targets"):
        assert observations._no_guests() is None


# ---- setting the scope ---------------------------------------------------------------------
def test_an_unknown_scope_is_refused():
    """A typo would not fail on its own: the gate refuses everything outside the allowlist for
    ANY non-empty scope, so a misspelt value locks the account out of the page it was made for
    and reads as a broken password."""
    with pytest.raises(ValueError):
        accounts.set_access_scope(1, "acitons")
