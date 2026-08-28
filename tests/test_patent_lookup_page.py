"""The register lookup, and the fact that it is a different process behind the same domain.

nginx gives `/patentlookup/` wholly to the lookup engine, a FastAPI app with the adapters for
USPTO ODP, EPO OPS, Google Patents and DPMAregister. That is why the engine, and not this app,
renders the page, and it is why this app once had a second complete lookup UI that no browser had
rendered since the nginx rule was added. That template is gone.

What is pinned here is the seam. The engine authenticates on THIS app's session cookie, which is
same-origin and root-path, so the base URL must stay root-relative. The engine draws THIS app's
masthead, so /api/chrome must keep answering. And the deep link every reference card emits must
keep arriving somewhere that reads it.
"""
import os
import re

import pytest

import webapp

ENGINE = "/home/nimrod_rotem/patent-lookup/app.py"


@pytest.fixture()
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def _engine():
    if not os.path.exists(ENGINE):
        pytest.skip("the lookup engine is not on this box")
    return open(ENGINE, encoding="utf-8").read()


def test_the_route_sends_a_reader_to_the_engine(client):
    """It used to render a page of its own. nginx has been sending that URL elsewhere for weeks."""
    r = client.get("/patentlookup")
    assert r.status_code == 302
    loc = r.headers.get("Location", "")
    assert webapp.LOOKUP_BASE.rstrip("/") + "/" in loc or "login" in loc


def test_the_deep_link_keeps_its_number(client):
    """Every reference card links to /patentlookup?number=..., so the redirect must carry it or
    the reader lands on an empty box and types a number the page was already told."""
    r = client.get("/patentlookup?number=US-11413727-B2")
    assert r.status_code == 302
    assert "number=US-11413727-B2" in r.headers.get("Location", "")


def test_the_engine_reads_that_number():
    """The other half of the same link. The engine never read it, so the link was landing on an
    empty search box: the redirect was right and the destination ignored it."""
    js = _engine()
    assert "URLSearchParams" in js, "the engine ignores its query string"
    assert 'p.get("number")' in js
    assert "run();" in js


def test_the_engine_base_is_root_relative():
    """NOT under this app's prefix and NOT an absolute origin. The engine authenticates on this
    app's session cookie, which is same-origin with path "/"; an absolute URL to another host
    would not carry it, and prefixing it with /patents would 404."""
    assert webapp.LOOKUP_BASE.startswith("/")
    assert not webapp.LOOKUP_BASE.startswith("//")
    assert "://" not in webapp.LOOKUP_BASE
    assert not webapp.LOOKUP_BASE.startswith("/patents/")


def test_there_is_no_second_lookup_ui_in_this_app():
    """289 lines of duplicate that nginx made unreachable. The next person to fix a lookup bug
    would have found this one first."""
    assert not os.path.exists("templates/patentlookup.html")
    assert not os.path.exists("templates/designs.html")


# ------------------------------------------------------------------ the shared masthead

def test_the_masthead_is_one_partial_that_both_apps_use(client):
    """Copying the nav into the engine would put it out of date the first time a link moved, and
    three of them moved in the change that asked for this."""
    base = open("templates/base.html", encoding="utf-8").read()
    assert '{% include "_chrome.html" %}' in base, "base.html no longer uses the partial"
    chrome = open("templates/_chrome.html", encoding="utf-8").read()
    assert "primarynav" in chrome and "IPtorch" in chrome

    r = client.get("/api/chrome")
    assert r.status_code == 200
    d = r.get_json()
    assert "primarynav" in d["html"] and d["css"].endswith(tuple("0123456789abcdef"))
    #  It carries the reader's name and e-mail, so it must never be cached by a shared proxy.
    assert "no-store" in r.headers.get("Cache-Control", "")


def test_the_engine_fetches_it_and_does_not_wait_on_it():
    js = _engine()
    assert "'/api/chrome" in js
    assert "credentials: 'same-origin'" in js
    assert ".catch(" in js, "a failed fetch must leave the lookup usable"
    assert "appchrome" in js
    #  It has to say which page it is, or the masthead arrives with nothing marked current and
    #  reads as a header borrowed from somewhere else.
    assert "active=patent_lookup" in js


def test_the_masthead_lights_the_item_the_caller_names(client):
    """Only a known endpoint name is honoured: the value is a query parameter and it ends up in a
    class attribute."""
    good = client.get("/api/chrome?active=patent_lookup").get_json()["html"]
    assert 'aria-current="page"' in good
    assert re.search(r'class="navitem active"[^>]*aria-current="page"[^>]*>\s*Lookup', good)

    for junk in ("nonsense", 'x" onmouseover="alert(1)', "auth.account"):
        html = client.get("/api/chrome", query_string={"active": junk}).get_json()["html"]
        assert "aria-current" not in html, "an unknown value lit something: %r" % junk
        assert "onmouseover" not in html


