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
    yield slug
    #  NO TEST ENDS WITH A BUILD STILL RUNNING. The worker is a daemon thread that reads
    #  CONCISE_DIR at write time, so one left over from a finished test writes into the NEXT
    #  test's directory and runs its stale-file sweep there, deleting what that test just wrote.
    #  That is what it looked like: an unrelated test failing on a missing .model.json, only in a
    #  full-file run, with its own job reported done.
    import time as _t
    for _ in range(300):
        if (webapp._concise_job(slug) or {}).get("state") != "running":
            break
        _t.sleep(0.1)
    webapp._CONCISE_JOBS.clear()


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


def test_a_report_with_no_reading_stage_goes_back_to_the_report(client, tmp_path, monkeypatch):
    """There is nothing to describe and nothing to choose, so there is no page. The report's own
    phase bar carries the one real action, which is running the full search."""
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    slug = "adhoc-noreading"
    (tmp_path / ("%s.deep.json" % slug)).write_text(json.dumps({"references": []}))
    r = client.get("/report/%s/concise" % slug)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/report/%s" % slug)


def test_posting_builds_both_formats_and_offers_them(client, report):
    r = client.post("/report/%s/concise" % report,
                    data={"pubs": ["US-11413727-B2"], "app_no": "18/915,337",
                          "pub_no": "US 2025/0033224 A1", "title": "Portable vacuum gripper",
                          "inventor": "Nhon Hoa Nguyen"})
    #  Post/Redirect/Get: the POST hands back a redirect so a refresh cannot re-submit it and
    #  start a second build over the same directory.
    assert r.status_code == 302
    assert b'id="cdProg"' in client.get(r.headers["Location"]).data, "no progress bar on the GET"
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
    assert model, ("the model should be written for provenance; directory held %s, job was %s"
                   % (listed, webapp._concise_job(report)))
    assert client.get("/report/%s/concise/%s" % (report, model[0])).status_code == 404


def test_a_bad_slug_is_not_a_path(client):
    assert client.get("/report/..%2f..%2fetc/concise").status_code in (301, 308, 404)


def test_the_picker_opens_on_one_fee_unit_and_pre_picks_what_it_buys(client, report, monkeypatch):
    """1.290(f) charges per ten items or fraction thereof, so the page opens on the cheapest unit
    and fills exactly the slots that unit pays for. It counts ELIGIBLE candidates, because a
    flagged document must not consume a paid slot it will not be filed in."""
    import re

    import submission as S

    #  Twelve candidates, two of them flagged, so a naive "first ten rows" would pick eight.
    def classify(slug, cands, deep):
        for i, c in enumerate(cands):
            c["basis"] = S.NOT_ART if i in (1, 3) else S.PUBLIC
            c["co_owned"] = False
            c["default_include"] = c["basis"] == S.PUBLIC
        return cands

    import concise_description as cd
    monkeypatch.setattr(webapp, "_classify", classify)
    monkeypatch.setattr(cd, "office_action_candidates", lambda report: [])
    monkeypatch.setattr(cd, "candidates",
                        lambda report, deep, limit=40, collapse_families=True: [
                            {"pub": "US-%07d-B2" % i, "title": "t", "rows": 1,
                             "strong": 1, "claims": "1"} for i in range(1, 13)])
    body = client.get("/report/%s/concise" % report).get_data(as_text=True)
    assert re.search(r'<option value="1"\s*selected', body), "it should open on one fee unit"
    checked = re.findall(r'class="pickbox"[^>]*checked', body, re.S)
    assert len(checked) == 10, "one unit buys ten documents, %d were pre-picked" % len(checked)
    assert body.count('data-eligible="1"') == 10
    assert "$%s" % S._money(S.fee_amount(1, "small")[1]) in body


