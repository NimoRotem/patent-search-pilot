"""Publishing a report: what a stranger may see, what they may not, and what gets recorded.

Two properties matter more than the rest and both are easy to lose in a refactor:

  * an UNPUBLISHED slug and a nonexistent one must be indistinguishable from outside. If a
    published slug 404s and an unpublished one 403s, the difference is a directory of which
    reports exist.
  * a published page must carry NONE of the application. The recipient has no account and cannot
    get one, so every control that leads somewhere they cannot go is a broken promise — and the
    page has to be right when the OWNER opens their own link while signed in, which is exactly how
    it will first be tested by a human.
"""
import re

import pytest

import auth
import public_report as PR
import webapp

SLUG = "unit-public-report-slug"


@pytest.fixture()
def anon(monkeypatch):
    """No session, gate genuinely on — the state a recipient of a link is actually in."""
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    webapp.app.config["TESTING"] = True
    webapp.app.config["FORCE_AUTH"] = True
    webapp.app.config["FORCE_ACCOUNTS"] = True
    try:
        yield webapp.app.test_client()
    finally:
        webapp.app.config.pop("FORCE_AUTH", None)
        webapp.app.config.pop("FORCE_ACCOUNTS", None)


# ---------------------------------------------------------------------------
# the guard, which is the whole security model
# ---------------------------------------------------------------------------
def test_an_unpublished_slug_is_not_distinguishable_from_a_missing_one(anon, monkeypatch):
    """A slug in a URL is guessable where a 32-byte token is not, so an unpublished report must
    404 exactly like one that never existed. Anything else is a directory of what we hold."""
    monkeypatch.setattr(PR, "get", lambda slug: {})
    a = anon.get(f"/public-report/{SLUG}")
    b = anon.get("/public-report/no-such-report-at-all")
    assert a.status_code == b.status_code == 404


def test_a_revoked_link_stops_working_immediately(anon, monkeypatch):
    """`get` returns nothing for a revoked row, so revocation and never-published are one case
    downstream. The visit log is deliberately NOT deleted with it."""
    monkeypatch.setattr(PR, "get", lambda slug: {})
    assert anon.get(f"/public-report/{SLUG}").status_code == 404


def test_the_public_endpoints_are_open_and_the_owner_ones_are_not():
    """The global gate allow-lists by endpoint NAME. When it did not know these, every public link
    302'd its recipient to a login they can never satisfy — the one thing a shared document must
    not do. And the inverse is just as important: publishing and the viewer log are the owner's."""
    assert "public_report_page" in auth._OPEN_ENDPOINTS
    assert "public_report_unlock" in auth._OPEN_ENDPOINTS
    assert "public_report_beacon" in auth._OPEN_ENDPOINTS
    assert "report_publish" not in auth._OPEN_ENDPOINTS
    assert "report_visitors" not in auth._OPEN_ENDPOINTS


def test_the_password_gate_does_not_serve_the_report(anon, monkeypatch):
    """A 401 that ships the document in the body is not a gate. Measured against the real page:
    2.4 kB of prompt versus 2.6 MB of report."""
    monkeypatch.setattr(PR, "get", lambda slug: {"slug": SLUG, "user_id": 1,
                                                 "password_hash": "x", "title": "t"})
    r = anon.get(f"/public-report/{SLUG}")
    assert r.status_code == 401
    body = r.get_data(as_text=True)
    assert "password protected" in body
    assert "refcard" not in body and len(body) < 20000


def test_a_wrong_password_re_renders_the_gate(anon, monkeypatch):
    monkeypatch.setattr(PR, "get", lambda slug: {"slug": SLUG, "user_id": 1,
                                                 "password_hash": "x", "title": "t"})
    monkeypatch.setattr(PR, "check_password", lambda slug, pw: False)
    r = anon.post(f"/public-report/{SLUG}/unlock", data={"password": "nope"})
    assert r.status_code == 401
    assert "did not match" in r.get_data(as_text=True)


def test_unlocking_one_report_does_not_unlock_another(anon, monkeypatch):
    """The session key is per slug. A visitor who was given one password must not thereby hold
    every published report on the instance."""
    monkeypatch.setattr(PR, "get", lambda slug: {"slug": slug, "user_id": 1,
                                                 "password_hash": "x", "title": "t"})
    monkeypatch.setattr(PR, "check_password", lambda slug, pw: True)
    anon.post(f"/public-report/{SLUG}/unlock", data={"password": "right"})
    other = anon.get("/public-report/some-other-slug")
    assert other.status_code == 401, "a second report opened without its own password"


# ---------------------------------------------------------------------------
# storage behaviour that a UI cannot show you is wrong
# ---------------------------------------------------------------------------
def test_the_beacon_only_ever_raises_the_recorded_maximum(monkeypatch):
    """A heartbeat fires while the page is open and a final beacon on pagehide — and that final
    delivery is exactly the one most likely to be lost. A later, smaller number must never
    overwrite an earlier, larger one, or a closed laptop resets a real reading to zero."""
    sql = {}

    class _Cur:
        rowcount = 1

        def execute(self, q, args=None):
            sql["q"] = q
            sql["args"] = args

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import contextlib
    monkeypatch.setattr(PR.db, "cursor", lambda *a, **k: contextlib.nullcontext(_Cur()))
    PR.record_beacon("k", {"seconds_on_page": 12, "max_scroll_pct": 40})
    assert "GREATEST(seconds_on_page" in sql["q"]
    assert "GREATEST(max_scroll_pct" in sql["q"]


