"""The reading has to cover the WHOLE checklist, and it has to match on meaning.

Every test here is anchored on a defect that was live and measurable on a finished report
(adhoc-220e6c97a41e / adhoc-939fb711122b, 13 Aug 2026):

  * ``deep_rank`` built a 40-item checklist and ``deep_analysis`` charted the first 20. The other
    20 were never put to a single reference — and ``rarity`` still counted them in the denominator,
    so half the invention was simultaneously reported as "nothing discloses this" and used to
    divide every reference's score. The last 20 features had df=0. EXACTLY 20.
  * the element x reference grid was drawn from retrieval cosines while the full-text reading sat
    beside it, so "seal element elastically deformable at contact surface" showed a single 0.07
    cell on a search whose reading had grounded that feature in 364 references.
  * the query text shipped 1,700 characters of figure narration ("Figure 1 presents a perspective
    view ... an outer wall 210 ... thickness T") as the description of the invention.
  * a card showed a relevancy of 85 while the search was still running and 47 when it finished,
    because the partial page rendered the pre-reading cosine in the box that later holds the
    evidence score.
"""
import json
import re
from pathlib import Path

import pytest

import deep_analysis
import deep_rank


ROOT = Path(__file__).resolve().parents[1]

REF = {
    "found": True, "pub": "US-9-B2", "title": "A suction lifting device",
    "chars": 900, "n_claims": 1, "n_paragraphs": 1, "truncated": False,
    "passages": [
        {"kind": "abstract", "coord": {}, "label": "abstract",
         "text": "A suction lifting device having a rigid plate and a resilient sealing ring."},
        {"kind": "claim", "coord": {"claim_no": 1}, "label": "claim 1",
         "text": "A lifting device comprising a plate, a resilient sealing ring on its underside "
                 "and a rib standing proud of the underside inside the ring."},
        {"kind": "paragraph", "coord": {"para_no": 4}, "label": "paragraph 4",
         "text": "The sealing ring deforms against the workpiece so that the rib limits how far "
                 "the ring may be compressed."},
    ],
}


def _many_features(n):
    return [f"feature number {i} of the invention" for i in range(1, n + 1)]


def _stub(monkeypatch, answer_for):
    """Stub the model. `answer_for(features)` returns the answer for one feature batch."""
    seen = []

    def chat(system, user, **kw):
        if "REFUTE" in system.upper():
            return {"checks": []}
        payload = json.loads(user)
        feats = [f if isinstance(f, str) else f.get("item")
                 for f in (payload.get("subject_features") or payload.get("features") or [])]
        seen.append(feats)
        return answer_for(feats)
    monkeypatch.setattr(deep_analysis.llm, "chat_json", chat)
    monkeypatch.setattr(deep_analysis, "full_text", lambda pub, **kw: dict(REF, pub=pub))
    return seen


# ---------------------------------------------------------------------------
# the truncation
# ---------------------------------------------------------------------------
def test_the_chart_can_hold_the_whole_disclosure_list():
    """A checklist deep_rank can build must be one deep_analysis can chart.

    This is the invariant the live defect violated: DISCLOSURE_CAP was 40 and MAX_FEATURES was
    20, so the last 20 disclosures of every document search were unanswerable by construction.
    """
    assert deep_analysis.MAX_FEATURES >= deep_rank.DISCLOSURE_CAP


def test_every_feature_given_is_charted_not_just_the_first_batch(monkeypatch):
    feats = _many_features(deep_analysis.FEATURE_BATCH * 2 + 3)
    seen = _stub(monkeypatch, lambda fs: {"features": [
        {"item": f, "verdict": "absent", "quote": "", "confidence": 0.1} for f in fs]})
    out = deep_analysis.analyse_reference("US-9-B2", feats, [])
    assert [r["item"] for r in out["features"]] == feats
    #  and each one was actually PUT to the model, exactly once
    asked = [f for batch in seen for f in batch]
    assert sorted(asked) == sorted(feats)
    assert len(seen) > 1, "a long checklist must be asked in batches, not in one answer"


