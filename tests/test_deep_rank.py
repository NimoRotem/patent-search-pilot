"""The rebuild: search with a query set, rank on what the references were measured to disclose.

Every test here is anchored on a MEASURED failure from RECALL_STUDY_2026-08-02.md, so a
regression breaks a test rather than quietly reappearing in a report.
"""
import math

import pytest

import agent
import deep_analysis
import deep_rank
import query_set
import retrieval
import webview


# ---------------------------------------------------------------------------------------------
# query_set: never embed the figure prose, never search with only one long query
# ---------------------------------------------------------------------------------------------
LIVE_QUERY = (
    "This invention describes a portable or hand-held vacuum gripper with a rigid base element "
    "and a loop-shaped vacuum seal element.\r\n\r\n"
    "Drawings (figures analysed and folded into the query text; the raw drawings are ALSO sent to "
    "the image-similarity channel): FIGURE 1 depicts a perspective view of a vacuum gripper 200, "
    "which appears to be an oval or racetrack-shaped ring. It comprises an outer wall 210 and an "
    "inner wall 220, defining a channel 230 between them."
)


def test_retrieval_text_strips_the_folded_in_figure_description():
    """54% of the live query was figure prose. Deleting it moved a named reference from dense
    rank #528 to #35 with nothing else changed."""
    out = query_set.retrieval_text(LIVE_QUERY)
    assert "portable or hand-held vacuum gripper" in out
    assert "FIGURE 1" not in out
    assert "outer wall 210" not in out
    assert "figures analysed" not in out
    #  and the block is still recoverable for the reader / audit
    assert "FIGURE 1" in query_set.figure_text(LIVE_QUERY)


def test_retrieval_text_leaves_a_typed_query_alone():
    typed = "handheld battery powered vacuum lifter with a foam sealing lip"
    assert query_set.retrieval_text(typed) == typed


def test_build_makes_many_short_queries_and_never_only_one(monkeypatch):
    monkeypatch.setattr(query_set.llm, "chat_json", lambda *a, **k: {
        "essence": "Battery powered portable vacuum gripper with a deformable peripheral seal",
        "alts": ["handheld vacuum lifter", "powered suction cup", "handheld vacuum lifter"]})
    query_set._CACHE.clear()
    specs = query_set.build(LIVE_QUERY,
                            elements=["a rigid base element", "a loop-shaped seal"],
                            claims=[{"claim_no": 1, "text": "A vacuum gripper comprising a rigid "
                                                            "base element and a seal.",
                                     "independent": True},
                                    {"claim_no": 2, "text": "The gripper of claim 1.",
                                     "independent": False}])
    kinds = [s.kind for s in specs]
    assert "essence" in kinds and "alt" in kinds and "brief" in kinds
    assert kinds.count("element") == 2 and kinds.count("claim") == 2
    #  the duplicate alternative is dropped, not embedded twice
    assert len([s for s in specs if s.kind == "alt"]) == 2
    #  nothing that reaches an embedding may carry the figure prose
    assert not any("FIGURE 1" in s.text for s in specs)
    #  the independent claim is ordered ahead of the dependent one
    claims = [s for s in specs if s.kind == "claim"]
    assert claims[0].name == "claim1"


def test_build_still_works_without_the_llm(monkeypatch):
    monkeypatch.setattr(query_set.llm, "chat_json", lambda *a, **k: {})
    query_set._CACHE.clear()
    specs = query_set.build(LIVE_QUERY, elements=["a rigid base element"])
    assert [s.kind for s in specs] == ["brief", "element"]


def test_seed_specs_excludes_element_queries():
    specs = [query_set.QuerySpec("a", "x" * 20, "essence"),
             query_set.QuerySpec("b", "y" * 20, "element"),
             query_set.QuerySpec("c", "z" * 20, "claim")]
    assert [s.kind for s in query_set.seed_specs(specs)] == ["essence", "claim"]