def test_the_beacon_accepts_only_known_client_fields():
    """This endpoint takes JSON from anybody holding the link and writes it into a column an owner
    will read. An allow-list, and every value bounded."""
    got = PR._clean_client({"screen_w": 1920, "timezone": "Europe/London",
                            "evil": "<script>", "user_agent": "x" * 5000,
                            "languages": ["en"] * 99})
    assert "evil" not in got
    assert got["screen_w"] == 1920 and got["timezone"] == "Europe/London"
    assert len(got["user_agent"]) <= 300
    assert len(got["languages"]) <= 20


def test_a_nonsense_duration_cannot_be_recorded(monkeypatch):
    """Bounded, because the value comes from the page and lands in a total the owner reads."""
    seen = {}

    class _Cur:
        rowcount = 1

        def execute(self, q, args=None):
            seen["args"] = args

    import contextlib
    monkeypatch.setattr(PR.db, "cursor", lambda *a, **k: contextlib.nullcontext(_Cur()))
    PR.record_beacon("k", {"seconds_on_page": 10 ** 9, "max_scroll_pct": 4000})
    assert seen["args"][1] <= 86400
    assert seen["args"][2] <= 100


def test_automated_fetches_are_labelled_not_counted_as_readings():
    """A link pasted into a chat app is unfurled before any human sees it. Counting that as a
    reading reports an audience that was never there."""
    assert PR.looks_automated({"user_agent": "curl/7.74.0"})
    assert PR.looks_automated({"user_agent": "Mozilla/5.0 facebookexternalhit/1.1"})
    assert PR.looks_automated({"user_agent": "Mozilla/5.0", "client": {"webdriver": True},
                               "beacon_ok": True, "seconds_on_page": 5})
    assert PR.looks_automated({"user_agent": "Mozilla/5.0", "beacon_ok": False,
                               "seconds_on_page": 0})
    assert not PR.looks_automated({"user_agent": "Mozilla/5.0 Chrome/141", "beacon_ok": True,
                                   "seconds_on_page": 47, "client": {}})


def test_the_summary_separates_people_from_machines():
    rows = [{"user_agent": "Mozilla/5.0 Chrome", "beacon_ok": True, "seconds_on_page": 40,
             "ip": "1.2.3.4", "client": {}, "automated": False},
            {"user_agent": "curl/8", "beacon_ok": False, "seconds_on_page": 0, "ip": "9.9.9.9",
             "client": {}, "automated": True}]
    s = PR.summary(rows)
    assert s["views"] == 2 and s["human_views"] == 1 and s["automated_views"] == 1
    assert s["distinct_ips"] == 1                     # the machine's address is not a reader
    assert s["median_seconds"] == 40


def test_the_forwarded_address_is_not_trusted_on_its_own(monkeypatch):
    """X-Forwarded-For is entirely forgeable. auth.client_ip already decides when to believe it;
    two copies of that rule would be one too many, because the rate limiter and this log have to
    agree about who a caller is."""
    import auth as A
    monkeypatch.setattr(A, "client_ip", lambda: "203.0.113.9")

    class _Req:
        headers = {"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}
        remote_addr = "10.0.0.1"

    ip, chain = PR.client_ip(_Req())
    assert ip == "203.0.113.9"                        # from auth, not from the header
    assert chain == "1.1.1.1, 2.2.2.2"                # kept verbatim for the record


# ---------------------------------------------------------------------------
# the page itself
# ---------------------------------------------------------------------------
OWNER_ONLY = ("Draft US application", ">Report details<", "Save report", ">Rename report<",
              ">Re-run<", ">Print / PDF view<", ">Download ZIP<", 'id="publishBtn"',
              ">Sign out<", ">History<", ">Library<", ">Drafting<", ">Admin<", "accountemail")


def test_the_public_layout_carries_none_of_the_application():
    """base_public.html is a separate skeleton on purpose. base.html's nav is driven by
    current_user, so gating it with flags would show the whole application to a stranger the
    moment somebody adds a nav item without thinking about this page."""
    src = open("templates/base_public.html").read()
    for bad in (">History<", ">Library<", ">Drafting<", ">Admin<", ">Sign out<",
                "accountnav", "primarynav"):
        assert bad not in src, bad
    assert "noindex" in src                           # a client document, not something to index


def test_the_report_body_is_one_template_for_both_audiences():
    """The owner's page and the published page must render the SAME evidence. A second template
    is a second thing to keep true."""
    src = open("templates/report.html").read()
    assert '{% extends layout|default("base.html") %}' in src


def test_every_owner_control_is_gated_in_the_report_template():
    """Each of these was found unguarded at some point: Re-run and the archive ZIP were still open
    after the token share shipped, and the save/rename script was still being sent to readers."""
    src = open("templates/report.html").read()
    for needle in ("Re-run</a>", "Download ZIP", "Rename report", "Draft US application"):
        i = src.find(needle)
        assert i > 0, needle
        assert "read_only" in src[max(0, i - 900):i], f"{needle} is not behind read_only"


def test_a_second_owner_is_told_why_rather_than_shown_a_404(monkeypatch):
    """The caller reached the publish route through the access check, so they CAN see this report.
    Answering 404 would tell them their own report does not exist. One link per report, owned by
    whoever published it first, and the second person is told that."""
    class _Cur:
        def execute(self, q, args=None):
            self._q = q

        def fetchone(self):
            return {"user_id": 999, "slug": SLUG}

    import contextlib
    monkeypatch.setattr(PR, "ensure_schema", lambda: None)
    monkeypatch.setattr(PR.db, "cursor", lambda *a, **k: contextlib.nullcontext(_Cur()))
    out = PR.publish(1, SLUG)
    assert out.get("error") == "already_published_by_another_user"
