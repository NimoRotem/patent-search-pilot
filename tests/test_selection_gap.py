"""Why a document the claim grid ranks first can fall out of the package, and what says so.

Counsel, 2026-08-24, on the packet for adhoc-efbf2979420b: the grid's joint-top reference at 16 of
32 limitations, a Schunk family, was not in the thirteen filed, and reconciling that by hand took
twenty minutes. Four faults behind it, each held shut here.

1. The order led with what the ledger says a document ANTICIPATES, then whether the Office applied
   it. On this target nothing anticipates and nothing was applied, so both keys were ties for every
   candidate and the ranking fell through to weaker tie-breakers before coverage ever voted.
2. Breadth is not the same as reach. Twelve documents that each read on the same twelve popular
   limitations cover twelve between them.
3. A reference the corpus holds only an abstract for scores HIGH on coverage, not low, and two
   were selected: US 8,991,263 is a fibre-testing snubbing clamp charted against "pole shoes guide
   a magnetic field portion".
4. Nothing said what had been passed over, or why.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import concise_description as cd                                          # noqa: E402
import submission as S                                                    # noqa: E402


def _ref(pub, covers, absent=(), rank=50, family=None):
    """A reference whose chart cells say exactly what `covers` and `absent` say.

    `covers` are limitations with a usable verdict AND a verified passage, so they can be charted.
    `absent` are limitations it plainly does not reach. Anything in neither is 'uncertain', which
    the grid counts as reading on and the chart cannot use: that gap is the whole point.
    """
    cells = [{"item": i, "verdict": "disclosed", "grounding": "verified", "bar": "discloses",
              "quote": "q", "confidence": 0.9} for i in covers]
    cells += [{"item": i, "verdict": "absent", "grounding": "model-absent"} for i in absent]
    return {"pub": pub, "title": "t", "rank": rank, "family": family or pub, "claims": cells}


def _deep(refs, n_claims=6):
    claims = [{"label": "claim %d[a]" % n, "claim_no": n, "independent": n == 1,
               "text": "limitation %d" % n} for n in range(1, n_claims + 1)]
    return {"claims": claims, "references": refs, "subject_label": "US-1-A1"}


LIMS = ["claim %d[a]" % n for n in range(1, 7)]


# ------------------------------------------------------------------ 1. the fallback key

def test_coverage_leads_when_nothing_anticipates():
    """The signal that matters in a 103-only case, and it is already computed."""
    deep = _deep([
        _ref("US-THIN-A1", LIMS[:1], absent=LIMS[1:], rank=1),      # best retrieval rank, 1 lim
        _ref("US-BROAD-A1", LIMS[:5], absent=LIMS[5:], rank=90),    # worst rank, 5 limitations
    ])
    out = cd.candidates({}, deep)
    assert [c["pub"] for c in out][0] == "US-BROAD-A1", (
        "coverage did not lead: %s" % [(c["pub"], c["n_limitations"]) for c in out])


def test_anticipation_still_decides_when_there_is_any(monkeypatch):
    """An anticipation is decisive on its own and must not be reordered by how much company it
    keeps. The fallback is a fallback."""
    deep = _deep([
        _ref("US-BROAD-A1", LIMS[:5], absent=LIMS[5:], rank=90),
        _ref("US-KILLER-A1", LIMS[:1], absent=LIMS[1:], rank=91),
    ])
    monkeypatch.setattr(cd, "_ledger_weights",
                        lambda rep: ({"US-KILLER-A1": {"anticipates": ["claim 1"], "adds": []}},
                                     None))
    out = cd.candidates({}, deep)
    assert out[0]["pub"] == "US-KILLER-A1"


# ------------------------------------------------------------------ 2. marginal coverage

def test_a_document_that_reaches_a_limitation_nothing_else_reaches_rises():
    """Set cover, not breadth. Three documents on the same four limitations plus one that reaches
    a fifth: the fifth is worth more than a fourth copy of the same four."""
    deep = _deep([
        _ref("US-A-A1", LIMS[:4], absent=LIMS[4:], rank=1),
        _ref("US-B-A1", LIMS[:4], absent=LIMS[4:], rank=2),
        _ref("US-C-A1", LIMS[:4], absent=LIMS[4:], rank=3),
        _ref("US-ONLY-A1", [LIMS[4]], absent=LIMS[:4] + LIMS[5:], rank=99),
    ])
    order = [c["pub"] for c in cd.candidates({}, deep)]
    assert order[0] == "US-A-A1", order
    assert order[1] == "US-ONLY-A1", (
        "the one document reaching a fifth limitation ranked below a duplicate: %s" % order)


def test_each_row_says_how_much_it_added():
    deep = _deep([
        _ref("US-A-A1", LIMS[:4], absent=LIMS[4:], rank=1),
        _ref("US-B-A1", LIMS[:4], absent=LIMS[4:], rank=2),
    ])
    out = cd.candidates({}, deep)
    assert out[0]["new_limitations"] == 4
    assert out[1]["new_limitations"] == 0, "a duplicate must not claim to add anything"


# ------------------------------------------------------------------ 3. unreadable

def _report_with_unread(*pubs):
    return {"deep_rank": {"not_readable": [{"pub": p, "title": "t"} for p in pubs]}}


def test_a_reference_never_read_in_full_is_marked_and_sorted_below_every_readable_one():
    """It scores HIGH, not low: a short abstract is mapped generously onto many limitations and
    every cell verifies against the abstract it came from. Left in the order it starves every
    readable document of anything new to add."""
    deep = _deep([
        _ref("US-ABSTRACTONLY-A1", LIMS[:6], rank=1),         # reads on everything, never read
        _ref("US-READ-A1", LIMS[:2], absent=LIMS[2:], rank=80),
    ])
    out = cd.candidates(_report_with_unread("US-ABSTRACTONLY-A1"), deep)
    by = {c["pub"]: c for c in out}
    assert by["US-ABSTRACTONLY-A1"]["readable"] is False
    assert by["US-READ-A1"]["readable"] is True
    assert [c["pub"] for c in out][0] == "US-READ-A1", (
        "an unread reference outranked a read one: %s" % [c["pub"] for c in out])


def test_an_unread_reference_is_never_pre_selected(monkeypatch):
    cands = [{"pub": "US-1-A1", "readable": False}, {"pub": "US-2-A1", "readable": True}]
    _patch_db(monkeypatch, {
        "US-1-A1": _row("US-1-A1"), "US-2-A1": _row("US-2-A1")})
    out = S.classify_candidates(cands, "2024-09-09")
    assert out[0]["basis"] == S.PUBLIC, "the dates are fine; readability is the only difference"
    assert out[0]["default_include"] is False
    assert out[1]["default_include"] is True


def test_the_unread_ones_the_grid_ranks_highest_stay_on_the_page():
    """Sorting them to the bottom also sorts them past the limit, and a grid-topping reference
    that vanishes with no explanation is the silent drop this whole change is about."""
    #  The readable ones between them reach EVERY limitation, so the unread one is not the sole
    #  reference for anything: it can only come back through the unread reserve, which is what
    #  this test is for. Without that, it returns through the sole-reach door and the guard passes
    #  with the reserve deleted.
    readable = [_ref("US-R%03d-A1" % i, LIMS, rank=i) for i in range(60)]
    unread = [_ref("US-UNREAD-A1", LIMS, rank=1)]
    out = cd.candidates(_report_with_unread("US-UNREAD-A1"), _deep(readable + unread), limit=40)
    pubs = [c["pub"] for c in out]
    assert not cd.sole_reach_notes(_deep(readable + unread)), "the fixture must have no sole reach"
    assert "US-UNREAD-A1" in pubs, "it fell off the page entirely"
    assert pubs.index("US-UNREAD-A1") >= 40, "it must be after everything that can be filed"
    assert len(out) <= 40 + cd.UNREADABLE_SHOWN


# ------------------------------------------------------------------ 4. say what was passed over

def test_passed_over_names_the_document_and_the_reason():
    cands = [
        {"pub": "US-PICKED-A1", "title": "t", "reads_on": 20, "n_limitations": 20,
         "default_include": True, "readable": True},
        {"pub": "US-THIN-A1", "title": "t", "reads_on": 18, "n_limitations": 4,
         "default_include": True, "readable": True},
        {"pub": "US-UNREAD-A1", "title": "t", "reads_on": 25, "n_limitations": 25,
         "default_include": False, "readable": False},
    ]
    out = {p["pub"]: p for p in S.passed_over(cands, budget_items=1)}
    assert "US-PICKED-A1" not in out, "it was selected, so it was not passed over"
    assert "reads on 18 limitations but only 4" in out["US-THIN-A1"]["why"]
    assert "never read" in out["US-UNREAD-A1"]["why"]
    assert out["US-UNREAD-A1"]["reads_on"] == 25 and out["US-UNREAD-A1"]["charts"] == 25


@pytest.mark.parametrize("cand,expect", [
    ({"readable": False, "basis": S.NOT_ART, "co_owned": True}, "never read"),
    ({"readable": True, "basis": S.NOT_ART, "not_art_why": "outside 102(a)(2)"},
     "outside 102(a)(2)"),
    ({"readable": True, "basis": S.PUBLIC, "co_owned": True}, "102(b)(2)(C)"),
    ({"readable": True, "basis": S.UNKNOWN}, "dates could not be established"),
    ({"readable": True, "basis": S.PUBLIC, "reads_on": 9, "n_limitations": 9},
     "ranks below"),
])
def test_the_reason_given_is_the_one_that_governs(cand, expect):
    """Several can be true at once. The one that decides is the one that is said."""
    assert expect in S._why_not(cand, budget_items=10)


# ------------------------------------------------------------------ shared fixtures

def _row(pub):
    import datetime
    return {"publication_number": pub, "publication_date": datetime.date(2020, 1, 1),
            "filing_date": datetime.date(2019, 1, 1),
            "earliest_priority_date": datetime.date(2019, 1, 1), "owners": [],
            "country": pub[:2]}


def _patch_db(monkeypatch, rows):
    class Cur:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return list(rows.values())

    class DB:
        @staticmethod
        def cursor():
            import contextlib

            @contextlib.contextmanager
            def cm():
                yield Cur()
            return cm()

    monkeypatch.setitem(sys.modules, "db", DB)


# ------------------------------- 5. the one document that reaches something nothing else does

def _lonely():
    """The real shape: not absent on one limitation, and nothing quotable to chart it with."""
    ref = _ref("DE-SELF-A1", [], absent=LIMS[:5], rank=200)
    ref["claims"].append({"item": LIMS[5], "verdict": "partial", "bar": "teaches",
                          "grounding": "teaches-unquoted",
                          "note": "discloses 130 to 170 degrees, which overlaps the claimed "
                                  "170 to 190 only at the endpoint"})
    return ref


def test_the_only_reference_reaching_a_limitation_is_reported_even_with_nothing_chartable():
    """The most valuable sentence a search produces, and it had nowhere to appear.

    It is not a filing candidate: with no quotable passage there is no chart row to build, and on
    the measured report the document is the applicant's own. It goes on the page as a finding
    about the CLAIMS, with what it actually teaches, because a practitioner building a case on it
    needs the overlap and the absence of a quotation as much as the reach.
    """
    others = [_ref("US-R%03d-A1" % i, LIMS[:3], absent=LIMS[3:], rank=i) for i in range(60)]
    deep = _deep(others + [_lonely()])

    notes = cd.sole_reach_notes(deep)
    assert [n["limitation"] for n in notes] == [LIMS[5]], notes
    n = notes[0]
    assert n["pub"] == "DE-SELF-A1"
    assert n["chartable"] is False, "an unquoted teaching cannot be charted"
    assert "overlaps the claimed" in n["note"], "say what it actually teaches"
    assert n["text"] == "limitation 6", "name the limitation in the claim's own words"

    #  And it is NOT offered as a document to file, because there is nothing to file for it.
    assert "DE-SELF-A1" not in [c["pub"] for c in cd.candidates({}, deep, limit=40)]


def test_a_sole_reach_document_is_offered_even_when_it_adds_nothing_chartable():
    """The exact shape of the real one, which is why the reserve exists at all.

    DE 10 2024 105 114 A1 charts one limitation, claim 1[a], which a hundred and one other
    references also chart, so it adds nothing to the cover and ranks 142nd. It is also the only
    document of 233 not absent on claim 1[e]. Coverage will never bring it back and it has to
    come back, so the reserve does it.
    """
    lonely = _ref("DE-SELF-A1", [LIMS[0]], absent=LIMS[1:5], rank=200)
    lonely["claims"].append({"item": LIMS[5], "verdict": "partial", "bar": "teaches",
                             "grounding": "teaches-unquoted", "note": "n"})
    others = [_ref("US-R%03d-A1" % i, LIMS[:5], rank=i) for i in range(60)]
    deep = _deep(others + [lonely])
    out = cd.candidates({}, deep, limit=40)
    by = {c["pub"]: c for c in out}
    assert by["DE-SELF-A1"]["new_limitations"] == 0, "it must add nothing, or the greedy carries it"
    assert "DE-SELF-A1" in by, "a one-of-a-kind fell off the page: %d rows" % len(out)
    assert by["DE-SELF-A1"]["sole_reach"] == [LIMS[5]]


def test_a_limitation_nothing_reaches_is_said_plainly():
    """The other half of the same answer, and the one that decides whether a claim survives.

    A reference that says "absent" and a reference with no cell for the limitation at all mean
    the same thing here: nobody reaches it. Both count as unreached.
    """
    deep = _deep([_ref("US-A-A1", LIMS[:2], absent=LIMS[2:4])])
    got = {u["limitation"] for u in cd.unreached_limitations(deep)}
    assert got == set(LIMS[2:]), got
    assert cd.unreached_limitations(_deep([_ref("US-A-A1", LIMS)])) == []
    #  and it carries the claim's own words, because a label alone says nothing
    assert cd.unreached_limitations(deep)[0]["text"] == "limitation 3"


def test_a_limitation_two_references_reach_makes_neither_of_them_sole():
    deep = _deep([_ref("US-A-A1", LIMS[:2], absent=LIMS[2:]),
                  _ref("US-B-A1", LIMS[:2], absent=LIMS[2:])])
    assert all(not c["sole_reach"] for c in cd.candidates({}, deep))


# ------------------------------------------------- 6. dead here, decisive at another office

def test_a_self_collision_the_us_cannot_use_says_where_it_can_be_used():
    """Counsel, 2026-08-24: "Dead in the US, lethal in Germany, and not available at the EPO. That
    is why the system should flag self-collisions rather than filter them: what is unusable in one
    office is decisive in another." """
    note = S.elsewhere_note("DE", us_reachable=False, co_owned=True)
    assert "§ 3(2) PatG" in note and "DPMA" in note
    assert "102(a)(2)" in note, "say what took it away here"
    assert "own earlier filing is the strongest" in note

    ep = S.elsewhere_note("EP", us_reachable=False, co_owned=False)
    assert "54(3)" in ep and "European Patent Office" in ep

    #  A document the United States CAN reach and that is not commonly owned has nothing to say.
    assert S.elsewhere_note("DE", us_reachable=True, co_owned=False) == ""
    #  And an office with no equivalent right in the table is not given an invented one.
    assert S.elsewhere_note("TW", us_reachable=False, co_owned=False) == ""


