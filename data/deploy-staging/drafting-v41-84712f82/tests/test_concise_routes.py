"""The routes: what they refuse, what they serve, and what they never serve.

Hermetic — the phrasing call and the enrichment fetch are replaced, so no paid API is touched and
the assertions are about the route's own behaviour.
"""
import json

import pytest

import webapp


@pytest.fixture()
def client(monkeypatch):
    webapp.app.config["TESTING"] = True
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    return webapp.app.test_client()


@pytest.fixture()
def report(tmp_path, monkeypatch):
    """A minimal finished report with one reference carrying one verified cell."""
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(webapp, "CONCISE_DIR", tmp_path / "concise")
    slug = "adhoc-testconcise"
    deep = {
        "subject_label": "US-20250033224-A1",
        "claims": [{"label": "claim 1[a]", "claim_no": 1, "independent": True,
                    "text": "a base element comprising one or more openings"}],
        "references": [{"pub": "US-11413727-B2", "title": "Vacuum Gripper", "rank": 1, "claims": [
            {"item": "claim 1[a]", "verdict": "disclosed", "grounding": "verified",
             "bar": "discloses", "quote": "a base element 141 having an elliptical track 148",
             "note": "The reference discloses a base element with peripheral openings.",
             "location": "paragraph p0012", "coord": {"para_no": "p0012"}, "confidence": 0.9}]}],
    }
    (tmp_path / ("%s.deep.json" % slug)).write_text(json.dumps(deep))
    (tmp_path / ("%s.meta.json" % slug)).write_text(json.dumps({"subject": "US-20250033224-A1"}))
    #  Keep the build offline: no model call, no enrichment fetch.
    import concise_description as cd
    monkeypatch.setattr(cd, "phrase", lambda doc, tier="strong", model=None: doc)
    monkeypatch.setattr(cd, "_display", lambda pub, allow_fetch=True: {
        "title": "Vacuum Gripper", "inventors": ["Nimrod Rotem"],
        "publication_date": "2022-08-16", "priority_date": "2018-05-09"})
    monkeypatch.setattr(cd, "subject_facts", lambda label: {"efd": None, "assignees": []})
    #  THE FIXTURE'S QUOTE HAS TO BE IN THE FIXTURE'S SOURCE. `verify_quotes` re-reads the real
    #  corpus text for US-11413727-B2, which of course does not contain this invented passage, and
    #  a row whose quotation cannot be found is now dropped rather than filed unquoted. Without
    #  this the build produced nothing and the failure looked like a route bug.
    monkeypatch.setattr(webapp, "_concise_source_text",
                        lambda pub: "The gripper has a base element 141 having an elliptical "
                                    "track 148 around its periphery.")
    webapp._CONCISE_JOBS.clear()
    return slug


import time


def _finished(slug, timeout=25):
    """Wait for the background build. The POST returns as soon as the work is queued now, so a
    test that checks the output files has to wait for them the way the page does."""
    import webapp as _w
    end = time.time() + timeout
    while time.time() < end:
        j = _w._concise_job(slug) or {}
        if j.get("state") in ("done", "failed"):
            return j
        time.sleep(0.05)
    return _w._concise_job(slug) or {}


def test_the_picker_lists_the_reference_and_its_claims(client, report):
    r = client.get("/report/%s/concise" % report)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "US-11413727-B2" in body
    assert "37 CFR" in body


def test_the_publication_number_is_prefilled_from_the_report(client, report):
    body = client.get("/report/%s/concise" % report).get_data(as_text=True)
    #  The submission names the application under examination; defaulting it from the searched
    #  subject is what stops a paper going out identifying the wrong case.
    assert "US 2025/0033224 A1" in body


def test_a_report_with_no_reading_stage_says_so_instead_of_rendering_an_empty_table(
        client, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    slug = "adhoc-noreading"
    (tmp_path / ("%s.deep.json" % slug)).write_text(json.dumps({"references": []}))
    body = client.get("/report/%s/concise" % slug).get_data(as_text=True)
    assert "no per-claim evidence" in body or "no full-text reading stage" in body


def test_posting_builds_both_formats_and_offers_them(client, report):
    r = client.post("/report/%s/concise" % report,
                    data={"pubs": ["US-11413727-B2"], "app_no": "18/915,337",
                          "pub_no": "US 2025/0033224 A1", "title": "Portable vacuum gripper",
                          "inventor": "Nhon Hoa Nguyen"})
    assert r.status_code == 200
    assert b'id="cdProg"' in r.data, "the POST should come back with the progress bar"
    j = _finished(report)
    assert j.get("state") == "done", j.get("error")
    out = webapp.CONCISE_DIR / report
    assert (out / "ConciseDescription_Doc1_US11413727B2.pdf").read_bytes()[:5] == b"%PDF-"
    assert (out / "ConciseDescription_Doc1_US11413727B2.docx").read_bytes()[:2] == b"PK"
    #  And the finished page lists them.
    body = client.get("/report/%s/concise" % report).get_data(as_text=True)
    assert "ConciseDescription_Doc1_US11413727B2.pdf" in body
    assert "ConciseDescription_Doc1_US11413727B2.docx" in body


def test_a_publication_not_in_the_report_cannot_be_smuggled_in(client, report):
    """`pubs` is user input naming a document that will be looked up and rendered."""
    r = client.post("/report/%s/concise" % report,
                    data={"pubs": ["US-9999999-B2"], "app_no": "18/915,337"})
    assert r.status_code == 400
    body = r.get_data(as_text=True)
    #  Named, not silently dropped: a success page with no documents on it and no reason is the
    #  worse failure, because the user cannot tell it from "nothing was relevant".
    assert "US-9999999-B2" in body
    assert "ConciseDescription_Doc1_US9999999B2.pdf" not in body


def test_the_download_route_refuses_a_traversal(client, report):
    client.post("/report/%s/concise" % report,
                data={"pubs": ["US-11413727-B2"], "app_no": "18/915,337"})
    #  Werkzeug normalises and 308s some of these; what matters is where the request LANDS, so
    #  follow the redirect and require that nothing outside the feature's own directory is served.
    for name in ("../../../etc/passwd", "....//etc/passwd", "/etc/passwd", "..%2f..%2fpasswd"):
        r = client.get("/report/%s/concise/%s" % (report, name), follow_redirects=True)
        assert r.status_code == 404, "%s -> %s" % (name, r.status_code)


def test_the_download_route_never_serves_the_internal_model(client, report):
    """The .model.json holds the raw cells; the route serves filing artefacts only."""
    client.post("/report/%s/concise" % report,
                data={"pubs": ["US-11413727-B2"], "app_no": "18/915,337"})
    _finished(report)
    listed = [p.name for p in (webapp.CONCISE_DIR / report).iterdir()]
    model = [n for n in listed if n.endswith(".model.json")]
    assert model, "the model should be written for provenance"
    assert client.get("/report/%s/concise/%s" % (report, model[0])).status_code == 404


def test_a_bad_slug_is_not_a_path(client):
    assert client.get("/report/..%2f..%2fetc/concise").status_code in (301, 308, 404)
