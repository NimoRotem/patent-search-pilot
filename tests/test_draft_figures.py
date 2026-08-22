"""Autonomous patent-drawing generation and pixel-level filing gates."""
import io
import json
import re

import pytest
from PIL import Image

import draft_figures


def blank_png(width=640, height=420):
    image = Image.new("RGB", (width, height), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_semantic_response_schema_is_inline_for_vertex():
    encoded = json.dumps(draft_figures.SEMANTIC_RESPONSE_SCHEMA)
    assert '"$ref"' not in encoded and '"$defs"' not in encoded
    assert draft_figures.SEMANTIC_RESPONSE_SCHEMA["properties"]["anchors"]["items"][
        "properties"]["numeral"]["type"] == "string"


def test_figure_identity_uses_the_figure_number_not_a_truncated_caption():
    long = "FIG. 2 - Side elevation in vertical section showing the chamber and every air path"
    assert draft_figures.figure_key(long) == draft_figures.figure_key(long[:80]) == "fig-2"
    assert draft_figures.canonical_figure_label(long) == "FIG. 2"


def test_cloud_vision_ocr_keeps_duplicates_and_separates_the_figure_label():
    response = {"responses": [{
        "fullTextAnnotation": {
            "text": "10   12   12\nFIG. 2\n",
            "pages": [{"blocks": [{"paragraphs": [{"words": [
                {"confidence": 0.98}, {"confidence": 0.97},
                {"confidence": 0.99}, {"confidence": 0.96},
            ]}]}]}],
        }
    }]}
    found = draft_figures.parse_ocr_response(response)
    assert found["numerals"] == ["10", "12", "12"]
    assert found["figure_label"] == "FIG. 2"
    assert found["other_text"] == []
    assert found["confidence"] > 0.95


def test_ocr_audit_requires_the_right_figure_label_exact_numerals_and_no_other_text():
    good = draft_figures.ocr_audit(
        ["10 = body", "12 = pump"],
        {"ok": True, "numerals": ["10", "12"], "figure_label": "FIG. 3",
         "other_text": [], "confidence": 0.98},
        "FIG. 3 - side view")
    assert good["ok"] is True

    wrong = draft_figures.ocr_audit(
        ["10 = body", "12 = pump"],
        {"ok": True, "numerals": ["10", "12", "12"], "figure_label": "FIG. 4",
         "other_text": ["pump"], "confidence": 0.98},
        "FIG. 3")
    assert wrong["ok"] is False
    assert wrong["duplicates"] == ["12"]
    assert wrong["correct_figure_label"] is False
    assert wrong["other_text"] == ["pump"]


def test_semantic_audit_requires_one_grounded_anchor_per_expected_part():
    result = {
        "matches_spec": True, "summary": "matches", "errors": [], "unexpected_text": [],
        "anchors": [
            {"numeral": "10", "x": 220, "y": 300, "visible": True,
             "evidence": "rectangular body"},
            {"numeral": "12", "x": 620, "y": 250, "visible": True,
             "evidence": "pump on body"},
        ],
    }
    assert draft_figures.semantic_audit(["10 = body", "12 = pump"], result)["ok"] is True
    result["anchors"][1]["visible"] = False
    audit = draft_figures.semantic_audit(["10 = body", "12 = pump"], result)
    assert audit["ok"] is False and audit["missing"] == ["12"]


def test_semantic_audit_normalizes_model_written_human_text():
    audit = draft_figures.semantic_audit(["10 = body"], {
        "matches_spec": False, "summary": "body\u2014wrong view",
        "errors": ["relationship\u2014not shown"], "unexpected_text": [],
        "anchors": [{"numeral": "10", "x": 200, "y": 300, "visible": True,
                     "evidence": "body\u2014visible"}],
    })
    assert "\u2014" not in json.dumps(audit, ensure_ascii=False)


def test_semantic_audit_ignores_only_feedback_for_the_later_label_overlay():
    audit = draft_figures.semantic_audit(["10 = body", "12 = pump"], {
        "matches_spec": False, "summary": "geometry is correct but labels are absent",
        "errors": [
            "The image lacks all specified reference numerals.",
            "The image lacks the specified view legend.",
            "The pump is not called out by a leader line.",
        ],
        "unexpected_text": [],
        "anchors": [
            {"numeral": "10", "x": 220, "y": 300, "visible": True,
             "evidence": "rectangular body"},
            {"numeral": "12", "x": 620, "y": 250, "visible": True,
             "evidence": "pump on body"},
        ],
    })
    assert audit["ok"] is True and audit["errors"] == []

    audit = draft_figures.semantic_audit(["10 = body", "12 = pump"], {
        "matches_spec": False, "summary": "wrong relationship",
        "errors": ["The pump axis is vertical instead of horizontal."],
        "unexpected_text": [],
        "anchors": [
            {"numeral": "10", "x": 220, "y": 300, "visible": True,
             "evidence": "rectangular body"},
            {"numeral": "12", "x": 620, "y": 250, "visible": True,
             "evidence": "vertical pump on body"},
        ],
    })
    assert audit["ok"] is False and "vertical" in audit["errors"][0]


def test_labels_are_overlaid_deterministically_after_geometry_review():
    output = draft_figures.annotate_png(blank_png(), "FIG. 3 - side view", [
        {"numeral": "10", "x": 200, "y": 300, "visible": True, "evidence": "body"},
        {"numeral": "12", "x": 750, "y": 250, "visible": True, "evidence": "pump"},
    ])
    image = Image.open(io.BytesIO(output))
    assert image.width > 640 and image.height > 420
    assert output == draft_figures.annotate_png(blank_png(), "FIG. 3 - side view", [
        {"numeral": "10", "x": 200, "y": 300, "visible": True, "evidence": "body"},
        {"numeral": "12", "x": 750, "y": 250, "visible": True, "evidence": "pump"},
    ])


def test_geometry_prompt_strips_every_annotation_instruction_and_reference_number():
    prompt = draft_figures.build_prompt(
        "FIG. 3 - sectional view",
        "FIG. 3 shows the body 10 around the pump 12. Label body 10 with a leader line and legend.",
        ["10 = body", "12 = pump"], spec_context="The body 10 supports the pump 12.")
    lowered = prompt.lower()
    assert "fig. 3" not in lowered and " 10" not in prompt and " 12" not in prompt
    assert "leader" not in lowered and "legend" not in lowered and "label body" not in lowered
    assert "the body" in lowered and "the pump" in lowered


def test_geometry_prompt_removes_section_marks_and_spells_geometric_numbers():
    prompt = draft_figures.build_prompt(
        "FIG. 2 - perspective view",
        "The assembly is viewed from 30 degrees above. A section line 2-2 crosses the body.",
        ["10 = body"])
    assert not re.search(r"\d", prompt)
    assert "thirty degrees" in prompt.lower()
    assert "section line" not in prompt.lower()


def test_render_refuses_to_store_a_semantically_wrong_drawing(monkeypatch):
    monkeypatch.setattr(draft_figures, "_cached_generate", lambda *a, **k: blank_png())
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": False, "missing": ["12"], "errors": ["pump is absent"], "anchors": []})
    monkeypatch.setattr(
        draft_figures, "create_figure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not persist")))

    with pytest.raises(draft_figures.FigureError, match="semantic"):
        draft_figures.render_figure(
            7, 91, label="FIG. 1", caption="side view of body and pump",
            numerals=["10 = body", "12 = pump"])