def test_the_note_reaches_the_candidate(monkeypatch):
    import datetime
    _patch_db(monkeypatch, {"DE-1-A1": {
        "publication_number": "DE-1-A1", "publication_date": datetime.date(2026, 1, 5),
        "filing_date": datetime.date(2017, 4, 27),
        "earliest_priority_date": datetime.date(2017, 4, 27),
        "owners": ["J. Schmalz GmbH"], "country": "DE"}})
    out = S.classify_candidates([{"pub": "DE-1-A1"}], "2024-09-09", ["J Schmalz GmbH"])
    assert out[0]["basis"] == S.NOT_ART, "a DE national publication is outside 102(a)(2)"
    assert out[0]["co_owned"] is True
    assert "PatG" in out[0]["elsewhere"], out[0]["elsewhere"]


def test_public_art_gets_no_elsewhere_note(monkeypatch):
    """It is prior art everywhere already. A note saying so would be noise on most of the table."""
    import datetime
    _patch_db(monkeypatch, {"DE-2-A1": {
        "publication_number": "DE-2-A1", "publication_date": datetime.date(2020, 1, 1),
        "filing_date": datetime.date(2019, 1, 1),
        "earliest_priority_date": datetime.date(2019, 1, 1), "owners": [], "country": "DE"}})
    out = S.classify_candidates([{"pub": "DE-2-A1"}], "2024-09-09")
    assert out[0]["basis"] == S.PUBLIC and out[0]["elsewhere"] == ""