# ---------------------------------------------------------------------------------------------
# the wrong-abstract guard
# ---------------------------------------------------------------------------------------------
def test_a_foreign_abstract_is_not_shown_to_the_screener():
    """US-10625955-B2 "Electric vacuum suction lifter" carries a touch-display abstract UPSTREAM.
    The screener scored it 0, three times out of three."""
    assert not deep_rank.abstract_is_trustworthy(
        "A touch display device includes a display module, a touch module and a "
        "light-transmitting substrate fixed by optical adhesive.",
        "Electric vacuum suction lifter",
        "An electric vacuum suction lifter comprising a main body and an annular sealing unit")


def test_a_normal_abstract_is_trusted():
    assert deep_rank.abstract_is_trustworthy(
        "A vacuum gripper having a sealing element mounted on a rigid base plate.",
        "Vacuum gripper",
        "A vacuum gripper comprising a rigid base plate")


def test_a_missing_abstract_is_not_trusted():
    assert not deep_rank.abstract_is_trustworthy("", "Vacuum gripper", "A vacuum gripper")


# ---------------------------------------------------------------------------------------------
# scoring: grounded evidence only, weighted by rarity
# ---------------------------------------------------------------------------------------------
FEATURES = ["portable vacuum gripper", "bracing structure limiting seal compression"]


def _ref(pub, rows, method="llm", chars=50000):
    return {"pub": pub, "title": pub, "method": method, "chars": chars,
            "features": rows, "claims": []}


def _row(item, verdict, grounding="verified", quote="a real verbatim passage from the reference",
         location="claim 1", confidence=0.8):
    return {"item": item, "verdict": verdict, "grounding": grounding, "quote": quote,
            "location": location, "confidence": confidence, "kind": "feature"}


def test_ungrounded_rows_never_count():
    """The free-form full-text score gave 85 to nine records holding ZERO characters of text,
    inventing 8 to 12 quotes each. Only quotes that passed the grounding gate may score."""
    charts = [
        _ref("US-1", [_row(FEATURES[0], "disclosed"), _row(FEATURES[1], "disclosed")]),
        _ref("SU-1", [_row(FEATURES[0], "disclosed", grounding="dropped-ungrounded-quote"),
                      _row(FEATURES[1], "disclosed", grounding="dropped-ungrounded-quote")],
             method="no-text", chars=0),
    ]
    rar = deep_rank.rarity(charts, FEATURES, [])
    assert rar["feature_df"] == {FEATURES[0]: 1, FEATURES[1]: 1}
    good, _ = deep_rank.score_reference(charts[0], rar)
    junk, detail = deep_rank.score_reference(charts[1], rar)
    assert junk == 0
    assert good > 0
    assert detail["read_in_full"] is False


def test_a_rare_disclosure_outscores_a_common_one():
    """"portable vacuum gripper" was disclosed by 34 of 60 references and "bracing structure
    protrudes less than the seal" by 9 of 60. The second is what a novelty argument turns on."""
    common = [_ref(f"C-{i}", [_row(FEATURES[0], "disclosed")]) for i in range(9)]
    rare = _ref("RARE-1", [_row(FEATURES[1], "disclosed")])
    charts = common + [rare]
    rar = deep_rank.rarity(charts, FEATURES, [])
    assert rar["feature_df"][FEATURES[0]] == 9
    assert rar["feature_df"][FEATURES[1]] == 1
    common_score, _ = deep_rank.score_reference(common[0], rar)
    rare_score, _ = deep_rank.score_reference(rare, rar)
    assert rare_score > common_score


def test_score_is_a_share_of_the_distinctive_mass_not_a_rank():
    """A reference grounding every feature scores 100; adding an unrelated reference does not
    change an existing reference's number."""
    full = _ref("US-FULL", [_row(f, "disclosed") for f in FEATURES])
    rar = deep_rank.rarity([full], FEATURES, [])
    assert deep_rank.score_reference(full, rar)[0] == 100
    partial = _ref("US-PART", [_row(FEATURES[0], "partial")])
    assert deep_rank.score_reference(partial, rar)[0] < 100


