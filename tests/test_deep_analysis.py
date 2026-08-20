"""The full-text reading of the top references, and the two tables it produces.

Hermetic: the model is stubbed per test and the database is not touched except through a stubbed
`full_text`. Each test pins a decision that a measurement forced:

  * a quote the model invented, or one that cannot be traced to a passage, must not survive as a
    chart cell — an audit put naive chart overclaim at 22%;
  * a "disclosed" verdict the refuter will not confirm becomes "uncertain" — 7 of 12
    coordinate-backed cells were false positives whose quote AND coordinate were both real;
  * the refuter must be told what a claim SAYS, not just its number. Given "claim 3" it replied
    "the assertion 'claim 3' is not a statement that can be refuted" and downgraded every claim
    row in the table for a reason about our prompt rather than about the evidence;
  * the reading must reach past the 25 cards the page renders, and must not chart the uploaded
    patent against its own claims.
"""
import json

import pytest

import deep_analysis


REF = {
    "found": True, "pub": "US-1-B2", "title": "A vacuum lifter",
    "chars": 400, "n_claims": 2, "n_paragraphs": 1, "truncated": False,
    "passages": [
        {"kind": "abstract", "coord": {}, "label": "abstract",
         "text": "A handheld vacuum lifter for glass panels with a compliant sealing lip."},
        {"kind": "claim", "coord": {"claim_no": 1}, "label": "claim 1",
         "text": "A vacuum lifter comprising a suction cup, an electric vacuum pump and a "
                 "pressure sensor arranged to detect loss of grip vacuum."},
        {"kind": "paragraph", "coord": {"para_no": 12}, "label": "paragraph 12",
         "text": "The pump draws air through a port so that the sealing lip conforms to the "
                 "panel and the sensor alarms the operator before the load can be dropped."},
    ],
}
FEATURES = ["a compliant sealing lip", "an electric vacuum pump", "a laser rangefinder"]
CLAIMS = [{"label": "claim 1", "claim_no": 1, "independent": True,
           "text": "A lifter comprising a suction cup and a pump."}]


def _stub_model(monkeypatch, answer, refute=None):
    calls = []

    def chat(system, user, **kw):
        calls.append((system, user))
        if "REFUTE" in system.upper():
            return refute if refute is not None else {"checks": []}
        return answer
    monkeypatch.setattr(deep_analysis.llm, "chat_json", chat)
    monkeypatch.setattr(deep_analysis, "full_text", lambda pub, **kw: dict(REF, pub=pub))
    return calls


# ---------------------------------------------------------------------------
# grounding: a cell must rest on a quote that is really in the reference
# ---------------------------------------------------------------------------
def test_an_invented_quote_is_dropped_not_shown(monkeypatch):
    _stub_model(monkeypatch, {"features": [
        {"item": "a compliant sealing lip", "verdict": "disclosed",
         "quote": "the apparatus includes a laser interferometer and a cryogenic manifold",
         "confidence": 0.9},
    ], "claims": []})
    out = deep_analysis.analyse_reference("US-1-B2", ["a compliant sealing lip"], [])
    row = out["features"][0]
    assert row["verdict"] == "absent"
    assert row["quote"] == ""
    assert row["grounding"] == "dropped-ungrounded-quote"


def test_a_real_quote_gets_the_passage_it_came_from(monkeypatch):
    """The location is resolved by code from the quote — the model never authors a citation."""
    _stub_model(monkeypatch, {"features": [
        {"item": "an electric vacuum pump", "verdict": "disclosed",
         "quote": "a suction cup, an electric vacuum pump and a pressure sensor arranged to "
                  "detect loss of grip vacuum",
         "note": "the claim recites the pump", "confidence": 0.8},
    ], "claims": []})
    out = deep_analysis.analyse_reference("US-1-B2", ["an electric vacuum pump"], [])
    row = out["features"][0]
    assert row["verdict"] == "disclosed"
    assert row["location"] == "claim 1"
    assert row["coord"] == {"claim_no": 1}
    assert row["grounding"] == "verified"


