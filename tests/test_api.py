"""API endpoint tests via the Flask test client: status codes + shape + edge cases (no 500s)."""
import json
import pytest

GOLD = "grabo_gripper_novelty"
PUB = "US-11207792-B2"


def test_healthz(app_client):
    r = app_client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_home_has_both_entry_points(app_client):
    r = app_client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "invention" in html.lower()          # free-text box
    assert "exchip" in html                       # example prompt chips
    assert GOLD in html                            # gold examples grid


def test_gold_report_renders(app_client):
    r = app_client.get(f"/report/{GOLD}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "claim chart" in html.lower()
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