# ------------------------------- 7. public art the coverage order cannot see

def _c(pub, reads, charts, basis=S.PUBLIC, picked=False, readable=True):
    return {"pub": pub, "title": "t", "reads_on": reads, "n_limitations": charts,
            "basis": basis, "default_include": picked, "readable": readable}


def test_public_art_at_least_as_broad_as_something_selected_is_always_named():
    """The coverage order cannot see the basis, and the basis is often what decides.

    Schunk's DE 10 2022 135 066 A1 published before the filing date, so it is 102(a)(1) art with
    no 102(b)(2) argument available against it. The member of the same disclosure that ranked
    higher is 102(a)(2) only. It read on 16, sat outside the top ten by breadth, and appeared
    nowhere: not in the picker, not in the passed-over table, not on any page.
    """
    cands = [_c("US-P%02d-A1" % i, 30 - i, 20, picked=True) for i in range(10)]
    #  Exactly at the floor, which is the boundary worth pinning: the narrowest document actually
    #  selected reads on 21, so 21 is "at least as broad as something you picked".
    cands.append(_c("DE-STRONG-A1", 21, 1))
    cands.append(_c("US-WEAK-A1", 21, 1, basis=S.SECRET))
    out = {p["pub"]: p for p in S.passed_over(cands, budget_items=10)}
    assert "DE-STRONG-A1" in out, "public art as broad as a selected document was not named"
    assert out["DE-STRONG-A1"]["basis_label"] == "102(a)(1) public art"
    assert "only 1 carries a verified passage" in out["DE-STRONG-A1"]["why"]
    #  Below the floor it is not forced in: the table has to stay readable.
    thin = cands + [_c("DE-NARROW-A1", 3, 1)]
    assert "DE-NARROW-A1" not in {p["pub"] for p in S.passed_over(thin, budget_items=10)}


def test_public_art_is_listed_before_the_rest():
    cands = [_c("US-P%02d-A1" % i, 30 - i, 20, picked=True) for i in range(10)]
    cands += [_c("US-SECRET-A1", 29, 2, basis=S.SECRET), _c("DE-PUBLIC-A1", 21, 2)]
    order = [p["pub"] for p in S.passed_over(cands, budget_items=10)]
    assert order.index("DE-PUBLIC-A1") < order.index("US-SECRET-A1"), order


def test_the_table_is_capped_so_somebody_reads_to_the_bottom():
    cands = [_c("US-P%02d-A1" % i, 30, 20, picked=True) for i in range(10)]
    cands += [_c("US-X%03d-A1" % i, 30, 1) for i in range(200)]
    out = S.passed_over(cands, budget_items=10)
    assert len(out) == S.PASSED_OVER_MAX
    assert len({p["pub"] for p in out}) == len(out), "a document was listed twice"
