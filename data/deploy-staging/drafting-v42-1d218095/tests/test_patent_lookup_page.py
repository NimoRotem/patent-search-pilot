"""The register lookup, inside the search app.

rotem.ai/patentlookup is a separate FastAPI app with the adapters for USPTO ODP, EPO OPS, Google
Patents and DPMAregister, a pacer for OPS's burst detection, and a document store. It already
authenticates on THIS app's session cookie and already serves another app on the domain the same
way. So the integration is a page whose JavaScript calls that API, a nav link, and one base URL —
no engine code moved and no adapter was reimplemented.

These tests pin the three things that would silently break it: the base URL being absolute or
prefixed (the shared cookie is same-origin and root-path), the button disappearing from the card,
and the page opening a deep fetch nobody asked for.
"""
import re

import pytest

import webapp


@pytest.fixture()
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def test_the_page_is_served_under_this_app(client):
    r = client.get("/patentlookup")
    assert r.status_code in (200, 302)
    if r.status_code == 302:
        assert "login" in r.headers.get("Location", "")


def test_the_engine_base_is_root_relative():
    """NOT under this app's prefix and NOT an absolute origin. The engine authenticates on this
    app's session cookie, which is same-origin with path "/"; an absolute URL to another host
    would not carry it, and prefixing it with /patents would 404."""
    assert webapp.LOOKUP_BASE.startswith("/")
    assert not webapp.LOOKUP_BASE.startswith("//")
    assert "://" not in webapp.LOOKUP_BASE
    assert not webapp.LOOKUP_BASE.startswith("/patents/")


def test_every_template_can_see_it():
    src = open(webapp.__file__.replace(".pyc", ".py")).read()
    assert "@app.context_processor" in src and "lookup_base" in src
    base = open("templates/base.html").read()
    assert "window.LOOKUP_BASE" in base, "the browser cannot find the engine"
    assert "lookup_base|tojson" in base, "the base URL is hard-coded in the template"


def test_the_nav_links_to_it():
    base = open("templates/base.html").read()
    assert "/patentlookup" in base
    assert "endpoint == 'patent_lookup'" in base, "the nav item never shows as active"


def test_every_reference_card_offers_the_whole_file():
    card = open("templates/_refcard.html").read()
    m = re.search(r'<button[^>]*class="filelink"[^>]*>', card)
    assert m, "no Full file button on the reference card"
    assert 'data-pub="{{ c.pub }}"' in m.group(0), "the button does not carry its publication"


def test_the_panel_asks_before_it_fetches():
    """A deep fetch costs register calls and tens of megabytes. Opening a panel must ask the cheap
    question (GET /api/file) and only POST when a reader clicks."""
    js = open("static/app.js").read()
    i = js.index("THE WHOLE FILE, on any reference card")
    block = js[i:i + 12000]
    assert "'/api/file?number='" in block, "the panel never asks whether the file is already held"
    get_at = block.index("'/api/file?number='")
    post_at = block.index("'/api/file', {method: 'POST'")
    assert "function start(" in block
    #  the POST lives in start(), which is only reached from a click handler
    assert ".fpgo').addEventListener('click'" in block, "the pull is not behind a click"
    assert get_at != post_at


def test_the_browser_carries_the_session_and_nothing_else():
    """No token is minted or forwarded: the engine reads this app's own cookie."""
    js = open("static/app.js").read()
    i = js.index("THE WHOLE FILE, on any reference card")
    block = js[i:i + 12000]
    assert "credentials = 'same-origin'" in block
    for leak in ("Authorization", "api_key", "apiKey", "token="):
        assert leak not in block, "the card panel is passing %s to the engine" % leak
    page = open("templates/patentlookup.html").read()
    assert "credentials = 'same-origin'" in page
    for leak in ("Authorization", "api_key", "apiKey"):
        assert leak not in page


def test_the_lookup_page_deep_links_by_number():
    """The card's "open in Lookup" and any external link both arrive as ?number=..."""
    page = open("templates/patentlookup.html").read()
    assert "URLSearchParams(location.search).get('number')" in page
    js = open("static/app.js").read()
    assert "/patentlookup?number='" in js
