"""API endpoint tests via the Flask test client: status codes + shape + edge cases (no 500s)."""
import json
import pytest

GOLD = "grabo_gripper_novelty"
PUB = "US-11207792-B2"


def test_healthz(app_client):
    r = app_client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_home_is_just_the_search(app_client):
    """The search page is the search field.

    The example chips and the gold-set grid moved to /history and the scope wall moved to /about,
    so this now asserts the ABSENCE of that furniture as well as the presence of the input --
    otherwise the page could silently regrow a brochure above the box and still pass.
    """
    r = app_client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "invention" in html.lower()             # free-text box
    assert "Rotem Patents" in html                 # masthead / title
    assert "exchip" not in html                    # example chips are gone
    assert GOLD not in html                        # gold grid moved to /history
    assert "Search scope and measured reliability" not in html   # wall moved to /about
    assert 'name="wide"' not in html               # federation is unconditional, no checkbox
    assert "/about" in html                        # one compact line links to the relocated content
    assert "First matches" in html                 # distinguish useful partials from final refinement
    assert "pageshow" in html                      # BFCache restore re-enables the submit button


def test_about_holds_the_relocated_disclosure(app_client):
    r = app_client.get("/about")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Search scope and measured reliability" in html
    assert "Absence of results is not evidence of absence" in html
    assert "What is and is not indexed" in html


def test_history_lists_examples_and_past_searches(app_client):
    r = app_client.get("/history")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert GOLD in html                            # gold set is here now
    assert "Worked examples" in html               # ...and labelled as examples, not history
    assert "Your searches" in html


def test_the_scope_disclosure_is_not_on_the_report(app_client):
    """REVERSED ON REQUEST, 2026-08-23, and recorded here rather than deleted.

    This used to assert the opposite: the scope-and-reliability block was repeated at the foot of
    every report so a thin result set could not be read as a clear field. Two things changed. Every
    search now fans out to ten external databases, so "one indexed field, measured recall well
    below completeness" had stopped describing what a search does; and the block sat under the
    reader's OWN result, where a recall figure and three audit bases read as a disclaimer on their
    findings rather than as the scope of the tool.

    The disclosure itself is unchanged and is still rendered IN FULL where somebody goes to ask
    what the tool covers, which the next assertions hold shut. Removing it there too would be a
    different decision and nobody has made it.
    """
    html = app_client.get(f"/report/{GOLD}").get_data(as_text=True)
    assert "Search scope and measured reliability" not in html
    assert "Absence of results is not evidence of absence" not in html
    assert "Outside indexed field" not in html

    body = app_client.get("/about").get_data(as_text=True)
    assert "Search scope and measured reliability" in body
    assert "Absence of results is not evidence of absence" in body


def test_gold_report_renders(app_client):
    r = app_client.get(f"/report/{GOLD}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    #  The grid, by its own heading. This used to look for the words "claim chart", which reached
    #  the page only through the scope block's "rationales and claim charts are drafting aids"
    #  sentence, so it was passing on the disclaimer rather than on the grid being rendered.
    assert ("Claim × prior-art grid" in html) or ("Element × reference grid" in html)
    assert html.count('class="refcard"') >= 10


def test_api_ref_shape(app_client):
    r = app_client.get(f"/api/ref/{PUB}?slug={GOLD}")
    assert r.status_code == 200
    j = r.get_json()
    assert j["pub"] == PUB
    assert "display" in j and "sections" in j


def test_api_graph_shape(app_client):
    r = app_client.get(f"/api/graph/{PUB}")
    assert r.status_code == 200
    j = r.get_json()
    for k in ("backward", "forward", "similar"):
        assert k in j and isinstance(j[k], list)


def test_pdf_serves_and_missing_is_404(app_client):
    assert app_client.get(f"/pdf/{PUB}").status_code in (200, 302)  # cached local or remote redirect
    assert app_client.get("/pdf/JUNK-9").status_code == 404


def test_compare_and_print(app_client):
    assert app_client.get(f"/compare?slug={GOLD}&pubs=US-3005652-A,{PUB}").status_code == 200
    assert app_client.get(f"/print/{GOLD}").status_code == 200


def test_compare_uses_cached_metadata_for_a_federated_only_reference():
    import webapp

    b = webapp._compare_biblio(None, None, "US20220380181A1", {
        "pub": "US-20220380181-A1",
        "country": "US",
        "title": "Modular power pack for a vacuum pad lifter",
        "assignees": ["Vacuworx Global LLC"],
        "publication_date": "2022-12-01",
    })
    assert b["pub"] == "US-20220380181-A1"
    assert b["title"].startswith("Modular power pack")
    assert b["assignees"] == ["Vacuworx Global LLC"]
    assert b["flag"] == "🇺🇸"


def test_export_pdf_and_docx(app_client):
    for fmt, magic in (("pdf", b"%PDF-"), ("docx", b"PK")):
        r = app_client.post("/export", data={"slug": GOLD,
                            "pubs": "US-3005652-A,US-11207792-B2,US-9457478-B2", "format": fmt})
        assert r.status_code == 200
        assert r.data[:5].startswith(magic) or r.data[:2] == magic[:2]
        assert len(r.data) > 20000


def test_flags_persist(app_client):
    app_client.post(f"/api/flags/{GOLD}", json={"pub": "US-9457478-B2", "flag": "maybe", "note": "unit"})
    j = app_client.get(f"/api/flags/{GOLD}").get_json()
    assert j.get("US-9457478-B2", {}).get("note") == "unit"
    app_client.post(f"/api/flags/{GOLD}", json={"pub": "US-9457478-B2", "flag": "", "note": ""})  # reset


# ---- edge cases: clean, not 500 ------------------------------------------------------------
def test_empty_query_redirects(app_client):
    assert app_client.post("/run", data={"query": "", "mode": "novelty"}).status_code == 302


def test_bad_slug_404(app_client):
    assert app_client.get("/report/does-not-exist-zzz").status_code == 404


def test_junk_ref_graceful(app_client):
    assert app_client.get(f"/api/ref/JUNK-9?slug={GOLD}").status_code == 200


def test_junk_graph_graceful(app_client):
    assert app_client.get("/api/graph/JUNK-9").status_code == 200


def test_empty_export_400(app_client):
    assert app_client.post("/export", data={"slug": GOLD, "pubs": "", "format": "pdf"}).status_code == 400