def test_refuted_rows_are_worth_less_than_confirmed_ones():
    rar = deep_rank.rarity([_ref("A", [_row(f, "disclosed") for f in FEATURES])], FEATURES, [])
    confirmed = _ref("A", [_row(FEATURES[1], "disclosed")])
    refuted = _ref("B", [_row(FEATURES[1], "uncertain")])
    assert deep_rank.score_reference(confirmed, rar)[0] > deep_rank.score_reference(refuted, rar)[0]


def test_why_is_assembled_from_the_evidence_and_says_when_nothing_was_read():
    rar = deep_rank.rarity([_ref("A", [_row(f, "disclosed") for f in FEATURES])], FEATURES, [])
    ref = _ref("A", [_row(FEATURES[1], "disclosed", location="claim 7")])
    _, detail = deep_rank.score_reference(ref, rar)
    why = deep_rank._why(ref, detail)
    assert "Read in full" in why and "claim 7" in why
    empty = _ref("Z", [], method="no-text", chars=0)
    _, d2 = deep_rank.score_reference(empty, rar)
    assert "no text in the corpus" in deep_rank._why(empty, d2)


def test_by_feature_puts_the_rarest_feature_first_and_disclosed_before_partial():
    charts = [_ref(f"COMMON-{i}", [_row(FEATURES[0], "disclosed")]) for i in range(5)]
    charts += [_ref("RARE-P", [_row(FEATURES[0], "disclosed"), _row(FEATURES[1], "partial")]),
               _ref("RARE-D", [_row(FEATURES[0], "disclosed"), _row(FEATURES[1], "disclosed")])]
    rar = deep_rank.rarity(charts, FEATURES, [])
    assert rar["feature_df"][FEATURES[0]] == 7 and rar["feature_df"][FEATURES[1]] == 2
    rows = deep_rank.by_feature(charts, rar)
    assert rows[0]["feature"] == FEATURES[1]           # rarest first
    assert rows[0]["references"][0]["pub"] == "RARE-D"  # disclosed before partial
    assert rows[0]["df"] == 2


# ---------------------------------------------------------------------------------------------
# what the card shows
# ---------------------------------------------------------------------------------------------
def _report_with_deep_rank():
    return {"deep_rank": {
        "by_pub": {"US-READ-B2": {"score": 82, "screen": 75, "family": "F1", "retrieval_rank": 9,
                                  "why": "Read in full.", "n_disclosed": 9, "n_partial": 1,
                                  "n_features": 12, "read_in_full": True, "chars_read": 142823,
                                  "covered": []}},
        "unread": {"US-SCREENED-A1": 95},
        "feature_df": {f: 1 for f in FEATURES}}}


def test_a_read_reference_is_scored_on_its_reading():
    card = {"pub": "US-READ-B2", "relevancy": 55}
    webview._attach_deep_rank(_report_with_deep_rank(), card)
    assert card["relevancy_score"] == 82
    assert card["relevancy_source"] == "deep_rank"
    assert card["deep_read"] is True


def test_an_unread_candidate_is_capped_below_a_read_one():
    """A judgement made from an abstract must never outrank a reference whose full text was
    quoted, however confident the screener was."""
    card = {"pub": "US-SCREENED-A1", "relevancy": 99}
    webview._attach_deep_rank(_report_with_deep_rank(), card)
    assert card["relevancy_score"] == deep_rank.UNREAD_SCORE_CAP == 70
    assert card["deep_read"] is False
    assert card["relevancy_score"] < 82


