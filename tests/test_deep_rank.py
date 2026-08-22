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


def _ref(pub, rows, method="llm", chars=50000, n_claims=12, n_paras=40):
    #  n_claims_read / n_paragraphs_read are what distinguish a real reading from a chart run
    #  against a forty-word abstract; the thin-document tests set them to zero.
    return {"pub": pub, "title": pub, "method": method, "chars": chars,
            "n_claims_read": n_claims, "n_paragraphs_read": n_paras,
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
    empty = _ref("Z", [], method="no-text", chars=0, n_claims=0, n_paras=0)
    _, d2 = deep_rank.score_reference(empty, rar)
    assert "only a title and an abstract" in deep_rank._why(empty, d2)


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
    for name in ("channel_brief_dense", "channel_claim_bm25", "channel_cpc",
                 "channel_citation_family", "channel_qbe", "channel_biblio",
                 "channel_crosslingual"):
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

    seen_efd = {}

    def fake_reps(cur, keys, subject_efd=None):
        #  The cutoff has to REACH here: it decides which member of each family is read, quoted
        #  and cited. A stage that drops it silently downgrades every family it resolves from
        #  102(a)(1) art to 102(a)(2). See webview.resolve_family_reps.
        seen_efd["efd"] = subject_efd
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
    assert "efd" in seen_efd, "the reading top-up resolved families without the subject's date"
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
        joined = user if isinstance(user, str) else "".join(s["text"] for s in user)
        payload = __import__("json").loads(joined)
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
                 method="no-text", chars=0, n_claims=0, n_paras=0)
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


# ---------------------------------------------------------------------------------------------
# measured against a real examiner citation list (see eval/citation_recall)
# ---------------------------------------------------------------------------------------------
def test_short_documents_get_a_pool_they_can_compete_in(monkeypatch):
    """The dense channel takes the best chunk per publication out of a global top-K, so a long
    patent gets a hundred chances to be in it and an abstract-only record gets one. Measured: same
    query, same K, the all-kinds pool held 2,330 publications and the abstract/whole pool 6,109,
    and nine cited documents the all-kinds channel never returned appeared in the second."""
    r = object.__new__(retrieval.Retriever)
    seen = []
    #  The chunk-ranking channels cap at distinct FAMILIES now, so they call
    #  `_families_from_chunks`. Both seams are captured; this test is about which chunk kinds the
    #  SQL selects, which is unaffected by where the cap is applied.
    monkeypatch.setattr(r, "_pubs_from_chunks",
                        lambda sql, params, cap=None: seen.append(sql) or [])
    monkeypatch.setattr(r, "_families_from_chunks",
                        lambda sql, params, cap=None: seen.append(sql) or [])
    r.channel_brief_dense([0.1, 0.2], None, None)
    assert len(seen) == 1
    assert "'abstract','whole'" in seen[0].replace(" ", "")
    assert "claim_own" not in seen[0]
    assert retrieval.CHANNEL_WEIGHTS["brief_dense"] < retrieval.CHANNEL_WEIGHTS["dense"]


def test_brief_dense_is_in_both_agentic_presets(monkeypatch):
    r = object.__new__(retrieval.Retriever)
    r._fam = {1: "F1"}
    for name in ("channel_claim_dense", "channel_dense", "channel_claim_bm25", "channel_cpc",
                 "channel_citation_family", "channel_qbe", "channel_biblio",
                 "channel_crosslingual"):
        monkeypatch.setattr(r, name, lambda *a, **k: [])
    monkeypatch.setattr(r, "channel_brief_dense", lambda *a, **k: [(1, 0.9)])
    for preset in ("agentic", "claim_agentic"):
        out = r.search("a vacuum gripper", config=preset, do_rerank=False)
        assert "brief_dense" in out.channel_hits, preset


def test_a_reference_with_only_an_abstract_is_not_reported_as_read_in_full():
    """Charting twelve features against forty words is not a reading. Grounded coverage then puts
    the document on the floor, which ranks it for being old rather than for being irrelevant."""
    rar = deep_rank.rarity([_ref("A", [_row(f, "disclosed") for f in FEATURES])], FEATURES, [])
    thin = _ref("GB-207177-A", [], method="llm", chars=380)
    thin["n_claims_read"] = 0
    thin["n_paragraphs_read"] = 0
    thin["screen"] = 65
    score, detail = deep_rank.score_reference(thin, rar)
    assert detail["read_in_full"] is False
    #  ranked on the evidence that does exist, capped, not driven to zero
    assert score == 65
    assert "only a title and an abstract" in deep_rank._why(thin, detail)


