"""One sign-in for the domain: adopting the peer app's session (src/auth.py PEER_*).

nimo.iptorch.com serves two copies of this application, the full-text app at "/" and this one at
/classic/, each with its own accounts table and its own .secret_key. After the 2026-08-27 root
cutover, somebody signed in at the root arrived here signed out, and this copy is the one holding
the drafting studio, so a second login box read as having lost seventeen live drafts.

What these tests pin, in order of how much it would cost to get wrong:

  * A peer session is matched to a local account by EMAIL and never by id. The two databases
    number their users independently. On the live pair, peer id 1 is nimo@rotem.ai and local id 1
    is a retired QA account, so trusting the id would have signed the owner in as somebody else.
  * A cookie signed with any other key is not a session. That is the whole security boundary.
  * A peer account with no live account here is refused, and the refusal is REMEMBERED, so a
    visitor who has one there and not here does not re-query the peer database on every request.
  * With neither variable set, nothing happens at all: this is off for every other deployment.
"""
import pytest
from flask import Flask, session
from flask.sessions import SecureCookieSessionInterface

import accounts
import auth
import webapp

PEER_KEY = "peer-key-for-tests-8bd41c6a"
OTHER_KEY = "a-different-key-entirely-77f2"
EMAIL = "peer-sso@example.test"
#  Deliberately mismatched ids: the peer calls this person 1, this app calls them 904. Matching
#  on id would sign them in as local id 1, which on the live pair is a retired QA account.
PEER_ID = 1
LOCAL = {"id": 904, "email": EMAIL, "full_name": "Peer Person", "is_admin": False,
         "is_active": True, "session_version": 5}


def _sign(payload, key=PEER_KEY):
    """A cookie exactly as the peer Flask app would have written it."""
    holder = Flask(__name__)
    holder.secret_key = key
    return SecureCookieSessionInterface().get_signing_serializer(holder).dumps(payload)


@pytest.fixture()
def peer(monkeypatch, tmp_path):
    key_file = tmp_path / ".secret_key"
    key_file.write_text(PEER_KEY + "\n")
    monkeypatch.setattr(auth, "PEER_SECRET_FILE", str(key_file))
    monkeypatch.setattr(auth, "PEER_ACCOUNTS_DSN", "postgresql://unused/for-these-tests")
    monkeypatch.setattr(auth, "PEER_COOKIE_NAME", "session")
    monkeypatch.setattr(auth, "_peer_signer", None)
    #  The one thing these tests do not exercise is the peer's database. `_peer_account` is the
    #  thin adapter over it; everything it can answer is expressed here as its return value.
    monkeypatch.setattr(auth, "_peer_account",
                        lambda uid, ver: EMAIL if (int(uid) == PEER_ID and int(ver) == 1) else None)
    monkeypatch.setattr(accounts, "get_user_by_email",
                        lambda email: dict(LOCAL) if email == EMAIL else None)
    monkeypatch.setattr(accounts, "get_user",
                        lambda uid: dict(LOCAL) if int(uid) == LOCAL["id"] else None)
    webapp.app.config["TESTING"] = True
    yield webapp.app
    monkeypatch.setattr(auth, "_peer_signer", None)


def _adopt(app, cookie):
    """Run one request carrying `cookie` and report (adopted?, the session it left behind)."""
    with app.test_request_context("/", headers={"Cookie": f"session={cookie}"} if cookie else {}):
        adopted = auth._adopt_peer_session()
        return adopted, dict(session)


def test_a_valid_peer_session_signs_you_in_here(peer):
    adopted, left = _adopt(peer, _sign({"user_id": PEER_ID, "session_version": 1}))
    assert adopted is True
    assert left["session_version"] == LOCAL["session_version"]
    assert left["csrf_token"]


def test_the_local_account_is_found_by_email_not_by_the_peers_id(peer):
    """The regression that would have signed the owner in as a retired QA account."""
    _adopted, left = _adopt(peer, _sign({"user_id": PEER_ID, "session_version": 1}))
    assert left["user_id"] == LOCAL["id"] == 904
    assert left["user_id"] != PEER_ID