def test_absent_is_a_correct_answer_not_a_failure(monkeypatch):
    _stub_model(monkeypatch, {"features": [
        {"item": "a laser rangefinder", "verdict": "absent", "quote": "",
         "note": "no rangefinder appears", "confidence": 0.1}], "claims": []})
    out = deep_analysis.analyse_reference("US-1-B2", ["a laser rangefinder"], [])
    row = out["features"][0]
    assert row["verdict"] == "absent" and row["grounding"] == "model-absent"
    assert row["note"] == "no rangefinder appears"


def test_no_local_text_charts_nothing_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(deep_analysis, "full_text",
                        lambda pub, **kw: {"found": False, "pub": pub, "title": "", "passages": [],
                                           "chars": 0, "n_claims": 0, "n_paragraphs": 0,
                                           "truncated": False})
    called = []
    monkeypatch.setattr(deep_analysis.llm, "chat_json",
                        lambda *a, **k: called.append(1) or {})
    out = deep_analysis.analyse_reference("US-9-B2", FEATURES, CLAIMS)
    assert out["method"] == "no-text"
    assert called == [], "with no text to read, the model must not be asked at all"
    assert all(r["verdict"] == "absent" and r["grounding"] == "no-reference-text"
               for r in out["features"] + out["claims"])


# ---------------------------------------------------------------------------
# refutation
# ---------------------------------------------------------------------------
def test_a_disclosed_cell_the_refuter_rejects_becomes_uncertain(monkeypatch):
    _stub_model(monkeypatch,
                {"features": [{"item": "a compliant sealing lip", "verdict": "disclosed",
                               "quote": "the sealing lip conforms to the panel",
                               "confidence": 0.9}], "claims": []},
                refute={"checks": [{"i": 0, "refuted": True,
                                    "why": "the quote is about conformance, not compliance"}]})
    out = deep_analysis.analyse_reference("US-1-B2", ["a compliant sealing lip"], [])
    row = out["features"][0]
    assert row["verdict"] == "uncertain"
    assert "conformance" in row["refuted"]
    assert out["refuted"] == 1


def test_a_disclosed_cell_the_refuter_confirms_stays_disclosed(monkeypatch):
    _stub_model(monkeypatch,
                {"features": [{"item": "an electric vacuum pump", "verdict": "disclosed",
                               "quote": "an electric vacuum pump and a pressure sensor",
                               "confidence": 0.9}], "claims": []},
                refute={"checks": [{"i": 0, "refuted": False, "why": "it plainly recites it"}]})
    out = deep_analysis.analyse_reference("US-1-B2", ["an electric vacuum pump"], [])
    assert out["features"][0]["verdict"] == "disclosed"
    assert out["features"][0]["refuted"] is False


def test_the_refuter_is_told_what_a_claim_says_not_just_its_number(monkeypatch):
    """Handed "claim 1" alone it replied "the assertion 'claim 1' is not a statement that can be
    refuted" and downgraded every claim row in the table."""
    seen = {}

    def chat(system, user, **kw):
        if "REFUTE" in system.upper():
            seen["refute_user"] = user
            return {"checks": []}
        return {"features": [], "claims": [
            {"item": "claim 1", "verdict": "disclosed",
             "quote": "A vacuum lifter comprising a suction cup, an electric vacuum pump",
             "confidence": 0.9}]}
    monkeypatch.setattr(deep_analysis.llm, "chat_json", chat)
    monkeypatch.setattr(deep_analysis, "full_text", lambda pub, **kw: dict(REF, pub=pub))
    deep_analysis.analyse_reference("US-1-B2", [], CLAIMS)
    payload = json.loads(seen["refute_user"])
    assertion = payload["pairs"][0]["assertion"]
    assert assertion == CLAIMS[0]["text"], "the refuter must see the claim's text"
    assert assertion != "claim 1"


