"""The build must not block the request, and the page must be able to say where it got to.

Before this, the POST ran the whole build — a model call and an enrichment fetch per document, the
compliance pass, then two renderings each — so the browser sat on a dead page for minutes with
nothing on screen, which reads exactly like a click that never registered.
"""
import json
import threading
import time

import pytest

import concise_description as cd
import webapp


@pytest.fixture()
def report(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(webapp, "CONCISE_DIR", tmp_path / "concise")
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    slug = "adhoc-progtest"
    deep = {
        "subject_label": "US-20250033224-A1",
        "claims": [{"label": "claim 1[a]", "claim_no": 1, "independent": True,
                    "text": "a base element comprising one or more openings"}],
        "references": [
            {"pub": "US-11413727-B2", "title": "Vacuum Gripper", "rank": 1, "claims": [
                {"item": "claim 1[a]", "verdict": "disclosed", "grounding": "verified",
                 "bar": "discloses", "quote": "a base element 141", "note": "n",
                 "location": "paragraph p0012", "coord": {"para_no": "p0012"},
                 "confidence": 0.9}]},
            {"pub": "US-7240935-B2", "title": "Suction grip arm", "rank": 2, "claims": [
                {"item": "claim 1[a]", "verdict": "disclosed", "grounding": "verified",
                 "bar": "discloses", "quote": "a flexible suction body", "note": "n",
                 "location": "paragraph p0001", "coord": {"para_no": "p0001"},
                 "confidence": 0.8}]}],
    }
    (tmp_path / ("%s.deep.json" % slug)).write_text(json.dumps(deep))
    (tmp_path / ("%s.meta.json" % slug)).write_text(json.dumps({"mode": "novelty"}))
    monkeypatch.setattr(cd, "phrase", lambda doc, tier="strong", model=None: doc)
    monkeypatch.setattr(cd, "_display", lambda pub, allow_fetch=True: {
        "title": "T", "inventors": ["X"], "publication_date": "2002-01-01",
        "priority_date": "2001-01-01"})
    monkeypatch.setattr(cd, "subject_facts", lambda label: {"efd": None, "assignees": []})
    webapp._CONCISE_JOBS.clear()
    return slug


@pytest.fixture()
def client():
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def _wait(slug, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        j = webapp._concise_job(slug) or {}
        if j.get("state") in ("done", "failed"):
            return j
        time.sleep(0.05)
    return webapp._concise_job(slug) or {}


def test_the_post_returns_at_once_instead_of_holding_the_browser(client, report, monkeypatch):
    """The build is deliberately made SLOW here.

    A first version of this test used the fast mocked build, so moving the work back inline still
    returned quickly and the test passed with the defect in place — it was measuring the mock, not
    the architecture. The build now blocks until the test releases it, which is the only way the
    assertion means anything.
    """
    gate = threading.Event()
    real = cd.build

    def slow(*a, **kw):
        assert gate.wait(timeout=20), "build was not released"
        return real(*a, **kw)

    monkeypatch.setattr(cd, "build", slow)
    t0 = time.time()
    r = client.post("/report/%s/concise" % report,
                    data={"pubs": ["US-11413727-B2", "US-7240935-B2"], "app_no": "18/915,337"})
    took = time.time() - t0
    gate.set()
    assert r.status_code == 200
    assert took < 3, "the POST held the browser for %.1fs; the build must run off the request" % took
    assert b'id="cdProg"' in r.data, "the page must come back showing the progress bar"
    _wait(report)


def test_progress_counts_real_steps_and_reaches_done(client, report):
    client.post("/report/%s/concise" % report,
                data={"pubs": ["US-11413727-B2", "US-7240935-B2"], "app_no": "18/915,337"})
    j = _wait(report)
    assert j.get("state") == "done", j.get("error")
    #  Two documents: one build step each, one compliance step, one write step each.
    assert j["total"] == 5 and j["done"] == 5
    r = client.get("/report/%s/concise/progress" % report)
    d = r.get_json()
    assert d["state"] == "done" and d["pct"] == 100.0
    assert "2 documents ready" in d["msg"]


def test_progress_names_the_document_being_worked_on(report):
    """A count alone does not tell you which reference is costing the wait."""
    seen = []
    subject = {"app_no": "18/915,337", "pub_no": "US 2025/0033224 A1"}
    deep = json.loads((webapp.REPORTS / ("%s.deep.json" % report)).read_text())
    cd.build(deep, ["US-11413727-B2", "US-7240935-B2"], subject, do_phrase=False,
             on_progress=lambda n, msg: seen.append((n, msg)))
    assert [n for n, _ in seen] == [0, 1]
    assert "US-11413727-B2" in seen[0][1] and "1 of 2" in seen[0][1]
    assert "US-7240935-B2" in seen[1][1]


def test_an_idle_slug_reports_idle_not_a_phantom_build(client, report):
    d = client.get("/report/%s/concise/progress" % report).get_json()
    assert d["state"] == "idle"


def test_a_second_click_does_not_start_a_second_build(client, report, monkeypatch):
    """Two builds over one output directory would interleave their writes."""
    gate = threading.Event()
    real = cd.build

    #  Count BUILD INVOCATIONS, not threads. Counting threads passed even with the guard removed,
    #  because a build that runs inline starts no thread either — the test agreed for the wrong
    #  reason.
    calls = []

    def counting(*a, **kw):
        calls.append(1)
        assert gate.wait(timeout=20), "build was not released"
        return real(*a, **kw)

    monkeypatch.setattr(cd, "build", counting)
    data = {"pubs": ["US-11413727-B2"], "app_no": "18/915,337"}
    client.post("/report/%s/concise" % report, data=data)
    time.sleep(0.4)
    assert calls == [1], "the first build did not start"
    client.post("/report/%s/concise" % report, data=data)      # the second click
    time.sleep(0.4)
    assert calls == [1], "a second build started over the same output directory"
    gate.set()
    _wait(report)


def test_a_failed_build_is_reported_as_failed_not_left_spinning(client, report, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cd, "build", boom)
    client.post("/report/%s/concise" % report,
                data={"pubs": ["US-11413727-B2"], "app_no": "18/915,337"})
    j = _wait(report)
    assert j.get("state") == "failed"
    assert "kaboom" in (j.get("error") or "")
    d = client.get("/report/%s/concise/progress" % report).get_json()
    assert d["state"] == "failed" and d["error"]


def test_progress_is_not_readable_for_a_report_you_cannot_see(client, report, monkeypatch):
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: False)
    assert client.get("/report/%s/concise/progress" % report).status_code == 404