def test_a_feature_past_the_first_batch_can_still_be_disclosed(monkeypatch):
    """The last feature of a long list must be able to come back grounded.

    On the live report every feature past position 20 was 'absent' with df=0 — not because no
    reference disclosed them but because no reference was ever asked.
    """
    feats = _many_features(deep_analysis.FEATURE_BATCH + 5)
    target = feats[-1]

    def answer(fs):
        return {"features": [
            {"item": f, "verdict": "disclosed" if f == target else "absent",
             "quote": ("a rib standing proud of the underside inside the ring"
                       if f == target else ""),
             "confidence": 0.8} for f in fs]}
    _stub(monkeypatch, answer)
    out = deep_analysis.analyse_reference("US-9-B2", feats, [])
    row = next(r for r in out["features"] if r["item"] == target)
    assert row["verdict"] == "disclosed"
    assert row["grounding"] == "verified"
    assert row["location"] == "claim 1"


def test_the_rarity_denominator_only_counts_features_that_were_charted(monkeypatch):
    """Every feature in the denominator must be one a reference had the chance to answer."""
    feats = _many_features(deep_analysis.FEATURE_BATCH + 4)
    _stub(monkeypatch, lambda fs: {"features": [
        {"item": f, "verdict": "disclosed",
         "quote": "a resilient sealing ring on its underside", "confidence": 0.7} for f in fs]})
    ref = deep_analysis.analyse_reference("US-9-B2", feats, [])
    ref["method"] = "llm"
    rar = deep_rank.rarity([ref], feats, [])
    assert set(rar["feature_df"]) == set(feats)
    assert all(v > 0 for v in rar["feature_df"].values()), \
        "a feature nothing was asked about must not sit in the denominator at df=0"


# ---------------------------------------------------------------------------
# matching on meaning
# ---------------------------------------------------------------------------
def test_the_reader_is_told_to_match_on_meaning_not_on_wording():
    sys = deep_analysis._SYS
    assert "MATCH ON MEANING" in sys
    assert "absent" in sys and "words it differently" in sys


def test_concept_expansions_are_sent_to_the_reader(monkeypatch):
    feats = ["a bracing structure on the base, inside the seal"]
    hints = {feats[0]: "a rib on the underside — also called: rib; stiffener; boss"}
    payloads = []

    def chat(system, user, **kw):
        if "REFUTE" in system.upper():
            return {"checks": []}
        payloads.append(json.loads(user))
        return {"features": [{"item": feats[0], "verdict": "absent", "quote": ""}]}
    monkeypatch.setattr(deep_analysis.llm, "chat_json", chat)
    monkeypatch.setattr(deep_analysis, "full_text", lambda pub, **kw: dict(REF, pub=pub))
    deep_analysis.analyse_reference("US-9-B2", feats, [], hints=hints)
    assert payloads and payloads[0].get("other_words_for_each_feature") == hints