def test_a_federated_only_card_is_capped_too():
    card = {"pub": "EP-EXTERNAL-A1", "relevancy": 99}
    webview._attach_deep_rank(_report_with_deep_rank(), card)
    assert card["relevancy_score"] == deep_rank.UNREAD_SCORE_CAP
    assert card["relevancy_source"] == "unread"
    assert "could not be read in full" in card["relevancy_opinion"]


def test_a_report_with_no_deep_rank_is_left_alone():
    card = {"pub": "US-1", "relevancy": 55}
    webview._attach_deep_rank({}, card)
    assert "relevancy_score" not in card


# ---------------------------------------------------------------------------------------------
# the defects the study found
# ---------------------------------------------------------------------------------------------
def test_claim_focus_still_searches_the_description(monkeypatch):
    """The claim preset used to omit `dense`, so description paragraphs were unsearchable. On the
    case that prompted the rebuild the best passage of the #1 result was a description paragraph
    and only 10 of 25 displayed cards matched on a claim at all."""
    r = object.__new__(retrieval.Retriever)
    r._fam = {1: "F1", 2: "F2"}
    monkeypatch.setattr(r, "channel_claim_dense", lambda *a, **k: [(1, 0.9)])
    monkeypatch.setattr(r, "channel_dense", lambda *a, **k: [(2, 0.8)])
    for name in ("channel_claim_bm25", "channel_cpc", "channel_citation_family",
                 "channel_qbe", "channel_biblio", "channel_crosslingual"):
        monkeypatch.setattr(r, name, lambda *a, **k: [])
    out = r.search("a vacuum gripper", config="claim_agentic", do_rerank=False)
    assert "claim_dense" in out.channel_hits
    assert "dense" in out.channel_hits, "claim focus must BOOST claims, not delete all-text search"


def test_rerank_depth_is_the_documented_one_not_a_hard_coded_25(monkeypatch):
    """retrieval.RERANK_TOP said 50 and carried a comment explaining the raise, while
    agent._final_rank hard-coded 25 in two places, so the raise never reached the live path."""
    seen = {}

    class FakeR:
        def rerank_families(self, query, fam, top=None, on_progress=None, return_meta=False):
            seen["top"] = top
            seen["query"] = query
            return (fam, {"attempted": True, "applied": True, "scored": len(fam),
                          "requested": len(fam), "model": "x"})

    a = agent.CoverageAgent.__new__(agent.CoverageAgent)
    a.r = FakeR()
    a._rank_text = "the de-figured text"
    ledger = agent.CoverageLedger(["e"])
    for i in range(80):
        ledger.register_families([(f"F{i}", i, 1.0 - i / 100.0)], bucket="seed")
    monkeypatch.setattr(retrieval, "RERANK_TOP", 50)
    a._final_rank("the ORIGINAL query with figure prose", ledger, return_meta=True)
    assert seen["top"] == 50
    #  and the cross-encoder is given the de-figured text, not the figure prose
    assert seen["query"] == "the de-figured text"


def test_extend_to_tops_up_from_families_the_cards_do_not_cover():
    """The old slice was ranked_families[len(cards):], which assumed the cards were exactly the
    first N ranked families. After a rerank and a federated merge they are not."""
    report = {"ranked_families": ["F1", "F2", "F3", "F4", "F5"]}
    cards = [{"pub": "P3", "family": "F3", "rank": 1}, {"pub": "P1", "family": "F1", "rank": 2}]

    class FakeCur:
        def close(self):
            pass

    def fake_reps(cur, keys):
        return {k: {"publication_number": "P" + k[1:], "title": k} for k in keys}

    import webview as wv
    orig = wv.resolve_family_reps
    wv.resolve_family_reps = fake_reps
    try:
        out = deep_analysis._extend_to(list(cards), report, 5)
    finally:
        wv.resolve_family_reps = orig
    pubs = [c["pub"] for c in out]
    assert pubs[:2] == ["P3", "P1"]
    #  F3 and F1 are already covered, so the top-up is F2, F4, F5 — never F3 again
    assert set(pubs[2:]) == {"P2", "P4", "P5"}
    assert len(pubs) == len(set(pubs))


