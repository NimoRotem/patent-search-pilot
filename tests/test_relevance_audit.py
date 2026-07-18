"""Milestone 9 regression tests — the relevance/rationale/claim-chart quality fixes.

These guard the fixes surfaced by the M9 audit:
 - display-layer substance filter (drop design patents, demote title-only) — recall-safe because it
   never touches report["ranked_families"] / the retrieval path;
 - evidence-grounded `reads_on` (drop any element whose quote is not in the reference text);
 - the empty-reference rationale guard (no hallucinated disclosure for a text-less doc);
 - the claim-chart `strength` flag (whole-doc-only cells are marked weak, not implied-covered);
 - the audit harness `ref_text` chunk-body fallback (old OCR'd patents whose disclosure lives only
   in chunks are no longer judged on a bare title).
"""
import json
import pytest
import webview
import webapp
import audit


# ---- display-layer substance filter --------------------------------------------------------
def test_is_design_detects_design_patents():
    assert webview._is_design("US-D932726-S", "S1")
    assert webview._is_design("US-D123456-S", None)
    assert webview._is_design("US-1234567-A", "S")        # kind 'S…'
    assert not webview._is_design("US-11078051-B2", "B2")
    assert not webview._is_design("EP-4048620-B1", "B1")
    assert not webview._is_design("US-3139300-A", "A")


def test_substance_order_drops_design_and_demotes_titleonly(monkeypatch):
    reps = {
        "F1": {"id": 1, "publication_number": "US-111-A", "kind_code": "A"},   # substantive
        "F2": {"id": 2, "publication_number": "US-D999-S", "kind_code": "S1"},  # design -> drop
        "F3": {"id": 3, "publication_number": "US-333-A", "kind_code": "B2"},   # title-only -> demote
        "F4": {"id": 4, "publication_number": "US-444-A", "kind_code": "A"},    # substantive
    }
    families = ["F1", "F2", "F3", "F4"]
    monkeypatch.setattr(webview, "_titleonly_ids", lambda cur, ids: {3})
    monkeypatch.setattr(webview, "_thin_ids", lambda cur, ids: set())
    ordered, stats = webview.substance_order(None, families, reps, keep=10)
    assert "F2" not in ordered                     # design dropped entirely
    assert ordered == ["F1", "F4", "F3"]           # substantive in rank order, title-only demoted last
    assert stats["design_dropped"] == 1
    assert stats["titleonly_demoted"] == 1


def test_substance_order_demotes_thin_docs(monkeypatch):
    reps = {
        "F1": {"id": 1, "publication_number": "US-111-A", "kind_code": "A"},   # substantive
        "F2": {"id": 2, "publication_number": "US-222-A", "kind_code": "A"},   # thin (no text) -> demote
        "F3": {"id": 3, "publication_number": "US-333-A", "kind_code": "A"},   # substantive
    }
    monkeypatch.setattr(webview, "_titleonly_ids", lambda cur, ids: set())
    monkeypatch.setattr(webview, "_thin_ids", lambda cur, ids: {2})
    ordered, _ = webview.substance_order(None, ["F1", "F2", "F3"], reps, keep=10)
    assert ordered == ["F1", "F3", "F2"]           # the text-less ref sinks below the substantive ones


def test_substance_order_trims_to_keep(monkeypatch):
    reps = {f"F{i}": {"id": i, "publication_number": f"US-{i}-A", "kind_code": "A"} for i in range(20)}
    families = [f"F{i}" for i in range(20)]
    monkeypatch.setattr(webview, "_titleonly_ids", lambda cur, ids: set())
    monkeypatch.setattr(webview, "_thin_ids", lambda cur, ids: set())
    ordered, _ = webview.substance_order(None, families, reps, keep=5)
    assert ordered == ["F0", "F1", "F2", "F3", "F4"]   # top-5, order preserved


# ---- evidence-grounded reads_on ------------------------------------------------------------
def test_ground_reads_on_keeps_grounded_drops_ungrounded():
    ref = "A vacuum gripper with a flexible sealing lip and an electric vacuum pump for lifting glass."
    raw = [
        {"element": "flexible sealing lip", "evidence": "a flexible sealing lip"},        # grounded
        {"element": "electric vacuum pump", "evidence": "an electric vacuum pump"},       # grounded
        {"element": "capacitive part-presence sensor",
         "evidence": "a capacitive sensor distinguishing part types"},                    # NOT in text
        {"element": "no-evidence element", "evidence": ""},                               # no quote -> drop
    ]
    kept = webapp._ground_reads_on(raw, ref)
    assert "flexible sealing lip" in kept
    assert "electric vacuum pump" in kept
    assert "capacitive part-presence sensor" not in kept    # ungrounded overclaim removed
    assert "no-evidence element" not in kept