def test_concept_expansions_survive_a_model_failure(monkeypatch):
    monkeypatch.setattr(deep_analysis.llm, "chat_json",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vertex down")))
    assert deep_analysis.concept_expansions(["a seal"], brief="x") == {}


def test_the_second_reading_recovers_a_missed_disclosure_but_only_on_a_real_quote(monkeypatch):
    rows = [
        {"item": "an elastic seal at the contact face", "verdict": "absent", "quote": "",
         "grounding": "model-absent", "kind": "feature"},
        {"item": "a laser rangefinder", "verdict": "absent", "quote": "",
         "grounding": "model-absent", "kind": "feature"},
    ]
    monkeypatch.setattr(deep_analysis, "full_text", lambda pub, **kw: dict(REF, pub=pub))
    monkeypatch.setattr(deep_analysis.llm, "chat_json", lambda system, user, **kw: {"features": [
        {"item": "an elastic seal at the contact face", "verdict": "disclosed",
         "quote": "The sealing ring deforms against the workpiece", "confidence": 0.8},
        {"item": "a laser rangefinder", "verdict": "disclosed",
         "quote": "a laser rangefinder measures the standoff distance", "confidence": 0.9},
    ]})
    changed = deep_analysis.reread_absent("US-9-B2", rows)
    assert changed == 1
    assert rows[0]["verdict"] == "disclosed" and rows[0]["second_pass"] is True
    assert rows[0]["location"] == "paragraph 4"
    #  the invented one is still absent: a second look widens what is RECOGNISED, never what may
    #  be asserted, so it goes through exactly the same grounding gate as the first.
    assert rows[1]["verdict"] == "absent"
    assert not rows[1].get("second_pass")


def test_the_second_reading_never_touches_a_row_that_was_already_answered(monkeypatch):
    rows = [{"item": "a resilient sealing ring", "verdict": "disclosed",
             "quote": "a resilient sealing ring on its underside", "grounding": "verified",
             "kind": "feature"}]
    monkeypatch.setattr(deep_analysis, "full_text", lambda pub, **kw: dict(REF, pub=pub))
    monkeypatch.setattr(deep_analysis.llm, "chat_json",
                        lambda *a, **k: pytest.fail("nothing was absent; the model must not run"))
    assert deep_analysis.reread_absent("US-9-B2", rows) == 0


def test_the_checklist_extractor_is_told_to_drop_claim_scaffolding():
    import disclosures
    sys = disclosures._SYS
    assert "STRIP THE CLAIM SCAFFOLDING" in sys
    assert "at least one" in sys
    #  and NOT the old instruction that produced the literal statements
    assert "Prefer the specific to the generic" not in sys


# ---------------------------------------------------------------------------
# the grid is the reading, not the retrieval
# ---------------------------------------------------------------------------
def _deep_fixture():
    return {
        "features": ["an elastic seal at the contact face", "a rib inside the seal"],
        "references": [
            {"pub": "US-9-B2", "title": "A suction lifting device", "method": "llm",
             "features": [
                 {"item": "an elastic seal at the contact face", "verdict": "disclosed",
                  "quote": "The sealing ring deforms against the workpiece",
                  "location": "paragraph 4", "grounding": "verified", "confidence": 0.8},
                 {"item": "a rib inside the seal", "verdict": "partial",
                  "quote": "a rib standing proud of the underside inside the ring",
                  "location": "claim 1", "grounding": "verified", "confidence": 0.5,
                  "second_pass": True},
             ]},
            {"pub": "DE-7-A1", "title": "Sauger", "method": "llm",
             "features": [
                 {"item": "an elastic seal at the contact face", "verdict": "disclosed",
                  "quote": "an invented passage", "grounding": "dropped-ungrounded-quote"},
                 {"item": "a rib inside the seal", "verdict": "absent", "quote": "",
                  "grounding": "model-absent"},
             ]},
        ],
    }


def test_the_element_grid_is_built_from_the_reading_with_its_quotes():
    import webview
    report = {"deep_rank": {"order": ["US-9-B2", "DE-7-A1"],
                            "feature_idf": {"a rib inside the seal": 1.4,
                                            "an elastic seal at the contact face": 0.2},
                            "feature_df": {"a rib inside the seal": 1,
                                           "an elastic seal at the contact face": 1}},
              "combination_view": {}}
    chart = webview.build_reading_chart(report, _deep_fixture())
    assert chart["source"] == "reading"
    #  rarest feature first: that is the order a novelty argument is made in
    assert chart["rows"][0]["element"] == "a rib inside the seal"
    cell = chart["rows"][0]["cells"][0]
    assert cell["pub"] == "US-9-B2" and cell["covered"] is True
    assert cell["verify"] == "weak" and cell["verdict"] == "partial"
    assert cell["quote"].startswith("a rib standing proud")
    assert cell["location"] == "claim 1"
    assert cell["second_pass"] is True
    #  an ungrounded quote is not coverage, however confident the model was
    ungrounded = chart["rows"][1]["cells"][1]
    assert ungrounded["pub"] == "DE-7-A1" and ungrounded["covered"] is False
    assert chart["n_unasked"] == 0 and all(r["asked"] for r in chart["rows"])


def test_a_feature_no_reference_was_asked_about_is_marked_not_ranked_rarest():
    """An unanswered row must never be presented as the rarest disclosure in the art.

    Reports generated before the charting truncation was fixed carry features no reference ever
    returned a row for. Sorted by rarity those land FIRST, at the maximum idf, reading as "nothing
    in 400 references discloses this" — the strongest claim in the report, made from no evidence.
    """
    import webview
    deep = _deep_fixture()
    deep["features"].append("a feature nobody was asked about")
    report = {"deep_rank": {"order": ["US-9-B2", "DE-7-A1"],
                            "feature_idf": {"a feature nobody was asked about": 9.9,
                                            "a rib inside the seal": 1.4,
                                            "an elastic seal at the contact face": 0.2},
                            "feature_df": {}},
              "combination_view": {}}
    chart = webview.build_reading_chart(report, deep)
    assert chart["n_unasked"] == 1
    assert chart["rows"][-1]["element"] == "a feature nobody was asked about"
    assert chart["rows"][-1]["asked"] is False
    assert all(r["asked"] for r in chart["rows"][:-1])


def test_the_element_grid_falls_back_to_retrieval_when_nothing_was_read():
    import webview
    assert webview.build_reading_chart({}, {"features": [], "references": []}) is None
    assert webview.build_reading_chart({}, None) is None


# ---------------------------------------------------------------------------
# the blurb, and the pending score
# ---------------------------------------------------------------------------
def test_the_search_blurb_carries_no_figure_narration(monkeypatch):
    import ingest_input as ii
    import llm
    monkeypatch.setattr(llm, "condense_for_search",
                        lambda t: {"disclosure": "A hand-held vacuum gripper with a rigid base "
                                                 "and a loop-shaped elastic seal.", "title": "Gripper"})
    monkeypatch.setattr(llm, "describe_figures",
                        lambda blobs, context="", **k:
                        "Figure 1 presents a perspective view of an oval frame 210 with an upper "
                        "surface 212, a thickness T and a dimension D1.")
    monkeypatch.setattr(llm, "chat_json", lambda system, user, **kw: {
        "description": "A hand-held vacuum gripper has a rigid oval base carrying a loop-shaped "
                       "elastic seal around its rim, the seal standing proud of the underside so "
                       "that it defines a vacuum chamber against the workpiece."})
    monkeypatch.setattr(ii, "_thumb", lambda b, **k: "data:image/jpeg;base64,AAA")
    r = ii._build(text="a gripper " * 40, figures=[b"png"], source="upload", label="x.pdf",
                  notes=[], embed_chunks=False)
    brief = r["brief"]
    assert "Drawings (figures analysed" not in brief
    assert "Figure 1" not in brief and "figures illustrate" not in brief
    assert not re.search(r"\b\d{3}\b", brief), "reference numerals must not reach the query"
    assert "loop-shaped" in brief
    #  the raw reading of the drawings is still kept, for audit and for the review panel
    assert "Figure 1" in r["figure_descriptions"]


def test_a_partial_card_shows_no_relevancy_number():
    card = (ROOT / "templates" / "_refcard.html").read_text()
    #  the pending box must exist, and the scored box must be behind the same condition
    assert "relbox-pending" in card
    pending = card.split("{% if partial %}")
    assert any("relbox-pending" in chunk and "relnum" not in chunk.split("{% else %}")[0]
               for chunk in pending[1:]), \
        "while the ranking is partial the card must not render a relevancy number"
    assert "rtab-pending" in card and "Reading this reference in full" in card