def test_text_contaminated_geometry_retries_from_a_clean_canvas(monkeypatch):
    previous_images = []
    prompts = []

    def generate(prompt, previous=None):
        prompts.append(prompt)
        previous_images.append(previous)
        return blank_png()

    monkeypatch.setattr(draft_figures, "_cached_generate", generate)
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": False, "missing": [], "errors": ["component 10 contains visible text"],
        "unexpected_text": ["BODY"], "anchors": []})
    with pytest.raises(draft_figures.FigureError, match="semantic"):
        draft_figures.render_figure(
            7, 91, label="FIG. 1", caption="side view of body",
            numerals=["10 = body"])
    assert previous_images == [None] * draft_figures.MAX_SEMANTIC_ATTEMPTS
    assert all(not re.search(r"\d", prompt) for prompt in prompts)


def test_render_stores_only_after_semantic_and_ocr_gates_pass(monkeypatch):
    events = []
    monkeypatch.setattr(draft_figures, "_cached_generate", lambda *a, **k: blank_png())
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": True, "missing": [], "errors": [], "unexpected": [],
        "anchors": [{"numeral": "10", "x": 200, "y": 300, "visible": True,
                     "evidence": "body"}]})
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99})
    monkeypatch.setattr(draft_figures, "create_figure", lambda *a, **k: {"id": 44})

    def save(figure_id, **kwargs):
        events.append((figure_id, kwargs))
        return {"version_no": 1, "audit": kwargs["ocr_audit"],
                "semantic_audit": kwargs["semantic_audit"], "detected_numerals": ["10"]}

    monkeypatch.setattr(draft_figures, "_audited_version", save)
    out = draft_figures.render_figure(
        7, 91, label="FIG. 1 - side view", caption="side view of the body",
        numerals=["10 = body"])
    assert out["numeral_audit"]["ok"] is True
    assert out["semantic_audit"]["ok"] is True
    assert events and events[0][0] == 44