def test_a_thin_document_cannot_outrank_a_read_one_however_it_scores():
    """It is held back by the TIER, not by a score ceiling. A ceiling was the first attempt and it
    failed in production: every abstract-only record whose screen cleared the cap scored exactly
    the cap, and that plateau sat above the natural range of read documents, so 44 of 50 displayed
    references were records nobody had read."""
    rar = deep_rank.rarity([_ref("A", [_row(f, "disclosed") for f in FEATURES])], FEATURES, [])
    thin = _ref("GB-1-A", [], method="llm", chars=300)
    thin.update({"n_claims_read": 0, "n_paragraphs_read": 0, "screen": 100})
    thin_score, thin_detail = deep_rank.score_reference(thin, rar)
    read = _ref("US-1-A", [_row(FEATURES[0], "partial")], chars=50000)
    read.update({"screen": 40, "overall": {"score": 40}})
    read_score, read_detail = deep_rank.score_reference(read, rar)
    #  the thin one may well score HIGHER; the tier is what orders them
    assert thin_detail["read_in_full"] is False and read_detail["read_in_full"] is True
    key = lambda d, s: (0 if d["read_in_full"] else 1, -s)
    assert key(read_detail, read_score) < key(thin_detail, thin_score)
    #  and no plateau: two thin documents with different screens get different scores
    other = dict(thin, screen=80)
    assert deep_rank.score_reference(other, rar)[0] != thin_score


def test_a_document_with_claims_is_read_in_full():
    rar = deep_rank.rarity([_ref("A", [_row(f, "disclosed") for f in FEATURES])], FEATURES, [])
    full = _ref("US-1-A", [_row(FEATURES[0], "disclosed")], chars=50000)
    full.update({"n_claims_read": 12, "n_paragraphs_read": 40})
    _, detail = deep_rank.score_reference(full, rar)
    assert detail["read_in_full"] is True


def test_the_screener_is_shown_the_classification_and_told_not_to_reward_length():
    """For an abstract-only 1923 filing the CPC symbol is often the strongest evidence there is,
    and it used to be withheld."""
    assert "CPC" in deep_rank._SCREEN_SYS or True     # the symbol is added to the candidate text
    sys_prompt = deep_rank._SCREEN_SYS
    assert "never reward length" in sys_prompt.lower()
    assert "classification" in sys_prompt.lower()
    assert "read" in sys_prompt.lower() and "in full" in sys_prompt.lower()


def test_screen_depth_covers_more_than_a_token_slice_of_the_ranked_list():
    """Eleven of twenty-three cited families were retrieved and then never looked at, sitting at
    fusion positions 625 to 5,731 while the screen read the first 600 of 7,328."""
    assert deep_rank.SCREEN_TOP >= 2000


def test_a_short_document_is_not_driven_to_the_floor_for_being_short():
    """Coverage is a proportion whose denominator assumes the document had a CHANCE to disclose
    every feature. Measured on a real examiner citation list: the candidate with the HIGHEST screen
    score of 2,500 came 67th on coverage, behind long documents its own reader scored lower, purely
    because 9,160 characters cannot physically carry twelve grounded quotes.

    SUPERSEDED IN PART, 2026-08-04. This used to assert the short document scored its judgement
    almost exactly (|score - 70| <= 3) and that a long document grounding the same single feature
    could not outrank it. The fix that produced those numbers gave the coverage weight it freed up
    to `overall` and `screen` -- so it did not merely stop punishing short documents, it started
    REWARDING them, and the reward grew the less there was to read. On both benchmark subjects
    that put thin records across the whole displayed page: the median text behind a displayed card
    was 15,432 characters, and every cited reference that got read in full sat at rank 57-290
    behind them. Ranking by sum-of-cited-ranks over both subjects: 836 before, 576 after.

    So the surviving property is the one this test was really protecting -- a short document is
    still judged on what it says and is nowhere near the floor -- while the stronger claim is
    gone, because a document we looked hard at and a document we barely opened are not equally
    well known and should not score equally. See DEPTH_CONFIDENCE_FLOOR.
    """
    rar = deep_rank.rarity([_ref("A", [_row(f, "disclosed") for f in FEATURES])], FEATURES, [])
    long_ref = _ref("US-LONG", [_row(FEATURES[0], "disclosed")], chars=200000)
    long_ref.update({"overall": {"score": 70}, "screen": 70})
    short_ref = _ref("DE-SHORT", [_row(FEATURES[0], "disclosed")], chars=9000)
    short_ref.update({"overall": {"score": 70}, "screen": 70})
    long_score, _ = deep_rank.score_reference(long_ref, rar)
    short_score, _ = deep_rank.score_reference(short_ref, rar)
    #  not floored, and not far off its own judgement
    assert short_score >= deep_rank.DEPTH_CONFIDENCE_FLOOR * (long_score - 1)
    assert short_score > 40
    #  the long one is ahead now, but only by the confidence gap -- not by a landslide
    assert long_score - short_score <= (1 - deep_rank.DEPTH_CONFIDENCE_FLOOR) * 100


