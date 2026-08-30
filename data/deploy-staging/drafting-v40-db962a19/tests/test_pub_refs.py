"""A reader of a shared report can see what the report is made of.

Reported 2026-08-19: figures present on the owner's page were missing from the password-protected
public link. /api/figs is disk-only and ungated, so a figure only appears once something has
DOWNLOADED it, and the endpoints that do the downloading were owner-only. The owner's own browsing
filled the cache, so the page looked complete to the one person who could not see the bug.
"""
import webapp


def _pub(monkeypatch, published=True, needs_pw=False):
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: False)
    monkeypatch.setattr(webapp.public_report, "get", lambda slug: {"published": published})
    monkeypatch.setattr(webapp.public_report, "needs_password", lambda slug: needs_pw)


def test_a_public_reader_may_fetch_the_reference_cards(monkeypatch):
    _pub(monkeypatch)
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    r = c.get("/api/ref-batch/adhoc-xyz?pubs=US-1-A")
    assert r.status_code != 404, "a shared report cannot render its cards"


def test_a_stranger_still_cannot(monkeypatch):
    _pub(monkeypatch, published=False)
    webapp.app.config["TESTING"] = True
    r = webapp.app.test_client().get("/api/ref-batch/adhoc-xyz?pubs=US-1-A")
    assert r.status_code == 404


def test_a_password_protected_link_needs_the_password_first(monkeypatch):
    _pub(monkeypatch, needs_pw=True)
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    assert c.get("/api/ref-batch/adhoc-xyz?pubs=US-1-A").status_code == 404
    with c.session_transaction() as sess:
        sess["public_unlocked"] = ["adhoc-xyz"]
    assert c.get("/api/ref-batch/adhoc-xyz?pubs=US-1-A").status_code != 404


def test_the_figure_route_stays_open_and_traversal_proof():
    """It serves only files already on disk under a canonical key, so it needs no account —
    but it must still refuse a crafted name."""
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    for bad in ("../../etc/passwd", "..%2f..%2fpasswd"):
        assert c.get("/figures/US-1-A/%s" % bad, follow_redirects=True).status_code == 404


def test_the_gate_helper_is_the_report_wide_one():
    assert hasattr(webapp, "_may_read_report")
    assert not hasattr(webapp, "_concise_may_read"), "the old narrow name should be gone"
