"""Two of the compliance gaps counsel named, in the code that decides them.

1. "every reference needs a 102(a)(1)/(a)(2) effective-date check against the target's priority
   date before it can be relied on. E.g., '232's claim 10 is credited to JP-2026-002795 — a 2026
   publication that may not be prior art at all."
   It is not. The dates make it Art. 54(3)-style secret art, and 35 U.S.C. 102(a)(2) reaches only
   US patents, US pre-grant publications and PCT applications designating the United States.

2. "in the '232 report, the 10-document package contains none of the references the ledger credits
   with anticipation. What's the selection logic?"
"""
import concise_description as cd
import search_modes
import submission_compliance as sc


# --------------------------------------------------------------------------- the forum rule


def test_a_later_published_japanese_application_is_not_us_prior_art():
    assert search_modes.secret_art_reaches("JP", "US") is False
    assert "102(a)(2)" in search_modes.secret_art_note("JP", "US")
    for cc in ("CN", "DE", "EP", "KR", "GB"):
        assert search_modes.secret_art_reaches(cc, "US") is False, cc


def test_a_us_or_pct_application_does_reach():
    assert search_modes.secret_art_reaches("US", "US") is True
    assert search_modes.secret_art_reaches("WO", "US") is True
    assert search_modes.secret_art_note("US", "US") == ""


def test_the_epo_has_its_own_list():
    """Art. 54(3) is a European right; a later-published US application is not one."""
    assert search_modes.secret_art_reaches("EP", "EP") is True
    assert search_modes.secret_art_reaches("US", "EP") is False


def test_an_unknown_forum_invents_no_bar():
    assert search_modes.secret_art_reaches("JP", "JP") is True


def _doc(pub, country, pub_date, prio):
    return {"pub": pub, "rows": [], "summary": "",
            "biblio": {"pub": pub, "country": country, "publication_date": pub_date,
                       "priority_date": prio, "filing_date": prio, "assignee": ""}}


def test_the_reported_japanese_reference_is_blocked_from_a_us_submission():
    """JP-2026-002795: published 2026-01-08 against a target whose EFD is 2024-09-09, own priority
    2024-06-20. Right on the dates; unavailable at the USPTO."""
    q = sc.qualify(_doc("JP-2026002795-A", "JP", "2026-01-08", "2024-06-20"), "2024-09-09")
    assert q["basis"] == "secret_prior_art"
    assert q["blocked"] is True
    assert q["forum_bar"] == "JP"
    assert "not prior art in the United States" in q["note"]


def test_the_same_dates_on_a_us_document_are_fine():
    q = sc.qualify(_doc("US-2026012345-A1", "US", "2026-01-08", "2024-06-20"), "2024-09-09")
    assert q["basis"] == "secret_prior_art" and q["blocked"] is False


def test_a_foreign_document_published_before_the_cutoff_is_untouched():
    """102(a)(1) reaches anything published anywhere. The forum rule must not leak into it."""
    q = sc.qualify(_doc("JP-2019155534-A", "JP", "2019-09-05", "2018-03-01"), "2024-09-09")
    assert q["basis"] == "public_prior_art" and q["blocked"] is False


def test_the_office_is_read_off_the_number_when_the_field_is_missing():
    d = _doc("CN-101567244-B", "", "2026-01-08", "2024-06-20")
    assert sc.qualify(d, "2024-09-09")["forum_bar"] == "CN"


def test_a_file_wrapper_document_is_not_date_checked():
    """An office action POSTDATES the application by design. Date-checking it would block the one
    document whose whole value is that it discusses these very claims."""
    d = _doc("OA:17724791/2025-09-16", "US", "2025-09-16", "")
    d["not_prior_art_document"] = True
    kept, blocked, _ = sc.apply([d], {"efd": "2021-04-20"}, source_text_for=lambda p: "")
    assert blocked == [] and len(kept) == 1
    assert kept[0]["compliance"]["qualify"]["basis"] == "not_a_reference"


# --------------------------------------------------------------------------- selection


def _deep():
    claims = [{"label": "claim 1[a]", "claim_no": 1, "text": "a base", "independent": True},
              {"label": "claim 1[b]", "claim_no": 1, "text": "a seal", "independent": True},
              {"label": "claim 3[a]", "claim_no": 3, "text": "the seal is annular",
               "independent": False}]

    def ref(pub, items, rank):
        return {"pub": pub, "title": pub, "rank": rank, "family": pub,
                "claims": [{"item": i, "verdict": "disclosed", "grounding": "verified",
                            "bar": "discloses", "quote": "q", "location": "para 1",
                            "confidence": 0.9} for i in items]}

    return {"claims": claims,
            "references": [ref("BROAD", ["claim 1[a]", "claim 3[a]"], 1),
                           ref("KILLER", ["claim 1[a]", "claim 1[b]", "claim 3[a]"], 40),
                           ref("EXAMINER", ["claim 1[a]"], 90)]}