def test_the_deadline_is_counted_on_the_page_and_only_dated_in_the_paper(client, report,
                                                                         monkeypatch):
    """The countdown was a day out because it was printed into a PDF and read the next morning.
    The paper states the date; the page, rendered when somebody looks at it, does the counting."""
    import datetime

    import submission

    today = datetime.date.today()
    pub = today - datetime.timedelta(days=150)          # deadline is publication plus six months
    monkeypatch.setattr(submission, "prosecution_dates",
                        lambda rep: (pub.isoformat(), None, None))
    body = client.get("/report/%s/concise" % report).get_data(as_text=True)
    win = submission.window(pub.isoformat(), None, None)
    assert "File before %s" % win["deadline"] in body
    assert "%d days from today, %s" % (win["days_left"], today.isoformat()) in body
    #  and the paper it builds refuses to count, so it cannot go stale on the shelf
    docs = [{"n": 1, "pub": "US-1-A1", "rows": [{"claim_no": "1"}], "compliance": {},
             "biblio": {"pub": "US-1-A1", "label": "US 1", "kind": "publication", "country": "US",
                        "inventor": "A", "issue_date_pretty": "February 4, 2021"}}]
    timing = {f.id: f for f in submission.audit(docs, {"app_no": "19/318,450"}, {}, {}, win)}
    assert "count from today" in timing["TIMING"].detail


def test_a_closed_window_is_said_on_the_page_not_only_in_the_packet(client, report, monkeypatch):
    import datetime

    import submission

    long_ago = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
    monkeypatch.setattr(submission, "prosecution_dates", lambda rep: (long_ago, long_ago, None))
    body = client.get("/report/%s/concise" % report).get_data(as_text=True)
    assert "window closed on" in body
    assert "may not be entered" in body


def test_the_one_click_package_skips_the_flagged_candidates(client, report, monkeypatch):
    """The button spends one fee unit without asking anything else, so it must not spend it on a
    document the picker itself flags as not prior art or as commonly owned."""
    import submission as S

    seen = {}

    def classify(slug, cands, deep):
        for i, c in enumerate(cands):
            c["basis"] = S.NOT_ART if i == 0 else S.PUBLIC
            c["co_owned"] = i == 1
            c["default_include"] = c["basis"] == S.PUBLIC and not c["co_owned"]
        return cands

    def fake_build(deep, pubs, subject, **kw):
        seen["pubs"] = list(pubs)
        return []

    import concise_description as cd
    monkeypatch.setattr(webapp, "_classify", classify)
    monkeypatch.setattr(cd, "office_action_candidates", lambda report: [])
    monkeypatch.setattr(cd, "candidates",
                        lambda report, deep, limit=40, collapse_families=True: [
                            {"pub": "US-11413727-B2", "title": "t"}] + [
                            {"pub": "US-%07d-B2" % i, "title": "t"} for i in range(2, 15)])
    monkeypatch.setattr(cd, "build", fake_build)
    client.post("/report/%s/concise" % report, data={"auto": "10", "app_no": "18/915,337"})
    _finished(report)
    assert seen.get("pubs"), "the build was never reached"
    #  The first candidate is not prior art and the second is commonly owned; neither may be in it.
    assert "US-11413727-B2" not in seen["pubs"]
    assert "US-0000002-B2" not in seen["pubs"]
    assert len(seen["pubs"]) == S.ITEMS_PER_UNIT, "one click is exactly one fee unit"


def test_going_over_the_chosen_fee_budget_is_refused_with_the_money_named(client, report):
    """The budget is a ceiling on the server too. A browser that skipped the script, or a hand
    posted form, must not quietly buy a second fee unit on the filer's behalf."""
    r = client.post("/report/%s/concise" % report,
                    data={"pubs": ["US-11413727-B2"] * 11, "app_no": "18/915,337",
                          "fee_units": "1"})
    assert r.status_code == 400
    body = r.get_data(as_text=True)
    assert "1 fee unit" in body and "up to 10 documents" in body
    assert "$" in body, "tell them what the overrun costs, not just that it is one"
    #  and the picker is still on the page, so the overrun can actually be fixed
    assert 'name="pubs"' in body
