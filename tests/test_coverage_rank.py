"""Ranking by CONTRIBUTION: the property the old ranker provably lacked.

Every prior-art search exists to support one argument: disclosure by disclosure, was this already
known, and where. Scoring references independently and sorting gets that objective wrong, and the
test below is the case that proves it.
"""
import coverage_rank as CVR
import disclosures


def _ref(covered, score=50.0):
    return {"score": score,
            "covered": [{"item": k, "verdict": v} for k, v in covered.items()]}


IDF = {"d1": 1.0, "d2": 1.0, "d3": 1.0, "d10": 1.0}


def test_the_document_that_uniquely_answers_disclosure_ten_is_not_buried():
    """THE case. If many documents cover d1-d3 and exactly one covers d10, the old pointwise sort
    put every one of them above it, so the report showed the most similar results and stayed
    permanently blind to the only reference that speaks to d10."""
    by = {f"DUP-{i}": _ref({"d1": "disclosed", "d2": "disclosed", "d3": "disclosed"}, score=90)
          for i in range(20)}
    by["UNIQUE"] = _ref({"d10": "disclosed"}, score=40)
    order = sorted(by, key=lambda p: -by[p]["score"])          # the pointwise order
    assert order.index("UNIQUE") == len(order) - 1             # dead last, by construction
    new, gains = CVR.rank(order, by, IDF)
    assert new.index("UNIQUE") <= 1, f"UNIQUE landed at {new.index('UNIQUE')}"
    assert new[0].startswith("DUP"), "the strongest reference must still lead the report"


def test_a_redundant_document_scores_no_new_gain():
    by = {"A": _ref({"d1": "disclosed", "d2": "disclosed"}),
          "B": _ref({"d1": "disclosed", "d2": "disclosed"}),
          "C": _ref({"d3": "disclosed"})}
    new, gains = CVR.rank(["A", "B", "C"], by, IDF, corroboration=0.0, score_weight=0.0)
    assert new[1] == "C", "the document adding something new must come second"
    assert gains[2] == 0.0, "the duplicate adds nothing and must be recorded as adding nothing"


def test_corroboration_lets_a_second_witness_still_count():
    """A disclosure proven once is proven, but an argument is usually safer with two, and an
    examiner cites more than one. corroboration=0 is the purist setting, not the only one."""
    by = {"A": _ref({"d1": "disclosed"}), "B": _ref({"d1": "disclosed"}), "C": _ref({})}
    _n, g0 = CVR.rank(["A", "B", "C"], by, IDF, corroboration=0.0, score_weight=0.0)
    _n, g1 = CVR.rank(["A", "B", "C"], by, IDF, corroboration=1.0, score_weight=0.0)
    assert g0[1] == 0.0 and g1[1] > 0.0


def test_a_weak_verdict_cannot_shut_a_disclosure_down():
    """'uncertain' is a disclosure an independent refuter would not confirm. It may contribute; it
    may not close a disclosure off, so a later confident reference must still gain over it."""
    by = {"WEAK": _ref({"d1": "uncertain"}), "STRONG": _ref({"d1": "disclosed"})}
    order, gains = CVR.rank(["WEAK", "STRONG"], by, IDF, corroboration=0.0, score_weight=0.0)
    assert order[0] == "STRONG", "the confident reference is the better first pick"
    #  and taken the other way round, the confident one still earns the difference
    by2 = {"WEAK": _ref({"d1": "uncertain"}), "OTHER": _ref({"d2": "disclosed"}),
           "STRONG": _ref({"d1": "disclosed"})}
    o2, g2 = CVR.rank(["OTHER", "WEAK", "STRONG"], by2, IDF,
                      corroboration=0.0, score_weight=0.0)
    assert g2[o2.index("STRONG")] > 0.0


def test_ranking_is_a_permutation_and_never_drops_a_reference():
    by = {f"P{i}": _ref({"d1": "disclosed"} if i % 2 else {"d2": "partial"}) for i in range(30)}
    order = list(by)
    new, _g = CVR.rank(order, by, IDF, depth=10)
    assert sorted(new) == sorted(order) and len(new) == len(order)


def test_no_disclosures_leaves_the_order_untouched():
    by = {"A": _ref({"d1": "disclosed"})}
    assert CVR.rank(["A"], by, {})[0] == ["A"]


def test_covered_mass_counts_dead_slots():
    by = {"A": _ref({"d1": "disclosed", "d2": "disclosed"}),
          "B": _ref({"d1": "disclosed"}), "C": _ref({"d1": "disclosed"})}
    cov, tot, dead = CVR.covered_mass(["A", "B", "C"], by, IDF)
    assert dead == 2 and cov == 2.0 and tot == 4.0


# --- the checklist itself ---------------------------------------------------------------------
def test_disclosure_kinds_are_weighted_by_legal_load():
    """An independent claim's limitation decides validity; a potential claim is contingency."""
    W = disclosures.KIND_WEIGHT
    assert W["independent_limitation"] > W["dependent_limitation"] > W["potential_claim"]
    assert W["combination"] > W["dependent_limitation"]


def test_extract_is_fail_soft_and_deduplicates(monkeypatch):
    monkeypatch.setattr(disclosures.llm, "chat_json", lambda *a, **k: {"disclosures": [
        {"text": "a sealing lip deflects inward under vacuum", "kind": "independent_limitation",
         "source": "claim 1"},
        {"text": "A sealing lip deflects inward under vacuum.", "kind": "potential_claim"},
        {"text": "short", "kind": "potential_claim"},
        {"text": "the body is made of aluminium alloy", "kind": "nonsense_kind"},
    ]})
    got = disclosures.extract(claims=["1. a claim"], description="x" * 500)
    assert len(got) == 2, [d["text"] for d in got]
    assert got[0]["kind"] == "independent_limitation"       # heaviest first
    assert got[1]["kind"] == "potential_claim"              # unknown kind coerced


def test_extract_returns_empty_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(disclosures.llm, "chat_json", lambda *a, **k: {})
    assert disclosures.extract(claims=["1. a claim"], description="x" * 500) == []
    assert disclosures.extract(claims=[], description="") == []