def test_render_increases_deterministic_label_size_until_ocr_is_exact(monkeypatch):
    scales = []
    original = draft_figures.annotate_png
    monkeypatch.setattr(draft_figures, "_cached_generate", lambda *a, **k: blank_png())
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": True, "anchors": [{"numeral": "10", "x": 200, "y": 300,
                                    "visible": True, "evidence": "body"}]})
    monkeypatch.setattr(
        draft_figures, "annotate_png",
        lambda png, label, anchors, scale=1.0: scales.append(scale) or
        original(png, label, anchors, scale=scale))
    inspections = iter([
        {"ok": True, "numerals": ["10", "10"], "figure_label": "FIG. 1",
         "other_text": [], "confidence": 0.96},
        {"ok": True, "numerals": ["10", "10"], "figure_label": "FIG. 1",
         "other_text": [], "confidence": 0.96},
        {"ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
         "other_text": [], "confidence": 0.98},
    ])
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: next(inspections))
    monkeypatch.setattr(draft_figures, "create_figure", lambda *a, **k: {"id": 44})
    monkeypatch.setattr(draft_figures, "_audited_version", lambda *a, **k: {
        "version_no": 1, "audit": k["ocr_audit"], "semantic_audit": k["semantic_audit"],
        "detected_numerals": ["10"]})
    out = draft_figures.render_figure(
        7, 91, label="FIG. 1", caption="body", numerals=["10 = body"])
    assert out["numeral_audit"]["ok"] is True
    assert scales == [1.0, 1.35, 1.8]


def test_ensure_project_figures_draws_every_missing_spec_with_canonical_parts(monkeypatch):
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [])
    calls = []
    monkeypatch.setattr(draft_figures, "render_figure", lambda *a, **k: calls.append(k) or {
        "figure_id": len(calls), "numeral_audit": {"ok": True},
        "semantic_audit": {"ok": True}})
    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="a body carrying a pump",
        numeral_table=[{"numeral": "10", "part": "body"},
                       {"numeral": "12", "part": "pump"}],
        figure_specs=[
            {"label": "FIG. 1 - side view", "caption": "body and pump", "numerals": ["10", "12"]},
            {"label": "FIG. 2", "caption": "pump detail", "numerals": ["12"]},
        ])
    assert out["generated"] == 2 and len(calls) == 2
    assert calls[0]["numerals"] == ["10 = body", "12 = pump"]