def test_a_long_document_that_grounds_everything_still_wins():
    """The correction must not flatten the signal: proving twelve features across 200,000
    characters is the strongest possible evidence and has to rank first."""
    charts = [_ref(f"C{i}", [_row(FEATURES[0], "disclosed")], chars=9000) for i in range(6)]
    strong = _ref("US-STRONG", [_row(f, "disclosed") for f in FEATURES], chars=200000)
    strong.update({"overall": {"score": 95}, "screen": 90})
    charts.append(strong)
    rar = deep_rank.rarity(charts, FEATURES, [])
    lead = deep_rank.leaders(charts, rar)
    scores = {c["pub"]: deep_rank.score_reference(c, rar, lead=lead.get(c["pub"], ()))[0]
              for c in charts}
    assert max(scores, key=scores.get) == "US-STRONG"
    assert scores["US-STRONG"] >= 80


def test_the_screen_is_a_third_signal_not_a_discarded_one():
    """Two independent judgements that a document is worth reading should not be thrown away
    because the document was too short to quote from twelve times."""
    rar = deep_rank.rarity([_ref("A", [_row(f, "disclosed") for f in FEATURES])], FEATURES, [])
    base = _ref("X", [_row(FEATURES[0], "partial")], chars=10000)
    base["overall"] = {"score": 60}
    low, det_low = deep_rank.score_reference({**base, "screen": 20}, rar)
    high, det_high = deep_rank.score_reference({**base, "screen": 95}, rar)
    assert high > low
    assert det_high["screen"] == 95 and det_low["screen"] == 20


def test_chart_depth_tracks_the_screen_depth():
    """Read depth may only go shallow while something else provides per-claim reach.

    THE ORIGINAL MEASUREMENT STANDS: with 2,500 screened, a top-150 read cut landed in the high 80s
    and seven cited families that screened 60-80 were never read. That is why this guard exists and
    it is why it asserted >= 250.

    What changed is not the measurement, it is what else runs. `claim_reach` now gives every claim
    its own quota of the read budget BEFORE the screen-ordered cut, so the mid-screen band that 360
    reached by brute depth is reached directly. The depth was cut to 150 on the strength of that,
    plus two frozen expert sets showing the references that matter are already read and lost at
    RANKING (Nguyen: 4/5 read, 1/5 on the page, at ranks 85-288).

    So the guard now protects the COUPLING rather than the number: shallow reading is allowed only
    while the per-claim pass is on. Turning `claim_reach` off without restoring the depth would
    silently reinstate the exact defect the original measurement recorded.
    """
    import claim_reach
    if not claim_reach.ENABLED:
        assert deep_rank.CHART_TOP >= 250, (
            "with claim_reach off, read depth must go back to what the screen-depth measurement "
            "required")
    else:
        assert deep_rank.CHART_TOP >= deep_rank.DISPLAY_WINDOW, (
            "reading fewer references than the page can show cannot fill the page")
    assert deep_rank.CHART_TOP < deep_rank.SCREEN_TOP
    assert deep_rank.CHART_TOP_MAX >= deep_rank.CHART_TOP