def test_ground_reads_on_tolerates_plain_strings():
    kept = webapp._ground_reads_on(["a vacuum seal", "a base element"], "anything")
    assert kept == ["a vacuum seal", "a base element"]      # old string shape passes through


def test_ground_reads_on_dedups():
    ref = "a vacuum seal element with a contact surface"
    raw = [{"element": "vacuum seal", "evidence": "a vacuum seal element"},
           {"element": "Vacuum Seal", "evidence": "a vacuum seal element"}]
    assert len(webapp._ground_reads_on(raw, ref)) == 1


# ---- empty-reference rationale guard -------------------------------------------------------
def test_rationale_guard_no_text_returns_unconfirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "RATIONALE", tmp_path)

    def _boom(*a, **k):
        raise AssertionError("LLM must not be called for a text-less reference")
    monkeypatch.setattr(webapp.llm, "chat_json", _boom)
    res = webapp._rationale("slugX", "JUNK-9", "a vacuum gripper query", ["el"], "JUNK-9 ", None)
    assert res["reads_on"] == []
    assert "not available" in res["why"].lower() or "unconfirmed" in res["why"].lower()


# ---- claim-chart strength flag -------------------------------------------------------------
def test_claim_chart_marks_whole_only_cells_weak(gold_slug):
    rep = json.loads((webapp.REPORTS / f"{gold_slug}.json").read_text())
    chart = webview.build_claim_chart(rep)
    covered = [c for row in chart["rows"] for c in row["cells"] if c.get("covered")]
    assert covered, "expected some covered cells"
    for c in covered:
        assert c.get("strength") in ("cited", "weak")
        # a cell with no specific coordinate string must be flagged weak (no passage to cite)
        if not c.get("coord"):
            assert c["strength"] == "weak"
        else:
            assert c["strength"] == "cited"


# ---- audit harness ref_text chunk-body fallback --------------------------------------------
def test_ref_text_falls_back_to_chunk_body_for_old_patent():
    # US-762499-A (1904) has paragraph chunks but NO abstract and NO rows in the claims table;
    # ref_text must surface the body so a judge sees the real disclosure, not just the title.
    t = audit.ref_text("US-762499-A")
    assert t["snippet"]
    assert len(t["snippet"]) > 60                       # more than just "Vacuum-lifter."
    assert "vacuum" in t["snippet"].lower()


def test_ref_text_handles_missing_pub_gracefully():
    t = audit.ref_text("US-DOES-NOT-EXIST-9999-A")
    assert t["snippet"] == ""                           # no crash, empty snippet


# ---- UI: relevancy score + inline-card data ------------------------------------------------
def test_relevancy_is_monotonic_and_bounded():
    assert webview._relevancy(0.90) > webview._relevancy(0.75) > webview._relevancy(0.55)
    for c in (-1, 0.0, 0.5, 1.0, 2):                     # never crashes / out of range
        assert 1 <= webview._relevancy(c) <= 99


def test_cards_carry_relevancy_and_abstract(gold_slug):
    rep = json.loads((webapp.REPORTS / f"{gold_slug}.json").read_text())
    v = webview.build_view(rep, top_n=10)
    assert v["cards"]
    for c in v["cards"]:
        assert 1 <= c["relevancy"] <= 99                # a display score on every card
        assert "abstract" in c                          # inline-abstract field present (may be None)


def test_cards_carry_server_rendered_content(gold_slug):
    """The fix for 'no details / Claims not ingested': claims + description + figure captions are
    attached to each card from the DB so the page shows real data without a per-tab round-trip."""
    rep = json.loads((webapp.REPORTS / f"{gold_slug}.json").read_text())
    v = webview.build_view(rep, top_n=12)
    for c in v["cards"]:
        assert "claims" in c and "description" in c and "figure_caps" in c and "images" in c
    # the top of a gold report must have real, readable content (not a wall of thin old patents)
    top = v["cards"][:8]
    assert sum(1 for c in top if c["claims"]) >= 6      # most top cards have ingested claims
    assert any(c["images"] for c in top)                # at least some have drawings on disk
