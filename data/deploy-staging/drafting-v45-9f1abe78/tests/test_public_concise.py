"""Who may take the papers, and what a finished report may claim about its sources.

Two things reported from the live public link on 2026-08-19: it said "PQAI searching now" on a
search that had finished hours earlier, and it did not offer the 1.290 documents at all.
"""
import json

import pytest

import webapp
import webview


# ------------------------------------------------------------------ source panel honesty


def _finished(**over):
    r = {"partial": False, "wide": True, "federation": None,
         "external": {"ok": True, "per_source": {"pqai": 1919, "uspto": 250}}}
    r.update(over)
    return r


def test_a_finished_report_never_says_a_source_is_still_searching(monkeypatch):
    """The federation block is gone for good (FEDERATION_CHANNEL=0), and "no block" used to mean
    "not landed yet", so every provider rendered as pending for ever on a completed search."""
    monkeypatch.setattr(webview, "_engine_sources", lambda: [
        {"name": "pqai", "label": "PQAI", "enabled": True},
        {"name": "uspto", "label": "USPTO", "enabled": True},
        {"name": "lens", "label": "Lens", "enabled": True}])
    tags = webview._source_tags(_finished(), 60)
    by = {t["id"]: t for t in tags}
    assert by["pqai"]["state"] == "used" and by["pqai"]["n"] == 1919
    assert by["uspto"]["state"] == "used" and by["uspto"]["n"] == 250
    #  Configured, ran, returned nothing: that is "no results", not "searching".
    assert by["lens"]["state"] == "none"
    assert not any(t["state"] == "pending" for t in tags)


def test_a_running_report_may_still_say_searching(monkeypatch):
    """The original behaviour is right while the fan-out really has not landed."""
    monkeypatch.setattr(webview, "_engine_sources", lambda: [
        {"name": "pqai", "label": "PQAI", "enabled": True}])
    tags = webview._source_tags({"partial": True, "wide": True}, 10)
    assert any(t["state"] == "pending" for t in tags)


def test_a_failed_fanout_is_reported_as_failed_not_as_searching(monkeypatch):
    monkeypatch.setattr(webview, "_engine_sources", lambda: [
        {"name": "pqai", "label": "PQAI", "enabled": True}])
    tags = webview._source_tags(
        _finished(external={"ok": False, "error": "upstream timeout", "per_source": {}}), 10)
    by = {t["id"]: t for t in tags}
    assert by["pqai"]["state"] == "failed" and "timeout" in by["pqai"]["why"]


# ------------------------------------------------------------------ who may take the papers


@pytest.fixture()
def built(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "CONCISE_DIR", tmp_path)
    d = tmp_path / "adhoc-pubtest"
    d.mkdir()
    (d / "ConciseDescription_Doc1_US11413727B2.pdf").write_bytes(b"%PDF-1.4 fake")
    (d / "ConciseDescription_Doc1_US11413727B2.docx").write_bytes(b"PK fake")
    (d / "ConciseDescription_Doc1_US11413727B2.model.json").write_text("{}")
    return "adhoc-pubtest"


def test_a_stranger_cannot_take_the_papers(built, monkeypatch):
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: False)
    monkeypatch.setattr(webapp.public_report, "get", lambda slug: {"published": False})
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    r = c.get("/report/%s/concise/ConciseDescription_Doc1_US11413727B2.pdf" % built)
    assert r.status_code == 404
    assert c.get("/report/%s/concise.zip" % built).status_code == 404


def test_a_published_link_with_no_password_shares_its_papers(built, monkeypatch):
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: False)
    monkeypatch.setattr(webapp.public_report, "get", lambda slug: {"published": True})
    monkeypatch.setattr(webapp.public_report, "needs_password", lambda slug: False)
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    r = c.get("/report/%s/concise/ConciseDescription_Doc1_US11413727B2.pdf" % built)
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"


def test_a_password_protected_link_hands_nothing_over_until_it_is_answered(built, monkeypatch):
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: False)
    monkeypatch.setattr(webapp.public_report, "get", lambda slug: {"published": True})
    monkeypatch.setattr(webapp.public_report, "needs_password", lambda slug: True)
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    path = "/report/%s/concise/ConciseDescription_Doc1_US11413727B2.pdf" % built
    assert c.get(path).status_code == 404
    with c.session_transaction() as sess:          # answer the password
        sess["public_unlocked"] = [built]
    assert c.get(path).status_code == 200


def test_the_provenance_model_is_never_shared_even_publicly(built, monkeypatch):
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: False)
    monkeypatch.setattr(webapp.public_report, "get", lambda slug: {"published": True})
    monkeypatch.setattr(webapp.public_report, "needs_password", lambda slug: False)
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    r = c.get("/report/%s/concise/ConciseDescription_Doc1_US11413727B2.model.json" % built)
    assert r.status_code == 404


def test_the_zip_a_public_reader_gets_carries_only_filing_artefacts(built, monkeypatch):
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: False)
    monkeypatch.setattr(webapp.public_report, "get", lambda slug: {"published": True})
    monkeypatch.setattr(webapp.public_report, "needs_password", lambda slug: False)
    webapp.app.config["TESTING"] = True
    r = webapp.app.test_client().get("/report/%s/concise.zip" % built)
    assert r.status_code == 200
    import io
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert names and all(n.endswith((".pdf", ".docx")) for n in names)


def test_building_stays_owner_only_however_public_the_report_is(built, monkeypatch):
    """Reading the papers is sharing; building them spends money and changes what would be filed."""
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: False)
    monkeypatch.setattr(webapp.public_report, "get", lambda slug: {"published": True})
    monkeypatch.setattr(webapp.public_report, "needs_password", lambda slug: False)
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    assert c.get("/report/%s/concise" % built).status_code == 404
    assert c.post("/report/%s/concise" % built, data={"pubs": ["US-1-A"]}).status_code == 404
    assert c.get("/report/%s/concise/doc/1" % built).status_code == 404
