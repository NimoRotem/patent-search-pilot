"""Two links on every report pointed somewhere a browser cannot follow.

REPORTED 2026-08-23: "drawings unavailable on all rows" and a dead PDF link under Documents read.
Neither was a missing file. Both were URLs that answer 200 to us and something else to a browser.

**The drawings.** The reference-drawing server is a route in this app at `/figures/<pub>/<file>`.
The figure compiler is a SEPARATE app, and nginx mounts it on this host as

    location ^~ /figures/ { proxy_pass http://patents_figures/; }   # 127.0.0.1:8637

`^~` beats every regex location in nginx, so once that mount existed no pattern could route the
reference drawings back here: every one of them reached the compiler, which answered 302 to its own
login, and a browser renders a redirected <img> as broken. Measured on adhoc-66bbfcfff0bc: 59 of 60
cards had a figure resolved server-side with the file on disk, and the page showed none of them.
The app's own prefix moved to `/refdrawing/`, which nothing else claims.

**The Office PDFs.** "Documents read" linked USPTO ODP's `downloadUrl` directly. That endpoint
authenticates with an `X-API-KEY` HEADER, which a link cannot carry, so every one was a 403.
Measured: `https://api.uspto.gov/api/v1/download/applications/19318450/MKYA6UIOX137X95.pdf` -> 403.
They are proxied through `/odp-document/...` now, which attaches the key.
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


# --------------------------------------------------------------------------- reference drawings

def test_nothing_emits_a_drawing_url_under_the_compilers_prefix():
    """The guard. A future edit that writes `/figures/<pub>/...` back into a template or the JS
    puts every drawing behind the figure compiler's login again, and the page will look exactly as
    though the corpus has no drawings."""
    #  Matches URL CONSTRUCTION, not prose: the comments in these files explain the collision and
    #  have to be free to name the old prefix. A line only counts when it both mentions the prefix
    #  and is building an address with it.
    builds = re.compile(r"""(src\s*=|href\s*=|script_root|PUBLIC_BASE_URL|B\s*\+|f["'])""")
    offenders = []
    for path in ("templates/_refcard.html", "templates/print.html", "static/app.js",
                 "src/report_archive.py"):
        for line in _read(*path.split("/")).splitlines():
            if "/figures/" not in line or "drafts/" in line:
                continue
            if builds.search(line):
                offenders.append("%s: %s" % (path, line.strip()[:110]))
    assert not offenders, (
        "these build a drawing URL under /figures/, which nginx gives to the figure compiler:\n"
        + "\n".join(offenders))


def test_the_app_serves_reference_drawings_from_its_own_prefix():
    import webapp
    assert webapp.REFDRAW_PREFIX == "/refdrawing"
    rules = {str(r) for r in webapp.app.url_map.iter_rules()}
    assert "/refdrawing/<pub>/<path:fname>" in rules
    #  The old path stays registered so anything already holding one keeps working from inside the
    #  network, where nginx is not in the way.
    assert "/figures/<pub>/<path:fname>" in rules


def test_the_producers_agree_on_the_prefix():
    for path in ("templates/_refcard.html", "templates/print.html"):
        assert "/refdrawing/" in _read(*path.split("/")), path
    assert "'/refdrawing/'" in _read("static", "app.js")
    assert "/refdrawing/" in _read("src", "report_archive.py")


# --------------------------------------------------------------------------- office documents

def test_an_odp_download_url_is_rewritten_to_our_proxy():
    import webview
    href = webview._odp_pdf_href(
        {"pdf": "https://api.uspto.gov/api/v1/download/applications/19318450/MKYA6UIOX137X95.pdf"})
    assert href == "/odp-document/19318450/MKYA6UIOX137X95.pdf"


@pytest.mark.parametrize("bad", [
    "", None, "https://example.com/whatever.pdf", "https://api.uspto.gov/api/v1/download/x",
    "https://api.uspto.gov/api/v1/download/applications/../../etc/passwd.pdf",
])
def test_a_url_we_cannot_parse_yields_no_link_rather_than_a_broken_one(bad):
    import webview
    assert webview._odp_pdf_href({"pdf": bad}) == ""


def test_the_template_links_the_proxy_and_never_the_raw_url():
    report = _read("templates", "report.html")
    assert "d.pdf_href" in report
    assert 'href="{{ d.pdf }}"' not in report, (
        "the report links ODP's own downloadUrl again, which is a 403 in a browser")


def test_the_view_attaches_a_href_to_every_document():
    import webview
    v = webview.build_prosecution_view({"prosecution": {"mined": {"documents": [
        {"app": "19318450", "description": "Non-Final Rejection", "date": "2026-01-02",
         "pdf": "https://api.uspto.gov/api/v1/download/applications/19318450/AAA111.pdf"},
        {"app": "19318450", "description": "Notice of References Cited", "date": "2026-01-02",
         "pdf": ""},
    ], "applied": [{"pub": "US-1-A"}]}}})
    hrefs = [d.get("pdf_href") for d in v["documents"]]
    assert hrefs == ["/odp-document/19318450/AAA111.pdf", ""]
    #  and the raw url is still on the record, because the fetcher needs it
    assert v["documents"][0]["pdf"].startswith("https://api.uspto.gov/")


def test_the_proxy_route_refuses_a_crafted_identifier():
    import webapp
    c = webapp.app.test_client()
    for bad in ("..%2f..%2fetc%2fpasswd", "a/b"):
        r = c.get("/odp-document/%s/x.pdf" % bad)
        assert r.status_code in (404, 308), (bad, r.status_code)