def test_the_partial_renders_without_a_page_around_it():
    """base.html sets `endpoint` and `signed_in` before including it; /api/chrome does not."""
    chrome = open("templates/_chrome.html", encoding="utf-8").read()
    assert "endpoint is defined" in chrome and "signed_in is defined" in chrome


# ------------------------------------------------------------------ designs, now a tab

def test_designs_is_a_tab_on_the_lookup_and_not_a_destination(client):
    r = client.get("/designs")
    assert r.status_code == 302
    loc = r.headers.get("Location", "")
    assert "#designs" in loc or "login" in loc
    base = open("templates/base.html", encoding="utf-8").read()
    chrome = open("templates/_chrome.html", encoding="utf-8").read()
    assert ">Designs</a>" not in base + chrome, "still its own nav item"


def test_the_engine_has_the_designs_pane_and_calls_this_app_for_it():
    """The EUIPO image endpoints need this app's OAuth token, so the drawings are proxied through
    it and the rows come from the same place."""
    js = _engine()
    assert "designsPane" in js and "navDesigns" in js
    assert "'/api/designs?q='" in js or "/api/designs?q=" in js
    assert "/api/designs/" in js, "the drawings are not proxied through the search app"


# ------------------------------------------------------------------ the reference card seam

def test_every_reference_card_offers_the_whole_file():
    card = open("templates/_refcard.html", encoding="utf-8").read()
    m = re.search(r'<button[^>]*class="filelink"[^>]*>', card)
    assert m, "no Full file button on the reference card"
    assert 'data-pub="{{ c.pub }}"' in m.group(0), "the button does not carry its publication"


def test_the_panel_asks_before_it_fetches():
    """A deep fetch costs register calls and tens of megabytes. Opening a panel must ask the cheap
    question (GET /api/file) and only POST when a reader clicks."""
    js = open("static/app.js", encoding="utf-8").read()
    i = js.index("THE WHOLE FILE, on any reference card")
    block = js[i:i + 12000]
    assert "'/api/file?number='" in block, "the panel never asks whether the file is already held"
    get_at = block.index("'/api/file?number='")
    post_at = block.index("'/api/file', {method: 'POST'")
    assert "function start(" in block
    assert ".fpgo').addEventListener('click'" in block, "the pull is not behind a click"
    assert get_at != post_at


def test_the_browser_carries_the_session_and_nothing_else():
    """No token is minted or forwarded: the engine reads this app's own cookie."""
    js = open("static/app.js", encoding="utf-8").read()
    i = js.index("THE WHOLE FILE, on any reference card")
    block = js[i:i + 12000]
    assert "credentials = 'same-origin'" in block
    for leak in ("Authorization", "api_key", "apiKey", "token="):
        assert leak not in block, "the card panel is passing %s to the engine" % leak


def test_the_card_still_links_into_the_lookup_by_number():
    js = open("static/app.js", encoding="utf-8").read()
    assert "/patentlookup?number='" in js


def test_the_two_palettes_do_not_repaint_each_other():
    """Both apps declare a light palette on :root and ten of the variable names are the same.

    Whichever stylesheet is parsed last wins, so injecting ours into that page would have turned
    its green accent our blue and moved every line colour. The fix is two-sided and both halves
    have to hold: the engine inserts our stylesheet FIRST so its own :root still wins for its own
    UI, and the masthead carries the palette it needs on #appchrome so it looks like itself
    wherever it is dropped.
    """
    import re

    css = open("static/style.css", encoding="utf-8").read()
    engine = _engine()

    ours = set(re.findall(r"(--[a-z0-9-]+)\s*:",
                          re.search(r":root\s*\{(.*?)\}", css, re.S).group(1)))
    theirs = set(re.findall(r"(--[a-z0-9-]+)\s*:",
                            re.search(r":root\{(.*?)\}", engine, re.S).group(1)))
    clash = ours & theirs
    assert clash, "the premise changed: if nothing collides, say so and delete this test"

    #  Half one: it goes in first, so their :root is parsed after ours and wins for their page.
    assert "insertBefore(l, document.head.firstChild)" in engine, (
        "our stylesheet is being appended again, which repaints the whole lookup")
    assert "appendChild(l)" not in engine

    #  Half two: every colliding name is re-declared on the masthead itself.
    block = re.search(r"#appchrome\{(.*?)\}", css, re.S)
    assert block, "the masthead declares no palette of its own"
    declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", block.group(1)))
    missing = sorted(clash - declared)
    assert not missing, "the masthead would take the lookup's colours for: %s" % missing