def test_ensure_project_figures_collects_every_failed_sheet_before_repair(monkeypatch):
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [])
    attempted = []

    def render(*_args, **values):
        attempted.append(values["label"])
        if "1" in values["label"]:
            raise draft_figures.FigureError("wrong motor axis")
        return {"figure_id": 2, "numeral_audit": {"ok": True},
                "semantic_audit": {"ok": True}}

    monkeypatch.setattr(draft_figures, "render_figure", render)
    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body and pump",
        numeral_table=[{"numeral": "10", "part": "body"}],
        figure_specs=[
            {"label": "FIG. 1", "caption": "side view", "numerals": ["10"]},
            {"label": "FIG. 2", "caption": "bottom view", "numerals": ["10"]},
        ])
    assert attempted == ["FIG. 1", "FIG. 2"]
    assert out["ok"] is False and out["generated"] == 1
    assert out["errors"] == ["FIG. 1: wrong motor axis"]


def test_changed_figure_spec_is_reinspected_even_when_its_numerals_are_unchanged(monkeypatch):
    spec = {"label": "FIG. 1", "caption": "new sectional view", "numerals": ["10"]}
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 1", "active_version": 1,
        "versions": [{"version_no": 1, "numeral_audit": {"ok": True, "expected": ["10"]},
                      "semantic_audit": {"ok": True, "specification_hash": "stale"}}],
    }])
    calls = []
    monkeypatch.setattr(draft_figures, "render_figure", lambda *a, **k: calls.append(k) or {
        "figure_id": 8, "numeral_audit": {"ok": True}, "semantic_audit": {"ok": True}})
    monkeypatch.setattr(draft_figures, "archive_figure", lambda *a: True)
    draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body", numeral_table=[{"numeral": "10", "part": "body"}],
        figure_specs=[spec])
    assert len(calls) == 1


def test_obsolete_and_duplicate_sheets_are_archived_without_losing_history(monkeypatch):
    spec = {"label": "FIG. 1", "caption": "side view", "numerals": ["10"]}
    digest = draft_figures.specification_hash("FIG. 1", "side view", ["10 = body"])
    active = {"version_no": 1, "numeral_audit": {"ok": True, "expected": ["10"]},
              "semantic_audit": {"ok": True, "specification_hash": digest}}
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [
        {"id": 2, "figure_label": "FIG. 1", "active_version": 1, "versions": [active]},
        {"id": 3, "figure_label": "FIG. 1: newer", "active_version": 1, "versions": [active]},
        {"id": 4, "figure_label": "FIG. 9", "active_version": 1, "versions": [active]},
    ])
    archived = []
    monkeypatch.setattr(
        draft_figures, "archive_figure", lambda figure_id, user_id: archived.append(figure_id) or True)
    monkeypatch.setattr(
        draft_figures, "render_figure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the current sheet should be reused")))
    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body", numeral_table=[{"numeral": "10", "part": "body"}],
        figure_specs=[spec])
    assert archived == [2, 4]
    assert out["archived"] == 2 and out["reused"] == 1


def test_checked_images_are_materialized_for_the_independent_reviewer(monkeypatch, tmp_path):
    figures_dir = tmp_path / "figures"
    figures_dir.mkdir()
    (figures_dir / "rendered-old.png").write_bytes(b"old")
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 1: side view", "active_version": 2,
        "versions": [{"version_no": 2, "numeral_audit": {"ok": True},
                      "semantic_audit": {"ok": True}}],
    }])
    monkeypatch.setattr(draft_figures, "png_bytes", lambda *a, **k: ("image/png", b"checked"))
    count = draft_figures.materialize_review_images(7, 91, tmp_path)
    assert count == 1
    assert not (figures_dir / "rendered-old.png").exists()
    assert (figures_dir / "rendered-FIG-1.png").read_bytes() == b"checked"
