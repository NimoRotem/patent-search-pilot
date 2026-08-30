"""The public shell: a visitor can read what the product is before being asked to sign in.

Before this existed, an anonymous request to the site was redirected straight to a login box.
Somebody deciding whether to upload an unpublished invention to a service has to be able to read
what that service does, what it indexes and what it does not claim, first. These tests keep the
explanation public and the SEARCH gated, which are two different things and easy to conflate.
"""
import re

import pytest

import auth
import webapp


@pytest.fixture()
def anon(monkeypatch):
    """A client with no session and the gate genuinely on.

    The rest of the suite runs with auth off (`TESTING` short-circuits it) so it can drive the
    real handlers. These tests are ABOUT the gate, so they opt in with FORCE_AUTH/FORCE_ACCOUNTS
    and drop the loopback trust, which is what makes a request from the test client anonymous.
    """
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    webapp.app.config["TESTING"] = True
    webapp.app.config["FORCE_AUTH"] = True
    webapp.app.config["FORCE_ACCOUNTS"] = True
    try:
        yield webapp.app.test_client()
    finally:
        webapp.app.config.pop("FORCE_AUTH", None)
        webapp.app.config.pop("FORCE_ACCOUNTS", None)


PUBLIC = ["/", "/about", "/how-it-works", "/login", "/register"]
GATED = ["/history", "/library", "/drafts", "/account"]


@pytest.mark.parametrize("path", PUBLIC)
def test_a_signed_out_visitor_can_read_the_public_pages(anon, path):
    r = anon.get(path)
    assert r.status_code == 200, f"{path} should be readable without an account"


@pytest.mark.parametrize("path", GATED)
def test_the_app_itself_is_still_gated(anon, path):
    r = anon.get(path)
    assert r.status_code in (302, 401), f"{path} must not be public"
    if r.status_code == 302:
        assert "/login" in r.headers.get("Location", "")


def test_the_root_shows_the_landing_page_not_the_search_box_when_signed_out(anon):
    body = anon.get("/").get_data(as_text=True)
    assert "Create a free account" in body
    #  The search form must NOT be served to an anonymous visitor: it would post to a gated
    #  endpoint and fail, and it implies an entitlement they do not have.
    assert 'id="searchform"' not in body
    assert "What is the invention?" not in body


def test_the_landing_page_states_the_corpus_from_the_database_not_a_constant(anon):
    """A hand-written number on a marketing page drifts away from the corpus the day after it is
    written. Everything factual here comes from corpus_facts."""
    body = anon.get("/").get_data(as_text=True)
    assert "publications indexed" in body
    assert "references read in full" in body


def test_every_public_page_offers_a_way_in_and_a_way_to_understand(anon):
    for path in ("/", "/about", "/how-it-works"):
        body = anon.get(path).get_data(as_text=True)
        assert "/register" in body, f"{path} has no sign-up path"
        assert "/how-it-works" in body or path == "/how-it-works"


def test_the_public_pages_do_not_overclaim(anon):
    """The product is a retrieval aid, and every public surface has to say so. This is the one
    piece of copy that is not allowed to be quietly softened."""
    for path in ("/", "/about", "/how-it-works"):
        body = anon.get(path).get_data(as_text=True).lower()
        assert "not a search opinion" in body or "not legal advice" in body, path


def test_how_it_works_describes_the_stage_that_actually_decides_the_order(anon):
    """If the reading stage is ever removed, this page becomes a lie and this test fails."""
    body = anon.get("/how-it-works").get_data(as_text=True)
    assert "read in full" in body
    assert "verbatim quote" in body
    assert re.search(r"refut", body, re.I)


def test_a_signed_in_user_gets_the_search_box_at_the_root(monkeypatch):
    """The landing page is for visitors only: it must never stand between a user and the search."""
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", True)
    webapp.app.config["TESTING"] = True
    body = webapp.app.test_client().get("/").get_data(as_text=True)
    assert 'id="searchform"' in body
    assert "What is the invention?" in body


# ---------------------------------------------------------------------------------------------
# never return the searcher's own patent family as prior art against itself
# ---------------------------------------------------------------------------------------------
def test_the_subject_family_is_dropped_from_the_results(monkeypatch):
    """A DOCDB simple family routinely runs to thirty members across a dozen offices. Excluding
    only the exact publication number let the same invention come back as its own closest prior
    art under a different number: on a real search the #1 result was the US member of the uploaded
    EP patent's family, on every run."""
    import contextlib
    import webapp

    class Cur:
        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return {"fam": "66624664"}

    @contextlib.contextmanager
    def cursor():
        yield Cur()

    monkeypatch.setattr(webapp.db, "cursor", cursor)
    rep = {"query_document": {"publication_number": "EP3707092B1"},
           "ranked_families": ["111", "66624664", "222", "333"]}
    webapp._drop_self_family(rep)
    assert rep["ranked_families"] == ["111", "222", "333"]
    assert rep["self_family_excluded"]["family"] == "66624664"


def test_a_search_with_no_identified_subject_is_untouched(monkeypatch):
    import webapp
    rep = {"ranked_families": ["111", "222"]}
    webapp._drop_self_family(rep)
    assert rep["ranked_families"] == ["111", "222"]
    assert "self_family_excluded" not in rep
