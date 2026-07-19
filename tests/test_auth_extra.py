"""Auth gate + rate limiting + spend caps (src/auth.py).

The default `app_client` fixture runs with TESTING=True, which deliberately disables the gate so
the rest of the suite exercises real handlers. These tests opt the gate back ON via FORCE_AUTH and
turn off the loopback exemption (the Flask test client always presents as 127.0.0.1).
"""
import time
import pytest
import auth
import webapp

PASSWORD = "unit-test-password-8f3a"
GOLD = "grabo_gripper_novelty"


@pytest.fixture()
def secured(monkeypatch):
    """A client with the auth gate actually enforced."""
    webapp.app.config["TESTING"] = True
    webapp.app.config["FORCE_AUTH"] = True
    webapp.app.config["APP_PASSWORD"] = PASSWORD
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)   # test client looks like loopback
    monkeypatch.setattr(auth, "API_TOKEN", "")
    auth.reset_limits()
    try:
        yield webapp.app.test_client()
    finally:
        webapp.app.config.pop("FORCE_AUTH", None)
        webapp.app.config.pop("APP_PASSWORD", None)
        auth.reset_limits()


# ---- the gate ------------------------------------------------------------------------------
def test_healthz_stays_open(secured):
    """Monitoring must keep working without credentials."""
    r = secured.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_html_route_redirects_to_login(secured):
    r = secured.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_expensive_route_blocked_when_anonymous(secured):
    """The whole point: /run must not be reachable anonymously."""
    r = secured.post("/run", data={"query": "a vacuum gripper", "mode": "novelty"})
    assert r.status_code in (302, 401)
    assert "/login" in r.headers.get("Location", "") or r.status_code == 401


def test_api_route_returns_401_json_not_redirect(secured):
    """XHR/EventSource callers need a status code, not an HTML login page."""
    r = secured.get(f"/api/graph/US-11207792-B2")
    assert r.status_code == 401
    assert r.get_json()["error"] == "authentication required"


def test_sse_endpoint_returns_401_when_anonymous(secured):
    r = secured.get("/events/anything", headers={"Accept": "text/event-stream"})
    assert r.status_code == 401


def test_login_then_access_granted(secured):
    assert secured.post("/login", data={"password": PASSWORD}).status_code == 302
    assert secured.get("/").status_code == 200          # session cookie now carried


def test_wrong_password_is_401_and_grants_nothing(secured):
    r = secured.post("/login", data={"password": "wrong"})
    assert r.status_code == 401
    assert secured.get("/").status_code == 302


def test_logout_revokes_session(secured):
    secured.post("/login", data={"password": PASSWORD})
    assert secured.get("/").status_code == 200
    secured.get("/logout")
    assert secured.get("/").status_code == 302


def test_api_token_header_works(secured, monkeypatch):
    monkeypatch.setattr(auth, "API_TOKEN", "tok-abc-123")
    assert secured.get("/api/graph/US-11207792-B2",
                       headers={"Authorization": "Bearer tok-abc-123"}).status_code == 200
    assert secured.get("/api/graph/US-11207792-B2",
                       headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_loopback_exemption_when_enabled(secured, monkeypatch):
    """regression.sh / cron hit 127.0.0.1 directly; port 8631 is VPC-only so this is safe."""
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", True)
    assert secured.get("/").status_code == 200


# ---- open-redirect + prefix safety ----------------------------------------------------------
def test_login_next_is_path_only(secured):
    """An absolute `next` must be refused (open-redirect guard)."""
    r = secured.post("/login?next=https://evil.example/x", data={"password": PASSWORD})
    assert r.status_code == 302
    assert "evil.example" not in r.headers["Location"]


def test_login_redirect_keeps_proxy_prefix(secured):
    """Behind nginx the app is mounted at /patents-data; the post-login redirect must keep it."""
    r = secured.post("/login?next=/report/x", data={"password": PASSWORD},
                     headers={"X-Forwarded-Prefix": "/patents-data"})
    assert r.status_code == 302
    assert "/patents-data/report/x" in r.headers["Location"]


def test_anonymous_redirect_targets_prefixed_login(secured):
    r = secured.get("/", headers={"X-Forwarded-Prefix": "/patents-data"})
    assert r.status_code == 302
    assert "/patents-data/login" in r.headers["Location"]


# ---- rate limiting -------------------------------------------------------------------------
def test_expensive_route_is_rate_limited(secured, monkeypatch):
    secured.post("/login", data={"password": PASSWORD})
    # tiny bucket so the test is fast and deterministic
    monkeypatch.setitem(auth._LIMITERS, "api_graph",
                        auth.Limiter("graph", 0.0, 2, 0.0, 100))
    codes = [secured.get("/api/graph/US-11207792-B2").status_code for _ in range(4)]
    assert codes[:2] == [200, 200]
    assert 429 in codes[2:]
    r = secured.get("/api/graph/US-11207792-B2")
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_global_limit_applies_across_ips(secured, monkeypatch):
    secured.post("/login", data={"password": PASSWORD})
    monkeypatch.setitem(auth._LIMITERS, "api_graph",
                        auth.Limiter("graph", 0.0, 50, 0.0, 2))   # generous per-IP, tight global
    seen = []
    for i in range(4):
        seen.append(secured.get("/api/graph/US-11207792-B2",
                                headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code)
    assert seen.count(429) >= 2          # global bucket exhausted regardless of source IP


def test_cheap_routes_are_not_rate_limited(secured):
    """Report reads / status must never be throttled — only the expensive routes are."""
    secured.post("/login", data={"password": PASSWORD})
    for _ in range(30):
        assert secured.get(f"/status/{GOLD}").status_code == 200
    assert secured.get("/healthz").status_code == 200


def test_token_bucket_refills():
    b = auth.TokenBucket(rate=100.0, burst=1)
    assert b.take()[0] is True
    assert b.take()[0] is False
    time.sleep(0.05)
    assert b.take()[0] is True


# ---- hard caps -----------------------------------------------------------------------------
def test_run_gate_bounds_concurrency(tmp_path):
    g = auth.RunGate(max_concurrent=2, daily_cap=100, state_path=tmp_path / "b.json")
    assert g.try_begin()[0] is True
    assert g.try_begin()[0] is True
    ok, why = g.try_begin()
    assert ok is False and "already running" in why
    g.end()
    assert g.try_begin()[0] is True          # slot freed


def test_run_gate_daily_cap_and_persistence(tmp_path):
    p = tmp_path / "b.json"
    g = auth.RunGate(max_concurrent=10, daily_cap=3, state_path=p)
    for _ in range(3):
        assert g.try_begin()[0] is True
        g.end()
    ok, why = g.try_begin()
    assert ok is False and "Daily search budget" in why
    # a restart must NOT reset the meter
    g2 = auth.RunGate(max_concurrent=10, daily_cap=3, state_path=p)
    assert g2.try_begin()[0] is False


def test_healthz_exposes_budget(app_client):
    j = app_client.get("/healthz").get_json()
    assert "runs" in j and "daily_cap" in j["runs"]