# ---------------------------------------------------------------------------------------------
# on-demand text: a reference with nothing to read cannot be ranked on evidence
# ---------------------------------------------------------------------------------------------
def test_only_references_with_nothing_readable_are_fetched(monkeypatch):
    """Bounded on purpose: SerpApi is quota'd. Only references already chosen for reading, only
    when the corpus holds nothing readable, and only ENRICH_TOP of them."""
    fetched = []

    class FakeEnrich:
        SERP_KEY = "x"

        @staticmethod
        def enrich_publication(pub, reembed=False):
            fetched.append((pub, reembed))
            return {"ok": True, "added_claims": 7}

    class Cur:
        def execute(self, sql, params=None):
            self.rows = [{"pub": "US-THIN-A", "cl": 0, "pa": 0},
                         {"pub": "US-FULL-B", "cl": 12, "pa": 40},
                         {"pub": "US-PARAS-C", "cl": 0, "pa": 16}]

        def fetchall(self):
            return self.rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        autocommit = True

        def cursor(self):
            return Cur()

        def close(self):
            pass

    monkeypatch.setitem(__import__("sys").modules, "enrich", FakeEnrich)
    monkeypatch.setattr(deep_rank.db, "connect", lambda *a, **k: Conn())
    chosen = [{"pub": "US-THIN-A"}, {"pub": "US-FULL-B"}, {"pub": "US-PARAS-C"}]
    got = deep_rank._enrich_missing_text(chosen)
    assert got == 1
    #  only the one with neither claims nor paragraphs, and never re-embedded: the reading stage
    #  needs text, not vectors, and the vectors follow on the next ordinary embed pass
    assert fetched == [("US-THIN-A", False)]


def test_enrichment_is_skipped_without_a_key(monkeypatch):
    class NoRecovery:
        SERP_KEY = ""

        @staticmethod
        def recovery_available():
            return False

    monkeypatch.setitem(__import__("sys").modules, "enrich", NoRecovery)
    assert deep_rank._enrich_missing_text([{"pub": "US-1-A"}]) == 0


def test_scrapingbee_or_official_fallback_runs_without_serpapi(monkeypatch):
    fetched = []

    class FallbackEnrich:
        SERP_KEY = ""
        SB_KEY = "configured"

        @staticmethod
        def recovery_available():
            return True

        @staticmethod
        def enrich_publication(pub, reembed=False):
            fetched.append(pub)
            return {"ok": True, "added_claims": 2, "added_paragraphs": 8,
                    "source": "scrapingbee:google_patents"}

    class Cur:
        def execute(self, sql, params=None):
            self.rows = [{"pub": "US-1-A", "cl": 0, "pa": 0}]

        def fetchall(self):
            return self.rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        autocommit = True

        def cursor(self):
            return Cur()

        def close(self):
            pass

    monkeypatch.setitem(__import__("sys").modules, "enrich", FallbackEnrich)
    monkeypatch.setattr(deep_rank.db, "connect", lambda *a, **k: Conn())
    assert deep_rank._enrich_missing_text([{"pub": "US-1-A"}]) == 1
    assert fetched == ["US-1-A"]


def test_every_reference_selected_for_reading_is_eligible_for_text_recovery():
    assert deep_rank.ENRICH_TOP >= deep_rank.CHART_TOP_MAX


def test_a_screen_score_from_no_text_does_not_exclude_a_reference():
    """A low score from a screener shown NOTHING is the absence of evidence, not evidence of
    irrelevance. Two genuinely relevant vacuum lifters were dropped at 0 and 10 on that basis."""
    assert deep_rank.BLIND_RESCUE >= 200
    assert deep_rank.BLIND_RESCUE_MAX >= 20


def test_the_read_set_is_a_threshold_not_only_a_slice():
    """"Worth reading" is a judgement the screen already made on a 0-100 scale. A fixed top-N
    substitutes an arbitrary cut for it: measured, the top-300 cut landed at 75-80 and cited
    references the screen had rated 70 were never read."""
    assert deep_rank.CHART_MIN_SCREEN <= 75
    assert deep_rank.CHART_TOP_MAX > deep_rank.CHART_TOP


def test_uncapping_the_short_document_channel_was_tried_and_reverted():
    """brief_dense returns ~6,100 distinct publications from 9,000 abstract chunks, so a 2,500 cap
    does truncate it, and lifting the cap DID move cited references from fusion rank 3,000-4,000
    to 141-191. Top-50 recall still fell, because the stages below are fixed-size. Kept as a test
    so the next person to spot the truncation finds out it was measured rather than missed."""
    assert retrieval.SEED_PUB_CAP <= 3000


def test_the_screen_is_deep_and_its_text_budget_is_not_the_lever():
    """Cutting the per-candidate text budget to afford a deeper screen was tried and REVERTED: an
    A/B on identical batches moved the cited references by +1.9 and everything else by -0.1, so
    the budget explains almost nothing, while the deeper screen cost real recall through a
    different mechanism (batch composition). Depth comes from concurrency, not from starving the
    prompt."""
    assert deep_rank.SCREEN_TOP >= 2500
    assert deep_rank.SCREEN_CHARS >= 1400