def test_deep_analysis_cache_version_was_bumped():
    """Caches written by the pre-deep_rank path were built against a partial, pre-listwise
    ordering and were never invalidated."""
    assert deep_analysis.VERSION >= 3


def test_merge_take_is_no_longer_eight():
    """8 threw away 192 of the 200 families the document-chunk channel found."""
    import webapp
    assert webapp.MERGE_TAKE >= 50
    assert webapp._DISPLAY_TOP >= 50


# ---------------------------------------------------------------------------------------------
# regressions found by running the rebuilt pipeline end to end on the original case
# ---------------------------------------------------------------------------------------------
def test_a_reference_read_in_full_outranks_one_that_was_only_screened():
    """Measured on a live run of the rebuilt pipeline: three federated-only hits sitting at the
    unread cap took the TOP THREE slots, ahead of a reference that grounded 9 of 12 features."""
    import webapp
    cards = [{"pub": "FED-1", "deep_read": False, "deep_score": 70},
             {"pub": "FED-2", "deep_read": False, "deep_score": 70},
             {"pub": "READ-9", "deep_read": True, "deep_score": 69},
             {"pub": "READ-5", "deep_read": True, "deep_score": 51}]
    out = webapp.order_cards_by_evidence(cards)
    assert [c["pub"] for c in out] == ["READ-9", "READ-5", "FED-1", "FED-2"]
    assert [c["rank"] for c in out] == [1, 2, 3, 4]
    #  a permutation: nothing added, nothing dropped
    assert sorted(c["pub"] for c in out) == sorted(c["pub"] for c in cards)


def test_evidence_order_is_stable_for_equal_scores():
    import webapp
    cards = [{"pub": f"P{i}", "deep_read": True, "deep_score": 40} for i in range(5)]
    assert [c["pub"] for c in webapp.order_cards_by_evidence(cards)] == \
           ["P0", "P1", "P2", "P3", "P4"]


def test_a_federated_hit_dedups_against_the_corpus_across_the_dropped_zero_spelling():
    """Google/PQAI return US20190375604A1; the corpus stores US-2019375604-A1. The exact join
    failed, so the SAME invention was rendered twice, once as a corpus card and once as a
    federated-only card five places higher."""
    seen = {}

    class FakeCur:
        def execute(self, sql, params):
            seen["keys"] = set(params[0])

        def fetchall(self):
            return [{"id": 16653, "k": "US2019375604A1", "fam": "66996049"}]

    out = webview._resolve_fed_pubs(FakeCur(), ["US20190375604A1"])
    assert "US2019375604A1" in seen["keys"], "the zero-stripped spelling must be queried too"
    #  and the answer comes back keyed the way the caller asked
    assert out["US20190375604A1"] == (16653, "66996049")


def test_the_reference_is_read_for_features_and_claims_separately(monkeypatch):
    """Asking for 12 feature rows AND 13 claim rows in one answer made the model economise: the
    same reference grounded 10 of 12 features when asked about features alone and 2 when the
    claims were asked for in the same breath."""
    asked = []

    def fake_chat(system, user, **kw):
        payload = __import__("json").loads(user)
        asked.append((len(payload["subject_features"]), len(payload["subject_claims"])))
        return {"features": [], "claims": []}

    monkeypatch.setattr(deep_analysis.llm, "chat_json", fake_chat)
    monkeypatch.setattr(deep_analysis, "full_text", lambda pub, **kw: {
        "found": True, "pub": pub, "title": "t", "chars": 10, "n_claims": 1, "n_paragraphs": 0,
        "truncated": False, "passages": [{"kind": "claim", "coord": {"claim_no": 1},
                                          "label": "claim 1", "text": "a passage"}]})
    deep_analysis.analyse_reference("US-1", ["f1", "f2"],
                                    [{"label": "claim 1", "claim_no": 1, "text": "a claim"}])
    assert asked == [(2, 0), (0, 1)], "features and claims must be two focused reads"


