"""`is_loopback()` decided that the entire internet was a local caller.

Behind nginx, REMOTE_ADDR is 127.0.0.1 for every request that has ever arrived, and the function
read REMOTE_ADDR. Eleven routes are gated on `current_user() or is_loopback()`, so eleven routes
were open. Measured against the live site on 2026-08-25 with no session and no credentials:

    GET /api/designs?q=gripper   200, EUIPO rows
    GET /api/factory/pulse       200, the corpus build status

The distinction that holds is not the peer address, it is whether the request came through the
front door: nginx sets X-Forwarded-For on everything it proxies and a process on this box
connecting to the port sets nothing.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import auth                                                               # noqa: E402
import webapp                                                             # noqa: E402


@pytest.fixture()
def app_ctx():
    webapp.app.config["TESTING"] = True
    return webapp.app


def _loopback(app, headers=None, peer="127.0.0.1"):
    with app.test_request_context("/api/designs", headers=headers or {},
                                  environ_base={"REMOTE_ADDR": peer}):
        return auth.is_loopback()


def test_a_process_on_this_box_is_loopback(app_ctx):
    """The draft worker, a cron, a developer with curl. No proxy header, so nothing to doubt."""
    assert _loopback(app_ctx) is True
    assert _loopback(app_ctx, peer="::1") is True


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Real-IP", "X-Forwarded-Proto"])
def test_anything_that_came_through_nginx_is_not(app_ctx, header):
    """This is the whole bug. nginx connects from 127.0.0.1 on behalf of the public."""
    assert _loopback(app_ctx, {header: "203.0.113.9"}) is False


def test_a_forged_header_can_only_take_the_privilege_away(app_ctx):
    """X-Forwarded-For is caller-controlled, so it must never be able to GRANT anything. Here it
    can only remove, which is the safe direction: the worst a forger achieves is locking itself
    out of a privilege it would otherwise have had."""
    assert _loopback(app_ctx, {"X-Forwarded-For": "127.0.0.1"}) is False
    assert _loopback(app_ctx, {"X-Forwarded-For": "127.0.0.1, 127.0.0.1"}) is False


def test_a_real_remote_peer_is_never_loopback(app_ctx):
    assert _loopback(app_ctx, peer="203.0.113.9") is False
    assert _loopback(app_ctx, peer="10.128.0.13") is False, "the VPC is not this box"


def test_the_routes_that_were_open_refuse_a_request_that_came_through_the_proxy(monkeypatch):
    """End to end on the two that were measured open, with the gate on and nobody signed in."""
    webapp.app.config.update(TESTING=True, FORCE_AUTH=True, FORCE_ACCOUNTS=True)
    monkeypatch.setattr(auth, "current_user", lambda: None)
    try:
        c = webapp.app.test_client()
        proxied = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
        for path in ("/api/designs?q=gripper", "/api/factory/pulse"):
            r = c.get(path, headers=proxied)
            assert r.status_code in (401, 403, 404), "%s -> %s" % (path, r.status_code)
    finally:
        for k in ("FORCE_AUTH", "FORCE_ACCOUNTS"):
            webapp.app.config.pop(k, None)


# ------------------------------------------------- the areas withheld from customers

USER = {"id": 71, "email": "a@example.test", "full_name": "A", "is_admin": False,
        "is_active": True, "email_on_completion": True, "session_version": 3}
ADMIN = dict(USER, id=72, is_admin=True)
PROXIED = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
WITHHELD = ["/corpus", "/factory", "/drafts", "/drafts/new", "/api/factory/pulse"]


@pytest.fixture()
def gated(monkeypatch):
    import accounts
    webapp.app.config.update(TESTING=True, FORCE_AUTH=True, FORCE_ACCOUNTS=True)
    monkeypatch.setattr(accounts, "get_user",
                        lambda uid: dict(ADMIN if int(uid) == 72 else USER))
    auth.reset_limits()
    try:
        yield webapp.app
    finally:
        for k in ("FORCE_AUTH", "FORCE_ACCOUNTS"):
            webapp.app.config.pop(k, None)
        auth.reset_limits()


def _as(app, uid):
    c = app.test_client()
    if uid:
        with c.session_transaction() as s:
            s["user_id"] = uid
            s["session_version"] = 3
    return c


@pytest.mark.parametrize("path", WITHHELD)
def test_coverage_and_drafting_are_not_served_to_a_customer(gated, path):
    """Hiding a link hides it from a reader, not from a URL. 404 and not 403, because a customer
    has no business knowing these exist."""
    for uid in (None, 71):
        r = _as(gated, uid).get(path, headers=PROXIED)
        assert r.status_code == 404, "%s as %s -> %s" % (path, uid or "anon", r.status_code)


def test_an_administrator_still_has_them(gated):
    for path in ("/corpus", "/drafts", "/api/factory/pulse"):
        r = _as(gated, 72).get(path, headers=PROXIED)
        assert r.status_code == 200, "%s -> %s" % (path, r.status_code)


def test_the_gate_is_a_prefix_so_a_new_drafting_route_is_covered(gated):
    """Forty-one drafting routes exist and more will be added. The gate keys on the prefix so the
    next one is covered without anybody remembering."""
    assert webapp._is_admin_only("/drafts/9/studio/message")
    assert webapp._is_admin_only("/drafts/9/figure-compiler/start")
    assert webapp._is_admin_only("/corpus")
    #  and it does not overreach into the areas customers are meant to have
    for ok in ("/", "/history", "/patentlookup", "/library", "/report/adhoc-1", "/about",
               "/api/designs", "/draftsmanship"):
        assert not webapp._is_admin_only(ok), ok