# ---------------------------------------------------------------------------
# scope of the reading
# ---------------------------------------------------------------------------
def test_the_reading_goes_past_the_cards_the_page_renders(monkeypatch):
    """The page builds 25 full cards on purpose; the reading must still reach 50."""
    cards = [{"pub": f"US-{i}-B2", "title": f"t{i}", "rank": i} for i in range(1, 26)]
    report = {"ranked_families": [f"fam{i}" for i in range(1, 200)]}

    class FakeCur:
        def execute(self, *a, **k): pass

    import webview
    monkeypatch.setattr(webview, "resolve_family_reps",
                        lambda cur, fams, subject_efd=None: {
                            f: {"publication_number": "US-X%s-B2" % f,
                                "title": "tail " + f, "simple_family_id": f}
                            for f in fams})
    monkeypatch.setattr(deep_analysis.db, "connect",
                        lambda *a, **k: type("C", (), {"autocommit": True,
                                                       "cursor": lambda self: FakeCur(),
                                                       "close": lambda self: None})())
    out = deep_analysis._extend_to(list(cards), report, 50)
    assert len(out) == 50
    assert out[25]["beyond_cards"] is True
    assert out[-1]["rank"] == 50


def test_the_uploaded_patent_is_not_charted_against_its_own_claims(monkeypatch):
    """Rank 1 being the subject itself put a row of "discloses claim 1" for every claim at the
    top of the analysis, which tells the reader nothing."""
    report = {"query_document": {"publication_number": "US 11,338,449 B2", "claims": []},
              "ranked_families": []}
    view = {"cards": [{"pub": "US-11338449-B2", "title": "the subject", "rank": 1},
                      {"pub": "US-2-B2", "title": "real art", "rank": 2}],
            "elements": ["a feature"]}
    monkeypatch.setattr(deep_analysis, "analyse_reference",
                        lambda pub, f, c, title="": {"pub": pub, "features": [], "claims": [],
                                                     "method": "llm", "chars": 1})
    out = deep_analysis.build(report, view)
    pubs = [r["pub"] for r in out["references"]]
    assert "US-11338449-B2" not in pubs
    assert "US-2-B2" in pubs
    assert out["subject_pub_excluded"] == "US11338449B2"


def test_subject_material_reads_the_features_and_the_uploaded_claims():
    report = {"query_document": {"label": "spec.pdf", "claims": [
        {"claim_no": 1, "text": "A lifter comprising a cup.", "independent": True},
        {"claim_no": 2, "text": "The lifter of claim 1.", "independent": False}]}}
    view = {"elements": ["a suction cup", "a pump"]}
    features, claims, qd = deep_analysis.subject_material(report, view)
    assert features == ["a suction cup", "a pump"]
    assert [c["label"] for c in claims] == ["claim 1", "claim 2"]
    assert claims[0]["independent"] is True
    assert qd["label"] == "spec.pdf"


def test_no_subject_claims_when_the_search_was_typed():
    features, claims, qd = deep_analysis.subject_material({}, {"elements": ["a cup"]})
    assert features == ["a cup"] and claims == []


# ---------------------------------------------------------------------------
# the whole-report shape
# ---------------------------------------------------------------------------
def test_build_reports_which_features_no_reference_reached(monkeypatch):
    view = {"cards": [{"pub": "US-1-B2", "rank": 1}, {"pub": "US-2-B2", "rank": 2}],
            "elements": ["found everywhere", "found nowhere"]}

    def fake(pub, features, claims, title=""):
        return {"pub": pub, "method": "llm", "chars": 10, "claims": [], "features": [
            {"item": features[0], "verdict": "disclosed", "quote": "q", "grounding": "verified"},
            {"item": features[1], "verdict": "absent", "quote": "", "grounding": "model-absent"}]}
    monkeypatch.setattr(deep_analysis, "analyse_reference", fake)
    out = deep_analysis.build({"ranked_families": []}, view)
    assert out["available"] is True
    assert out["uncovered_features"] == ["found nowhere"]
    assert out["counts"]["disclosed"] == 2 and out["counts"]["absent"] == 2


def test_build_is_honest_when_there_is_nothing_to_chart():
    out = deep_analysis.build({}, {"cards": [], "elements": []})
    assert out["available"] is False and "no ranked references" in out["reason"]


def test_full_text_orders_claims_before_description(monkeypatch):
    """If the budget runs out it must run out in the description: the claims are the reference's
    legal disclosure and are what a chart argues about."""
    import inspect
    src = inspect.getsource(deep_analysis.full_text)
    assert src.index("FROM claims") < src.index("FROM chunks")


def test_the_deep_routes_exist():
    import webapp
    have = {r.endpoint for r in webapp.app.url_map.iter_rules()}
    assert "api_deep_analysis" in have and "analysis_page" in have
