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
    readable = [_ref("US-R%03d-A1" % i, LIMS[:2], absent=LIMS[2:], rank=i) for i in range(60)]
    unread = [_ref("US-UNREAD-A1", LIMS[:6], rank=1)]
    out = cd.candidates(_report_with_unread("US-UNREAD-A1"), _deep(readable + unread), limit=40)
    pubs = [c["pub"] for c in out]
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