def test_a_typed_query_with_no_claims_still_makes_one_call(monkeypatch):
    calls = []
    monkeypatch.setattr(deep_analysis.llm, "chat_json",
                        lambda s, u, **k: calls.append(1) or {"features": [], "claims": []})
    monkeypatch.setattr(deep_analysis, "full_text", lambda pub, **kw: {
        "found": True, "pub": pub, "title": "t", "chars": 10, "n_claims": 0, "n_paragraphs": 1,
        "truncated": False, "passages": [{"kind": "paragraph", "coord": {}, "label": "p1",
                                          "text": "a passage"}]})
    deep_analysis.analyse_reference("US-1", ["f1"], [])
    assert len(calls) == 1


# ---------------------------------------------------------------------------------------------
# the holistic score is a blend, and it is GATED on grounded evidence
# ---------------------------------------------------------------------------------------------
def test_the_holistic_score_is_ignored_without_grounded_evidence():
    """The free-form full-text number gave 85 to records with ZERO characters of text. It may only
    contribute when the reference was read AND at least one quote survived the grounding gate."""
    rar = deep_rank.rarity([_ref("A", [_row(f, "disclosed") for f in FEATURES])], FEATURES, [])
    empty = _ref("SU-1", [_row(FEATURES[0], "disclosed", grounding="dropped-ungrounded-quote")],
                 method="no-text", chars=0)
    empty["overall"] = {"score": 85, "why": "looks relevant"}
    score, detail = deep_rank.score_reference(empty, rar)
    assert score == 0
    assert detail["overall"] is None


def test_the_holistic_score_lifts_a_conservatively_charted_reference():
    """The chart's refuter defaults to "refuted" and the prompt says "absent" is expected, which is
    right for a legal artefact and compresses a ranking: a reference an examiner-style read scored
    85 grounded only 2 clean disclosures."""
    charts = [_ref(f"C-{i}", [_row(FEATURES[0], "disclosed")]) for i in range(6)]
    thin = _ref("US-CONSERVATIVE", [_row(FEATURES[0], "partial")])
    rar = deep_rank.rarity(charts + [thin], FEATURES, [])
    bare, _ = deep_rank.score_reference(thin, rar)
    thin["overall"] = {"score": 85, "why": "reads on the invention as a whole"}
    blended, detail = deep_rank.score_reference(thin, rar)
    assert blended > bare
    assert detail["overall"] == 85
    assert detail["coverage"] == bare


def test_leading_a_rare_feature_is_credited():
    """DE-3724659-A1 discloses 1 of 12 features and is among the best disclosures of the
    characterising one out of 183 read. Coverage alone puts it off the page."""
    charts = [_ref(f"C-{i}", [_row(FEATURES[0], "disclosed")]) for i in range(8)]
    narrow = _ref("DE-NARROW", [_row(FEATURES[1], "disclosed")])
    charts.append(narrow)
    rar = deep_rank.rarity(charts, FEATURES, [])
    lead = deep_rank.leaders(charts, rar)
    assert "DE-NARROW" in lead
    without, _ = deep_rank.score_reference(narrow, rar)
    with_lead, detail = deep_rank.score_reference(narrow, rar, lead=lead["DE-NARROW"])
    assert with_lead > without
    assert detail["leads"] == [FEATURES[1]]


def test_an_uncertain_row_is_worth_more_than_nothing_but_less_than_a_disclosure():
    """The refuter is told to default to refuted when unsure, because its job is to protect a legal
    chart. That bias must not zero out a real, located, verbatim quote for ranking purposes."""
    assert 0 < deep_rank._W["uncertain"] < deep_rank._W["disclosed"]
    assert deep_rank._W["uncertain"] >= 0.4
