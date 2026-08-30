"""Client-IP attribution behind the reverse proxy (src/auth.py client_ip).

nginx fronts this app with `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`, which
APPENDS the real peer to whatever the client already sent. So for a request that arrives with a
forged header, nginx hands us:

    X-Forwarded-For: <attacker-controlled>, <real client IP>

The trustworthy element is therefore the LAST one (the hop our own proxy appended), not the first.
Reading element [0] meant every per-IP bucket was keyed on a value the attacker chose, so login
brute-force protection could be reset at will by rotating one header.
"""
import pytest
import accounts
import auth
import webapp

PASSWORD = "unit-test-password-8f3a"
EMAIL = "proxy-auth@example.test"
USER = {"id": 902, "email": EMAIL, "full_name": "Proxy Auth", "is_admin": False,
        "is_active": True, "session_version": 1}
PROXY = "10.128.0.7"          # nginx's VPC address: a trusted peer
REAL = "198.51.100.77"        # the actual attacker, appended by nginx


@pytest.fixture()
def secured(monkeypatch):
    webapp.app.config["TESTING"] = True
    webapp.app.config["FORCE_AUTH"] = True
    webapp.app.config["FORCE_ACCOUNTS"] = True
    monkeypatch.setattr(accounts, "authenticate", lambda email, password:
                        dict(USER) if email == EMAIL and password == PASSWORD else None)
    monkeypatch.setattr(accounts, "get_user", lambda uid:
                        dict(USER) if int(uid) == USER["id"] else None)
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    monkeypatch.setattr(auth, "API_TOKEN", "")
    auth.reset_limits()
    try:
        yield webapp.app.test_client()
    finally:
        webapp.app.config.pop("FORCE_AUTH", None)
        webapp.app.config.pop("FORCE_ACCOUNTS", None)
        auth.reset_limits()


def _login(client, spoof, real=REAL):
    """One wrong-password POST carrying an nginx-shaped XFF chain."""
    return client.post(
        "/login",
        data={"email": EMAIL, "password": "wrong"},
        environ_overrides={"REMOTE_ADDR": PROXY},
        headers={"X-Forwarded-For": f"{spoof}, {real}"},
    )


# ---- client_ip attribution ------------------------------------------------------------------
def test_client_ip_uses_the_hop_our_proxy_appended(secured):
    with webapp.app.test_request_context(
        "/", environ_overrides={"REMOTE_ADDR": PROXY},
        headers={"X-Forwarded-For": f"203.0.113.9, {REAL}"}):
        assert auth.client_ip() == REAL


def test_client_ip_ignores_forged_leading_entries(secured):
    """A whole forged chain must not shift attribution off the real client."""
    with webapp.app.test_request_context(
        "/", environ_overrides={"REMOTE_ADDR": PROXY},
        headers={"X-Forwarded-For": f"1.1.1.1, 2.2.2.2, 3.3.3.3, {REAL}"}):
        assert auth.client_ip() == REAL


def test_client_ip_falls_back_to_peer_without_xff(secured):
    with webapp.app.test_request_context("/", environ_overrides={"REMOTE_ADDR": PROXY}):
        assert auth.client_ip() == PROXY


def test_untrusted_peer_xff_is_never_believed(secured):
    """A direct (non-proxy) caller cannot dictate its own identity."""
    with webapp.app.test_request_context(
        "/", environ_overrides={"REMOTE_ADDR": "203.0.113.5"},
        headers={"X-Forwarded-For": "10.0.0.1, 8.8.8.8"}):
        assert auth.client_ip() == "203.0.113.5"


# ---- the actual bypass ----------------------------------------------------------------------
def test_rotating_xff_cannot_refresh_the_login_bucket(secured):
    """THE REPRO: lock the real IP out, then rotate the forged leading entry.

    Before the fix each rotation minted a brand-new per-IP bucket, so the attacker got another
    full burst of guesses (measured: 14 rotations -> 14 fresh 401s). After it, every request is
    still attributed to REAL and stays 429.
    """
    burst = int(auth._LIMITERS["auth.login"].burst)

    # burn the real client's bucket
    for _ in range(burst):
        _login(secured, "203.0.113.1")
    assert _login(secured, "203.0.113.1").status_code == 429, "per-IP bucket should be empty"

    # now rotate the attacker-controlled leading entry
    codes = [_login(secured, f"203.0.113.{n}").status_code for n in range(20, 34)]
    assert all(c == 429 for c in codes), (
        f"XFF rotation bypassed the per-IP limit: {codes.count(401)} of {len(codes)} "
        "attempts got a fresh guess")


def test_distinct_real_clients_still_get_their_own_buckets(secured):
    """The fix must not collapse everyone into one bucket -- that would be its own DoS."""
    burst = int(auth._LIMITERS["auth.login"].burst)
    for _ in range(burst):
        _login(secured, "203.0.113.1", real="198.51.100.10")
    assert _login(secured, "203.0.113.1", real="198.51.100.10").status_code == 429
    # a different real client is unaffected
    assert _login(secured, "203.0.113.1", real="198.51.100.11").status_code == 401


# ---- the global backstop must not be a login DoS ---------------------------------------------
def test_flood_from_one_source_cannot_lock_out_a_known_good_user(secured):
    """A previously-successful IP keeps being able to log in while someone else floods.

    The old global bucket (burst 30, 1 per 30 s) meant ~30 requests from anywhere denied logins to
    EVERYONE for ~15 minutes -- a cheaper attack than the brute-force it defended against.
    """
    lim = auth._LIMITERS["auth.login"]
    good = "198.51.100.200"

    # the real user logs in once, successfully
    r = secured.post("/login", data={"email": EMAIL, "password": PASSWORD},
                     environ_overrides={"REMOTE_ADDR": PROXY},
                     headers={"X-Forwarded-For": f"203.0.113.1, {good}"})
    assert r.status_code == 302, "correct password should sign in"
    assert lim._is_known_good(good)

    # drain the global bucket from a spread of other sources
    lim.global_bucket.tokens = 0.0

    # the known-good user is still served (their own per-IP bucket still applies)
    r = secured.post("/login", data={"email": EMAIL, "password": PASSWORD},
                     environ_overrides={"REMOTE_ADDR": PROXY},
                     headers={"X-Forwarded-For": f"203.0.113.1, {good}"})
    assert r.status_code == 302, "global backstop locked out the legitimate user"

    # an unknown IP is still shed by the backstop
    r = secured.post("/login", data={"email": EMAIL, "password": "wrong"},
                     environ_overrides={"REMOTE_ADDR": PROXY},
                     headers={"X-Forwarded-For": f"203.0.113.1, 198.51.100.201"})
    assert r.status_code == 429, "global backstop should still shed unknown floods"


def test_known_good_still_bounded_by_its_own_per_ip_bucket(secured):
    """The exemption is only from the GLOBAL bucket -- it is not a free pass."""
    lim = auth._LIMITERS["auth.login"]
    good = "198.51.100.210"
    lim.mark_known_good(good)
    for _ in range(int(lim.burst)):
        _login(secured, "203.0.113.1", real=good)
    assert _login(secured, "203.0.113.1", real=good).status_code == 429