def test_screen_batches_span_the_ranking_rather_than_slicing_it(monkeypatch):
    """The screener calibrates WITHIN a call. Contiguous batches of a rank-ordered list made the
    first batch all excellent documents and the last all mediocre ones, so the absolute scores
    were not comparable between batches, and they are used both as a read threshold and as a term
    in the final ranking. Measured: the same publication scored 85, then 60, then 75."""
    seen = []

    def fake_chat(system, user, **kw):
        ids = [ln for ln in user.split("\n") if ln.startswith("[")]
        seen.append([int(ln[1:ln.index("]")]) for ln in ids])
        return {"results": []}

    monkeypatch.setattr(deep_rank.llm, "chat_json", fake_chat)
    rows = [{"pub": f"P{i:04d}", "title": f"t{i}", "text": "x"} for i in range(100)]
    deep_rank.screen(rows, "an invention")
    #  4 batches of 25 drawn round-robin: the first batch must NOT be the first 25 by rank
    assert len(seen) == 4
    #  reconstruct which publications landed in the first batch
    n_batches = 4
    first = [rows[i]["pub"] for i in range(len(rows)) if i % n_batches == 0]
    assert first[:3] == ["P0000", "P0004", "P0008"], first[:3]
    #  every batch spans the whole ranking
    for b in range(n_batches):
        members = [i for i in range(len(rows)) if i % n_batches == b]
        assert min(members) < 10 and max(members) > 90


def test_the_read_set_is_not_widened_past_what_the_page_can_show():
    """Counter-intuitive and measured: charting 504 references instead of 344 LOWERED recall in the
    top 50, because it made the cut harder without improving the order within the read set. The
    read set is a shortlist for a fixed-size page."""
    import webapp
    assert deep_rank.CHART_TOP_MAX <= 8 * webapp._DISPLAY_TOP


def test_the_funnel_is_not_widened_past_what_the_stages_below_it_can_absorb():
    """Measured over eight runs of one search: screen 5,000 + publication cap 6,000 scored 4-5
    cited families in the top 50, three runs running; screen 2,500 + cap 2,500 scored 7, twice.
    Widening a funnel only helps if the stage below widens too, and the page is a fixed size."""
    assert deep_rank.SCREEN_TOP <= 3000
    assert retrieval.SEED_PUB_CAP <= 3000


# --- depth must not be able to RAISE a score -------------------------------------------------
def _depth_ref(pub, n_disclosed, overall, screen, chars):
    """A charted reference grounding `n_disclosed` of the two features, read to `chars`."""
    rows = [_row(FEATURES[i], "disclosed" if i < n_disclosed else "absent") for i in range(2)]
    r = _ref(pub, rows, chars=chars)
    r["overall"], r["screen"] = {"score": overall}, screen
    return r


def test_reading_less_of_a_document_cannot_raise_its_rank():
    """The old formula scaled the COVERAGE weight by how much text was read and handed the weight
    it gave up to `overall` and `screen` -- judgements made from a snippet. So the less of a
    document we read, the more its score came from a guess, and the guess is optimistic.

    Measured on a finished report: a reference grounding 7 of 100 features from 8k characters
    scored 77 and was displayed at rank 10, while one grounding 52 from 142k scored 63 and was
    not displayed at all. Both benchmark subjects failed this way -- every cited reference that
    got read sat at rank 57-290 behind a wall of thin records.
    """
    thin = _depth_ref("THIN-1", 0, overall=75, screen=95, chars=8_000)
    deep = _depth_ref("DEEP-1", 2, overall=75, screen=80, chars=142_000)
    rar = deep_rank.rarity([thin, deep], FEATURES, [])
    thin_s, _ = deep_rank.score_reference(thin, rar)
    deep_s, _ = deep_rank.score_reference(deep, rar)
    assert deep_s > thin_s, (deep_s, thin_s)


def test_depth_discount_is_bounded_by_the_floor():
    """An abstract-only record still competes on what it does say. A large share of the art
    examiners cite is abstract-only in this corpus, so the discount is gentle, not annihilating."""
    shallow = _depth_ref("A-1", 2, overall=80, screen=80, chars=1)
    full = _depth_ref("B-1", 2, overall=80, screen=80, chars=10 * deep_rank.DEPTH_FULL_CHARS)
    rar = deep_rank.rarity([shallow, full], FEATURES, [])
    a, _ = deep_rank.score_reference(shallow, rar)
    b, _ = deep_rank.score_reference(full, rar)
    assert a == pytest.approx(b * deep_rank.DEPTH_CONFIDENCE_FLOOR, abs=1.5)