def _report_with_ledger():
    lims = [{"id": "claim 1[a]", "claim_label": "claim 1", "claim_no": 1, "index": 0,
             "text": "a base", "independent": True, "depends_on": None},
            {"id": "claim 1[b]", "claim_label": "claim 1", "claim_no": 1, "index": 1,
             "text": "a seal", "independent": True, "depends_on": None},
            {"id": "claim 3[a]", "claim_label": "claim 3", "claim_no": 3, "index": 0,
             "text": "annular", "independent": False, "depends_on": 1}]
    ev = {"claim 1[a]": ["BROAD", "KILLER", "EXAMINER"], "claim 1[b]": ["KILLER"],
          "claim 3[a]": ["BROAD", "KILLER"]}
    rows = [dict(l, status="covered", n_evidence=len(ev[l["id"]]),
                 evidence=[{"pub": p, "verdict": "disclosed", "quote": "q", "location": "p",
                            "date": "", "confidence": 0.9, "bar": "discloses"}
                           for p in ev[l["id"]]]) for l in lims]
    return {"ledger": {"limitations": rows, "summary": {"cover_min": 1}},
            "prosecution": {"mined": {"applied": [{"pub": "EXAMINER", "statute": "102(a)(2)",
                                                   "claims": "1-3"}],
                                      "considered": ["EXAMINER"]}}}


def test_the_document_that_kills_a_claim_outranks_the_one_that_says_more():
    """BROAD touches two claims and completes neither. KILLER anticipates both. Ordering on row
    count puts BROAD first, which is how a ten-document package ends up containing none of the
    references the ledger credits."""
    got = cd.candidates(_report_with_ledger(), _deep(), limit=10)
    assert [c["pub"] for c in got][0] == "KILLER", [c["pub"] for c in got]
    assert got[0]["anticipates"] == ["claim 1", "claim 3"]


def test_a_reference_the_examiner_applied_outranks_breadth():
    got = {c["pub"]: i for i, c in enumerate(cd.candidates(_report_with_ledger(), _deep(),
                                                           limit=10))}
    assert got["EXAMINER"] < got["BROAD"], got


def test_what_a_claim_adds_is_reported_as_such_and_not_as_anticipation():
    got = {c["pub"]: c for c in cd.candidates(_report_with_ledger(), _deep(), limit=10)}
    assert got["BROAD"]["anticipates"] == []
    assert got["BROAD"]["adds"] == ["claim 3"]


def test_one_document_per_family():
    deep = _deep()
    for r in deep["references"]:
        r["family"] = "F1"
    got = cd.candidates(_report_with_ledger(), deep, limit=10)
    assert len(got) == 1
    assert sorted(got[0]["family_siblings"]) == ["BROAD", "EXAMINER"]


def test_the_route_gate_does_not_collapse_families():
    """The gate asks "does this publication carry evidence", of a pub the user typed. Collapsing
    there refuses a sibling somebody deliberately chose."""
    deep = _deep()
    for r in deep["references"]:
        r["family"] = "F1"
    got = cd.candidates(_report_with_ledger(), deep, limit=1000, collapse_families=False)
    assert len(got) == 3


def test_selection_still_works_with_no_ledger_and_no_wrapper():
    got = cd.candidates({}, _deep(), limit=10)
    assert len(got) == 3 and all(c["anticipates"] == [] for c in got)


def test_a_double_patenting_citation_does_not_get_the_examiner_boost():
    """An examiner citing the applicant's own earlier patent under obviousness-type double
    patenting has not said it is prior art, and on the measured subject it was not: US 12,115,659
    is the patent that application is a continuation of, same priority date. It led the package."""
    rep = _report_with_ledger()
    rep["prosecution"]["mined"]["applied"] = [
        {"pub": "EXAMINER", "statute": "obviousness-type double patenting", "claims": "1-20"}]
    got = {c["pub"]: c for c in cd.candidates(rep, _deep(), limit=10)}
    assert got["EXAMINER"]["office"] != "applied"


def test_a_102_citation_still_gets_it():
    rep = _report_with_ledger()
    rep["prosecution"]["mined"]["applied"] = [
        {"pub": "EXAMINER", "statute": "102(a)(2)", "claims": "1-3"}]
    got = {c["pub"]: c for c in cd.candidates(rep, _deep(), limit=10)}
    assert got["EXAMINER"]["office"] == "applied"