def test_a_cookie_signed_with_another_key_is_not_a_session(peer):
    adopted, left = _adopt(peer, _sign({"user_id": PEER_ID, "session_version": 1}, key=OTHER_KEY))
    assert adopted is False
    assert "user_id" not in left


def test_a_peer_cookie_naming_nobody_here_is_refused_and_remembered(peer, monkeypatch):
    """A visitor of the peer app with no account here must not re-query it on every request."""
    calls = []
    monkeypatch.setattr(auth, "_peer_account",
                        lambda uid, ver: calls.append((uid, ver)) or "stranger@example.test")
    cookie = _sign({"user_id": 77, "session_version": 1})
    adopted, left = _adopt(peer, cookie)
    assert adopted is False
    assert "user_id" not in left
    assert left["peer_refused"] == cookie[-24:]
    #  Second request, same cookie, carrying what the first one left behind: no second lookup.
    with peer.test_request_context("/", headers={"Cookie": f"session={cookie}"}):
        session.update(left)
        assert auth._adopt_peer_session() is False
    assert len(calls) == 1


def test_a_peer_session_that_names_no_user_is_ignored(peer):
    """An anonymous peer cookie carries only a csrf token. It is signed, and it is nobody."""
    adopted, left = _adopt(peer, _sign({"csrf_token": "anonymous-visitor"}))
    assert adopted is False
    assert "user_id" not in left


def test_an_existing_local_session_is_never_overwritten(peer):
    cookie = _sign({"user_id": PEER_ID, "session_version": 1})
    with peer.test_request_context("/", headers={"Cookie": f"session={cookie}"}):
        session["user_id"] = 42
        assert auth._adopt_peer_session() is False
        assert session["user_id"] == 42


def test_it_is_off_when_it_is_not_configured(monkeypatch):
    """Every deployment that is not one of a pair must behave exactly as it did before."""
    monkeypatch.setattr(auth, "PEER_SECRET_FILE", "")
    monkeypatch.setattr(auth, "PEER_ACCOUNTS_DSN", "")
    webapp.app.config["TESTING"] = True
    adopted, left = _adopt(webapp.app, _sign({"user_id": PEER_ID, "session_version": 1}))
    assert adopted is False
    assert "user_id" not in left


def test_a_signed_in_visitor_is_not_shown_the_login_form(peer, monkeypatch):
    """The exact URL that was reported: /login?next=/drafts, arriving with a peer session.

    It is the redirect this app issued a moment earlier, so following it must not present a
    password box to somebody who is already signed in.
    """
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    monkeypatch.setattr(auth, "API_TOKEN", "")
    peer.config["FORCE_AUTH"] = True
    peer.config["FORCE_ACCOUNTS"] = True
    auth.reset_limits()
    try:
        client = peer.test_client()
        client.set_cookie("session", _sign({"user_id": PEER_ID, "session_version": 1}),
                          domain="localhost")
        response = client.get("/login?next=/drafts")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/drafts")
        #  ...and the escape hatch still works, so switching accounts is always possible.
        assert client.get("/login?next=/drafts&force=1").status_code == 200
    finally:
        peer.config.pop("FORCE_AUTH", None)
        peer.config.pop("FORCE_ACCOUNTS", None)
        auth.reset_limits()


def test_the_gate_adopts_before_it_sends_anyone_to_a_login_box(peer, monkeypatch):
    """End to end: the peer's cookie, through the real before_request gate, on a gated page."""
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    monkeypatch.setattr(auth, "API_TOKEN", "")
    peer.config["FORCE_AUTH"] = True
    peer.config["FORCE_ACCOUNTS"] = True
    auth.reset_limits()
    try:
        client = peer.test_client()
        client.set_cookie("session", _sign({"user_id": PEER_ID, "session_version": 1}),
                          domain="localhost")
        response = client.get("/history")
        assert response.status_code != 302, "was sent to a second login box"
        assert response.status_code == 200
    finally:
        peer.config.pop("FORCE_AUTH", None)
        peer.config.pop("FORCE_ACCOUNTS", None)
        auth.reset_limits()
