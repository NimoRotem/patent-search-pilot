"""Autonomous patent-drawing generation and pixel-level filing gates."""
import io
import hashlib
import json
import re

import pytest
from PIL import Image, ImageDraw

import draft_figures
import draft_workspace


def blank_png(width=640, height=420):
    image = Image.new("RGB", (width, height), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def accepted_leader_audit(**values):
    return {
        "ok": True, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.LEADER_PROMPT_VERSION,
        "review_count": draft_figures.LEADER_REVIEW_COUNT,
        "section_mark_anchor_audit": draft_figures._section_mark_anchor_audit([], []),
        **values,
    }


def accepted_ocr_audit(sheet_number="1/1", expected=("10",), **values):
    return {
        "ok": True, "inspected": True, "expected": list(expected),
        "prompt_version": draft_figures.OCR_PROMPT_VERSION,
        "correct_figure_label": True,
        "expected_sheet_number": sheet_number,
        "detected_sheet_numbers": [sheet_number],
        "correct_sheet_number": True,
        "expected_section_designations": [],
        "detected_section_designations": [],
        "correct_section_designations": True,
        **values,
    }


def accepted_marked_anchor_audit(**values):
    return {
        "ok": True, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.MARKED_ANCHOR_PROMPT_VERSION,
        "review_count": draft_figures.MARKED_ANCHOR_REVIEW_COUNT,
        **values,
    }


def accepted_cross_provider_audit(**values):
    return {
        "ok": True, "inspected": True,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_PROMPT_VERSION,
        "review_count": 1,
        **values,
    }


def accepted_cross_provider_geometry_audit(**values):
    return {
        "ok": True, "inspected": True,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_GEOMETRY_PROMPT_VERSION,
        "review_count": draft_figures.CROSS_PROVIDER_GEOMETRY_REVIEW_COUNT,
        "missing": [],
        "unexpected": [],
        "duplicates": [],
        "errors": [], "visible_elements": [],
        **values,
    }


def accepted_semantic_audit(**values):
    marked_values = ({"specification_hash": values["specification_hash"]}
                     if values.get("specification_hash") else {})
    if values.get("model_name"):
        marked_values["model_name"] = values["model_name"]
    return {
        "ok": True, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "pixel_anchor_audit": {
            "ok": True, "inspected": True,
            "version": draft_figures.PIXEL_ANCHOR_VERSION,
        },
        "topology_audit": {
            "ok": True, "inspected": False, "required": False,
            "version": draft_figures.CLOSED_REGION_AUDIT_VERSION,
        },
        "section_mark_audit": {
            "ok": True, "inspected": False, "required": False,
            "summary": "No cutting-plane designation is required.",
            "expected": [], "marks": [], "errors": [], "review_count": 0,
            "model_name": "deterministic-parser",
            "prompt_version": draft_figures.SECTION_MARK_PROMPT_VERSION,
        },
        "marked_anchor_audit": accepted_marked_anchor_audit(**marked_values),
        **values,
    }


def accept_pixel_grounding(monkeypatch):
    accepted = {
        "ok": True, "inspected": True,
        "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(
        draft_figures, "_apply_pixel_grounding",
        lambda _png, _numerals, semantic: {
            **semantic,
            "pixel_anchor_audit": dict(accepted),
        })
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, anchors, **_kwargs: (
            [dict(item) for item in anchors], dict(accepted)))
    monkeypatch.setattr(
        draft_figures, "inspect_marked_anchors",
        lambda *args, **kwargs: accepted_marked_anchor_audit())


def test_semantic_response_schema_is_inline_for_vertex():
    encoded = json.dumps(draft_figures.SEMANTIC_RESPONSE_SCHEMA)
    assert '"$ref"' not in encoded and '"$defs"' not in encoded
    assert draft_figures.SEMANTIC_RESPONSE_SCHEMA["properties"]["anchors"]["items"][
        "properties"]["numeral"]["type"] == "string"
    leader = json.dumps(draft_figures.LEADER_RESPONSE_SCHEMA)
    assert '"$ref"' not in leader and '"$defs"' not in leader
    marked = json.dumps(draft_figures.MARKED_ANCHOR_RESPONSE_SCHEMA)
    assert '"$ref"' not in marked and '"$defs"' not in marked
    marked_fields = draft_figures.MARKED_ANCHOR_RESPONSE_SCHEMA["properties"]["labels"][
        "items"]["properties"]
    assert {"repairable", "suggested_x", "suggested_y"} <= set(marked_fields)
    section_marks = json.dumps(draft_figures.SECTION_MARK_RESPONSE_SCHEMA)
    assert '"$ref"' not in section_marks and '"$defs"' not in section_marks
    section_fields = draft_figures.SECTION_MARK_RESPONSE_SCHEMA["properties"]["marks"][
        "items"]["properties"]
    assert {"start_x", "start_y", "end_x", "end_y", "view_dx", "view_dy"} <= set(
        section_fields)


def test_image_generation_uses_its_dedicated_location_client(monkeypatch):
    calls = []

    class Models:
        def generate_content(self, **values):
            calls.append(values)
            return "image-response"

    class Client:
        models = Models()

    monkeypatch.setattr(draft_figures, "_image_client", lambda: Client())
    monkeypatch.setattr(
        draft_figures.llm, "_client",
        lambda: (_ for _ in ()).throw(AssertionError("shared regional client must not be used")))

    assert draft_figures._model_call("draw exact geometry") == "image-response"
    assert calls and calls[0]["model"] == draft_figures.image_model()


def test_image_generation_waits_through_transient_capacity_exhaustion(monkeypatch):
    png = blank_png()
    calls = []
    sleeps = []

    class InlineData:
        data = png

    class Part:
        inline_data = InlineData()

    class Content:
        parts = [Part()]

    class Candidate:
        content = Content()

    class Response:
        usage_metadata = None
        candidates = [Candidate()]

    def generate(*_args, **_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) < 5:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: shared image capacity is busy")
        return Response()

    monkeypatch.setattr(draft_figures, "_model_call", generate)
    monkeypatch.setattr(draft_figures.time, "sleep", sleeps.append)
    monkeypatch.setattr(draft_figures.random, "uniform", lambda *_args: 0)

    assert draft_figures.generate_png("draw exact geometry") == png
    assert calls == [1, 2, 3, 4, 5]
    assert sleeps == [2, 4, 8, 16]


def test_image_generation_retries_a_provider_response_with_no_parts(monkeypatch):
    png = blank_png()
    calls = []
    sleeps = []

    class EmptyContent:
        parts = None

    class EmptyCandidate:
        content = EmptyContent()
        finish_reason = "IMAGE_RECITATION"

    class EmptyResponse:
        usage_metadata = None
        candidates = [EmptyCandidate()]

    class InlineData:
        data = png

    class ImagePart:
        inline_data = InlineData()

    class ImageContent:
        parts = [ImagePart()]

    class ImageCandidate:
        content = ImageContent()

    class ImageResponse:
        usage_metadata = None
        candidates = [ImageCandidate()]

    def generate(*_args, **_kwargs):
        calls.append(len(calls) + 1)
        return EmptyResponse() if len(calls) == 1 else ImageResponse()

    monkeypatch.setattr(draft_figures, "_model_call", generate)
    monkeypatch.setattr(draft_figures.time, "sleep", sleeps.append)
    monkeypatch.setattr(draft_figures.random, "uniform", lambda *_args: 0)

    assert draft_figures.generate_png("draw exact geometry") == png
    assert calls == [1, 2]
    assert sleeps == [0.35]


def test_image_generation_defers_repeated_provider_responses_with_no_parts(monkeypatch):
    calls = []
    sleeps = []

    class Content:
        parts = None

    class Candidate:
        content = Content()
        finish_reason = "IMAGE_RECITATION"

    class Response:
        usage_metadata = None
        candidates = [Candidate()]

    def generate(*_args, **_kwargs):
        calls.append(len(calls) + 1)
        return Response()

    monkeypatch.setattr(draft_figures, "_model_call", generate)
    monkeypatch.setattr(draft_figures.time, "sleep", sleeps.append)
    monkeypatch.setattr(draft_figures.random, "uniform", lambda *_args: 0)

    with pytest.raises(
            draft_figures.FigureTransientError, match="IMAGE_RECITATION"):
        draft_figures.generate_png("draw exact geometry")

    assert calls == [1, 2, 3]
    assert sleeps == [0.35, 0.7]


def test_marked_review_uses_a_full_sheet_coordinate_grid_as_its_correction_frame(monkeypatch):
    raw = blank_png()
    calls = []

    class Response:
        usage_metadata = None
        parsed = {
            "matches_spec": True, "summary": "endpoint is correct", "errors": [],
            "labels": [{
                "numeral": "10", "correct": True, "repairable": True,
                "evidence": "the center is inside the body",
                "suggested_x": 500, "suggested_y": 500,
            }],
        }

    class Models:
        def generate_content(self, **values):
            calls.append(values)
            return Response()

    class Client:
        models = Models()

    monkeypatch.setattr(draft_figures.llm, "_client", lambda: Client())
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *args: None)
    monkeypatch.setattr(draft_figures, "_analysis_cache_put", lambda *args, **kwargs: None)
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **kwargs: None)

    audit = draft_figures.inspect_marked_anchors(
        raw, label="FIG. 1", caption="The body 10 is rectangular.",
        numerals=["10 = body"],
        anchors=[{"numeral": "10", "x": 500, "y": 500, "visible": True}])

    assert audit["ok"] is True and len(calls) == 3
    coordinate_sheet = draft_figures._coordinate_grid_overlay(raw, native_pixels=True)
    for call in calls:
        images = [item for item in call["contents"]
                  if getattr(item, "inline_data", None)]
        assert len(images) == 2
        assert bytes(images[0].inline_data.data) == coordinate_sheet
        assert bytes(images[0].inline_data.data) != raw
        assert "coordinate grid" in call["contents"][-1].lower()


def test_coordinate_grid_preserves_raw_dimensions_and_marks_normalized_axes():
    raw = blank_png(1000, 800)

    sheet = Image.open(io.BytesIO(
        draft_figures._coordinate_grid_overlay(raw))).convert("RGB")

    assert sheet.size == (1000, 800)
    assert sheet.getpixel((500, 400)) != (255, 255, 255)
    assert sheet.getpixel((900, 400)) != (255, 255, 255)


def test_verbose_visual_evidence_does_not_abort_an_otherwise_valid_review():
    evidence = "visually verified endpoint " * 60
    semantic = draft_figures._SemanticInspection.model_validate({
        "matches_spec": True, "summary": "checked", "errors": [], "unexpected_text": [],
        "anchors": [{"numeral": "10", "x": 400, "y": 500, "visible": True,
                     "evidence": evidence}],
    })
    leaders = draft_figures._LeaderInspection.model_validate({
        "matches_spec": True, "summary": "checked", "errors": [],
        "labels": [{"numeral": "10", "correct": True, "evidence": evidence,
                    "suggested_x": 400, "suggested_y": 500}],
    })

    assert semantic.anchors[0].evidence == evidence
    assert leaders.labels[0].evidence == evidence


def test_figure_identity_uses_the_figure_number_not_a_truncated_caption():
    long = "FIG. 2 - Side elevation in vertical section showing the chamber and every air path"
    assert draft_figures.figure_key(long) == draft_figures.figure_key(long[:80]) == "fig-2"
    assert draft_figures.canonical_figure_label(long) == "FIG. 2"


def test_canonical_figure_label_normalizes_common_heading_separators():
    assert draft_figures.canonical_figure_label("FIG-1") == "FIG. 1"
    assert draft_figures.canonical_figure_label("Fig_2") == "FIG. 2"
    assert draft_figures.canonical_figure_label("FIG:3") == "FIG. 3"


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


def test_cloud_vision_ocr_separates_the_sheet_number_from_reference_numerals():
    response = {"responses": [{
        "fullTextAnnotation": {
            "text": "1 / 5\n10 12\nFIG. 1\n",
            "pages": [{"blocks": [{"paragraphs": [{"words": [
                {"confidence": 0.99}, {"confidence": 0.98},
                {"confidence": 0.97}, {"confidence": 0.96},
            ]}]}]}],
        }
    }]}

    found = draft_figures.parse_ocr_response(response)

    assert found["sheet_numbers"] == ["1/5"]
    assert found["numerals"] == ["10", "12"]
    assert found["figure_label"] == "FIG. 1"
    assert found["other_text"] == []


def test_section_designations_are_required_only_on_the_source_view_with_a_cutting_plane():
    source_view = (
        "A broken cutting-plane line crosses the upper carriage. A short arrow at each end "
        "points left. Each end carries the numeral 3, so the line reads as line 3-3; the two "
        "marks are section designations, not reference numerals.")
    resulting_view = (
        "FIG. 3 is a sectional view taken on line 3-3 of FIG. 1. Cut material is hatched.")

    assert draft_figures.section_designations(source_view) == ["3"]
    assert draft_figures.section_designations(resulting_view) == []


def test_section_designations_accept_a_comma_between_line_and_repeated_mark():
    source_view = (
        "A first cutting-plane line, 5-5, crosses the first carriage. Both ends have viewing "
        "arrows pointing left and the repeated designation 5 at each end. A second "
        "cutting-plane line, 8-8, crosses the second carriage. Both ends have viewing arrows "
        "pointing left and the repeated designation 8 at each end.")

    assert draft_figures.section_designations(source_view) == ["5", "8"]


def test_section_mark_consensus_requires_two_complete_coordinate_reviews():
    first = {
        "matches_spec": True, "summary": "line crosses the named carriage", "errors": [],
        "marks": [{
            "designation": "3", "start_x": 510, "start_y": 90,
            "end_x": 510, "end_y": 430, "view_dx": -1000, "view_dy": 0,
            "evidence": "outer endpoint to jaw face on the radial center line",
        }],
    }
    second = {
        "matches_spec": True, "summary": "same cutting plane", "errors": [],
        "marks": [{
            "designation": "3", "start_x": 500, "start_y": 100,
            "end_x": 500, "end_y": 440, "view_dx": -1000, "view_dy": 0,
            "evidence": "the specified top carriage axis",
        }],
    }

    accepted = draft_figures.section_mark_consensus(["3"], [first, second])
    divergent = draft_figures.section_mark_consensus([
        "3"], [first, {**second, "marks": [{
            **second["marks"][0], "start_x": 850, "end_x": 850,
        }]}])

    assert accepted["ok"] is True
    assert accepted["review_count"] == draft_figures.SECTION_MARK_REVIEW_COUNT
    assert accepted["marks"] == [{
        "designation": "3", "start_x": 505, "start_y": 95,
        "end_x": 505, "end_y": 435, "view_dx": -1000, "view_dy": 0,
        "evidence": (
            "outer endpoint to jaw face on the radial center line | "
            "the specified top carriage axis"),
    }]
    assert divergent["ok"] is False
    assert any("disagree" in item.lower() for item in divergent["errors"])


def test_no_section_mark_is_a_current_deterministic_audit():
    audit = draft_figures.inspect_section_marks(
        blank_png(), label="FIG. 1", caption="Plan view of the body.", anchors=[])

    assert audit["ok"] is True and audit["required"] is False
    assert draft_figures.current_section_mark_audit(audit) is True
    assert draft_figures.current_section_mark_audit({**audit, "prompt_version": "old"}) is False


def test_required_section_mark_uses_two_coordinate_grid_vision_reviews(monkeypatch):
    calls = []

    class Response:
        usage_metadata = None
        parsed = {
            "matches_spec": True, "summary": "the specified radial cutting plane", "errors": [],
            "marks": [{
                "designation": "3", "start_x": 500, "start_y": 80,
                "end_x": 500, "end_y": 440, "view_dx": -1000, "view_dy": 0,
                "evidence": "from outside the frame to the pad face",
            }],
        }

    class Models:
        def generate_content(self, **values):
            calls.append(values)
            return Response()

    class Client:
        models = Models()

    monkeypatch.setattr(draft_figures.llm, "_client", lambda: Client())
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *args: None)
    monkeypatch.setattr(draft_figures, "_analysis_cache_put", lambda *args, **kwargs: None)
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **kwargs: None)

    audit = draft_figures.inspect_section_marks(
        blank_png(), label="FIG. 1",
        caption=(
            "A broken cutting-plane line crosses the top carriage. Each end carries 3, so the "
            "line reads as line 3-3. Both arrows point left."),
        anchors=[{"numeral": "30", "x": 500, "y": 230, "visible": True}],
    )

    assert audit["ok"] is True and audit["required"] is True
    assert draft_figures.current_section_mark_audit(audit) is True
    assert len(calls) == draft_figures.SECTION_MARK_REVIEW_COUNT
    assert all("line 3-3" in call["contents"][-1].lower() for call in calls)
    assert all(len([item for item in call["contents"]
                    if getattr(item, "inline_data", None)]) == 2 for call in calls)


def test_ocr_audit_requires_the_exact_single_sheet_number_when_expected():
    inspection = {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 2",
        "sheet_numbers": ["2/5"], "other_text": [], "confidence": 0.99,
    }

    good = draft_figures.ocr_audit(
        ["10 = body"], inspection, "FIG. 2", sheet_number="2/5")
    wrong = draft_figures.ocr_audit(
        ["10 = body"], inspection, "FIG. 2", sheet_number="3/5")

    assert good["ok"] is True
    assert good["correct_sheet_number"] is True
    assert good["detected_sheet_numbers"] == ["2/5"]
    assert wrong["ok"] is False
    assert wrong["correct_sheet_number"] is False


def test_ocr_audit_accounts_for_exact_duplicate_section_designations_separately():
    inspection = {
        "ok": True, "numerals": ["3", "10", "3"], "figure_label": "FIG. 1",
        "sheet_numbers": ["1/4"], "other_text": [], "confidence": 0.99,
    }

    accepted = draft_figures.ocr_audit(
        ["10 = frame"], inspection, "FIG. 1", sheet_number="1/4",
        section_designations=["3"])
    missing = draft_figures.ocr_audit(
        ["10 = frame"], {**inspection, "numerals": ["3", "10"]},
        "FIG. 1", sheet_number="1/4", section_designations=["3"])
    duplicate = draft_figures.ocr_audit(
        ["10 = frame"], {**inspection, "numerals": ["3", "10", "3", "3"]},
        "FIG. 1", sheet_number="1/4", section_designations=["3"])

    assert accepted["ok"] is True
    assert accepted["detected"] == ["10"]
    assert accepted["expected_section_designations"] == ["3", "3"]
    assert accepted["detected_section_designations"] == ["3", "3"]
    assert accepted["correct_section_designations"] is True
    assert missing["ok"] is False and missing["correct_section_designations"] is False
    assert duplicate["ok"] is False and duplicate["correct_section_designations"] is False


def test_current_ocr_audit_rejects_an_old_gate_or_different_sheet_total():
    current = accepted_ocr_audit("2/5")

    assert draft_figures.current_ocr_audit(
        current, expected_sheet_number="2/5") is True
    assert draft_figures.current_ocr_audit(
        current, expected_sheet_number="2/6") is False
    assert draft_figures.current_ocr_audit(
        {**current, "prompt_version": "old"}, expected_sheet_number="2/5") is False
    assert draft_figures.current_ocr_audit(
        current, expected_sheet_number="2/5", expected_section_designations=["3"]) is False


def test_current_section_mark_audit_rejects_invalid_stored_coordinates():
    current = {
        "ok": True, "inspected": True, "required": True,
        "expected": ["3"], "errors": [], "review_count": 2,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SECTION_MARK_PROMPT_VERSION,
        "marks": [{
            "designation": "3", "start_x": 500, "start_y": 300,
            "end_x": 500, "end_y": 300, "view_dx": -1000, "view_dy": 0,
            "evidence": "collapsed line",
        }],
    }

    assert draft_figures.current_section_mark_audit(current) is False


def test_section_mark_anchor_audit_rejects_reference_dots_on_cutting_lines():
    anchors = [
        {"numeral": "16", "x": 500, "y": 500, "visible": True},
        {"numeral": "24", "x": 136, "y": 500, "visible": True},
    ]
    marks = [
        {"designation": "2", "start_x": 80, "start_y": 500,
         "end_x": 920, "end_y": 500, "view_dx": 0, "view_dy": 1},
        {"designation": "4", "start_x": 500, "start_y": 80,
         "end_x": 500, "end_y": 920, "view_dx": -1, "view_dy": 0},
    ]

    audit = draft_figures._section_mark_anchor_audit(anchors, marks)

    assert audit["ok"] is False
    assert audit["colliding_numerals"] == ["16", "24"]
    assert draft_figures.current_section_mark_anchor_audit(audit) is False


def test_section_mark_collision_repair_moves_interior_targets_off_both_lines():
    specification = """
    The sheet shows the perimeter member 24 as one rectangular ring, and within it the second
    side 16 as a plain open field; no other body is drawn. The ring is drawn as one rectangle
    with a smaller rectangle inside it, the inner rectangle standing well in from all four sides.
    The field enclosed by the inner rectangle is open paper. The ring stands well in from every
    side of the drawing area.
    - The perimeter member 24 is the band between the edges. Identified well inside that band
      along the left-hand side of the ring.
    - The second side 16 is the plain field inside the inner edge. Identified well inside it.
    """
    raw = draft_figures._deterministic_nested_plan_png(specification)
    numerals = ["16 = second side", "24 = perimeter member"]
    semantic = {
        "ok": True,
        "anchors": [
            {"numeral": "16", "x": 500, "y": 500, "visible": True,
             "evidence": "well inside the plain field"},
            {"numeral": "24", "x": 136, "y": 500, "visible": True,
             "evidence": "well inside the left ring band"},
        ],
    }
    marks = [
        {"designation": "2", "start_x": 80, "start_y": 500,
         "end_x": 920, "end_y": 500, "view_dx": 0, "view_dy": 1},
        {"designation": "4", "start_x": 500, "start_y": 80,
         "end_x": 500, "end_y": 920, "view_dx": -1, "view_dy": 0},
    ]

    repaired, audit = draft_figures._repair_section_mark_anchor_collisions(
        raw, semantic["anchors"], marks, numerals=numerals)

    assert audit["ok"] is True
    assert audit["adjusted_numerals"] == ["16", "24"]
    assert draft_figures.current_section_mark_anchor_audit(audit) is True
    positions = {item["numeral"]: (item["x"], item["y"]) for item in repaired}
    assert positions["16"] != (500, 500)
    assert positions["24"] != (136, 500)
    assert draft_figures._section_mark_anchor_audit(repaired, marks)["ok"] is True


def test_ocr_audit_rejects_an_extra_invalid_sheet_marking():
    inspection = {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 2",
        "sheet_numbers": ["2/5", "0/5"], "other_text": [], "confidence": 0.99,
    }

    audit = draft_figures.ocr_audit(
        ["10 = body"], inspection, "FIG. 2", sheet_number="2/5")

    assert audit["ok"] is False
    assert audit["correct_sheet_number"] is False
    assert audit["detected_sheet_numbers"] == ["2/5", "0/5"]
    assert draft_figures.current_ocr_audit(
        accepted_ocr_audit("2/5"), expected_sheet_number="2/0") is False


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

    result["anchors"][1].update({"visible": True, "x": 220, "y": 300})
    audit = draft_figures.semantic_audit(["10 = body", "12 = pump"], result)
    assert audit["ok"] is False and audit["anchor_collisions"] == [["10", "12"]]


def test_semantic_review_treats_named_surfaces_and_spaces_as_visible_geometry():
    guidance = draft_figures.SEMANTIC_GEOMETRY_RULES.lower()
    for term in ("face", "side", "surface", "opening", "chamber", "boundary"):
        assert term in guidance
    assert "physically separate object" in guidance
    assert "distinct representative endpoint" in guidance
    assert "must not share coordinates" in guidance


def test_semantic_consensus_fails_when_the_constraint_trace_disagrees():
    expected = ["10 = base", "12 = closed loop"]
    primary = {
        "matches_spec": True, "summary": "all geometry appears present", "errors": [],
        "unexpected_text": [],
        "anchors": [
            {"numeral": "10", "x": 300, "y": 500, "visible": True, "evidence": "base"},
            {"numeral": "12", "x": 500, "y": 500, "visible": True,
             "evidence": "closed loop"},
        ],
    }
    adversarial = {
        "matches_spec": False, "summary": "an extra ring violates the exact count",
        "errors": ["The image has four rings where exactly three are required."],
        "unexpected_text": [],
        "anchors": [
            {"numeral": "10", "x": 300, "y": 500, "visible": True, "evidence": "base"},
            {"numeral": "12", "x": 500, "y": 500, "visible": True,
             "evidence": "loop is present but its geometry is wrong"},
        ],
    }
    consensus = draft_figures.semantic_consensus(expected, [primary, adversarial])
    assert consensus["ok"] is False and consensus["review_count"] == 2
    assert "four rings" in consensus["errors"][0]


def test_marked_anchor_consensus_rejects_a_dot_on_neighboring_hatching():
    expected = ["26 = bearing face", "36 = covering element"]
    primary = {
        "matches_spec": True, "summary": "both centers match", "errors": [],
        "labels": [
            {"numeral": "26", "correct": True, "evidence": "center is on the boundary"},
            {"numeral": "36", "correct": True, "evidence": "center is within the band"},
        ],
    }
    adversarial = {
        "matches_spec": False, "summary": "26 is inside the neighboring band",
        "errors": ["The center for 26 is inside hatching, not on the bearing-face boundary."],
        "labels": [
            {"numeral": "26", "correct": False,
             "evidence": "the marked center lies below the required boundary"},
            {"numeral": "36", "correct": True, "evidence": "center is within the band"},
        ],
    }

    audit = draft_figures.marked_anchor_consensus(expected, [primary, adversarial])

    assert audit["ok"] is False
    assert audit["incorrect"] == ["26"]
    assert audit["review_count"] == 2


def test_marked_anchor_consensus_accepts_two_independent_approvals_out_of_three():
    expected = ["26 = bearing face"]
    approved = {
        "matches_spec": True, "summary": "center is on the boundary", "errors": [],
        "labels": [{
            "numeral": "26", "correct": True, "repairable": True,
            "evidence": "the center intersects the upper boundary",
            "suggested_x": 500, "suggested_y": 500,
        }],
    }
    dissent = {
        "matches_spec": False, "summary": "center may be below the boundary",
        "errors": ["The center appears just below the boundary."],
        "labels": [{
            "numeral": "26", "correct": False, "repairable": True,
            "evidence": "the center appears one pixel below the boundary",
            "suggested_x": 500, "suggested_y": 490,
        }],
    }

    audit = draft_figures.marked_anchor_consensus(
        expected, [approved, dissent, approved])

    assert audit["ok"] is True
    assert audit["labels"][0]["correct_votes"] == 2
    assert audit["labels"][0]["incorrect_votes"] == 1
    assert audit["errors"] == []


def test_marked_anchor_consensus_uses_the_median_majority_correction():
    expected = ["26 = bearing face"]
    approved = {
        "matches_spec": True, "summary": "center appears correct", "errors": [],
        "labels": [{
            "numeral": "26", "correct": True, "repairable": True,
            "evidence": "the center appears on the boundary",
            "suggested_x": 500, "suggested_y": 500,
        }],
    }
    rejected_left = {
        "matches_spec": False, "summary": "move right", "errors": ["center is left"],
        "labels": [{
            "numeral": "26", "correct": False, "repairable": True,
            "evidence": "the boundary is to the right",
            "suggested_x": 620, "suggested_y": 480,
        }],
    }
    rejected_right = {
        "matches_spec": False, "summary": "move right", "errors": ["center is left"],
        "labels": [{
            "numeral": "26", "correct": False, "repairable": True,
            "evidence": "the same boundary is slightly farther right",
            "suggested_x": 660, "suggested_y": 500,
        }],
    }

    audit = draft_figures.marked_anchor_consensus(
        expected, [approved, rejected_left, rejected_right])

    assert audit["ok"] is False and audit["incorrect"] == ["26"]
    assert audit["labels"][0]["correct_votes"] == 1
    assert audit["labels"][0]["incorrect_votes"] == 2
    assert audit["labels"][0]["suggested_x"] == 640
    assert audit["labels"][0]["suggested_y"] == 490


def test_marked_anchor_consensus_ignores_rejected_noop_coordinates():
    expected = ["44 = handle"]
    noop = {
        "matches_spec": False, "summary": "not at midpoint", "errors": ["wrong point"],
        "labels": [{
            "numeral": "44", "correct": False, "repairable": True,
            "evidence": "the current point is on the cross-bar corner",
            "suggested_x": 751, "suggested_y": 421,
        }],
    }
    actionable = {
        "matches_spec": False, "summary": "move to midpoint", "errors": ["wrong point"],
        "labels": [{
            "numeral": "44", "correct": False, "repairable": True,
            "evidence": "the midpoint is farther right",
            "suggested_x": 795, "suggested_y": 421,
        }],
    }

    audit = draft_figures.marked_anchor_consensus(
        expected, [noop, noop, actionable], current_positions={"44": (751, 421)})

    assert audit["labels"][0]["suggested_x"] == 795
    assert audit["labels"][0]["suggested_y"] == 421
    assert audit["labels"][0]["repairable"] is True


def test_marked_anchor_repair_uses_the_grid_grounded_full_sheet_suggestion():
    raw = blank_png(1000, 1000)
    anchors = [{"numeral": "26", "x": 100, "y": 200, "visible": True,
                "evidence": "bearing face"}]
    audit = {
        "incorrect": ["26"],
        "labels": [{
            "numeral": "26", "correct": False, "repairable": True,
            "evidence": "the bearing face is far to the lower right",
            "suggested_x": 900, "suggested_y": 800,
        }],
    }

    repaired, changed = draft_figures._repair_marked_anchors(raw, anchors, audit)

    assert changed is True
    assert repaired[0]["x"] == 900
    assert repaired[0]["y"] == 800


def test_marked_anchor_repair_converts_native_raw_pixels_to_normalized_geometry():
    raw = blank_png(1400, 900)
    anchors = [{"numeral": "18", "x": 500, "y": 500, "visible": True,
                "evidence": "left housing front face"}]
    audit = {
        "coordinate_space": "raw_pixels",
        "coordinate_width": 1400,
        "coordinate_height": 900,
        "incorrect": ["18"],
        "labels": [{
            "numeral": "18", "correct": False, "repairable": True,
            "evidence": "the center of the housing is at raw pixel 290, 308",
            "suggested_x": 290, "suggested_y": 308,
        }],
    }

    repaired, changed = draft_figures._repair_marked_anchors(raw, anchors, audit)

    assert changed is True
    assert repaired[0]["x"] == 207
    assert repaired[0]["y"] == 343


def test_marked_anchor_repair_rejects_pixel_metadata_for_a_different_sheet():
    raw = blank_png(1400, 900)
    anchors = [{"numeral": "18", "x": 500, "y": 500, "visible": True,
                "evidence": "left housing front face"}]
    audit = {
        "coordinate_space": "raw_pixels",
        "coordinate_width": 1000,
        "coordinate_height": 1000,
        "incorrect": ["18"],
        "labels": [{
            "numeral": "18", "correct": False, "repairable": True,
            "evidence": "coordinate came from a different sheet",
            "suggested_x": 290, "suggested_y": 308,
        }],
    }

    repaired, changed = draft_figures._repair_marked_anchors(raw, anchors, audit)

    assert changed is False
    assert repaired == anchors


def test_marked_anchor_repair_breaks_a_two_coordinate_cycle():
    raw = blank_png(1000, 1000)
    anchors = [{"numeral": "10", "x": 500, "y": 563, "visible": True,
                "evidence": "front face"}]
    audit = {
        "incorrect": ["10"],
        "labels": [{
            "numeral": "10", "correct": False, "repairable": True,
            "evidence": "the other endpoint is the opposite edge of the face",
            "suggested_x": 500, "suggested_y": 625,
        }],
    }

    repaired, changed = draft_figures._repair_marked_anchors(
        raw, anchors, audit,
        coordinate_history={"10": [(500, 625), (500, 563)]})

    assert changed is True
    assert repaired[0]["x"] == 500
    assert repaired[0]["y"] == 594


def test_compose_rejects_a_tight_cluster_of_six_uncertified_coordinates(monkeypatch):
    raw = blank_png(1000, 1000)
    anchors = [{"numeral": "10", "x": 400, "y": 565, "visible": True,
                "evidence": "open surface"}]
    accepted_pixel = {
        "ok": True, "inspected": True, "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(draft_figures, "_marked_progress_get", lambda *a, **k: {
        "anchors": anchors, "certificates": {}, "attempts": 6,
        "coordinate_history": {"10": [
            (383, 550), (375, 565), (410, 575),
            (408, 568), (410, 550), (400, 565),
        ]},
    })
    monkeypatch.setattr(draft_figures, "_marked_progress_put", lambda *a, **k: None)
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, values, **_kwargs: ([dict(item) for item in values],
                                                    dict(accepted_pixel)))
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99,
    })
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [], "missing": [],
        "labels": [{"numeral": "10", "correct": True, "evidence": "route is clear"}],
    })
    monkeypatch.setattr(
        draft_figures, "inspect_marked_anchors",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("stalled geometry must regenerate before another review")))

    _png, labels, leaders, final_anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 1", caption="open surface", numerals=["10 = device"],
        semantic={"anchors": anchors, "pixel_anchor_audit": dict(accepted_pixel)})

    assert labels["ok"] is True and leaders["ok"] is False and pixel["ok"] is True
    assert "regenerate" in leaders["marked_anchor_audit"]["errors"][0].lower()
    assert (final_anchors[0]["x"], final_anchors[0]["y"]) == (400, 565)


def test_six_rejected_repairs_snapped_to_the_same_coordinate_are_stalled():
    anchors = [{"numeral": "26", "x": 500, "y": 520, "visible": True}]
    history = {}

    for _attempt in range(draft_figures.MARKED_ANCHOR_STALL_WINDOW):
        draft_figures._record_rejected_anchor_coordinates(history, anchors, ["26"])

    assert draft_figures._stalled_marked_anchor_numerals(history, ["26"]) == ["26"]


def test_six_rejected_repairs_within_the_same_sheet_region_are_stalled():
    history = {"36": [
        (756, 750), (800, 800), (800, 750),
        (875, 725), (812, 820), (810, 823),
    ]}

    assert draft_figures._stalled_marked_anchor_numerals(history, ["36"]) == ["36"]


def test_cross_provider_veto_rejects_unanimous_same_provider_certificate(monkeypatch):
    raw = blank_png(1000, 1000)
    anchors = [{"numeral": "10", "x": 400, "y": 565, "visible": True,
                "evidence": "upper-block top face"}]
    accepted_pixel = {
        "ok": True, "inspected": True, "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(draft_figures, "_marked_progress_get", lambda *a, **k: {
        "anchors": anchors,
        "certificates": {"10": {
            "x": 400, "y": 565, "attempt": 1,
            "label": {
                "numeral": "10", "correct": True,
                "evidence": "three same-provider reviewers approved this coordinate",
                "correct_votes": 3, "incorrect_votes": 0,
            },
        }},
        "attempts": 1, "coordinate_history": {"10": [(400, 565)]},
    })
    monkeypatch.setattr(draft_figures, "_marked_progress_put", lambda *a, **k: None)
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, values, **_kwargs: ([dict(item) for item in values],
                                                    dict(accepted_pixel)))
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99,
    })
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [], "missing": [],
        "labels": [{"numeral": "10", "correct": True, "evidence": "route is clear"}],
    })
    monkeypatch.setattr(
        draft_figures, "inspect_marked_anchors",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the stored same-provider certificate must be reused")))
    monkeypatch.setattr(draft_figures, "inspect_cross_provider_endpoints", lambda *a, **k: {
        "ok": False, "inspected": True, "incorrect": ["10"],
        "missing": [], "unexpected": [], "duplicates": [],
        "errors": ["Numeral 10 is on the front face, not the required top face."],
        "labels": [{
            "numeral": "10", "correct": False,
            "evidence": "the terminal dot is below the top-face boundary",
        }],
        "model_name": "claude-opus-5",
        "prompt_version": draft_figures.CROSS_PROVIDER_PROMPT_VERSION,
        "review_count": 1,
    })

    _png, labels, leaders, final_anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 1", caption="10 identified on the upper-block top face.",
        numerals=["10 = vibration device"],
        semantic={"anchors": anchors, "pixel_anchor_audit": dict(accepted_pixel)})

    marked = leaders["marked_anchor_audit"]
    assert labels["ok"] is True and leaders["ok"] is False and pixel["ok"] is True
    assert marked["cross_provider_audit"]["ok"] is False
    assert marked["incorrect"] == ["10"]
    assert "cross-provider" in marked["errors"][0].lower()
    assert (final_anchors[0]["x"], final_anchors[0]["y"]) == (400, 565)


def test_byte_exact_certificate_resolves_only_a_sub_dot_endpoint_correction(monkeypatch):
    raw = blank_png(1400, 900)
    anchors = [{"numeral": "44", "x": 315, "y": 350, "visible": True}]
    monkeypatch.setattr(draft_figures, "inspect_cross_provider_endpoints", lambda *a, **k: {
        "ok": False, "inspected": True, "incorrect": ["44"],
        "summary": "Numeral 44 is not clear of the component boundary.",
        "reported_matches_spec": False,
        "expected": ["44"], "observed": ["44"],
        "missing": [], "unexpected": [], "duplicates": [],
        "errors": ["44: the endpoint is on an edge."],
        "labels": [{
            "numeral": "44", "correct": False, "repairable": True,
            "evidence": "Current (441, 315); suggested interior (441, 311).",
            "suggested_x": 441, "suggested_y": 311,
        }],
        "coordinate_space": "raw_pixels",
        "coordinate_width": 1400, "coordinate_height": 900,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_PROMPT_VERSION,
        "review_count": draft_figures.CROSS_PROVIDER_REVIEW_COUNT,
    })
    certified = {
        "ok": True, "inspected": True,
        "model_name": "deterministic-compositor",
        "prompt_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "certificate_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "review_count": 0,
        "coordinate_certificates": [{
            "numeral": "44", "x": 315, "y": 350,
            "certificate_source": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        }],
    }

    result = draft_figures._apply_cross_provider_endpoint_gate(
        certified, raw, raw_png=raw, anchors=anchors,
        label="FIG. 1", caption="handle face", numerals=["44 = handle"])

    assert result["ok"] is True
    audit = result["cross_provider_audit"]
    assert audit["ok"] is True
    assert audit["incorrect"] == [] and audit["errors"] == []
    assert audit["deterministic_resolution"]["provider_incorrect"] == ["44"]
    assert audit["labels"][0]["correct"] is True
    assert audit["labels"][0]["provider_correct"] is False
    assert "not clear" not in audit["summary"]
    assert audit["provider_summary"] == (
        "Numeral 44 is not clear of the component boundary.")
    assert "byte-exact component certificate" in audit["labels"][0]["evidence"]
    assert "Current (441, 315)" in audit["labels"][0]["provider_evidence"]


def test_review_evidence_reconciles_a_stored_deterministic_endpoint_resolution():
    endpoints = accepted_cross_provider_audit(
        summary=(
            "Numeral 24 is not clear of both edges. The proposed correction was resolved by "
            "the complete byte-exact component certificate."),
        provider_incorrect=["24"],
        provider_errors=["Numeral 24 is not clear of both edges."],
        deterministic_resolution={
            "version": draft_figures.DETERMINISTIC_ENDPOINT_RESOLUTION_VERSION,
            "provider_incorrect": ["24"],
            "coordinates": [{
                "numeral": "24", "current_x": 190, "current_y": 450,
                "suggested_x": 190, "suggested_y": 450,
                "delta_x": 0, "delta_y": 0, "basis": "sub_dot",
            }],
        },
        labels=[{
            "numeral": "24", "correct": True, "provider_correct": False,
            "evidence": "The endpoint is not clear of both edges.",
            "suggested_x": 190, "suggested_y": 450,
        }],
    )

    review = draft_figures._review_endpoint_evidence(endpoints)

    assert review["summary"] == (
        "The byte-exact component certificate resolves the endpoint provider concern for "
        "numeral 24. The final endpoint is certified on its designated rendered component.")
    assert review["labels"] == [{
        "numeral": "24", "correct": True,
        "evidence": (
            "The byte-exact component certificate verifies numeral 24 at raw pixel "
            "(190, 450); the provider correction is smaller than the rendered endpoint dot."),
        "resolution_version": draft_figures.DETERMINISTIC_ENDPOINT_RESOLUTION_VERSION,
        "resolution_basis": "sub_dot",
        "certified_x": 190, "certified_y": 450,
    }]


def test_byte_exact_certificate_keeps_a_material_endpoint_correction_as_a_veto(monkeypatch):
    raw = blank_png(1400, 900)
    anchors = [{"numeral": "44", "x": 315, "y": 350, "visible": True}]
    monkeypatch.setattr(draft_figures, "inspect_cross_provider_endpoints", lambda *a, **k: {
        "ok": False, "inspected": True, "incorrect": ["44"],
        "reported_matches_spec": False,
        "expected": ["44"], "observed": ["44"],
        "missing": [], "unexpected": [], "duplicates": [],
        "errors": ["44: the endpoint is on the wrong feature."],
        "labels": [{
            "numeral": "44", "correct": False, "repairable": True,
            "evidence": "A different feature is at (465, 311).",
            "suggested_x": 465, "suggested_y": 311,
        }],
        "coordinate_space": "raw_pixels",
        "coordinate_width": 1400, "coordinate_height": 900,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_PROMPT_VERSION,
        "review_count": draft_figures.CROSS_PROVIDER_REVIEW_COUNT,
    })
    certified = {
        "ok": True, "inspected": True,
        "model_name": "deterministic-compositor",
        "prompt_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "certificate_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "review_count": 0,
        "coordinate_certificates": [{
            "numeral": "44", "x": 315, "y": 350,
            "certificate_source": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        }],
    }

    result = draft_figures._apply_cross_provider_endpoint_gate(
        certified, raw, raw_png=raw, anchors=anchors,
        label="FIG. 1", caption="handle face", numerals=["44 = handle"])

    assert result["ok"] is False
    assert result["cross_provider_audit"]["incorrect"] == ["44"]


def test_byte_exact_certificate_resolves_same_enclosed_component_provider_misread(monkeypatch):
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    its right-hand part, leaving a wide open expanse of tile to the left. The machine is one
    plain rectangular body standing on a band that runs round its underside. The body and the
    band are the whole of the machine drawn on this sheet. The flexible pulling element 46 is
    drawn as one slack curved path, a single continuous curved line. It runs away to the left,
    sagging gently over the open expanse of tile.
    """
    raw = draft_figures._deterministic_pulling_scene_png(specification)
    assert raw is not None
    anchors = [{"numeral": "24", "x": 608, "y": 521, "visible": True}]
    monkeypatch.setattr(draft_figures, "inspect_cross_provider_endpoints", lambda *a, **k: {
        "ok": False, "inspected": True, "incorrect": ["24"],
        "reported_matches_spec": False,
        "expected": ["24"], "observed": ["24"],
        "missing": [], "unexpected": [], "duplicates": [],
        "errors": ["Numeral 24: the endpoint is below the front strip."],
        "labels": [{
            "numeral": "24", "correct": False, "repairable": True,
            "evidence": "Current (851, 468); suggested interior (851, 447).",
            "suggested_x": 851, "suggested_y": 447,
        }],
        "coordinate_space": "raw_pixels",
        "coordinate_width": 1400, "coordinate_height": 900,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_PROMPT_VERSION,
        "review_count": draft_figures.CROSS_PROVIDER_REVIEW_COUNT,
    })
    certified = {
        "ok": True, "inspected": True,
        "model_name": "deterministic-compositor",
        "prompt_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "certificate_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "review_count": 0,
        "coordinate_certificates": [{
            "numeral": "24", "x": 608, "y": 521,
            "certificate_source": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        }],
    }

    result = draft_figures._apply_cross_provider_endpoint_gate(
        certified, raw, raw_png=raw, anchors=anchors,
        label="FIG. 5", caption=specification, numerals=["24 = perimeter member"])

    assert result["ok"] is True
    resolution = result["cross_provider_audit"]["deterministic_resolution"]
    assert resolution["coordinates"][0]["basis"] == "same_enclosed_component"


def test_byte_exact_certificate_resolves_a_clear_interior_provider_misread(monkeypatch):
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    its right-hand part, leaving a wide open expanse of tile to the left. The machine is one
    plain rectangular body standing on a band that runs round its underside. The body and the
    band are the whole of the machine drawn on this sheet. The flexible pulling element 46 is
    drawn as one slack curved path, a single continuous curved line. It runs away to the left,
    sagging gently over the open expanse of tile.
    """
    raw = draft_figures._deterministic_pulling_scene_png(specification)
    assert raw is not None
    anchors = [{
        "numeral": "24", "x": 608, "y": 521, "visible": True,
        "target_evidence": "well inside the broad front strip of the band",
    }]
    monkeypatch.setattr(draft_figures, "inspect_cross_provider_endpoints", lambda *a, **k: {
        "ok": False, "inspected": True, "incorrect": ["24"],
        "reported_matches_spec": False,
        "expected": ["24"], "observed": ["24"],
        "missing": [], "unexpected": [], "duplicates": [],
        "errors": ["Numeral 24: the endpoint is below the front strip."],
        "labels": [{
            "numeral": "24", "correct": False, "repairable": True,
            "evidence": "Current (851, 468); suggested boundary point (862, 442).",
            "suggested_x": 862, "suggested_y": 442,
        }],
        "coordinate_space": "raw_pixels",
        "coordinate_width": 1400, "coordinate_height": 900,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_PROMPT_VERSION,
        "review_count": draft_figures.CROSS_PROVIDER_REVIEW_COUNT,
    })
    certified = {
        "ok": True, "inspected": True,
        "model_name": "deterministic-compositor",
        "prompt_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "certificate_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "review_count": 0,
        "coordinate_certificates": [{
            "numeral": "24", "x": 608, "y": 521,
            "certificate_source": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        }],
    }

    result = draft_figures._apply_cross_provider_endpoint_gate(
        certified, raw, raw_png=raw, anchors=anchors,
        label="FIG. 5", caption=specification, numerals=["24 = perimeter member"])

    assert result["ok"] is True
    resolution = result["cross_provider_audit"]["deterministic_resolution"]
    assert resolution["coordinates"][0]["basis"] == "certified_clear_interior"


def test_byte_exact_certificate_resolves_a_certified_line_target_provider_misread(monkeypatch):
    image = Image.new("RGB", (1000, 1000), "white")
    ImageDraw.Draw(image).line((400, 300, 400, 500), fill="black", width=4)
    output = io.BytesIO()
    image.save(output, format="PNG")
    raw = output.getvalue()
    anchors = [{
        "numeral": "38", "x": 400, "y": 400, "visible": True,
        "target_evidence": "on the right wall of the radial-guide channel",
    }]
    monkeypatch.setattr(draft_figures, "inspect_cross_provider_endpoints", lambda *a, **k: {
        "ok": False, "inspected": True, "incorrect": ["38"],
        "reported_matches_spec": False,
        "expected": ["38"], "observed": ["38"],
        "missing": [], "unexpected": [], "duplicates": [],
        "errors": ["Numeral 38: the endpoint is outside the channel."],
        "labels": [{
            "numeral": "38", "correct": False, "repairable": True,
            "evidence": "Current (400, 400); suggested empty-space point (450, 400).",
            "suggested_x": 450, "suggested_y": 400,
        }],
        "coordinate_space": "raw_pixels",
        "coordinate_width": 1000, "coordinate_height": 1000,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_PROMPT_VERSION,
        "review_count": draft_figures.CROSS_PROVIDER_REVIEW_COUNT,
    })
    certified = {
        "ok": True, "inspected": True,
        "model_name": "deterministic-compositor",
        "prompt_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "certificate_version": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        "review_count": 0,
        "coordinate_certificates": [{
            "numeral": "38", "x": 400, "y": 400,
            "certificate_source": draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION,
        }],
    }

    result = draft_figures._apply_cross_provider_endpoint_gate(
        certified, raw, raw_png=raw, anchors=anchors,
        label="FIG. 3", caption="radial-guide section", numerals=["38 = radial guide"])

    assert result["ok"] is True
    resolution = result["cross_provider_audit"]["deterministic_resolution"]
    assert resolution["coordinates"][0]["basis"] == "certified_line_target"


def test_cross_provider_veto_coordinates_are_repaired_and_recertified(monkeypatch):
    raw = blank_png(1000, 1000)
    anchors = [{"numeral": "10", "x": 400, "y": 565, "visible": True,
                "evidence": "upper-block top face"}]
    accepted_pixel = {
        "ok": True, "inspected": True, "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(draft_figures, "_marked_progress_get", lambda *a, **k: {
        "anchors": anchors,
        "certificates": {"10": {
            "x": 400, "y": 565, "attempt": 1,
            "label": {
                "numeral": "10", "correct": True,
                "evidence": "three same-provider reviewers approved this coordinate",
                "correct_votes": 3, "incorrect_votes": 0,
            },
        }},
        "attempts": 1, "coordinate_history": {"10": [(400, 565)]},
    })
    monkeypatch.setattr(draft_figures, "_marked_progress_put", lambda *a, **k: None)
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, values, **_kwargs: ([dict(item) for item in values],
                                                    dict(accepted_pixel)))
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99,
    })
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [], "missing": [],
        "labels": [{"numeral": "10", "correct": True, "evidence": "route is clear"}],
    })
    marked_calls = []

    def marked(_png, **kwargs):
        marked_calls.append([dict(item) for item in kwargs["anchors"]])
        return {
            "ok": True, "inspected": True, "errors": [], "incorrect": [],
            "labels": [{
                "numeral": "10", "correct": True, "repairable": True,
                "evidence": "the moved dot is on the top face",
                "suggested_x": 700, "suggested_y": 400,
            }],
        }

    monkeypatch.setattr(draft_figures, "inspect_marked_anchors", marked)
    desired_x, desired_y = 700, 400
    cross_calls = []

    def cross_provider(*_args, **_kwargs):
        cross_calls.append(True)
        if len(cross_calls) == 1:
            return {
                "ok": False, "inspected": True, "incorrect": ["10"],
                "missing": [], "unexpected": [], "duplicates": [],
                "errors": ["Numeral 10 is on the front face, not the required top face."],
                "labels": [{
                    "numeral": "10", "correct": False, "repairable": True,
                    "evidence": "the top face center is visible",
                    "suggested_x": desired_x, "suggested_y": desired_y,
                }],
            }
        return accepted_cross_provider_audit(
            labels=[{"numeral": "10", "correct": True,
                     "evidence": "the moved dot is on the top face"}],
            incorrect=[], errors=[])

    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_endpoints", cross_provider)

    _png, labels, leaders, final_anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 1", caption="10 identified on the upper-block top face.",
        numerals=["10 = vibration device"],
        semantic={"anchors": anchors, "pixel_anchor_audit": dict(accepted_pixel)})

    marked_audit = leaders["marked_anchor_audit"]
    assert labels["ok"] is True and leaders["ok"] is True and pixel["ok"] is True
    assert len(cross_calls) == 2 and len(marked_calls) == 1
    assert abs(final_anchors[0]["x"] - desired_x) <= 2
    assert abs(final_anchors[0]["y"] - desired_y) <= 2
    assert marked_audit["cross_provider_audit"]["ok"] is True


def test_cross_provider_endpoint_review_uses_native_raw_pixel_coordinates(
        monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_MODEL", "claude-opus-5")
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)
    saved = []
    monkeypatch.setattr(
        draft_figures, "_analysis_cache_put",
        lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **_kwargs: None)
    monkeypatch.setattr(draft_figures.llm, "_record_usage", lambda *_args: None)
    calls = []

    def anthropic(payload, *, api_key):
        calls.append((payload, api_key))
        return {
            "usage": {"input_tokens": 120, "output_tokens": 40},
            "content": [{"type": "text", "text": """
                ```json
                {"matches_spec": false, "summary": "wrong face", "errors": [],
                 "labels": [{"numeral": "10", "correct": false,
                              "evidence": "dot is in the front polygon",
                              "repairable": true,
                              "suggested_x": 640, "suggested_y": 225}]}
                ```
            """}],
        }

    monkeypatch.setattr(draft_figures, "_anthropic_endpoint_message", anthropic)

    raw = blank_png(1400, 900)
    audit = draft_figures.inspect_cross_provider_endpoints(
        raw, raw_png=raw,
        anchors=[{"numeral": "10", "x": 400, "y": 565, "visible": True}],
        label="FIG. 1",
        caption="The device 10 is identified on its top face.",
        numerals=["10 = device"])

    assert audit["ok"] is False and audit["inspected"] is True
    assert audit["incorrect"] == ["10"] and audit["review_count"] == 1
    assert audit["labels"][0]["repairable"] is True
    assert audit["labels"][0]["suggested_x"] == 640
    assert audit["labels"][0]["suggested_y"] == 225
    assert audit["coordinate_space"] == "raw_pixels"
    assert audit["coordinate_width"] == 1400
    assert audit["coordinate_height"] == 900
    assert audit["model_name"] == "claude-opus-5"
    assert len(calls) == 1 and calls[0][1] == "test-anthropic-key"
    assert calls[0][0]["thinking"] == {"type": "disabled"}
    assert calls[0][0]["messages"][0]["content"][0]["type"] == "image"
    assert [item["type"] for item in calls[0][0]["messages"][0]["content"]] == [
        "image", "image", "image", "text"]
    prompt = calls[0][0]["messages"][0]["content"][3]["text"]
    assert "suggested_x" in prompt and "raw geometry sheet" in prompt
    assert "CURRENT PIXEL" in prompt and "1399,899" in prompt
    assert saved and saved[0][1]["provider"] == "anthropic"


def test_cross_provider_endpoint_audit_uses_complete_label_evidence_over_boolean():
    audit = draft_figures.cross_provider_endpoint_audit([
        "16 = inner field",
        "24 = perimeter band",
    ], {
        "matches_spec": False,
        "summary": "Both terminal dots are correctly placed.",
        "errors": [],
        "labels": [{
            "numeral": "16",
            "correct": True,
            "evidence": "The terminal dot is well inside the inner field.",
        }, {
            "numeral": "24",
            "correct": True,
            "evidence": "The terminal dot lies midway inside the perimeter band.",
        }],
    })

    assert audit["ok"] is True
    assert audit["incorrect"] == []
    assert audit["errors"] == []


def test_required_cross_provider_review_fails_closed_without_a_credential(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_REQUIRED", "1")
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)
    monkeypatch.setattr(
        draft_figures, "_vertex_cross_provider_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("Vertex unavailable")))

    audit = draft_figures.inspect_cross_provider_endpoints(
        blank_png(), label="FIG. 1", caption="device", numerals=["10 = device"])

    assert audit["ok"] is False and audit["inspected"] is False
    assert audit["missing"] == ["10"]
    assert "vertex unavailable" in audit["errors"][0].lower()


def test_required_endpoint_review_uses_vertex_when_anthropic_is_not_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_REQUIRED", "1")
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_MODEL", "claude-opus-5")
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_FALLBACK_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)
    saved = []
    monkeypatch.setattr(
        draft_figures, "_analysis_cache_put",
        lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **_kwargs: None)
    monkeypatch.setattr(draft_figures.llm, "_record_usage", lambda *_args: None)
    calls = []

    def vertex(images, **kwargs):
        calls.append((images, kwargs))
        return {
            "usage": {"input_tokens": 75, "output_tokens": 25},
            "content": [{"type": "text", "text": json.dumps({
                "matches_spec": True,
                "summary": "The endpoint is on the device.",
                "errors": [],
                "labels": [{
                    "numeral": "10", "correct": True,
                    "evidence": "The terminal dot is within the device body.",
                    "repairable": False, "suggested_x": 0, "suggested_y": 0,
                }],
            })}],
        }

    monkeypatch.setattr(draft_figures, "_vertex_cross_provider_message", vertex)
    raw = blank_png(1400, 900)
    audit = draft_figures.inspect_cross_provider_endpoints(
        raw, raw_png=raw,
        anchors=[{"numeral": "10", "x": 400, "y": 565, "visible": True}],
        label="FIG. 1", caption="The device 10.", numerals=["10 = device"])

    assert audit["ok"] is True and audit["inspected"] is True
    assert audit["provider"] == "vertex"
    assert audit["model_name"] == "gemini-2.5-flash"
    assert audit["configured_model"] == "claude-opus-5"
    assert audit["fallback_from"] == "claude-opus-5"
    assert audit["fallback_reason"] == "anthropic_not_configured"
    assert draft_figures.current_cross_provider_endpoint_audit(
        audit, specification_hash=audit["specification_hash"])
    assert len(calls) == 1 and len(calls[0][0]) == 3
    assert calls[0][1]["response_schema"] == draft_figures.CROSS_PROVIDER_ENDPOINT_SCHEMA
    assert saved and saved[0][1]["provider"] == "vertex"


def test_cross_provider_geometry_audit_rejects_an_unrequested_power_cable():
    audit = draft_figures.cross_provider_geometry_audit(["10 = housing"], {
        "matches_spec": True,
        "summary": "The required housing is present.",
        "errors": [],
        "missing_geometry": [],
        "unexpected_geometry": [
            "A double-line power cable leaves the right side of the housing.",
        ],
        "parts": [{
            "numeral": "10", "visible": True,
            "evidence": "The rectangular housing is centered in the sheet.",
        }],
        "visible_elements": [{
            "description": "rectangular housing", "required": True,
            "matched_requirement": "10 = housing",
            "evidence": "The main closed rectangular body.",
        }, {
            "description": "double-line power cable", "required": False,
            "matched_requirement": "",
            "evidence": "Two wavy parallel lines leave the right wall.",
        }],
    })

    assert audit["ok"] is False and audit["inspected"] is True
    assert audit["missing"] == [] and audit["unexpected"]
    assert "power cable" in " ".join(audit["unexpected"]).lower()


def test_cross_provider_geometry_audit_rejects_two_strokes_for_single_curve():
    audit = draft_figures.cross_provider_geometry_audit(["46 = pulling element"], {
        "matches_spec": True,
        "summary": "The pulling element is present.",
        "errors": [],
        "missing_geometry": [],
        "unexpected_geometry": [],
        "parts": [{
            "numeral": "46", "visible": True,
            "evidence": "A flexible path extends left from the machine.",
        }],
        "visible_elements": [{
            "description": "Single slack flexible line",
            "required": True,
            "matched_requirement": (
                "Flexible pulling element 46: a single unbroken curved path of even thickness"),
            "evidence": "Two closely spaced parallel curves form a double-line path.",
        }],
    })

    assert audit["ok"] is False
    assert "multiple strokes" in " ".join(audit["unexpected"]).lower()


def test_cross_provider_geometry_review_uses_anthropic_pixels_and_caches_clean_result(
        monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_MODEL", "claude-opus-5")
    cache = {}
    monkeypatch.setattr(
        draft_figures, "_analysis_cache_get", lambda key: cache.get(key))

    def save(key, **kwargs):
        cache[key] = dict(kwargs["result"])

    monkeypatch.setattr(draft_figures, "_analysis_cache_put", save)
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **_kwargs: None)
    monkeypatch.setattr(draft_figures.llm, "_record_usage", lambda *_args: None)
    calls = []

    def anthropic(payload, *, api_key):
        calls.append((payload, api_key))
        return {
            "usage": {"input_tokens": 90, "output_tokens": 35},
            "content": [{"type": "text", "text": json.dumps({
                "matches_spec": True,
                "summary": "Only the requested housing is visible.",
                "errors": [], "missing_geometry": [], "unexpected_geometry": [],
                "parts": [{
                    "numeral": "10", "visible": True,
                    "evidence": "One rectangular housing is visible.",
                }],
                "visible_elements": [{
                    "description": "rectangular housing", "required": True,
                    "matched_requirement": "10 = housing",
                    "evidence": "One closed rectangular body.",
                }],
            })}],
        }

    monkeypatch.setattr(draft_figures, "_anthropic_endpoint_message", anthropic)
    png = blank_png()
    first = draft_figures.inspect_cross_provider_geometry(
        png, label="FIG. 1", caption="A housing.", numerals=["10 = housing"])
    second = draft_figures.inspect_cross_provider_geometry(
        png, label="FIG. 1", caption="A housing.", numerals=["10 = housing"])

    assert first["ok"] is True and second == first and len(calls) == 1
    assert first["specification_hash"] == draft_figures.specification_hash(
        "FIG. 1", "A housing.", ["10 = housing"])
    assert calls[0][1] == "test-anthropic-key"
    content = calls[0][0]["messages"][0]["content"]
    assert [item["type"] for item in content] == ["image", "text"]
    prompt = content[1]["text"].lower()
    assert "every visible" in prompt and "wire" in prompt and "specification" in prompt
    assert "black stroke centerlines" in prompt
    assert "supporting surface" in prompt and "occlusion" in prompt
    assert "finite-width ring" in prompt and "outer and inner" in prompt
    assert "parts list is an indexing aid, not an exhaustive geometry specification" in prompt
    assert "one representative instance" in prompt
    assert "do not infer the permitted instance count from the number of numerals" in prompt


@pytest.mark.parametrize("first_text", [
    "{\"matches_spec\":",
    json.dumps({"matches_spec": True, "parts": [], "visible_elements": []}),
])
def test_cross_provider_geometry_retries_an_incomplete_structured_response(
        monkeypatch, first_text):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_MODEL", "claude-opus-5")
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)
    monkeypatch.setattr(draft_figures, "_analysis_cache_put", lambda *_args, **_kwargs: None)
    usage = []
    audits = []
    calls = []
    monkeypatch.setattr(draft_figures.llm, "_record_usage", lambda *values: usage.append(values))
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **values: audits.append(values))

    complete = {
        "matches_spec": True,
        "summary": "Only the requested housing is visible.",
        "errors": [], "missing_geometry": [], "unexpected_geometry": [],
        "parts": [{
            "numeral": "10", "visible": True,
            "evidence": "One rectangular housing is visible.",
        }],
        "visible_elements": [{
            "description": "rectangular housing", "required": True,
            "matched_requirement": "10 = housing",
            "evidence": "One closed rectangular body.",
        }],
    }

    def anthropic(payload, *, api_key):
        calls.append((payload, api_key))
        if len(calls) == 1:
            return {
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 90, "output_tokens": 5000},
                "content": [{"type": "text", "text": first_text}],
            }
        return {
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 90, "output_tokens": 35},
            "content": [{"type": "text", "text": json.dumps(complete)}],
        }

    monkeypatch.setattr(draft_figures, "_anthropic_endpoint_message", anthropic)

    result = draft_figures.inspect_cross_provider_geometry(
        blank_png(), label="FIG. 1", caption="A housing.", numerals=["10 = housing"])

    assert result["ok"] is True
    assert len(calls) == 2
    assert calls[1][0]["max_tokens"] > calls[0][0]["max_tokens"]
    retry_prompt = calls[1][0]["messages"][0]["content"][1]["text"].lower()
    assert "concise" in retry_prompt and "complete json" in retry_prompt
    assert usage == [(90, 5000), (90, 35)]
    assert [audit["success"] for audit in audits] == [False, True]
    assert audits[0]["fallback_reason"] == "structured_output_retry"


def test_cross_provider_geometry_fails_closed_after_two_truncated_json_responses(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)
    audits = []
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **values: audits.append(values))
    monkeypatch.setattr(draft_figures.llm, "_record_usage", lambda *_args: None)
    calls = []

    def anthropic(payload, *, api_key):
        calls.append((payload, api_key))
        return {
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 90, "output_tokens": payload["max_tokens"]},
            "content": [{"type": "text", "text": "{\"matches_spec\":"}],
        }

    monkeypatch.setattr(draft_figures, "_anthropic_endpoint_message", anthropic)

    result = draft_figures.inspect_cross_provider_geometry(
        blank_png(), label="FIG. 1", caption="A housing.", numerals=["10 = housing"])

    assert result["ok"] is False and result["inspected"] is False
    assert len(calls) == 2
    assert "max_tokens" in " ".join(result["errors"])
    assert [audit["fallback_reason"] for audit in audits] == [
        "structured_output_retry", "transport_or_parse_error"]
    assert audits[1]["output_tokens"] == 9000


def test_required_cross_provider_geometry_review_fails_closed_without_a_credential(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_REQUIRED", "1")
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)
    monkeypatch.setattr(
        draft_figures, "_vertex_cross_provider_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("Vertex unavailable")))

    audit = draft_figures.inspect_cross_provider_geometry(
        blank_png(), label="FIG. 1", caption="housing", numerals=["10 = housing"])

    assert audit["ok"] is False and audit["inspected"] is False
    assert audit["missing"] == ["10"]
    assert "vertex unavailable" in audit["errors"][0].lower()


def test_geometry_review_uses_vertex_after_anthropic_usage_limit(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_REQUIRED", "1")
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_MODEL", "claude-opus-5")
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_FALLBACK_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)
    saved = []
    audits = []
    monkeypatch.setattr(
        draft_figures, "_analysis_cache_put",
        lambda *args, **kwargs: saved.append((args, kwargs)))
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **values: audits.append(values))
    monkeypatch.setattr(draft_figures.llm, "_record_usage", lambda *_args: None)
    monkeypatch.setattr(
        draft_figures, "_anthropic_endpoint_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(
            "You have reached your specified API usage limits.")))
    calls = []

    def vertex(images, **kwargs):
        calls.append((images, kwargs))
        return {
            "usage": {"input_tokens": 90, "output_tokens": 35},
            "content": [{"type": "text", "text": json.dumps({
                "matches_spec": True,
                "summary": "Only the requested housing is visible.",
                "errors": [], "missing_geometry": [], "unexpected_geometry": [],
                "parts": [{
                    "numeral": "10", "visible": True,
                    "evidence": "One rectangular housing is visible.",
                }],
                "visible_elements": [{
                    "description": "rectangular housing", "required": True,
                    "matched_requirement": "10 = housing",
                    "evidence": "One closed rectangular body.",
                }],
            })}],
        }

    monkeypatch.setattr(draft_figures, "_vertex_cross_provider_message", vertex)
    audit = draft_figures.inspect_cross_provider_geometry(
        blank_png(), label="FIG. 1", caption="A housing.", numerals=["10 = housing"])

    assert audit["ok"] is True and audit["inspected"] is True
    assert audit["provider"] == "vertex"
    assert audit["model_name"] == "gemini-2.5-flash"
    assert audit["configured_model"] == "claude-opus-5"
    assert audit["fallback_from"] == "claude-opus-5"
    assert audit["fallback_reason"] == "anthropic_quota_exhausted"
    assert draft_figures.current_cross_provider_geometry_audit(
        audit, specification_hash=audit["specification_hash"])
    assert len(calls) == 1 and len(calls[0][0]) == 1
    assert calls[0][1]["response_schema"] == draft_figures.CROSS_PROVIDER_GEOMETRY_SCHEMA
    assert saved and saved[0][1]["provider"] == "vertex"
    assert audits[-1]["provider"] == "vertex"


def test_cross_provider_geometry_veto_is_applied_to_same_provider_consensus(monkeypatch):
    monkeypatch.setattr(draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: {
        "ok": False, "inspected": True, "missing": [],
        "unexpected": ["double-line power cable"],
        "errors": ["Unexpected geometry: double-line power cable"],
    })
    same_provider = {
        "ok": True, "inspected": True, "errors": [], "unexpected": [],
        "anchors": [{
            "numeral": "10", "x": 500, "y": 500, "visible": True,
            "evidence": "housing",
        }],
    }

    audited = draft_figures._apply_cross_provider_geometry_gate(
        same_provider, blank_png(), label="FIG. 1", caption="housing",
        numerals=["10 = housing"])

    assert audited["ok"] is False
    assert audited["anchors"] == same_provider["anchors"]
    assert audited["cross_provider_geometry_audit"]["unexpected"] == [
        "double-line power cable"]
    assert "power cable" in " ".join(audited["errors"]).lower()


def test_exact_deterministic_geometry_can_resolve_extra_geometry_dissent(monkeypatch):
    caption = """
    The covering element 36 is one large plain tile, a flat rectangular panel seen in
    perspective. The machine stands on its left-hand part, leaving a wide open expanse of tile
    to the right. The machine is a plain rectangular slab standing on a band that runs round its
    underside. Two plain closed housings stand on the top face, one left and one right, and a
    grip stands above the slab between them. The handle 44 is drawn as a closed ring shape
    enclosing an open area, the bar forming that ring having its own width.
    """
    numerals = [
        "10 = vibration device", "12 = base", "18 = vibration motor",
        "20 = air-extraction mechanism", "24 = perimeter member",
        "36 = covering element", "44 = handle",
    ]
    png = draft_figures._deterministic_geometry_png(caption)
    assert png is not None
    spec_hash = draft_figures.specification_hash("FIG. 1", caption, numerals)
    dissent = {
        "ok": False, "inspected": True, "matches_spec": False,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_GEOMETRY_PROMPT_VERSION,
        "review_count": draft_figures.CROSS_PROVIDER_GEOMETRY_REVIEW_COUNT,
        "specification_hash": spec_hash,
        "missing": [], "missing_geometry": [],
        "unexpected": ["nonexistent third ring contour"],
        "errors": ["The ring appears to have three contours."],
        "summary": "Every required body is visible, but one extra contour appears.",
    }
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: dissent)
    semantic = {
        "ok": True, "inspected": True, "errors": [], "missing": [], "unexpected": [],
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
    }

    audited = draft_figures._apply_cross_provider_geometry_gate(
        semantic, png, label="FIG. 1", caption=caption, numerals=numerals)

    cross = audited["cross_provider_geometry_audit"]
    assert audited["ok"] is True and cross["ok"] is True
    assert cross["reviewer_ok"] is False
    assert cross["reviewer_errors"] == dissent["errors"]
    assert cross["consensus_resolution"]["version"] == (
        draft_figures.DETERMINISTIC_GEOMETRY_CERTIFICATE_VERSION)
    assert cross["consensus_resolution"]["exact_renderer_match"] is True
    assert draft_figures.current_cross_provider_geometry_audit(
        cross, specification_hash=spec_hash) is True


def test_exact_deterministic_geometry_resolves_same_provider_hatching_dissent(monkeypatch):
    caption = (
        "The sheet shows four bodies, one broken line, and nothing else: one horizontal "
        "hatched slab; one closed loop cut twice, appearing as two short hatched legs; one "
        "hatched band across the bottom; and one closed housing. One broken line runs from "
        "inside the housing to the chamber."
    )
    numerals = [
        "12 = base", "14 = first side", "20 = air-extraction mechanism",
        "22 = chamber", "24 = perimeter member", "36 = covering element",
    ]
    png = draft_figures._deterministic_geometry_png(caption)
    assert png is not None
    spec_hash = draft_figures.specification_hash("FIG. 2", caption, numerals)
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k:
        accepted_cross_provider_geometry_audit(specification_hash=spec_hash))
    semantic = {
        "ok": False, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "expected": ["12", "14", "20", "22", "24", "36"],
        "visible": ["12", "14", "20", "22", "24", "36"],
        "missing": [], "unexpected": [], "duplicates": [], "unexpected_text": [],
        "errors": ["All section hatching appears to use the same angle."],
        "anchors": [
            {"numeral": value, "x": 500, "y": 500, "visible": True,
             "evidence": "The requested component is visible."}
            for value in ("12", "14", "20", "22", "24", "36")
        ],
        "specification_hash": spec_hash,
    }

    audited = draft_figures._resolve_deterministic_semantic_dissent(
        semantic, png, label="FIG. 2", caption=caption, numerals=numerals)

    assert audited["ok"] is True and audited["errors"] == []
    assert audited["reviewer_ok"] is False
    assert audited["reviewer_errors"] == semantic["errors"]
    assert audited["semantic_consensus_resolution"]["exact_renderer_match"] is True
    assert draft_figures._current_deterministic_semantic_resolution(audited) is True
    tampered = json.loads(json.dumps(audited))
    tampered["anchors"][0]["evidence"] = ""
    assert draft_figures._current_deterministic_semantic_resolution(tampered) is False


def test_exact_section_pixels_resolve_reviewers_dissent_on_certified_constraints(monkeypatch):
    caption = """
    The sheet shows four schematic bodies and one broken line: one hatched horizontal slab, the
    base 12; one closed loop cut twice, appearing as two short hatched legs hanging from the
    underside of the slab, one at each end and flush with it; one hatched band across the bottom,
    the covering element 36, on which the legs stand; and one closed housing standing on the slab,
    the air-extraction mechanism 20. In the slab each stroke starts low on the left and ends high
    on the right, like a forward slash. In both legs each stroke starts high on the left and ends
    low on the right, like a backslash. In the band each stroke is steep, close to upright and
    leaning slightly to the right. One broken line runs from inside the housing to the chamber 22.
    That line stops at the upper face of the base 12 and resumes below its lower face, no passage
    through the base being drawn.
    """
    numerals = [
        "12 = base", "14 = first side", "20 = air-extraction mechanism",
        "22 = chamber", "24 = perimeter member", "36 = covering element",
    ]
    png = draft_figures._deterministic_geometry_png(caption)
    assert png is not None
    spec_hash = draft_figures.specification_hash("FIG. 2", caption, numerals)
    dissent = {
        "ok": False, "inspected": True, "matches_spec": False,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_GEOMETRY_PROMPT_VERSION,
        "review_count": draft_figures.CROSS_PROVIDER_GEOMETRY_REVIEW_COUNT,
        "specification_hash": spec_hash,
        "missing": [], "unexpected": [], "duplicates": [],
        "errors": [
            "Covering element hatching leans left rather than right.",
            "The perimeter legs are not flush with the ends of the slab.",
        ],
        "missing_geometry": ["Legs flush with the ends of the base slab."],
        "summary": "The required parts are visible, but certified relationships appear wrong.",
    }
    certificate = draft_figures._deterministic_geometry_certificate(png, caption)
    assert certificate["certified_constraints"]["section_hatching"]["ok"] is True
    assert certificate["certified_constraints"]["flush_legs"]["ok"] is True
    assert draft_figures._certified_geometry_dissent_categories(
        errors=dissent["errors"], missing_geometry=dissent["missing_geometry"],
        missing=[], unexpected=dissent["unexpected"], duplicates=[], certificate=certificate,
    ) == ["flush_legs", "section_hatching"]
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: dissent)
    semantic = {
        "ok": False, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "expected": ["12", "14", "20", "22", "24", "36"],
        "visible": ["12", "14", "20", "22", "24", "36"],
        "missing": [], "unexpected": [], "duplicates": [], "unexpected_text": [],
        "errors": ["The perimeter and covering-element hatch angles appear wrong."],
        "anchors": [
            {"numeral": value, "x": 500, "y": 500, "visible": True,
             "evidence": "The requested component is visible."}
            for value in ("12", "14", "20", "22", "24", "36")
        ],
        "specification_hash": spec_hash,
    }

    audited = draft_figures._resolve_deterministic_semantic_dissent(
        semantic, png, label="FIG. 2", caption=caption, numerals=numerals)

    assert audited["ok"] is True
    cross = audited["cross_provider_geometry_audit"]
    assert cross["ok"] is True and cross["reviewer_ok"] is False
    assert set(cross["consensus_resolution"]["certified_dissent_categories"]) == {
        "flush_legs", "section_hatching",
    }
    assert draft_figures.current_cross_provider_geometry_audit(
        cross, specification_hash=spec_hash) is True
    assert draft_figures._current_deterministic_semantic_resolution(audited) is True
    tampered = json.loads(json.dumps(cross))
    tampered["reviewer_missing_geometry"] = ["The air-extraction housing is absent."]
    assert draft_figures.current_cross_provider_geometry_audit(
        tampered, specification_hash=spec_hash) is False
    tampered = json.loads(json.dumps(cross))
    tampered["consensus_resolution"]["certified_dissent_categories"] = [
        "section_hatching"]
    assert draft_figures.current_cross_provider_geometry_audit(
        tampered, specification_hash=spec_hash) is False


def test_exact_flat_charging_pixels_resolve_reviewers_connectivity_dissent(monkeypatch):
    caption = """
    A flat schematic system diagram. A dashed rectangle, the charging installation, encloses the
    whole diagram. A horizontal branch conductor passes through a branch current sensor and
    supplies a first connector channel, a second connector channel, and a non-charging load in
    parallel. Its left end stops short of the dashed rectangle and its right end meets the right
    side without crossing it. An edge controller is joined to the branch current sensor by a line
    that runs down, then right, then down to its top side. An isolated local bus runs from a point
    vertically below the edge controller to a point vertically below the second connector channel.
    A vertical line connects the controller bottom to the bus left end, and vertical lines connect
    the first and second connector channels to the bus.
    """
    numerals = [
        "100 = charging installation", "102 = branch conductor",
        "104 = branch current sensor", "106 = edge controller",
        "108 = isolated local bus", "118 = non-charging load",
        "120 = first connector channel", "140 = second connector channel",
    ]
    png = draft_figures._deterministic_geometry_png(caption)
    assert png is not None
    spec_hash = draft_figures.specification_hash("FIG. 1", caption, numerals)
    dissent = accepted_cross_provider_geometry_audit(
        ok=False,
        specification_hash=spec_hash,
        errors=[
            "The branch conductor stops short of the right dashed enclosure boundary.",
        ],
        missing_geometry=[
            "A vertical line connecting the bottom of the second connector channel to the "
            "isolated local bus.",
        ],
        unexpected=[
            "A horizontal line segment connecting the bottom of the edge controller to the "
            "vertical line dropping from the first connector channel.",
            "A horizontal line segment connecting the vertical line dropping from the first "
            "connector channel to the vertical line dropping from the second connector channel.",
        ],
        summary="The controller wiring appears inconsistent with the brief.",
    )
    certificate = draft_figures._deterministic_geometry_certificate(png, caption)
    assert certificate["certified_constraints"][
        "charging_branch_conductor_endpoint"]["ok"] is True
    assert certificate["certified_constraints"][
        "charging_local_bus_connectivity"]["ok"] is True
    assert certificate["certified_constraints"][
        "charging_sensor_controller_path"]["ok"] is True
    assert draft_figures._certified_geometry_dissent_categories(
        errors=dissent["errors"], missing_geometry=dissent["missing_geometry"],
        missing=[], unexpected=dissent["unexpected"], duplicates=[], certificate=certificate,
    ) == ["charging_branch_conductor_endpoint", "charging_local_bus_connectivity"]
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: dissent)
    semantic = {
        "ok": False, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "expected": ["100", "102", "104", "106", "108", "118", "120", "140"],
        "visible": ["100", "102", "104", "106", "108", "118", "120", "140"],
        "missing": [], "unexpected": [], "duplicates": [], "unexpected_text": [],
        "errors": [
            "The line connecting the branch current sensor to the edge controller turns left "
            "instead of right.",
            "The isolated local bus extends past the point below the second connector channel.",
        ],
        "anchors": [
            {"numeral": value, "x": 500, "y": 500, "visible": True,
             "evidence": "The requested component is visible."}
            for value in ("100", "102", "104", "106", "108", "118", "120", "140")
        ],
        "specification_hash": spec_hash,
    }

    audited = draft_figures._resolve_deterministic_semantic_dissent(
        semantic, png, label="FIG. 1", caption=caption, numerals=numerals)

    assert audited["ok"] is True
    assert audited["reviewer_ok"] is False
    cross = audited["cross_provider_geometry_audit"]
    assert cross["ok"] is True and cross["reviewer_ok"] is False
    assert set(cross["consensus_resolution"]["certified_dissent_categories"]) == {
        "charging_branch_conductor_endpoint", "charging_local_bus_connectivity",
    }
    assert draft_figures._current_deterministic_semantic_resolution(audited) is True


def test_exact_section_pixels_resolve_false_body_separation_and_loop_dissent(monkeypatch):
    caption = """
    The sheet shows four schematic bodies: one hatched horizontal slab, the base 12; one closed
    loop cut twice, appearing as two hatched legs hanging from the underside of the slab, one at
    each end; one hatched band across the bottom, the covering element 36, on which the legs
    stand; and one housing standing on the slab, the air-extraction mechanism 20. The slab, the
    legs and the band are the cut bodies, each filled with regularly spaced parallel hatching:
    the slab falling to the right, both legs rising to the right, and the band falling to the
    right more steeply than the slab. Where two meet, a plain solid line runs along the join, so
    each reads as a separate hatched body. The housing lies outside the cut, in plain unhatched
    outline. A plain unhatched gap runs through the slab beneath the housing, from the inside of
    the housing to the chamber 22.
    """
    numerals = [
        "12 = base", "14 = first side", "20 = air-extraction mechanism",
        "22 = chamber", "24 = perimeter member", "36 = covering element",
    ]
    png = draft_figures._deterministic_geometry_png(caption)
    assert png is not None
    spec_hash = draft_figures.specification_hash("FIG. 2", caption, numerals)
    dissent = {
        "ok": False, "inspected": True, "matches_spec": False,
        "model_name": draft_figures.cross_provider_model(),
        "prompt_version": draft_figures.CROSS_PROVIDER_GEOMETRY_PROMPT_VERSION,
        "review_count": draft_figures.CROSS_PROVIDER_GEOMETRY_REVIEW_COUNT,
        "specification_hash": spec_hash,
        "missing": [], "unexpected": [], "duplicates": [], "missing_geometry": [],
        "errors": [
            "The slab, legs, and band read as one continuous monolithic hatched body instead "
            "of separate bodies with a plain solid line at each join.",
            "The two distinct leg sections do not visually represent the single closed loop "
            "cut twice required by the specification.",
        ],
        "summary": "The required components are visible, but their section convention appears wrong.",
    }
    certificate = draft_figures._deterministic_geometry_certificate(png, caption)
    assert certificate["certified_constraints"]["section_body_separation"]["ok"] is True
    assert certificate["certified_constraints"]["perimeter_loop_section"]["ok"] is True
    assert draft_figures._certified_geometry_dissent_categories(
        errors=dissent["errors"], missing_geometry=[], missing=[], unexpected=[],
        duplicates=[], certificate=certificate,
    ) == ["perimeter_loop_section", "section_body_separation"]
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: dissent)
    semantic = {
        "ok": False, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "expected": ["12", "14", "20", "22", "24", "36"],
        "visible": ["12", "14", "20", "22", "24", "36"],
        "missing": [], "unexpected": [], "duplicates": [], "unexpected_text": [],
        "errors": dissent["errors"],
        "anchors": [
            {"numeral": value, "x": 500, "y": 500, "visible": True,
             "evidence": "The requested component is visible."}
            for value in ("12", "14", "20", "22", "24", "36")
        ],
        "specification_hash": spec_hash,
    }

    audited = draft_figures._resolve_deterministic_semantic_dissent(
        semantic, png, label="FIG. 2", caption=caption, numerals=numerals)

    assert audited["ok"] is True
    cross = audited["cross_provider_geometry_audit"]
    assert cross["ok"] is True and cross["reviewer_ok"] is False
    assert set(cross["consensus_resolution"]["certified_dissent_categories"]) == {
        "perimeter_loop_section", "section_body_separation",
    }


def test_deterministic_semantic_dissent_fails_closed_without_complete_anchor_inventory(
        monkeypatch):
    caption = (
        "The sheet shows four bodies, one broken line, and nothing else: one horizontal "
        "hatched slab; one closed loop cut twice, appearing as two short hatched legs; one "
        "hatched band across the bottom; and one closed housing. One broken line runs from "
        "inside the housing to the chamber."
    )
    numerals = ["12 = base", "20 = air-extraction mechanism"]
    png = draft_figures._deterministic_geometry_png(caption)
    assert png is not None
    spec_hash = draft_figures.specification_hash("FIG. 2", caption, numerals)
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k:
        accepted_cross_provider_geometry_audit(specification_hash=spec_hash))
    semantic = {
        "ok": False, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "expected": ["12", "20"], "visible": ["12", "20"],
        "missing": [], "unexpected": [], "duplicates": [], "unexpected_text": [],
        "errors": ["Hatching appears ambiguous."],
        "anchors": [{"numeral": "12", "x": 500, "y": 500, "visible": True,
                     "evidence": "The slab is visible."}],
        "specification_hash": spec_hash,
    }

    audited = draft_figures._resolve_deterministic_semantic_dissent(
        semantic, png, label="FIG. 2", caption=caption, numerals=numerals)

    assert audited["ok"] is False
    assert "semantic_consensus_resolution" not in audited


def test_cached_complete_semantic_dissent_uses_independent_deterministic_resolution(monkeypatch):
    caption = (
        "The sheet shows four bodies, one broken line, and nothing else: one horizontal "
        "hatched slab; one closed loop cut twice, appearing as two short hatched legs; one "
        "hatched band across the bottom; and one closed housing. One broken line runs from "
        "inside the housing to the chamber."
    )
    numerals = ["12 = base", "20 = air-extraction mechanism"]
    png = draft_figures._deterministic_geometry_png(caption)
    assert png is not None
    spec_hash = draft_figures.specification_hash("FIG. 2", caption, numerals)
    cached = {
        "ok": False, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "expected": ["12", "20"], "visible": ["12", "20"],
        "missing": [], "unexpected": [], "duplicates": [], "unexpected_text": [],
        "errors": ["Hatching appears ambiguous."],
        "anchors": [
            {"numeral": value, "x": 500, "y": 500, "visible": True,
             "evidence": "The requested component is visible."}
            for value in ("12", "20")
        ],
        "specification_hash": spec_hash,
    }
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: dict(cached))
    writes = []
    monkeypatch.setattr(
        draft_figures, "_analysis_cache_put", lambda *args, **kwargs: writes.append((args, kwargs)))
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **_kwargs: None)
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k:
        accepted_cross_provider_geometry_audit(specification_hash=spec_hash))

    audited = draft_figures.inspect_semantics(
        png, label="FIG. 2", caption=caption, numerals=numerals)

    assert audited["ok"] is True
    assert audited["semantic_consensus_resolution"]["exact_renderer_match"] is True
    assert writes and writes[-1][1]["stage"] == "semantic"


def test_deterministic_resolution_fails_closed_for_missing_geometry(monkeypatch):
    caption = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    its left-hand part. The machine is a plain rectangular slab standing on a band round its
    underside. Two plain closed housings stand on the top face and a grip stands above them.
    The handle 44 is drawn as a closed ring shape enclosing an open area, the bar forming that
    ring having its own width.
    """
    numerals = ["36 = covering element", "44 = handle"]
    png = draft_figures._deterministic_geometry_png(caption)
    assert png is not None
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: {
            "ok": False, "inspected": True, "missing": ["44"],
            "missing_geometry": ["The handle is absent."], "unexpected": [],
            "errors": ["Required handle not found."],
        })
    semantic = {
        "ok": True, "inspected": True, "errors": [], "missing": [], "unexpected": [],
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
    }

    audited = draft_figures._apply_cross_provider_geometry_gate(
        semantic, png, label="FIG. 1", caption=caption, numerals=numerals)

    assert audited["ok"] is False
    assert "required handle" in " ".join(audited["errors"]).lower()


def test_deterministic_resolution_fails_closed_for_a_pixel_mismatch(monkeypatch):
    caption = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    its left-hand part. The machine is a plain rectangular slab standing on a band round its
    underside. Two plain closed housings stand on the top face and a grip stands above them.
    The handle 44 is drawn as a closed ring shape enclosing an open area, the bar forming that
    ring having its own width.
    """
    numerals = ["36 = covering element", "44 = handle"]
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: {
            "ok": False, "inspected": True, "missing": [], "missing_geometry": [],
            "unexpected": ["unrequested cable"], "errors": ["Extra cable."],
        })
    semantic = {
        "ok": True, "inspected": True, "errors": [], "missing": [], "unexpected": [],
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
    }

    audited = draft_figures._apply_cross_provider_geometry_gate(
        semantic, blank_png(1400, 900), label="FIG. 1", caption=caption, numerals=numerals)

    assert audited["ok"] is False
    assert "extra cable" in " ".join(audited["errors"]).lower()


def test_malformed_deterministic_resolution_is_rejected_without_raising():
    audit = accepted_cross_provider_geometry_audit(
        specification_hash="a" * 64,
        reviewer_ok=False,
        reviewer_missing_geometry=[],
        consensus_resolution={
            "version": draft_figures.DETERMINISTIC_GEOMETRY_CERTIFICATE_VERSION,
            "exact_renderer_match": True,
            "png_sha256": "b" * 64,
            "renderer_png_sha256": "b" * 64,
            "semantic_review_count": "not-an-integer",
            "specification_hash": "a" * 64,
        },
    )

    assert draft_figures.current_cross_provider_geometry_audit(
        audit, specification_hash="a" * 64) is False


def test_cached_same_provider_semantics_still_run_cross_provider_geometry_gate(monkeypatch):
    same_provider = {
        "ok": True, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "errors": [], "unexpected": [],
        "anchors": [{
            "numeral": "10", "x": 500, "y": 500, "visible": True,
            "evidence": "housing",
        }],
    }
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: same_provider)
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **_kwargs: None)
    calls = []
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "ok": False, "inspected": True, "missing": [],
            "unexpected": ["unrequested cable"], "errors": [],
        })

    result = draft_figures.inspect_semantics(
        blank_png(), label="FIG. 1", caption="housing", numerals=["10 = housing"])

    assert len(calls) == 1 and result["ok"] is False
    assert "unrequested cable" in " ".join(result["errors"])


def test_semantic_retry_explicitly_corrects_out_of_range_anchor_coordinates(monkeypatch):
    valid = {
        "matches_spec": True, "summary": "The housing is visible.", "errors": [],
        "unexpected_text": [],
        "anchors": [{
            "numeral": "10", "x": 500, "y": 500, "visible": True,
            "evidence": "The anchor is inside the housing body.",
        }],
    }
    responses = [
        {**valid, "anchors": [{**valid["anchors"][0], "x": 1550}]},
        valid,
        valid,
    ]
    calls = []

    class Response:
        usage_metadata = None

        def __init__(self, parsed):
            self.parsed = parsed

    class Models:
        def generate_content(self, **values):
            calls.append(values)
            return Response(responses.pop(0))

    class Client:
        models = Models()

    monkeypatch.setattr(draft_figures.llm, "_client", lambda: Client())
    monkeypatch.setattr(draft_figures.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)
    monkeypatch.setattr(draft_figures, "_analysis_cache_put", lambda *args, **kwargs: None)
    monkeypatch.setattr(draft_figures, "_audit_log", lambda **_kwargs: None)
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *args, **kwargs:
        accepted_cross_provider_geometry_audit(
            specification_hash=draft_figures.specification_hash(
                "FIG. 1", "A housing body is visible.", ["10 = housing"])))

    result = draft_figures.inspect_semantics(
        blank_png(), label="FIG. 1", caption="A housing body is visible.",
        numerals=["10 = housing"])

    assert result["ok"] is True and len(calls) == 3
    assert "PREVIOUS RESPONSE FAILED VALIDATION" in calls[1]["contents"][-1]
    assert "0 through 1000" in calls[1]["contents"][-1]


def test_compose_rechecks_every_gate_after_repairing_a_marked_endpoint(monkeypatch):
    raw = blank_png(1000, 1000)
    initial = [{"numeral": "26", "x": 500, "y": 500,
                "visible": True, "evidence": "bearing face"}]
    accepted_pixel = {
        "ok": True, "inspected": True,
        "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, anchors, **_kwargs: ([dict(item) for item in anchors],
                                                      dict(accepted_pixel)))
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["26"], "figure_label": "FIG. 2",
        "other_text": [], "confidence": 0.99})
    leader_calls = []
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: (
        leader_calls.append(True) or {
            "ok": True, "inspected": True, "errors": [], "incorrect": [],
            "labels": [{"numeral": "26", "correct": True,
                        "evidence": "leader reaches the marked point"}],
        }))
    marked_calls = []

    def inspect_marked(_png, **kwargs):
        marked_calls.append([dict(item) for item in kwargs["anchors"]])
        if len(marked_calls) < 2:
            return {
                "ok": False, "inspected": True,
                "errors": ["The center is below the bearing-face boundary."],
                "incorrect": ["26"], "missing": [],
                "labels": [{
                    "numeral": "26", "correct": False, "repairable": True,
                    "evidence": "the bearing face is right of center",
                    "suggested_x": 550, "suggested_y": 500,
                }],
            }
        return {
            "ok": True, "inspected": True, "errors": [], "incorrect": [],
            "labels": [{
                "numeral": "26", "correct": True, "repairable": True,
                "evidence": "the center is on the bearing face",
                "suggested_x": 500, "suggested_y": 500,
            }],
        }

    monkeypatch.setattr(draft_figures, "inspect_marked_anchors", inspect_marked)

    _png, labels, leaders, anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 2", caption="bearing face", numerals=["26 = bearing face"],
        semantic={"anchors": initial, "pixel_anchor_audit": dict(accepted_pixel)})

    assert labels["ok"] is True and leaders["ok"] is True and pixel["ok"] is True
    assert len(marked_calls) == 2 and len(leader_calls) == 2
    assert anchors[0]["x"] == 550
    assert leaders["marked_anchor_audit"]["ok"] is True


def test_compose_accumulates_consensus_for_unchanged_endpoint_coordinates(monkeypatch):
    raw = blank_png(1000, 1000)
    initial = [
        {"numeral": "10", "x": 400, "y": 500,
         "visible": True, "evidence": "first body"},
        {"numeral": "12", "x": 600, "y": 500,
         "visible": True, "evidence": "second body"},
    ]
    accepted_pixel = {
        "ok": True, "inspected": True,
        "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, anchors, **_kwargs: ([dict(item) for item in anchors],
                                                      dict(accepted_pixel)))
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10", "12"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99})
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [],
        "labels": [
            {"numeral": "10", "correct": True, "evidence": "leader reaches 10"},
            {"numeral": "12", "correct": True, "evidence": "leader reaches 12"},
        ],
    })
    marked_calls = []
    marked_numerals = []

    def inspect_marked(_png, **kwargs):
        marked_calls.append([dict(item) for item in kwargs["anchors"]])
        marked_numerals.append([
            item["numeral"] for item in draft_figures.numeral_entries(kwargs["numerals"])
        ])
        first_round = len(marked_calls) % 2 == 1
        result = {
            "ok": not first_round, "inspected": True,
            "errors": (["The center for 12 needs correction."] if first_round else []),
            "incorrect": (["12"] if first_round else []),
            "missing": [], "unexpected": [],
            "duplicates": [], "review_count": 3,
            "prompt_version": draft_figures.MARKED_ANCHOR_PROMPT_VERSION,
            "model_name": draft_figures.vision_model(),
            "labels": ([{
                    "numeral": "10", "correct": first_round, "repairable": True,
                    "evidence": "three reviewers inspected endpoint 10",
                    "suggested_x": 500, "suggested_y": 500,
                    "correct_votes": 3, "incorrect_votes": 0,
                }] if first_round else []) + [{
                    "numeral": "12", "correct": not first_round, "repairable": True,
                    "evidence": "three reviewers inspected endpoint 12",
                    "suggested_x": 650 if first_round else 500, "suggested_y": 500,
                    "correct_votes": 1 if first_round else 3,
                    "incorrect_votes": 2 if first_round else 0,
                }],
        }
        return result

    monkeypatch.setattr(draft_figures, "inspect_marked_anchors", inspect_marked)

    _png, labels, leaders, anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 1", caption="two bodies",
        numerals=["10 = first body", "12 = second body"],
        semantic={"anchors": initial, "pixel_anchor_audit": dict(accepted_pixel)})

    assert labels["ok"] is True and leaders["ok"] is True and pixel["ok"] is True
    assert len(marked_calls) == 2
    assert marked_numerals == [["10", "12"], ["12"]]
    assert anchors[0]["x"] == 400 and anchors[1]["x"] > 600
    assert leaders["marked_anchor_audit"]["ok"] is True
    assert leaders["marked_anchor_audit"]["certified_across_attempts"] is True


def test_compose_resumes_repaired_coordinates_after_a_process_restart(monkeypatch):
    raw = blank_png(1000, 1000)
    initial = [{"numeral": "26", "x": 500, "y": 500,
                "visible": True, "evidence": "bearing face"}]
    accepted_pixel = {
        "ok": True, "inspected": True,
        "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, anchors, **_kwargs: ([dict(item) for item in anchors],
                                                      dict(accepted_pixel)))
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["26"], "figure_label": "FIG. 2",
        "other_text": [], "confidence": 0.99})

    progress = {}

    def load_progress(*_args, **_kwargs):
        return dict(progress) if progress else None

    def save_progress(*_args, **values):
        progress.update({
            "anchors": [dict(item) for item in values["anchors"]],
            "certificates": dict(values["certificates"]),
            "attempts": values["attempts"],
        })

    monkeypatch.setattr(draft_figures, "_marked_progress_get", load_progress, raising=False)
    monkeypatch.setattr(draft_figures, "_marked_progress_put", save_progress, raising=False)
    leader_calls = []
    interrupt = [True]

    def inspect_leaders(*_args, **_kwargs):
        leader_calls.append(True)
        if interrupt[0] and len(leader_calls) == 2:
            raise KeyboardInterrupt()
        return {
            "ok": True, "inspected": True, "errors": [], "incorrect": [],
            "labels": [{"numeral": "26", "correct": True,
                        "evidence": "leader reaches the marked point"}],
        }

    monkeypatch.setattr(draft_figures, "inspect_leaders", inspect_leaders)
    marked_calls = []

    def inspect_marked(_png, **kwargs):
        marked_calls.append([dict(item) for item in kwargs["anchors"]])
        if len(marked_calls) == 1:
            return {
                "ok": False, "inspected": True, "errors": ["move right"],
                "incorrect": ["26"], "missing": [], "labels": [{
                    "numeral": "26", "correct": False, "repairable": True,
                    "evidence": "the bearing face is right of center",
                    "suggested_x": 600, "suggested_y": 500,
                    "correct_votes": 1, "incorrect_votes": 2,
                }],
            }
        return {
            "ok": True, "inspected": True, "errors": [], "incorrect": [],
            "missing": [], "labels": [{
                "numeral": "26", "correct": True, "repairable": True,
                "evidence": "the center is on the bearing face",
                "suggested_x": 500, "suggested_y": 500,
                "correct_votes": 3, "incorrect_votes": 0,
            }],
        }

    monkeypatch.setattr(draft_figures, "inspect_marked_anchors", inspect_marked)

    with pytest.raises(KeyboardInterrupt):
        draft_figures._compose_checked_sheet(
            raw, label="FIG. 2", caption="bearing face", numerals=["26 = bearing face"],
            semantic={"anchors": initial, "pixel_anchor_audit": dict(accepted_pixel)})

    assert progress["attempts"] == 1
    assert progress["anchors"][0]["x"] > 500

    interrupt[0] = False
    _png, labels, leaders, anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 2", caption="bearing face", numerals=["26 = bearing face"],
        semantic={"anchors": initial, "pixel_anchor_audit": dict(accepted_pixel)})

    assert labels["ok"] is True and leaders["ok"] is True and pixel["ok"] is True
    assert anchors[0]["x"] == progress["anchors"][0]["x"]
    assert leaders["marked_anchor_audit"]["inspection_rounds"] == 2
    assert draft_figures.current_marked_anchor_audit(
        leaders["marked_anchor_audit"],
        specification_hash=draft_figures.specification_hash(
            "FIG. 2", "bearing face", ["26 = bearing face"]))


def test_leader_repair_never_moves_an_endpoint_with_a_coordinate_certificate():
    raw = blank_png(1000, 1000)
    anchors = [
        {"numeral": "10", "x": 200, "y": 200, "visible": True},
        {"numeral": "12", "x": 800, "y": 800, "visible": True},
    ]
    audit = {
        "incorrect": ["10", "12"],
        "labels": [
            {"numeral": "10", "suggested_x": 500, "suggested_y": 500},
            {"numeral": "12", "suggested_x": 500, "suggested_y": 500},
        ],
    }

    repaired, changed = draft_figures._repair_leader_anchors(
        raw, anchors, audit, scale=1.0, protected={"10"})

    assert changed is True
    assert (repaired[0]["x"], repaired[0]["y"]) == (200, 200)
    assert (repaired[1]["x"], repaired[1]["y"]) != (800, 800)


def test_compose_retries_layout_without_moving_a_certified_endpoint(monkeypatch):
    raw = blank_png(1000, 1000)
    anchors = [{"numeral": "10", "x": 200, "y": 200,
                "visible": True, "evidence": "left face"}]
    accepted_pixel = {
        "ok": True, "inspected": True, "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    certificate = {
        "x": 200, "y": 200, "attempt": 1,
        "label": {
            "numeral": "10", "correct": True,
            "evidence": "three reviewers approved this exact point",
            "correct_votes": 3, "incorrect_votes": 0,
        },
    }
    monkeypatch.setattr(draft_figures, "_marked_progress_get", lambda *a, **k: {
        "anchors": anchors, "certificates": {"10": certificate}, "attempts": 1,
    })
    monkeypatch.setattr(draft_figures, "_marked_progress_put", lambda *a, **k: None)
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, values, **_kwargs: ([dict(item) for item in values],
                                                    dict(accepted_pixel)))
    scales = []

    def annotate(_png, _label, _anchors, *, scale, sheet_number=""):
        scales.append(scale)
        return raw

    monkeypatch.setattr(draft_figures, "annotate_png", annotate)
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99,
    })
    leader_calls = []

    def inspect_leaders(*_args, **_kwargs):
        leader_calls.append(True)
        if len(leader_calls) == 1:
            return {
                "ok": False, "inspected": True, "errors": ["route is ambiguous"],
                "incorrect": ["10"], "missing": [], "labels": [{
                    "numeral": "10", "correct": False, "evidence": "route is ambiguous",
                    "suggested_x": 500, "suggested_y": 500,
                }],
            }
        return {
            "ok": True, "inspected": True, "errors": [], "incorrect": [], "missing": [],
            "labels": [{"numeral": "10", "correct": True,
                        "evidence": "larger layout has a continuous route"}],
        }

    monkeypatch.setattr(draft_figures, "inspect_leaders", inspect_leaders)

    _png, labels, leaders, final_anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 1", caption="base", numerals=["10 = base"],
        semantic={"anchors": anchors, "pixel_anchor_audit": dict(accepted_pixel)})

    assert labels["ok"] is True and leaders["ok"] is True and pixel["ok"] is True
    assert scales[:2] == [1.0, 1.35]
    assert (final_anchors[0]["x"], final_anchors[0]["y"]) == (200, 200)


def test_compose_retains_a_passing_layout_scale_across_endpoint_repairs(monkeypatch):
    raw = blank_png(1000, 1000)
    anchors = [{"numeral": "10", "x": 200, "y": 200,
                "visible": True, "evidence": "left face"}]
    accepted_pixel = {
        "ok": True, "inspected": True, "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(draft_figures, "_marked_progress_get", lambda *a, **k: None)
    monkeypatch.setattr(draft_figures, "_marked_progress_put", lambda *a, **k: None)
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, values, **_kwargs: ([dict(item) for item in values],
                                                    dict(accepted_pixel)))
    scales = []

    def annotate(_png, _label, _anchors, *, scale, sheet_number=""):
        scales.append(scale)
        return raw

    monkeypatch.setattr(draft_figures, "annotate_png", annotate)
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99,
    })
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": scales[-1] >= 1.8, "inspected": True,
        "errors": [] if scales[-1] >= 1.8 else ["route is ambiguous"],
        "incorrect": [] if scales[-1] >= 1.8 else ["10"], "missing": [],
        "labels": [{"numeral": "10", "correct": scales[-1] >= 1.8,
                    "evidence": "route inspection"}],
    })
    marked_calls = []

    def inspect_marked(*_args, **_kwargs):
        marked_calls.append(True)
        correct = len(marked_calls) > 1
        return {
            "ok": correct, "inspected": True,
            "errors": [] if correct else ["endpoint misses the left face"],
            "incorrect": [] if correct else ["10"], "missing": [],
            "labels": [{
                "numeral": "10", "correct": correct, "repairable": True,
                "evidence": "endpoint inspection", "suggested_x": 250,
                "suggested_y": 250, "correct_votes": 3 if correct else 0,
                "incorrect_votes": 0 if correct else 3,
            }],
        }

    monkeypatch.setattr(draft_figures, "inspect_marked_anchors", inspect_marked)
    monkeypatch.setattr(draft_figures, "inspect_cross_provider_endpoints", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [], "labels": [],
    })

    _png, labels, leaders, _final_anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 1", caption="base", numerals=["10 = base"],
        semantic={"anchors": anchors, "pixel_anchor_audit": dict(accepted_pixel)})

    assert labels["ok"] is True and leaders["ok"] is True and pixel["ok"] is True
    assert scales == [1.0, 1.35, 1.8, 1.8]


def test_compose_does_not_let_leader_routing_move_a_geometry_endpoint(monkeypatch):
    raw = blank_png(1000, 1000)
    anchors = [{"numeral": "10", "x": 200, "y": 200,
                "visible": True, "evidence": "left face"}]
    accepted_pixel = {
        "ok": True, "inspected": True, "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(draft_figures, "_marked_progress_get", lambda *a, **k: None)
    monkeypatch.setattr(draft_figures, "_marked_progress_put", lambda *a, **k: None)
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, values, **_kwargs: ([dict(item) for item in values],
                                                    dict(accepted_pixel)))
    scales = []

    def annotate(_png, _label, _anchors, *, scale, sheet_number=""):
        scales.append(scale)
        return raw

    monkeypatch.setattr(draft_figures, "annotate_png", annotate)
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99,
    })
    leader_calls = []

    def inspect_leaders(*_args, **_kwargs):
        leader_calls.append(True)
        if len(leader_calls) == 1:
            return {
                "ok": False, "inspected": True, "errors": ["route is ambiguous"],
                "incorrect": ["10"], "missing": [], "labels": [{
                    "numeral": "10", "correct": False, "evidence": "route is ambiguous",
                    "suggested_x": 500, "suggested_y": 500,
                }],
            }
        return {
            "ok": True, "inspected": True, "errors": [], "incorrect": [], "missing": [],
            "labels": [{"numeral": "10", "correct": True,
                        "evidence": "larger layout has a continuous route"}],
        }

    monkeypatch.setattr(draft_figures, "inspect_leaders", inspect_leaders)
    monkeypatch.setattr(draft_figures, "inspect_marked_anchors", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [], "missing": [],
        "labels": [{
            "numeral": "10", "correct": True, "repairable": True,
            "evidence": "the endpoint remains on the left face",
            "suggested_x": 500, "suggested_y": 500,
            "correct_votes": 3, "incorrect_votes": 0,
        }],
    })

    _png, labels, leaders, final_anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 1", caption="base", numerals=["10 = base"],
        semantic={"anchors": anchors, "pixel_anchor_audit": dict(accepted_pixel)})

    assert labels["ok"] is True and leaders["ok"] is True and pixel["ok"] is True
    assert scales[:2] == [1.0, 1.35]
    assert (final_anchors[0]["x"], final_anchors[0]["y"]) == (200, 200)


def test_marked_anchor_heading_states_the_exact_full_sheet_pixel_coordinate():
    heading = draft_figures._marked_anchor_heading(
        {"numeral": "26", "x": 500, "y": 500}, {"26": "bearing face"},
        source_size=(1400, 900))

    assert heading == "26: bearing face | CURRENT PIXEL (700, 450)"


def test_marked_anchor_montage_preserves_the_endpoint_pixel_inside_a_red_ring():
    image = Image.new("RGB", (400, 400), "white")
    ImageDraw.Draw(image).point((200, 200), fill="black")
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    montage = Image.open(io.BytesIO(draft_figures._marked_anchor_montage(
        raw.getvalue(), [{"numeral": "26", "x": 501, "y": 501, "visible": True}],
        ["26 = bearing face"]))).convert("RGB")

    assert montage.width >= 600
    center_x = 16 + 16 + 240 + 16 + 320 // 2
    center_y = 16 + 72 + 320 // 2
    assert all(channel < 80 for channel in montage.getpixel((center_x, center_y)))
    assert montage.getpixel((center_x + 17, center_y))[0] > 180


def test_compose_continues_an_eight_round_checkpoint_after_context_upgrade(monkeypatch):
    raw = blank_png(1000, 1000)
    anchors = [{"numeral": "22", "x": 300, "y": 300,
                "visible": True, "evidence": "chamber"}]
    accepted_pixel = {
        "ok": True, "inspected": True,
        "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(draft_figures, "_marked_progress_get", lambda *a, **k: {
        "anchors": anchors, "certificates": {}, "attempts": 8,
    })
    monkeypatch.setattr(draft_figures, "_marked_progress_put", lambda *a, **k: None)
    monkeypatch.setattr(
        draft_figures, "_ground_anchors_to_pixels",
        lambda _png, _numerals, values, **_kwargs: ([dict(item) for item in values],
                                                    dict(accepted_pixel)))
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["22"], "figure_label": "FIG. 3",
        "other_text": [], "confidence": 0.99})
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [],
        "labels": [{"numeral": "22", "correct": True, "evidence": "leader reaches dot"}],
    })
    marked_calls = []

    def inspect_marked(*_args, **_kwargs):
        marked_calls.append(True)
        return {
            "ok": True, "inspected": True, "errors": [], "incorrect": [],
            "missing": [], "labels": [{
                "numeral": "22", "correct": True, "repairable": True,
                "evidence": "the full-sheet overview identifies the chamber",
                "suggested_x": 500, "suggested_y": 500,
                "correct_votes": 3, "incorrect_votes": 0,
            }],
        }

    monkeypatch.setattr(draft_figures, "inspect_marked_anchors", inspect_marked)
    _png, _labels, leaders, _anchors, _pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 3", caption="chamber", numerals=["22 = chamber"],
        semantic={"anchors": anchors, "pixel_anchor_audit": dict(accepted_pixel)})

    assert marked_calls == [True]
    assert leaders["ok"] is True
    assert leaders["marked_anchor_audit"]["inspection_rounds"] == 9


def test_only_compatible_two_trace_semantic_reviews_are_accepted(monkeypatch):
    current = {
        "ok": True, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": 2,
        "pixel_anchor_audit": {
            "ok": True, "inspected": True,
            "version": draft_figures.PIXEL_ANCHOR_VERSION,
        },
        "topology_audit": {
            "ok": True, "inspected": False, "required": False,
            "version": draft_figures.CLOSED_REGION_AUDIT_VERSION,
        },
        "section_mark_audit": accepted_semantic_audit()["section_mark_audit"],
        "marked_anchor_audit": accepted_marked_anchor_audit(),
    }
    assert draft_figures.current_semantic_audit(current) is True
    assert draft_figures.current_semantic_audit({
        **current,
        "prompt_version": (
            "figure-semantic-v12-high-accuracy-geometry-only-consensus-"
            "pixel-grounded-marked-topology"
        ),
    }) is True
    assert draft_figures.current_semantic_audit({**current, "review_count": 1}) is False
    assert draft_figures.current_semantic_audit({**current, "prompt_version": "old"}) is False
    assert draft_figures.current_semantic_audit({
        **current,
        "pixel_anchor_audit": {
            **current["pixel_anchor_audit"],
            "version": "pixel-anchor-v2-sheet-boundary-clearance",
        },
    }) is False
    assert draft_figures.current_semantic_audit({**current, "pixel_anchor_audit": {}}) is False
    assert draft_figures.current_semantic_audit({**current, "topology_audit": {}}) is False
    assert draft_figures.current_semantic_audit({**current, "marked_anchor_audit": {}}) is False
    assert draft_figures.current_semantic_audit({"ok": True}) is False

    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_REQUIRED", "1")
    assert draft_figures.current_semantic_audit(current) is False
    current["cross_provider_geometry_audit"] = accepted_cross_provider_geometry_audit(
        specification_hash="a" * 64)
    current["specification_hash"] = "a" * 64
    current["marked_anchor_audit"] = accepted_marked_anchor_audit(
        specification_hash="a" * 64,
        cross_provider_audit=accepted_cross_provider_audit(
            specification_hash="a" * 64))
    assert draft_figures.current_semantic_audit(current) is True
    assert draft_figures.current_semantic_audit({
        **current,
        "cross_provider_geometry_audit": {
            **current["cross_provider_geometry_audit"], "prompt_version": "old",
        },
    }) is False
    assert draft_figures.current_semantic_audit({
        **current, "specification_hash": "b" * 64,
    }) is False


def test_marked_progress_ignores_coordinates_saved_under_old_grounding_rules(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda _key: {
        "version": "marked-progress-v1-final-coordinate-certificates",
        "anchors": [{
            "numeral": "24", "x": 500, "y": 206,
            "visible": True, "evidence": "space between two boundary lines",
        }],
        "certificates": {}, "coordinate_history": {"24": [[500, 206]]},
        "attempts": 6,
    })

    assert draft_figures._marked_progress_get(
        b"old pixels", label="FIG. 3", caption="nested plan",
        numerals=["24 = perimeter member"],
    ) is None


def test_marked_progress_ignores_current_layout_saved_under_old_pixel_grounding(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda _key: {
        "version": draft_figures.MARKED_PROGRESS_VERSION,
        "pixel_anchor_version": "pixel-anchor-v7-old-grounding",
        "anchors": [{
            "numeral": "36", "x": 203, "y": 800,
            "visible": True, "evidence": "reviewed surface point",
        }],
        "certificates": {}, "coordinate_history": {"36": [[203, 800]]},
        "attempts": draft_figures.MAX_MARKED_ANCHOR_REPAIR_ATTEMPTS,
    })

    assert draft_figures._marked_progress_get(
        b"same raw geometry", label="FIG. 5", caption="perspective cable embodiment",
        numerals=["36 = covering element"],
    ) is None


def test_marked_progress_cache_is_bound_to_the_deterministic_anchor_map():
    assert (draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION in
            draft_figures.MARKED_PROGRESS_VERSION)


def test_current_marked_audit_rejects_a_pre_native_pixel_review():
    audit = accepted_marked_anchor_audit(
        prompt_version=(
            "figure-anchor-v9-local-part-coordinate-certificate-majority-with-correction"
        ))

    assert draft_figures.current_marked_anchor_audit(audit) is False
    assert draft_figures.current_marked_anchor_audit({
        **audit, "prompt_version": "figure-anchor-v8-old",
    }) is False


def test_required_cross_provider_gate_invalidates_older_marked_audits(monkeypatch):
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_REQUIRED", "1")
    audit = accepted_marked_anchor_audit(specification_hash="a" * 64)

    assert draft_figures.current_marked_anchor_audit(
        audit, specification_hash="a" * 64) is False
    audit["cross_provider_audit"] = accepted_cross_provider_audit(
        specification_hash="a" * 64)
    assert draft_figures.current_marked_anchor_audit(
        audit, specification_hash="a" * 64) is True


def test_pixel_grounding_snaps_an_exterior_object_anchor_to_visible_ink():
    image = Image.new("RGB", (1000, 1000), "white")
    ImageDraw.Draw(image).rectangle((200, 200, 800, 600), outline="black", width=8)
    out = io.BytesIO()
    image.save(out, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        out.getvalue(), ["10 = base"], [{
            "numeral": "10", "x": 500, "y": 780, "visible": True,
            "evidence": "the base body",
        }])

    assert audit["ok"] is True and audit["adjusted"][0]["numeral"] == "10"
    assert 495 <= anchors[0]["x"] <= 505 and 590 <= anchors[0]["y"] <= 610


def test_pixel_grounding_rejects_an_endpoint_on_the_sheet_boundary():
    image = Image.new("RGB", (1000, 1000), "white")
    ImageDraw.Draw(image).line((100, 13, 900, 13), fill="black", width=5)
    out = io.BytesIO()
    image.save(out, format="PNG")

    _, audit = draft_figures._ground_anchors_to_pixels(
        out.getvalue(), ["14 = first side"], [{
            "numeral": "14", "x": 750, "y": 13, "visible": True,
            "evidence": "a point on the top horizontal edge line of the slab",
        }])

    assert audit["ok"] is False
    assert audit["ungrounded"][0]["numeral"] == "14"
    assert "sheet boundary" in audit["ungrounded"][0]["reason"]


def test_pixel_grounding_keeps_enclosed_bodies_and_intentional_empty_spaces():
    image = Image.new("RGB", (1000, 1000), "white")
    ImageDraw.Draw(image).rectangle((200, 200, 800, 600), outline="black", width=8)
    out = io.BytesIO()
    image.save(out, format="PNG")
    original = [
        {"numeral": "10", "x": 500, "y": 400, "visible": True,
         "evidence": "inside the base"},
        {"numeral": "12", "x": 500, "y": 780, "visible": True,
         "evidence": "open clearance"},
    ]

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        out.getvalue(), ["10 = base", "12 = clearance"], original)

    assert audit["ok"] is True and audit["adjusted"] == []
    assert [(item["x"], item["y"]) for item in anchors] == [(500, 400), (500, 780)]


@pytest.mark.parametrize("part", ["second side", "bearing face"])
def test_pixel_grounding_keeps_a_planar_surface_endpoint_inside_its_region(part):
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 900, 900), outline="black", width=8)
    draw.rectangle((200, 200, 800, 800), outline="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    original = [{
        "numeral": "16", "x": 150, "y": 150, "visible": True,
        "evidence": "the white planar band between the two outlines",
    }]

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), [f"16 = {part}"], original)

    assert audit["ok"] is True and audit["adjusted"] == []
    assert (anchors[0]["x"], anchors[0]["y"]) == (150, 150)


def test_pixel_grounding_keeps_a_space_between_named_edges_off_the_edges():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 900, 900), outline="black", width=8)
    draw.rectangle((200, 200, 800, 800), outline="black", width=8)
    draw.rectangle((350, 350, 650, 650), outline="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["24 = perimeter member"], [{
            "numeral": "24", "x": 500, "y": 275, "visible": True,
            "evidence": (
                "a point in the space between the top edge of the middle rectangle "
                "and the top edge of the innermost rectangle"
            ),
        }])

    assert audit["ok"] is True and audit["adjusted"] == []
    assert (anchors[0]["x"], anchors[0]["y"]) == (500, 275)


def test_pixel_grounding_rejects_a_narrow_white_margin_for_a_broad_interior_target():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 900, 900), outline="black", width=8)
    draw.rectangle((122, 122, 878, 878), outline="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    _, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["16 = second side"], [{
            "numeral": "16", "x": 111, "y": 500, "visible": True,
            "evidence": "well inside the white space margin, clear of both boundary lines",
        }])

    assert audit["ok"] is False
    assert audit["ungrounded"][0]["numeral"] == "16"
    assert "clearance" in audit["ungrounded"][0]["reason"]


def test_pixel_grounding_accepts_a_wide_white_margin_for_a_broad_interior_target():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 900, 900), outline="black", width=8)
    draw.rectangle((250, 250, 750, 750), outline="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["16 = second side"], [{
            "numeral": "16", "x": 175, "y": 500, "visible": True,
            "evidence": "well inside the white space margin, clear of both boundary lines",
        }])

    assert audit["ok"] is True and audit["ungrounded"] == []
    assert (anchors[0]["x"], anchors[0]["y"]) == (175, 500)


def test_pixel_grounding_keeps_an_open_perspective_surface_off_a_crossing_cable():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    # The left side is occluded or cropped, so flood fill connects the visible tile surface to
    # the paper even though the independent semantic reviews can identify that surface.
    draw.line((100, 500, 900, 500, 900, 900, 100, 900), fill="black", width=8)
    draw.line((100, 780, 700, 650), fill="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["36 = covering element"], [{
            "numeral": "36", "x": 200, "y": 758, "visible": True,
            "evidence": (
                "The top surface of the large tile, in the open area to the left of the machine."
            ),
        }])

    assert audit["ok"] is True and audit["ungrounded"] == []
    assert audit["adjusted"][0]["numeral"] == "36"
    assert audit["adjusted"][0]["reason"] == "moved to a nearby clear point on the reviewed surface"
    assert 100 < anchors[0]["x"] < 900
    assert 500 < anchors[0]["y"] < 900
    assert abs(anchors[0]["y"] - (780 - (anchors[0]["x"] - 100) * 130 / 600)) >= 20


def test_pixel_grounding_moves_a_shared_boundary_into_the_requested_lower_surface():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((200, 200, 800, 800), outline="black", width=8)
    draw.line((200, 500, 800, 500), fill="black", width=8)
    draw.line((200, 548, 800, 548), fill="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["24 = perimeter member"], [{
            "numeral": "24", "x": 500, "y": 500, "visible": True,
            "evidence": (
                "the front-facing surface of the lower band located beneath the main slab"
            ),
        }], preserve_reviewed_line_target=True)

    assert audit["ok"] is True and audit["ungrounded"] == []
    assert audit["adjusted"][0]["numeral"] == "24"
    assert 508 < anchors[0]["y"] < 540


def test_pixel_grounding_uses_the_brief_target_over_stale_semantic_line_evidence():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((200, 500, 800, 550), outline="black", width=4)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    caption = (
        "- The perimeter member 24 is the band under the rectangular body. "
        "Identified well inside the front surface of the band."
    )
    anchors = draft_figures._bind_anchor_target_evidence(
        [{
            "numeral": "24", "x": 500, "y": 546, "visible": True,
            "evidence": "the horizontal line defining the top of the front face",
        }], label="FIG. 5", caption=caption, numerals=["24 = perimeter member"])

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["24 = perimeter member"], anchors,
        preserve_reviewed_line_target=True)

    assert audit["ok"] is True and audit["ungrounded"] == []
    assert anchors[0]["evidence"] == (
        "the horizontal line defining the top of the front face")
    assert anchors[0]["target_evidence"] == (
        "Identified well inside the front surface of the band.")
    assert 512 < anchors[0]["y"] < 538


def test_pixel_grounding_does_not_leave_a_bounded_surface_for_more_clearance():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((400, 480, 650, 510), outline="black", width=6)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["24 = perimeter member"], [{
            "numeral": "24", "x": 525, "y": 495, "visible": True,
            "evidence": "the front-facing surface of the narrow perimeter band",
        }], preserve_reviewed_line_target=True)

    assert audit["ok"] is False
    assert audit["ungrounded"][0]["numeral"] == "24"
    assert (anchors[0]["x"], anchors[0]["y"]) == (525, 495)


@pytest.mark.parametrize("evidence", [
    "Identified well inside its front face.",
    (
        "Identified well inside the broad front strip of the band, below the underside "
        "of the slab and above the tile, clear of both of those boundaries."
    ),
])
def test_pixel_grounding_uses_surface_clearance_for_a_narrow_front_face(evidence):
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((400, 480, 650, 536), outline="black", width=6)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["24 = perimeter member"], [{
            "numeral": "24", "x": 525, "y": 508, "visible": True,
            "evidence": evidence,
        }], preserve_reviewed_line_target=True)

    assert audit["ok"] is True and audit["ungrounded"] == []
    assert 500 <= anchors[0]["y"] <= 516


def test_pixel_grounding_moves_a_bounded_band_target_out_of_exterior_paper():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 900, 900), outline="black", width=8)
    draw.rectangle((200, 200, 800, 800), outline="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["24 = perimeter member"], [{
            "numeral": "24", "x": 70, "y": 500, "visible": True,
            "evidence": (
                "Identified well inside that band on its straight left-hand side, clear of "
                "both its outer edge and its inner edge, and not in the open field."
            ),
        }], preserve_reviewed_line_target=True)

    assert audit["ok"] is True and audit["ungrounded"] == []
    assert audit["adjusted"][0]["reason"] == (
        "moved to a nearby clear point on the reviewed surface")
    assert 125 <= anchors[0]["x"] <= 175


def test_pixel_grounding_moves_a_boundary_near_anchor_deeper_into_the_same_wide_region():
    specification = (
        "The sheet shows one rectangular ring and nothing else. The ring is drawn with two "
        "closed thin lines and those two alone: its outer edge and its inner edge. Both are "
        "rectangular, and they are spaced apart on all four sides. The opening inside the ring "
        "is left plain, so that the finished sheet carries just those two closed lines."
    )
    png = draft_figures._deterministic_nested_plan_png(specification)

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        png, ["24 = perimeter member"], [{
            "numeral": "24", "x": 500, "y": 200, "visible": True,
            "evidence": "the band-like surface between the outer and inner rectangular outlines",
        }])

    assert audit["ok"] is True and audit["ungrounded"] == []
    assert audit["adjusted"][0]["numeral"] == "24"
    assert anchors[0]["x"] == 500
    assert 120 <= anchors[0]["y"] <= 180
    assert draft_figures.closed_region_audit(png, specification)["observed"] == 2


def test_pixel_grounding_does_not_apply_white_clearance_to_hatched_material():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 900, 900), outline="black", width=8)
    draw.line((100, 510, 900, 510), fill="black", width=4)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["12 = base"], [{
            "numeral": "12", "x": 500, "y": 500, "visible": True,
            "evidence": "well inside the hatching and clear of both boundary lines",
        }])

    assert audit["ok"] is True and audit["ungrounded"] == []
    assert (anchors[0]["x"], anchors[0]["y"]) == (500, 500)


@pytest.mark.parametrize("evidence", [
    "a point on the contact line where the leg meets the base",
    "the top horizontal line of the uppermost hatched layer",
])
def test_pixel_grounding_snaps_a_face_endpoint_when_evidence_requires_a_contact_line(evidence):
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 900, 900), outline="black", width=8)
    draw.line((100, 500, 900, 500), fill="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["26 = bearing face"], [{
            "numeral": "26", "x": 300, "y": 540, "visible": True,
            "evidence": evidence,
        }])

    assert audit["ok"] is True and audit["adjusted"][0]["numeral"] == "26"
    assert 495 <= anchors[0]["y"] <= 505


def test_pixel_grounding_prefers_a_long_boundary_over_nearby_hatching():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.line((100, 500, 900, 500), fill="black", width=6)
    draw.line((280, 450, 320, 450), fill="black", width=4)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["16 = second side"], [{
            "numeral": "16", "x": 300, "y": 450, "visible": True,
            "evidence": "the lower horizontal edge line of the slab",
        }])

    assert audit["ok"] is True
    assert audit["adjusted"][0]["numeral"] == "16"
    assert 495 <= anchors[0]["y"] <= 505


def test_pixel_grounding_keeps_the_nearest_substantial_boundary_over_a_farther_longer_one():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.line((350, 400, 650, 400), fill="black", width=6)
    draw.line((100, 550, 900, 550), fill="black", width=6)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["26 = bearing face"], [{
            "numeral": "26", "x": 500, "y": 410, "visible": True,
            "evidence": "the bottom horizontal line of the column",
        }])

    assert audit["ok"] is True
    assert audit["adjusted"][0]["numeral"] == "26"
    assert 395 <= anchors[0]["y"] <= 405


def test_pixel_grounding_preserves_a_reviewed_short_boundary_correction():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((700, 200, 800, 450), outline="black", width=6)
    draw.line((100, 572, 900, 572), fill="black", width=6)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["26 = bearing face"], [{
            "numeral": "26", "x": 750, "y": 450, "visible": True,
            "evidence": "the bottom horizontal line of the upper column",
        }], preserve_reviewed_line_target=True)

    assert audit["ok"] is True
    assert 745 <= anchors[0]["x"] <= 755
    assert 445 <= anchors[0]["y"] <= 455


def test_pixel_grounding_preserves_reviewed_boundary_behind_nearby_hatching():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.line((375, 466, 550, 466), fill="black", width=6)
    draw.line((50, 529, 950, 529), fill="black", width=6)
    draw.line((425, 425, 475, 475), fill="black", width=4)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["26 = bearing face"], [{
            "numeral": "26", "x": 450, "y": 450, "visible": True,
            "evidence": "the horizontal line forming the bottom edge of the column",
        }], preserve_reviewed_line_target=True)

    assert audit["ok"] is True
    assert 462 <= anchors[0]["y"] <= 470


def test_pixel_grounding_prefers_a_long_vertical_boundary_over_nearby_hatching():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.line((500, 100, 500, 900), fill="black", width=6)
    draw.line((450, 280, 450, 320), fill="black", width=4)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["18 = housing"], [{
            "numeral": "18", "x": 450, "y": 300, "visible": True,
            "evidence": "the right vertical boundary line of the housing",
        }])

    assert audit["ok"] is True
    assert audit["adjusted"][0]["numeral"] == "18"
    assert 495 <= anchors[0]["x"] <= 505


def test_pixel_grounding_does_not_treat_an_excluded_ring_as_the_target():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 900, 900), outline="black", width=8)
    draw.ellipse((420, 420, 580, 580), outline="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        raw.getvalue(), ["10 = vibration device"], [{
            "numeral": "10", "x": 500, "y": 700, "visible": True,
            "evidence": "inside the plain base face, not on the ring or its outline",
        }])

    assert audit["ok"] is True and audit["adjusted"] == []
    assert (anchors[0]["x"], anchors[0]["y"]) == (500, 700)


def test_closed_region_audit_enforces_an_explicit_exact_shape_count():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    for box in ((100, 100, 900, 900), (200, 200, 800, 800), (300, 300, 700, 700)):
        draw.rounded_rectangle(box, radius=80, outline="black", width=8)
    draw.ellipse((450, 450, 550, 550), outline="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    specification = (
        "The sheet contains exactly four shapes. Each shape is a single closed curve. "
        "No hatching is present.")

    accepted = draft_figures.closed_region_audit(raw.getvalue(), specification)
    assert accepted["ok"] is True and accepted["observed"] == 4

    draw.rounded_rectangle((380, 380, 620, 620), radius=50, outline="black", width=8)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    rejected = draft_figures.closed_region_audit(raw.getvalue(), specification)
    assert rejected["ok"] is False and rejected["observed"] == 5
    assert "exactly 4" in rejected["errors"][0]


def test_closed_region_audit_recognizes_contains_count_and_nothing_else():
    specification = (
        "The whole sheet contains four outlines and nothing else. "
        "Each outline is drawn once as one closed line."
    )

    assert draft_figures._expected_closed_region_count(specification) == 4
    assert draft_figures._expected_closed_region_count(
        "The sheet holds exactly four closed lines and nothing else.") == 4


def test_deterministic_nested_plan_has_exactly_three_rectangles_and_one_circle():
    specification = (
        "The whole sheet contains four outlines and nothing else. From the outside inward, "
        "draw three nested rectangles and one circle at the centre. Each outline is drawn "
        "once as one closed line."
    )

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 4
    assert draft_figures._deterministic_nested_plan_png(
        "A perspective view of a rectangular housing and a circular port.") is None
    rewritten = (
        "The sheet holds exactly four closed lines and nothing else: a large rectangle, a "
        "second rectangle within it, a third rectangle within that, and a circle within the "
        "third. Counting the lines gives four, three rectangular and one circular."
    )
    assert draft_figures._deterministic_nested_plan_png(rewritten) is not None


def test_deterministic_nested_plan_supports_two_rectangle_only_sheet_with_margins():
    specification = (
        "The whole sheet contains exactly two rectangular outlines, one nested inside the "
        "other, and no other line of any kind. Each rectangle is one closed thin line. The whole "
        "drawing stands clear of the edges of the sheet."
    )

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2
    image = Image.open(io.BytesIO(png)).convert("L")
    black_x = [x for x in range(image.width) for y in range(image.height)
               if image.getpixel((x, y)) < 32]
    black_y = [y for x in range(image.width) for y in range(image.height)
               if image.getpixel((x, y)) < 32]
    assert min(black_x) >= 100 and max(black_x) <= image.width - 100
    assert min(black_y) >= 75 and max(black_y) <= image.height - 75


def test_deterministic_nested_plan_accepts_filing_clean_two_lines_wording():
    specification = (
        "The sheet shows one rectangular ring and nothing else. The ring is drawn with two "
        "closed thin lines and those two alone: its outer edge and its inner edge. Both are "
        "rectangular and one is nested inside the other. The finished sheet carries just those "
        "two closed lines."
    )

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert draft_figures._deterministic_nested_plan_png(specification) is not None


def test_deterministic_nested_plan_accepts_ring_outer_inner_edge_wording():
    specification = (
        "The sheet shows one rectangular ring and nothing else. The ring is drawn with two "
        "closed thin lines and those two alone: its outer edge and its inner edge. Both are "
        "rectangular, and they are spaced apart on all four sides, so that the band between them "
        "is a continuous visible surface running all the way round. The opening inside the ring "
        "is left plain, so that the finished sheet carries just those two closed lines."
    )

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert draft_figures._deterministic_nested_plan_png(specification) is not None


def test_deterministic_nested_plan_accepts_widely_spaced_ring_edges():
    specification = (
        "The sheet shows one rectangular ring and nothing else. The ring is drawn with two "
        "closed thin lines and those two alone: its outer edge and its inner edge. Both are "
        "rectangular, and they are spaced widely apart on all four sides, so that the band "
        "between them is broad. The finished sheet carries just those two closed lines."
    )

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert draft_figures._deterministic_nested_plan_png(specification) is not None


def test_deterministic_nested_plan_accepts_source_clean_ring_wording():
    specification = (
        "The sheet shows the perimeter member as one rectangular ring, and within it a plain "
        "open field; no other body is drawn. The ring is drawn with two closed thin lines and "
        "those two alone, its outer edge and its inner edge, both rectangular. The inner "
        "rectangle lies within the outer one, the band between them being drawn broad enough "
        "everywhere to read plainly and forming one continuous surface running all the way "
        "round; no particular proportion is asserted. The finished sheet carries two closed "
        "rectangular lines and no third."
    )

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert draft_figures._deterministic_nested_plan_png(specification) is not None


def test_deterministic_nested_plan_accepts_separately_named_outer_and_inner_lines():
    specification = (
        "The sheet shows the perimeter member as one rectangular ring and no other body. "
        "The whole sheet carries exactly two closed lines and no others: one rectangle, which "
        "is the outer edge of the ring, and one smaller rectangle within it, which is the inner "
        "edge of the ring. The band lying between those two lines is the drawn body. Beyond the "
        "outer rectangle the paper is bare on every side."
    )

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert draft_figures._deterministic_nested_plan_png(specification) is not None


def test_deterministic_nested_plan_accepts_continuous_unpartitioned_ring_wording():
    specification = (
        "The sheet shows the perimeter member as one rectangular ring and within it a plain "
        "open field; no other body is drawn. The ring is drawn as one rectangle with a second, "
        "smaller rectangle inside it, the two sharing a common centre. The band of paper lying "
        "between the outer rectangle and the inner rectangle is the drawn body. No diagonal, "
        "mitre or corner line is drawn at any corner, and no line crosses the band at any place. "
        "It is one continuous surface bounded only by the outer and inner rectangles."
    )

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.getpixel((190, 140)) == 255
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2


def test_deterministic_nested_plan_accepts_positive_one_rectangle_inside_another_wording():
    specification = (
        "The sheet shows the perimeter member 24 as one rectangular ring, and within it the "
        "second side 16 as a plain open field; no other body is drawn. The ring is drawn as one "
        "rectangle with a smaller rectangle inside it, the inner rectangle standing clear of "
        "each of the four sides of the outer rectangle. The band of paper lying between the "
        "outer rectangle and the inner rectangle is the drawn body. It runs continuously all "
        "the way round. The field enclosed by the inner rectangle is left entirely open paper. "
        "Beyond the outer rectangle the paper is bare on every side."
    )

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2


def test_deterministic_nested_plan_accepts_inset_ring_without_bare_paper_instruction():
    specification = (
        "The sheet shows the perimeter member 24 as one rectangular ring, and within it the "
        "second side 16 as a plain open field; no other body is drawn. The ring is drawn as one "
        "rectangle with a smaller rectangle inside it, the inner rectangle standing clear of "
        "each of the four sides of the outer rectangle. The band of paper lying between the "
        "outer rectangle and the inner rectangle is the drawn body. It runs continuously all "
        "the way round. The field enclosed by the inner rectangle is left entirely open paper. "
        "The ring stands well in from every side of the drawing area."
    )

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2


def test_deterministic_nested_plan_accepts_well_inset_ring_wording():
    specification = (
        "The sheet shows the perimeter member 24 as one rectangular ring, and within it the "
        "second side 16 as a plain open field; no other body is drawn. The ring is drawn as one "
        "rectangle with a smaller rectangle inside it, the inner rectangle standing well in "
        "from each of the four sides of the outer rectangle. The band of paper lying between "
        "the outer rectangle and the inner rectangle is the drawn body. It runs continuously "
        "all the way round. The field enclosed by the inner rectangle is left entirely open "
        "paper. The ring stands well in from every side of the drawing area."
    )

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2


def test_deterministic_nested_plan_accepts_two_boundary_body_wording():
    specification = (
        "Plan view looking straight up at the underside of the machine. The sheet shows the "
        "underside as one closed body, made up of the perimeter member 24 running all the way "
        "round it and the second side 16 lying within. Both are shown schematically with a "
        "rectangular plan outline. The body is bounded by an outer boundary, and well in from "
        "it on all four sides by an inner boundary. The surface lying between those two "
        "boundaries is the perimeter member 24. The surface lying within the inner boundary "
        "is the second side 16. The area outside the outer boundary is background, and "
        "nothing is drawn there."
    )

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2


def test_deterministic_nested_plan_accepts_two_outline_ring_wording():
    specification = (
        "Plan view looking straight up at the underside of the machine. The sheet shows the "
        "underside as a rectangular ring in the manner of a picture frame. Two closed "
        "rectangular outlines appear in the view, one held within the other: the outer edge "
        "of the ring and the inner edge of the ring. The perimeter member 24 is the ring "
        "surface lying between the outer edge and the inner edge. The second side 16 is the "
        "plain face held within the inner edge. Beyond the outer edge lies the surrounding "
        "background."
    )

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2


def test_deterministic_nested_plan_accepts_current_sectioned_ring_wording():
    specification = """
    Plan view looking straight up at the underside of the machine. The sheet shows the
    underside of the machine as one rectangular ring in the manner of a picture frame.
    Exactly two closed rectangular outlines appear, one within the other: the outer edge of
    the ring and the inner edge. The surface between those two outlines runs unbroken from the
    outer edge straight to the inner edge along every side, so the view reads as a single frame
    of one width. The perimeter member 24 is the ring surface. The second side 16 is the face at
    which this view looks straight, appearing as the area held within the inner edge. Beyond the
    outer edge lies background.

    Two broken section lines cross the view, one horizontal across its lower part and one
    vertical across its right-hand part. Each begins and ends in the background just beyond
    the outer edge and crosses the ring surface and the face within it. The section lines are
    drawing conventions.
    """

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2


def test_deterministic_nested_plan_accepts_repaired_sectioned_ring_wording():
    specification = """
    Plan view looking straight up at the underside of the machine. The sheet shows the
    underside of the machine as one rectangular ring in the manner of a picture frame. The
    whole of its line work is: the outer edge of the ring, the inner edge of the ring held
    within it, and the two section lines described below. The surface between the outer edge
    and the inner edge runs unbroken from one straight to the other along every side, so the
    view reads as a single frame of one width. The perimeter member 24 is the ring surface.
    The second side 16 is the face at which this view looks straight, appearing as the area held
    within the inner edge. Beyond the outer edge lies background.

    Two broken section lines cross the view, one horizontal across its lower part and one
    vertical across its right-hand part. The section lines are drawing conventions.
    """

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2


def test_deterministic_nested_plan_accepts_line_work_inventory_wording():
    specification = """
    Plan view looking straight up at the underside of the machine. The sheet shows the
    underside of the machine as one rectangular ring, like a picture frame. Its line work is:
    the outer edge of the ring, the inner edge held within it, and the two section lines. The
    surface between those edges runs unbroken from one to the other along every side, so the
    view reads as one continuous closed frame. The perimeter member 24 is the ring surface. The
    second side 16 is the face at which this view looks straight, appearing as the area within
    the inner edge. Beyond the outer edge lies background.

    Two broken section lines cross the view. Each passes through the face within the inner edge,
    cuts the ring surface on both sides, and ends in the background past the outer edge.
    """

    png = draft_figures._deterministic_nested_plan_png(specification)

    assert draft_figures._expected_closed_region_count(specification) == 2
    assert png is not None
    audit = draft_figures.closed_region_audit(png, specification)
    assert audit["ok"] is True and audit["observed"] == 2


def test_deterministic_pulling_scene_accepts_source_clean_single_path_wording():
    specification = """
    The covering element 36 is one large plain tile filling the lower part of the drawing
    area. The machine stands on its right-hand part, leaving a wide open expanse of tile to
    the left. The machine is a plain slab carrying two closed housings and standing on a band
    round its underside, the band alone touching the tile. The flexible pulling element 46 is
    drawn as one curved path, in the manner of a slack cord, of even thickness along its whole
    length. It begins at the outer silhouette of the machine and runs away to the left across
    the open expanse of tile, sagging gently, ending well inside the left-hand limit.
    """

    png = draft_figures._deterministic_pulling_scene_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.size == (1400, 900)
    # The open left-hand endpoint must not be converted into a closed cable outline.
    assert min(image.crop((105, 470, 220, 600)).getextrema()) == 0
    for x in range(425, 651, 25):
        ink = [y for y in range(400, 591) if image.getpixel((x, y)) < 32]
        assert ink
        assert ink[-1] - ink[0] <= 7


def test_deterministic_pulling_scene_omits_parts_for_plain_body_wording():
    specification = """
    The covering element 36 is one large plain tile, a flat rectangular panel seen in
    perspective. The machine stands on its right-hand part, leaving a wide open expanse of tile
    to the left. The machine is shown schematically as one plain rectangular body standing on a
    band that runs round its underside. No housing, grip or other part is drawn on it. The
    flexible pulling element 46 is drawn as one slack curved path. It begins where it meets the
    left-hand side of the machine and runs away to the left, sagging gently over the tile. It lies
    wholly to the left of the machine and nowhere passes across the machine.
    """

    png = draft_figures._deterministic_pulling_scene_png(specification)

    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("L")
    for point in ((825, 314), (920, 314), (1000, 336), (1095, 336)):
        assert image.getpixel(point) == 255


def test_deterministic_pulling_scene_accepts_positive_whole_machine_wording():
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    its right-hand part, leaving a wide open expanse of tile to the left. The machine is shown
    schematically as one plain rectangular body standing on a band that runs round its
    underside, the band alone touching the tile. The rectangular body and the band beneath it
    are the whole of the machine drawn on this sheet. The flexible pulling element 46 is drawn
    as one slack curved path. It begins where it meets the left-hand side of the machine and
    runs away to the left, sagging gently over the open expanse of tile. It lies wholly to the
    left of the machine and nowhere crosses it.
    """

    png = draft_figures._deterministic_pulling_scene_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    for point in ((825, 314), (920, 314), (1000, 336), (1095, 336)):
        assert image.getpixel(point) == 255


def test_deterministic_pulling_scene_accepts_body_and_band_whole_machine_wording():
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    its right-hand part, leaving a wide open expanse of tile to the left. The machine and the
    tile are shown schematically, the machine as one plain rectangular body standing on a band
    that runs round its underside, the band alone touching the tile. The body and the band are
    the whole of the machine drawn on this sheet. The flexible pulling element 46 is drawn as
    one slack curved path, in the manner of a loose cord. It begins where it meets the left-hand
    side of the machine and runs away to the left, sagging gently over the open expanse of tile.
    """

    png = draft_figures._deterministic_pulling_scene_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png


def test_deterministic_pulling_scene_accepts_machine_as_complete_body_and_band_wording():
    specification = """
    The covering element 36 is one large plain tile seen in perspective, filling the lower part
    of the drawing area. The machine stands on its right-hand part, leaving a wide open expanse
    of tile to the left. The machine and the tile are shown schematically, the machine as one
    plain rectangular body standing on a band that runs round its underside, the band alone
    touching the tile, these forms being a depiction convention for this sheet only. The
    flexible pulling element 46 is drawn as one slack curved path, in the manner of a loose cord:
    a single continuous curved line. It begins where it meets the left-hand side of the machine
    and runs away to the left, sagging gently over the open expanse of tile.
    """

    png = draft_figures._deterministic_pulling_scene_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png


def test_deterministic_pulling_scene_accepts_an_outlined_cord_convention():
    specification = """
    The covering element 36 is one large plain tile in perspective. The machine stands on its
    right-hand part with open tile to the left. The machine is one plain rectangular body
    standing on a band that runs round its underside, the band alone touching the tile. The
    flexible pulling element 46 is drawn as a loose cord in outline: one long closed body bounded
    by two roughly parallel curved lines. It begins at the left-hand side of the machine, runs
    away to the left, and sags over the open tile. Its drawn width is a depiction convention.
    """

    png = draft_figures._deterministic_pulling_scene_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.getpixel((445, 489)) == 255
    assert min(image.getpixel((445, y)) for y in range(478, 485)) == 0
    assert min(image.getpixel((445, y)) for y in range(494, 501)) == 0


def test_deterministic_grip_scene_accepts_closed_block_grip_wording():
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    the left-hand part of the tile, leaving a wide open expanse of tile to the right. The machine
    is one plain rectangular slab standing on a band that runs round its underside, with two
    closed housings and a grip on the top face of the slab. The two housings stand on the top
    face of the slab, one at the left and one at the right. The grip stands on the top face
    between them and is a closed block of the same kind. The band meets the underside of the
    slab and follows the same rectangular run.
    """

    png = draft_figures._deterministic_grip_scene_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    for x, y in ((285, 315), (435, 300), (585, 315)):
        assert image.getpixel((x, y)) == 255


def test_deterministic_block_grip_has_one_plain_front_face_and_unbroken_band():
    specification = """
    The covering element 36 is one large plain tile in perspective. The machine stands on its
    left-hand part with open tile to the right. The machine is one plain rectangular slab
    standing on a band that runs round its underside. Three closed blocks stand side by side on
    the top face of the slab. The left-hand block is the vibration motor, the middle block is the
    handle, and the right-hand block is the air-extraction mechanism. The slab has one large
    plain front face and a visible left-hand end. The band has one unbroken front strip across
    the whole width.
    """

    png = draft_figures._deterministic_grip_scene_png(specification)

    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("L")
    center_column = [image.getpixel((435, y)) < 32 for y in range(340, 471)]
    longest_run = 0
    current_run = 0
    for dark in center_column:
        current_run = current_run + 1 if dark else 0
        longest_run = max(longest_run, current_run)
    assert longest_run <= 8, "no center ridge may divide the slab face or band strip"
    assert image.getpixel((435, 365)) == 255
    assert image.getpixel((435, 435)) == 255
    assert image.getpixel((635, 245)) == 255, "the tile edge must not form a peak above the slab"


def _split_clamp_plan_specification():
    return """
    Plan view of the split pipe clamp closed around a pipe, viewed along the pipe axis.
    The pipe 90 is seen end on at the centre as one circle. An annular frame body surrounds it,
    bounded by one inner circle and one outer circle. Two radial joint lines, at the left and at
    the right, divide the body into the first frame half 10 and the second frame half 12. At the
    left joint the hinge 14 is a small circle between the inner and outer circles, with the whole
    hinge lying within the width of the frame body. At the right joint the latch 16 is a
    rectangular block outside the frame body bridging both frame ends, with a lever reaching
    radially outward from it and inclined toward the first frame half 10.

    Three jaw carriages 30 are spaced around the annular frame body, at the top, the lower left
    and the lower right. Each has an outer portion within the frame body and an inner end inside
    the inner circle. Each carriage carries a separate jaw pad 40 at its inner end. Each jaw
    pad has a concave inner face meeting the pipe 90.
    """


def test_deterministic_split_clamp_plan_keeps_hinge_inside_and_pads_on_pipe():
    specification = _split_clamp_plan_specification()

    png = draft_figures._deterministic_split_clamp_plan_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.size == (1400, 900)
    assert min(image.crop((250, 400, 315, 500)).getextrema()) == 255
    assert image.getpixel((320, 450)) < 32
    assert image.getpixel((350, 425)) == 255
    assert image.getpixel((350, 475)) == 255
    assert image.getpixel((395, 450)) == 255
    assert image.getpixel((740, 270)) < 32
    assert image.getpixel((700, 330)) < 32
    assert image.getpixel((700, 326)) == 255
    assert image.getpixel((700, 330)) < 32
    assert min(image.crop((1070, 405, 1140, 495)).getextrema()) == 0


def test_deterministic_split_clamp_plan_accepts_front_elevation_inventory_wording():
    specification = """
    View: front elevation of the clamp closed around a pipe, along the pipe axis.
    A pipe is drawn as one plain circle centered in the drawing area. Around that circle, an
    annular frame body is drawn as an outer boundary and an inner boundary, concentric with the
    pipe circle and spaced outward from it. It is divided into two substantially semicircular
    halves by radial breaks at the left and at the right. At the left break, a hinge is drawn as
    a small circle straddling the two halves. At the right break, a latch is drawn as a compact
    rectangular body attached across the two halves. Three carriage blocks are spaced at roughly
    equal angular intervals. On the inner end of each carriage block, a small block has an inner
    edge that is a concave arc meeting the pipe circle, so that the small block contacts the pipe.
    """

    png = draft_figures._deterministic_split_clamp_plan_png(specification)

    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.getpixel((700, 330)) < 32
    assert image.getpixel((320, 450)) == 255
    assert image.getpixel((470, 450)) == 255
    assert image.getpixel((323, 400)) == 255
    assert image.getpixel((472, 420)) == 255
    assert image.getpixel((700, 254)) == 255
    assert min(image.crop((1180, 520, 1300, 680)).getextrema()) == 0
    assert min(image.crop((1180, 220, 1300, 380)).getextrema()) == 255
    assert min(image.crop((990, 100, 1045, 160)).getextrema()) == 0


def test_deterministic_split_clamp_plan_has_single_joints_and_separate_curved_pads():
    png = draft_figures._deterministic_split_clamp_plan_png(
        _split_clamp_plan_specification())

    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.getpixel((350, 450)) < 32
    assert image.getpixel((350, 425)) == 255
    assert image.getpixel((350, 475)) == 255
    assert image.getpixel((1040, 450)) < 32
    assert image.getpixel((1040, 425)) == 255
    assert image.getpixel((1040, 475)) == 255
    assert image.getpixel((700, 330)) < 32, "the pad inner arc must meet the pipe boundary"
    assert image.getpixel((700, 326)) == 255, "there must be no separate arc outside the pipe"
    assert image.getpixel((700, 330)) < 32, "the pad must meet the pipe's outer circle"
    assert image.getpixel((750, 337)) < 32, "the pad inner face must visibly curve"
    assert image.getpixel((750, 341)) < 32, "the pipe circle must remain independently visible"


def test_deterministic_split_clamp_lever_starts_outside_latch_block():
    png = draft_figures._deterministic_split_clamp_plan_png(
        _split_clamp_plan_specification())

    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert min(image.crop((1070, 415, 1155, 485)).getextrema()) == 255
        assert image.getpixel((700, 680)) < 32


def test_deterministic_split_clamp_plan_uses_exact_component_targets():
    specification = _split_clamp_plan_specification()
    numerals = [
        "100 = split pipe clamp", "10 = first frame half", "12 = second frame half",
        "14 = hinge", "16 = latch", "30 = jaw carriage", "40 = jaw pad", "90 = pipe",
    ]
    png = draft_figures._deterministic_split_clamp_plan_png(specification)
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, grounded)

    positions = {
        item["numeral"]: (item["x"], item["y"])
        for item in grounded["anchors"]
    }
    assert positions["14"] == (
        draft_figures._pixel_to_normalized(395, 1400),
        draft_figures._pixel_to_normalized(450, 900),
    )
    assert positions["30"] == (
        draft_figures._pixel_to_normalized(664, 1400),
        draft_figures._pixel_to_normalized(200, 900),
    )
    assert positions["40"] == (
        draft_figures._pixel_to_normalized(625, 1400),
        draft_figures._pixel_to_normalized(282, 900),
    )
    assert positions["90"] == (
        draft_figures._pixel_to_normalized(700, 1400),
        draft_figures._pixel_to_normalized(450, 900),
    )
    assert grounded["pixel_anchor_audit"]["ok"] is True, json.dumps(
        grounded["pixel_anchor_audit"], indent=2)
    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["renderer"] == "split_clamp_plan"
    assert {item["numeral"] for item in certificate["anchors"]} == {
        "100", "10", "12", "14", "16", "30", "40", "90",
    }


def test_deterministic_split_clamp_top_targets_clear_radial_cutting_line():
    specification = _split_clamp_plan_specification()
    numerals = [
        "100 = split pipe clamp", "10 = first frame half", "12 = second frame half",
        "14 = hinge", "16 = latch", "30 = jaw carriage", "40 = jaw pad", "90 = pipe",
    ]
    raw = draft_figures._deterministic_split_clamp_plan_png(specification)
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]
    semantic = draft_figures._apply_deterministic_anchor_certificate(
        raw, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(raw, numerals, semantic)
    marks = [{
        "designation": "3", "start_x": 500, "start_y": 80,
        "end_x": 500, "end_y": 370, "view_dx": 1, "view_dy": 0,
    }]

    audit = draft_figures._section_mark_anchor_audit(grounded["anchors"], marks)

    assert audit["ok"] is True
    assert audit["colliding_numerals"] == []


def _split_clamp_carriage_section_specification():
    return """
    Enlarged fragmentary section through one jaw carriage, taken on line 3-3 of FIG. 1.
    Cut solid material carries section hatching; the frame body, the segmented cam ring 20,
    the jaw carriage 30 with its follower 32, and the jaw pad 40 each at a slant different
    from that of every other cut element beside it. A hatched body is formed with a groove,
    the annular guide 18, and a channel, the radial guide 38, that opens at the underside.
    A hatched block, the segmented cam ring 20, is received in the groove. One rectangular
    opening, the oblique slot 34, is formed through that block and receives the follower 32.
    A hatched jaw carriage 30 lies in the channel and projects below the body. A zigzag spring
    symbol, the carriage return spring 44, acts between the carriage and the frame body. The
    jaw pad 40 hangs below the carriage on a small circle and its lower face is a concave arc.
    """


def test_deterministic_split_clamp_carriage_section_has_distinct_hatching_and_concave_pad():
    specification = _split_clamp_carriage_section_specification()
    png = draft_figures._deterministic_split_clamp_carriage_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    certificate = draft_figures._deterministic_section_hatch_certificate(
        png, specification)
    angles = [item["angle_degrees"] for item in certificate["components"]]
    assert certificate["renderer"] == "split_clamp_carriage_section"
    assert len(angles) == 5
    assert len(set(angles)) == 5
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert image.getpixel((570, 790)) < 32
        assert image.getpixel((700, 720)) < 32
        assert image.getpixel((700, 790)) == 255


def test_deterministic_split_clamp_carriage_section_certifies_every_target():
    specification = _split_clamp_carriage_section_specification()
    numerals = [
        "18 = annular guide", "20 = segmented cam ring", "30 = jaw carriage",
        "32 = follower", "34 = oblique slot", "38 = radial guide", "40 = jaw pad",
        "44 = carriage return spring",
    ]
    png = draft_figures._deterministic_split_clamp_carriage_section_png(specification)
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]

    semantic = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, semantic)

    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["renderer"] == "split_clamp_carriage_section"
    assert {item["numeral"] for item in certificate["anchors"]} == {
        "18", "20", "30", "32", "34", "38", "40", "44",
    }
    anchors = {item["numeral"]: item for item in certificate["anchors"]}
    assert (anchors["32"]["raw_x"], anchors["32"]["raw_y"]) == (680, 370)
    assert (anchors["34"]["raw_x"], anchors["34"]["raw_y"]) == (620, 320)
    assert (anchors["38"]["raw_x"], anchors["38"]["raw_y"]) == (900, 475)
    assert (anchors["44"]["raw_x"], anchors["44"]["raw_y"]) == (540, 475)
    with Image.open(io.BytesIO(png)).convert("L") as raw:
        assert raw.getpixel((450, 240)) < 32
        assert raw.getpixel((900, 475)) < 32
    grounded_anchors = {item["numeral"]: item for item in grounded["anchors"]}
    assert grounded_anchors["34"]["x"] == draft_figures._pixel_to_normalized(620, 1400)
    assert grounded_anchors["38"]["x"] == draft_figures._pixel_to_normalized(900, 1400)
    assert grounded["pixel_anchor_audit"]["ok"] is True, json.dumps(
        grounded["pixel_anchor_audit"], indent=2)


def _segmented_cam_ring_specification():
    return """
    Plan view of the segmented cam ring removed from the frame, its two segments coupled, viewed
    along the ring axis so the hinge end is at the left and the latch end at the right. A flat
    annulus is bounded by one inner circular boundary and one outer circular boundary. Two
    joints, one at the left and one at the right, divide the annulus into the first cam ring
    segment 22 above and the second cam ring segment 24 below. At the joints the segment ends
    meet along complementary coupling faces 26 and 28.

    Three elongated openings are formed in the annulus, near the top, lower left and lower right.
    Each is an oblique slot 34. The ring drive face 36 is a short straight flat cut into the outer
    boundary of the second cam ring segment 24 near the right joint. Its upper end, the end nearer
    that joint, lies radially inside the outer boundary and stops below the joint, with a short
    piece of circular outer boundary continuing to the joint. Its lower end, further from the
    joint, meets the circular outer boundary. The material cut away is deepest at the upper end
    and runs out to nothing at the lower end.
    """


def _source_clean_segmented_cam_ring_specification():
    return """
    Plan view of the segmented cam ring removed from the frame, its two segments coupled, viewed
    along the ring axis so the hinge end is at the left and the latch end at the right. A flat
    annulus is bounded by one inner circular boundary and one outer circular boundary. Two
    joints, one at the left and one at the right, divide the annulus into the first cam ring
    segment 22 above and the second cam ring segment 24 below. At the joints the segment ends
    meet along complementary coupling faces 26 and 28.

    Three elongated openings are formed in the annulus, near the top, lower left and lower right.
    Each is an oblique slot 34. The ring drive face 36 is shown schematically as one short straight
    flat on the outer boundary of the second cam ring segment 24 near the right joint. Its exact
    position, tilt, length, end positions and runout are depiction conventions for this sheet
    only and are not asserted as invention geometry.
    """


def test_deterministic_segmented_cam_ring_cuts_away_outer_arc_without_a_lens():
    specification = _segmented_cam_ring_specification()

    png = draft_figures._deterministic_segmented_cam_ring_plan_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.getpixel((1025, 507)) < 32
    assert image.getpixel((961, 633)) < 32
    assert image.getpixel((970, 639)) == 255
    assert image.getpixel((733, 130)) == 255


def test_deterministic_segmented_cam_ring_certifies_one_flat_and_circular_return():
    specification = _segmented_cam_ring_specification()
    png = draft_figures._deterministic_segmented_cam_ring_plan_png(specification)

    certificate = draft_figures._deterministic_geometry_certificate(png, specification)

    drive_face = certificate["certified_constraints"]["single_drive_face"]
    assert drive_face["ok"] is True
    assert drive_face["flat_count"] == 1
    assert drive_face["lower_endpoint_on_outer_circle"] is True
    assert drive_face["post_face_arc_degrees"] == [52, 65]
    assert draft_figures._certified_geometry_dissent_categories(
        errors=[
            "An additional short straight facet adjoins the lower end of the ring drive face."
        ],
        missing_geometry=[
            "A ring drive face whose lower end does not merge into the circular outer boundary."
        ],
        missing=[],
        unexpected=[
            "Extra short straight stroke after the drive face, forming a second chamfer facet."
        ],
        duplicates=[],
        certificate=certificate,
    ) == ["single_drive_face"]


def test_source_clean_cam_ring_uses_exact_renderer_and_certifies_visible_inventory():
    specification = _source_clean_segmented_cam_ring_specification()

    png = draft_figures._deterministic_segmented_cam_ring_plan_png(specification)
    certificate = draft_figures._deterministic_geometry_certificate(png, specification)

    assert png is not None
    assert certificate["ok"] is True
    constraints = certificate["certified_constraints"]
    assert constraints["cam_ring_segments_and_joints"] == {
        "ok": True,
        "segment_count": 2,
        "joint_count": 2,
        "joint_centerlines": [[370, 450, 490, 450], [910, 450, 1030, 450]],
    }
    slots = constraints["cam_ring_slot_pattern"]
    assert slots["ok"] is True
    assert slots["slot_count"] == 3
    assert slots["uniform_tangent_relative_tilt_degrees"] == 70
    assert draft_figures._certified_geometry_dissent_categories(
        errors=["The view appears to contain more than two cam-ring segments and three joints."],
        missing_geometry=["The three oblique slots do not all tilt in the same direction."],
        missing=[], unexpected=[], duplicates=[], certificate=certificate,
    ) == ["cam_ring_segments_and_joints", "cam_ring_slot_pattern"]


def test_exact_cam_ring_resolves_independent_slot_and_joint_false_negatives(monkeypatch):
    specification = _source_clean_segmented_cam_ring_specification()
    numerals = [
        "20 = segmented cam ring", "22 = first cam ring segment",
        "24 = second cam ring segment", "26 = complementary coupling faces at the hinge end",
        "28 = complementary coupling faces at the latch end", "34 = oblique slot",
        "36 = ring drive face",
    ]
    png = draft_figures._deterministic_segmented_cam_ring_plan_png(specification)
    spec_hash = draft_figures.specification_hash("FIG. 2", specification, numerals)
    dissent = accepted_cross_provider_geometry_audit(
        ok=False,
        specification_hash=spec_hash,
        errors=["The view appears to contain more than two cam-ring segments and three joints."],
        missing_geometry=["The three oblique slots do not all tilt in the same direction."],
        summary="The deterministic inventory appears wrong.",
    )
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: dissent)
    semantic = {
        "ok": True, "inspected": True, "errors": [], "missing": [], "unexpected": [],
        "duplicates": [], "unexpected_text": [],
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
    }

    audited = draft_figures._apply_cross_provider_geometry_gate(
        semantic, png, label="FIG. 2", caption=specification, numerals=numerals)

    cross = audited["cross_provider_geometry_audit"]
    assert audited["ok"] is True and cross["ok"] is True
    assert cross["reviewer_ok"] is False
    assert cross["consensus_resolution"]["certified_dissent_categories"] == [
        "cam_ring_segments_and_joints", "cam_ring_slot_pattern",
    ]
    assert draft_figures.current_cross_provider_geometry_audit(
        cross, specification_hash=spec_hash) is True


def test_deterministic_segmented_cam_ring_uses_exact_component_anchor_centers():
    specification = _segmented_cam_ring_specification()
    numerals = [
        "20 = segmented cam ring", "22 = first cam ring segment",
        "24 = second cam ring segment", "26 = complementary coupling faces at the hinge end",
        "28 = complementary coupling faces at the latch end", "34 = oblique slot",
        "36 = ring drive face",
    ]
    png = draft_figures._deterministic_segmented_cam_ring_plan_png(specification)
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, grounded)

    positions = {
        item["numeral"]: (item["x"], item["y"])
        for item in grounded["anchors"]
    }
    assert positions["34"] == (
        draft_figures._pixel_to_normalized(700, 1400),
        draft_figures._pixel_to_normalized(180, 900),
    )
    assert positions["36"] == (
        draft_figures._pixel_to_normalized(961, 1400),
        draft_figures._pixel_to_normalized(633, 900),
    )
    assert grounded["pixel_anchor_audit"]["ok"] is True
    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["renderer"] == "segmented_cam_ring_plan"
    assert {item["numeral"] for item in certificate["anchors"]} == {
        "20", "22", "24", "26", "28", "34", "36",
    }


def test_deterministic_block_grip_uses_exact_component_anchor_centers():
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    the left-hand part of the tile, leaving a wide open expanse of tile to the right. The machine
    is one plain rectangular slab standing on a band that runs round its underside, with two
    closed housings and a grip on the top face of the slab. The two housings stand on the top
    face of the slab, one at the left and one at the right. The grip stands on the top face
    between them and is a closed block of the same kind. The band meets the underside of the
    slab and follows the same rectangular run.
    - The vibration device 10 is the whole machine. Identified on the outer boundary at its
      right-hand end.
    - The base 12 is the slab. Identified well inside its broad front face.
    - The vibration motor 18 is the left housing. Identified well inside its front face.
    - The air-extraction mechanism 20 is the right housing. Identified well inside its front face.
    - The perimeter member 24 is the band. Identified well inside its front strip.
    - The covering element 36 is the tile. Identified well inside the open expanse to the right.
    - The handle 44 is the grip. Identified well inside its front face.
    """
    numerals = [
        "10 = vibration device", "12 = base", "18 = vibration motor",
        "20 = air-extraction mechanism", "24 = perimeter member",
        "36 = covering element", "44 = handle",
    ]
    png = draft_figures._deterministic_grip_scene_png(specification)
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, grounded)

    positions = {
        item["numeral"]: (item["x"], item["y"])
        for item in grounded["anchors"]
    }
    assert positions["10"] == (
        draft_figures._pixel_to_normalized(685, 1400),
        draft_figures._pixel_to_normalized(365, 900),
    )
    assert positions["12"] == (
        draft_figures._pixel_to_normalized(435, 1400),
        draft_figures._pixel_to_normalized(365, 900),
    )
    assert positions["44"] == (
        draft_figures._pixel_to_normalized(435, 1400),
        draft_figures._pixel_to_normalized(305, 900),
    )
    assert grounded["pixel_anchor_audit"]["ok"] is True
    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["ok"] is True
    assert {item["numeral"] for item in certificate["anchors"]} == {
        "10", "12", "18", "20", "24", "36", "44"}


def test_deterministic_stirring_scene_grounds_the_stirring_element_inside_its_block():
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    its left-hand part, with open tile to the right. The machine is shown schematically as one
    plain rectangular body standing on a band that runs round its underside, the band alone
    touching the tile. Two small closed blocks, each a stirring element 48, are carried by the
    machine against the upper part of the front face of the rectangular body, clear above the
    band, each broad enough for a point to stand well inside its front face. Their number, form
    and drawn position are a depiction convention for this sheet only.
    """
    numerals = [
        "10 = vibration device", "36 = covering element", "48 = stirring element",
    ]
    png = draft_figures._deterministic_grip_scene_png(specification)
    initial = [
        {"numeral": value.split(" = ", 1)[0], "x": 500, "y": 500, "visible": True}
        for value in numerals
    ]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, grounded)

    assert png is not None
    positions = {
        item["numeral"]: (item["x"], item["y"])
        for item in grounded["anchors"]
    }
    assert positions["48"] == (
        draft_figures._pixel_to_normalized(310, 1400),
        draft_figures._pixel_to_normalized(335, 900),
    )
    assert grounded["pixel_anchor_audit"]["ok"] is True
    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["renderer"] == "stirring_element_scene"
    assert {item["numeral"] for item in certificate["anchors"]} == {"10", "36", "48"}


def test_deterministic_stirring_scene_accepts_current_filing_brief_wording():
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    its left-hand part, with open tile to the right. The machine and the tile are shown
    schematically, the machine as one plain rectangular body standing on a band that runs round
    its underside, the band alone touching the tile. Two small closed blocks, each a stirring
    element 48, are drawn carried by the machine on the front face of the rectangular body, each
    drawn broad enough for a point to stand well inside its front face. Their number, form and
    drawn position on this sheet are a depiction convention for this sheet only.
    """

    png = draft_figures._deterministic_grip_scene_png(specification)

    assert png is not None
    certificate = draft_figures._deterministic_geometry_certificate(png, specification)
    assert certificate["ok"] is True


@pytest.mark.parametrize(("specification", "numerals", "renderer"), [
    (
        """
        View: schematic block diagram of the charging control system. A branch conductor 102
        passes through a branch current sensor 104 and supplies a first connector station 110,
        a second connector station 112, and a non-charging load 114. An edge controller 100 is
        joined to the sensor and to a network interface 108. An isolated local bus 106 joins the
        edge controller to the first and second connector stations.
        """,
        [
            "100 = edge controller", "102 = branch conductor",
            "104 = branch current sensor", "106 = isolated local bus",
            "108 = network interface", "110 = first connector station",
            "112 = second connector station", "114 = non-charging load",
        ],
        "charging_control_overview",
    ),
    (
        """
        View: enlarged schematic block diagram of the first connector station. The first
        connector station 110 encloses a first contactor 120, a first connector current sensor
        122, a first control-pilot interface 124, and a first electric-vehicle connector 126.
        A branch conductor 102 forms the power path and an isolated local bus 106 branches to
        the enclosed components.
        """,
        [
            "102 = branch conductor", "106 = isolated local bus",
            "110 = first connector station", "120 = first contactor",
            "122 = first connector current sensor",
            "124 = first control-pilot interface",
            "126 = first electric-vehicle connector",
        ],
        "connector_station",
    ),
    (
        """
        View: enlarged schematic block diagram of the edge controller. The edge controller 100
        contains nonvolatile memory 132 and joins a branch conductor 102 through a branch current
        sensor 104. A network interface 108, local fault indicator 134, service input 136, and
        isolated local bus 106 join the edge controller.
        """,
        [
            "100 = edge controller", "102 = branch conductor",
            "104 = branch current sensor", "106 = isolated local bus",
            "108 = network interface", "132 = nonvolatile memory",
            "134 = local fault indicator", "136 = service input",
        ],
        "edge_controller",
    ),
    (
        """
        View: process flow diagram of the allocation interval. The available-charging-current
        determination step 200 leads through the minimum sustaining current assignment step 202,
        deficit-based distribution step 204, and limit transmission and connector current
        verification step 206 to the pilot reduction step 208, ordered contactor shedding step
        210, and reclose permissive step 212. A welded-contactor isolation step 214 branches from
        the shedding step.
        """,
        [
            "200 = available-charging-current determination step",
            "202 = minimum sustaining current assignment step",
            "204 = deficit-based distribution step",
            "206 = limit transmission and connector current verification step",
            "208 = pilot reduction step", "210 = ordered contactor shedding step",
            "212 = reclose permissive step", "214 = welded-contactor isolation step",
        ],
        "allocation_flow",
    ),
    (
        """
        A flat schematic system diagram. A dashed rectangle, the charging installation,
        encloses the whole diagram. A branch conductor passes through a branch current sensor
        and supplies a first connector channel, a second connector channel, and a non-charging
        load in parallel. Its left end stops short of the enclosure and its right end meets the
        enclosure without crossing it. An edge controller is joined to the branch current sensor.
        An isolated local bus runs from a point vertically below the edge controller to a point
        vertically below the second connector channel and connects only the two connector channels.
        """,
        [
            "100 = charging installation", "102 = branch conductor",
            "104 = branch current sensor", "106 = edge controller",
            "108 = isolated local bus", "118 = non-charging load",
            "120 = first connector channel", "140 = second connector channel",
        ],
        "charging_installation_flat",
    ),
    (
        """
        Flat schematic of one connector channel. One dashed rectangle, and exactly one, is the
        first connector channel. An incoming branch supply passes through a contactor, turns
        right through a connector current sensor, and reaches a vehicle connector that straddles
        the dashed rectangle. A control-pilot interface joins the vehicle connector. An electric
        vehicle is right of the dashed rectangle. An isolated local bus enters from the left. A
        line from the connector current sensor exits downward independently of the local bus.
        """,
        [
            "108 = isolated local bus", "120 = first connector channel",
            "122 = contactor", "124 = connector current sensor",
            "126 = control-pilot interface", "128 = vehicle connector",
            "130 = electric vehicle",
        ],
        "connector_channel_flat",
    ),
    (
        """
        A flat block diagram of the edge controller. One large rectangle is the edge controller.
        Three smaller empty rectangles lie inside it: a network interface in the upper region,
        a nonvolatile memory in the lower region, and a service input in the left region. A local
        fault indicator stands outside the large rectangle. A line from the network interface runs
        left and crosses the left side, a line from the service input runs upward and crosses the
        upper side, and two short
        solid lines extend downward from the lower side of the large rectangle.
        """,
        [
            "106 = edge controller", "110 = network interface",
            "112 = nonvolatile memory", "114 = service input",
            "116 = local fault indicator",
        ],
        "edge_controller_flat",
    ),
    (
        """
        A flat process flow diagram. Eight empty shapes with blank interiors stand in one vertical
        column. In order they are four rectangles, a diamond, a rectangle, a diamond, and a
        rectangle. The upper diamond has a left return path to the topmost rectangle. The lower
        diamond branches right to a solid square terminator. The bottom rectangle has a right
        return path to the topmost rectangle.
        """,
        [
            "202 = available current determination step",
            "204 = sustaining and deficit assignment step", "206 = pilot command step",
            "208 = connector verification step", "210 = staged reduction step",
            "212 = ordered shedding step", "214 = welded-contactor isolation step",
            "216 = conditional reclosure step",
        ],
        "allocation_flow_vertical",
    ),
])
def test_deterministic_control_diagrams_are_text_free_and_anchor_every_part(
        specification, numerals, renderer):
    png = draft_figures._deterministic_control_diagram_png(specification)
    initial = [
        {"numeral": value.split(" = ", 1)[0], "x": 500, "y": 500, "visible": True}
        for value in numerals
    ]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, grounded)

    assert png is not None
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (1400, 900)
    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["renderer"] == renderer
    assert {item["numeral"] for item in certificate["anchors"]} == {
        value.split(" = ", 1)[0] for value in numerals
    }
    positions = {(item["x"], item["y"]) for item in grounded["anchors"]}
    assert len(positions) == len(numerals)
    assert grounded["pixel_anchor_audit"]["ok"] is True, grounded["pixel_anchor_audit"]
    assert draft_figures._deterministic_geometry_certificate(
        png, specification)["ok"] is True


def test_flat_charging_installation_template_tracks_reviewed_endpoint_geometry():
    specification = """
    A flat schematic system diagram. A dashed rectangle, the charging installation, encloses the
    whole diagram. A branch conductor passes through a branch current sensor and supplies a first
    connector channel, a second connector channel, and a non-charging load. The left end stops
    short of the dashed rectangle and the right end meets it without crossing. An edge controller
    is joined to the sensor. An isolated local bus runs from a point vertically below the edge
    controller to a point vertically below the second connector channel. Two connector channels
    connect to that bus while the non-charging load does not.
    """

    png = draft_figures._deterministic_control_diagram_png(specification)
    assert png is not None
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert image.getpixel((1290, 180)) < 64
        assert image.getpixel((1320, 180)) < 64
        assert image.getpixel((1350, 180)) > 240
        assert image.getpixel((300, 780)) < 64
        assert image.getpixel((935, 780)) < 64
        assert image.getpixel((980, 780)) > 240


def test_flat_edge_controller_template_tracks_current_port_directions():
    specification = """
    A flat block diagram of the edge controller. One large rectangle is the edge controller.
    Three smaller empty rectangles lie inside it: a network interface in the upper region, a
    nonvolatile memory in the lower region, and a service input in the left region. A local fault
    indicator stands outside the large rectangle. A line from the network interface runs left and
    crosses the left side, a line from the service input runs upward and crosses the upper side,
    and two short solid lines extend downward from the lower side of the large rectangle.
    """

    png = draft_figures._deterministic_control_diagram_png(specification)
    assert png is not None
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert image.getpixel((200, 250)) < 64
        assert image.getpixel((660, 70)) > 240
        assert image.getpixel((420, 80)) < 64
        assert image.getpixel((200, 410)) > 240


def test_flat_edge_controller_template_tracks_swapped_independent_port_directions():
    specification = """
    A flat block diagram of the edge controller. One large rectangle, the edge controller,
    occupies the left and central portion of the drawing area. Three smaller empty rectangles
    lie inside it: a network interface in the upper region, a service input in the left region,
    and a nonvolatile memory in the lower region. A local fault indicator stands outside the
    large rectangle. A short solid line runs upward from the network interface rectangle and
    crosses the upper side of the large rectangle. A short solid line runs leftward from the
    service input rectangle and crosses the left side of the large rectangle. Two short solid
    lines extend downward from the lower side of the large rectangle, spaced well apart.
    """

    png = draft_figures._deterministic_control_diagram_png(specification)

    assert png is not None
    with Image.open(io.BytesIO(png)).convert("L") as image:
        # Each inner port reaches its requested controller boundary directly.
        assert image.getpixel((660, 150)) < 64
        assert image.getpixel((660, 90)) < 64
        assert image.getpixel((300, 410)) < 64
        assert image.getpixel((210, 410)) < 64
        # The old swapped paths and their implied T-junction remain absent.
        assert image.getpixel((520, 250)) > 240
        assert image.getpixel((420, 330)) > 240

    certificate = draft_figures._deterministic_geometry_certificate(png, specification)
    constraints = certificate["certified_constraints"]
    assert constraints["controller_network_interface_path"]["ok"] is True
    assert constraints["controller_service_input_path"]["ok"] is True
    assert constraints["controller_boundary_ports"]["ok"] is True


def test_flat_edge_controller_template_terminates_ports_on_requested_boundaries():
    specification = """
    A flat block diagram of the edge controller. One large rectangle, the edge controller,
    occupies the left and central portion of the drawing area. Three smaller empty rectangles
    lie inside it: a network interface in the upper region, a service input in the left region,
    and a nonvolatile memory in the lower region. A local fault indicator stands outside the
    large rectangle. A straight vertical line originates on the top side of the network
    interface rectangle, runs upward, and terminates on the upper boundary of the large edge
    controller rectangle. A straight horizontal line originates on the left side of the service
    input rectangle, runs leftward, and terminates on the left boundary of the large edge
    controller rectangle. Two short solid lines extend downward from the lower side of the large
    rectangle, spaced well apart.
    """

    png = draft_figures._deterministic_control_diagram_png(specification)

    assert png is not None
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert image.getpixel((660, 120)) < 64
        assert image.getpixel((660, 90)) > 240
        assert image.getpixel((250, 410)) < 64
        assert image.getpixel((210, 410)) > 240
        assert image.getpixel((520, 250)) > 240
        assert image.getpixel((420, 330)) > 240

    constraints = draft_figures._deterministic_geometry_certificate(
        png, specification)["certified_constraints"]
    assert constraints["controller_network_interface_path"]["ok"] is True
    assert constraints["controller_service_input_path"]["ok"] is True
    assert constraints["controller_boundary_ports"]["ok"] is True


def test_flat_edge_controller_template_understands_from_to_boundary_paths():
    specification = """
    A flat block diagram of the edge controller. One large rectangle, the edge controller,
    occupies the left and central portion of the drawing area. Three smaller empty rectangles
    lie inside it: a network interface in the upper region, a service input in the left region,
    and a nonvolatile memory in the lower region. A local fault indicator stands outside the
    large rectangle. A straight solid vertical line runs from the top side of the network
    interface to the upper boundary of the edge controller. A straight solid horizontal line
    runs from the left side of the service input to the left boundary of the edge controller.
    Both lines terminate on the named boundaries and do not cross them. Two short solid lines
    extend downward from the lower side of the large rectangle, spaced well apart.
    """

    png = draft_figures._deterministic_control_diagram_png(specification)

    assert png is not None
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert image.getpixel((660, 120)) < 64
        assert image.getpixel((660, 90)) > 240
        assert image.getpixel((250, 410)) < 64
        assert image.getpixel((210, 410)) > 240
        assert image.getpixel((520, 250)) > 240
        assert image.getpixel((420, 330)) > 240

    constraints = draft_figures._deterministic_geometry_certificate(
        png, specification)["certified_constraints"]
    assert constraints["controller_network_interface_path"]["direction"] == "up"
    assert constraints["controller_service_input_path"]["direction"] == "left"
    assert all(item["ok"] is True for item in constraints.values())


def _flat_allocation_flow_specification():
    return """
    A flat process flow diagram in plain black line work on white. Eight empty shapes with blank
    interiors stand in one vertical column down the centre of the drawing area, evenly spaced
    from top to bottom. In order from the top: rectangle, rectangle, rectangle, rectangle,
    diamond, rectangle, diamond, rectangle. A vertical solid line with an arrowhead at its lower
    end joins each shape to the one below. A short horizontal line leaves the right vertex of the
    lower diamond, runs right, and ends in an arrowhead at a small solid square terminator. The
    left return path leaves the left vertex of the upper diamond, runs left, rises alongside the
    column, then runs right into the left side of the topmost rectangle. The right return path
    leaves the right side of the bottom rectangle, runs right, rises clear of the terminator and
    column, then runs left into the right side of the topmost rectangle.
    """


def _split_allocation_flow_first_specification():
    return """
    A flat process flow diagram in plain black line work on white. A column of five empty shapes
    with blank interiors is arranged vertically in the center. The first, second, third, and
    fourth shapes from the top are rectangles. The fifth shape from the top is a diamond. A
    vertical solid line with an arrowhead at its lower end joins each shape to the one below it.
    A left return path leaves the left vertex of the fifth shape, runs left, rises alongside the
    column, and runs right into the left side of the topmost rectangle. From the lower vertex of
    the fifth shape, a line with an arrowhead points downward to a small empty circle. The circle
    contains the capital letter 'A' and indicates continuation of the process in FIG. 5.
    """


def _split_allocation_flow_second_specification():
    return """
    A flat process flow diagram continues from FIG. 4, starting with a small empty circle labeled
    'A' at the top center. A column of five empty shapes with blank interiors stands below it.
    The first shape is a rectangle. The second shape is a diamond. The third shape is a
    rectangle. The fourth shape is a diamond. The fifth and bottommost shape is a rectangle. A
    vertical solid line with an arrowhead at its lower end joins the connector to the first shape
    and each shape to the one below. A line leaves the left vertex of the second shape and runs
    left. A short horizontal line leaves the right vertex of the fourth shape, runs right, and
    ends in an arrowhead at a small solid square terminator. A right return path starts at the
    right side of the bottom rectangle, runs right, rises clear of the column, and turns left.
    """


def test_split_allocation_flow_templates_certify_connector_and_every_route():
    first = draft_figures._deterministic_control_diagram_png(
        _split_allocation_flow_first_specification())
    second = draft_figures._deterministic_control_diagram_png(
        _split_allocation_flow_second_specification())

    assert first is not None and second is not None
    for png, specification, renderer in (
        (first, _split_allocation_flow_first_specification(), "allocation_flow_split_first"),
        (second, _split_allocation_flow_second_specification(), "allocation_flow_split_second"),
    ):
        certificate = draft_figures._deterministic_geometry_certificate(png, specification)
        assert certificate["ok"] is True and certificate["renderer"] == renderer
        constraints = certificate["certified_constraints"]
        assert constraints["allocation_flow_shape_sequence"]["ok"] is True
        assert constraints["allocation_flow_vertical_connections"]["ok"] is True
        assert constraints["allocation_flow_connector"]["ok"] is True
        assert all(
            item.get("required") is False or item.get("ok") is True
            for item in constraints.values())


def test_split_allocation_flow_templates_accept_filing_clean_caption_variants():
    first = """
    A flat process flow diagram in plain black line work on white. A column of five empty shapes
    with blank interiors is arranged vertically in the center. The first, second, third, and
    fourth shapes from the top are rectangles. The fifth shape from the top is a diamond. From
    the lower vertex of the fifth shape, a line points downward to a small circle. The circle
    contains the capital letter A and indicates continuation of the process in FIG. 5.
    """
    second = """
    A flat process flow diagram in plain black line work on white. The diagram continues from
    FIG. 4, starting with a small empty circle labeled A. A column of five empty shapes is below
    it. The second shape is a diamond. The fourth shape is a diamond. The fifth and bottommost
    shape is a rectangle.
    """

    assert draft_figures._control_diagram_kind(first) == "allocation_flow_split_first"
    assert draft_figures._control_diagram_kind(second) == "allocation_flow_split_second"
    assert draft_figures._deterministic_control_diagram_png(first) is not None
    assert draft_figures._deterministic_control_diagram_png(second) is not None


def test_flat_allocation_flow_template_certifies_shape_order_and_every_route():
    specification = _flat_allocation_flow_specification()

    png = draft_figures._deterministic_control_diagram_png(specification)
    certificate = draft_figures._deterministic_geometry_certificate(png, specification)

    assert png is not None and certificate["ok"] is True
    constraints = certificate["certified_constraints"]
    assert constraints["allocation_flow_shape_sequence"]["ok"] is True
    assert constraints["allocation_flow_vertical_connections"]["ok"] is True
    assert constraints["allocation_flow_left_return"]["ok"] is True
    assert constraints["allocation_flow_right_return"]["ok"] is True
    assert constraints["allocation_flow_weld_branch"]["ok"] is True


def test_exact_allocation_flow_resolves_unassignable_blank_step_shapes(monkeypatch):
    specification = _flat_allocation_flow_specification()
    numerals = [
        "202 = available current determination step",
        "204 = sustaining and deficit assignment step",
        "206 = pilot command step", "208 = connector verification step",
        "210 = staged reduction step", "212 = ordered shedding step",
        "214 = welded-contactor isolation step", "216 = conditional reclosure step",
    ]
    png = draft_figures._deterministic_control_diagram_png(specification)
    spec_hash = draft_figures.specification_hash("FIG. 4", specification, numerals)
    dissent = accepted_cross_provider_geometry_audit(
        ok=False, specification_hash=spec_hash,
        missing=["202", "204", "206", "208", "210", "212", "214", "216"],
        summary="The blank shapes are visible, but their semantic step identities are unverified.",
    )
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: dissent)
    semantic = {
        "ok": True, "inspected": True, "errors": [], "missing": [], "unexpected": [],
        "duplicates": [], "unexpected_text": [],
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "expected": [str(value) for value in range(202, 218, 2)],
        "visible": [str(value) for value in range(202, 218, 2)],
        "anchors": [
            {"numeral": str(value), "x": 500, "y": 500, "visible": True,
             "evidence": "The requested flow shape is visible in its specified slot."}
            for value in range(202, 218, 2)
        ],
        "specification_hash": spec_hash,
    }

    audited = draft_figures._apply_cross_provider_geometry_gate(
        semantic, png, label="FIG. 4", caption=specification, numerals=numerals)

    cross = audited["cross_provider_geometry_audit"]
    assert audited["ok"] is True and cross["ok"] is True
    assert cross["missing"] == []
    assert cross["reviewer_missing"] == dissent["missing"]
    assert cross["consensus_resolution"]["certified_dissent_categories"] == [
        "allocation_flow_shape_sequence"]
    assert draft_figures.current_cross_provider_geometry_audit(
        cross, specification_hash=spec_hash) is True
    tampered = json.loads(json.dumps(cross))
    tampered["reviewer_missing"] = ["999"]
    assert draft_figures.current_cross_provider_geometry_audit(
        tampered, specification_hash=spec_hash) is False


def test_exact_split_flow_resolves_only_certified_step_and_connector_dissent(monkeypatch):
    specification = _split_allocation_flow_first_specification()
    numerals = [
        "202 = available current determination step",
        "204 = sustaining and deficit assignment step",
        "206 = pilot command step", "208 = connector verification step",
        "209 = branch overcurrent detection step",
    ]
    png = draft_figures._deterministic_control_diagram_png(specification)
    spec_hash = draft_figures.specification_hash("FIG. 4", specification, numerals)
    missing = ["202", "204", "206", "208", "209"]
    dissent = accepted_cross_provider_geometry_audit(
        ok=False, specification_hash=spec_hash, missing=missing,
        missing_geometry=[
            "The continuation connector circle is missing the capital letter A."],
        summary="The blank shapes and continuation connector could not be assigned.",
    )
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_geometry", lambda *a, **k: dissent)
    semantic = {
        "ok": True, "inspected": True, "errors": [], "missing": [], "unexpected": [],
        "duplicates": [], "unexpected_text": [],
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
        "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
        "expected": missing, "visible": missing,
        "anchors": [
            {"numeral": value, "x": 500, "y": 500, "visible": True,
             "evidence": "The requested flow shape is visible in its specified slot."}
            for value in missing
        ],
        "specification_hash": spec_hash,
    }

    audited = draft_figures._apply_cross_provider_geometry_gate(
        semantic, png, label="FIG. 4", caption=specification, numerals=numerals)

    cross = audited["cross_provider_geometry_audit"]
    assert audited["ok"] is True and cross["ok"] is True
    assert cross["missing"] == [] and cross["missing_geometry"] == []
    assert cross["consensus_resolution"]["certified_dissent_categories"] == [
        "allocation_flow_connector", "allocation_flow_shape_sequence"]
    assert draft_figures.current_cross_provider_geometry_audit(
        cross, specification_hash=spec_hash) is True


def test_flat_edge_controller_exact_paths_classify_visual_dissent():
    specification = """
    A flat block diagram of the edge controller. One large rectangle, the edge controller,
    occupies the left and central portion of the drawing area. Three smaller empty rectangles
    lie inside it: a network interface in the upper region, a service input in the left region,
    and a nonvolatile memory in the lower region. A local fault indicator stands outside the
    large rectangle. A short solid line runs upward from the network interface rectangle and
    crosses the upper side of the large rectangle. A short solid line runs leftward from the
    service input rectangle and crosses the left side of the large rectangle. Two short solid
    lines extend downward from the lower side of the large rectangle, spaced well apart.
    """
    png = draft_figures._deterministic_control_diagram_png(specification)
    certificate = draft_figures._deterministic_geometry_certificate(png, specification)

    categories = draft_figures._certified_geometry_dissent_categories(
        errors=[
            "The connection from the network interface runs leftward to a junction instead of "
            "upward across the controller boundary.",
            "The service input path runs upward to a T-junction instead of directly leftward.",
            "An extra line extends downward from the edge-controller boundary.",
        ],
        missing_geometry=[], missing=[], unexpected=[], duplicates=[],
        certificate=certificate,
    )

    assert categories == [
        "controller_boundary_ports",
        "controller_network_interface_path",
        "controller_service_input_path",
    ]


def test_current_geometry_binding_rejects_generated_pixels_for_an_exact_controller_brief(
        monkeypatch):
    specification = """
    A flat block diagram of the edge controller. One large rectangle, the edge controller,
    occupies the left and central portion of the drawing area. Three smaller empty rectangles
    lie inside it: a network interface in the upper region, a service input in the left region,
    and a nonvolatile memory in the lower region. A local fault indicator stands outside the
    large rectangle. A short solid line runs upward from the network interface rectangle and
    crosses the upper side of the large rectangle. A short solid line runs leftward from the
    service input rectangle and crosses the left side of the large rectangle. Two short solid
    lines extend downward from the lower side of the large rectangle, spaced well apart.
    """
    exact = draft_figures._deterministic_geometry_png(specification)
    assert exact is not None
    monkeypatch.setattr(
        draft_figures, "png_bytes",
        lambda *_args, **_kwargs: ("image/png", exact))

    figure = {"id": 77, "active_version": 1}
    generated = {"version_no": 1, "source_kind": "generated"}
    deterministic = {"version_no": 1, "source_kind": "deterministic"}

    assert not draft_figures.current_geometry_binding(
        figure, 4, generated, specification)
    assert draft_figures.current_geometry_binding(
        figure, 4, deterministic, specification)


def test_connector_station_bus_anchor_is_below_and_left_of_enclosure():
    specification = """
    View: enlarged schematic block diagram of the first connector station. The first
    connector station 110 encloses a first contactor 120, a first connector current sensor
    122, a first control-pilot interface 124, and a first electric-vehicle connector 126.
    A branch conductor 102 forms the power path and an isolated local bus 106 branches to
    the enclosed components. End the leader for the isolated local bus 106 on the left
    portion of that line, at a point both below the enclosing rectangle and to the left of it.
    """

    kind, anchors = draft_figures._deterministic_control_diagram_anchors(specification)

    assert kind == "connector_station"
    x, y, evidence = anchors["isolated local bus"]
    assert x < 200
    assert y > 720
    assert "below and left of the station" in evidence


def test_connector_station_bus_is_one_continuous_path_and_interface_is_lower_middle():
    specification = """
    View: enlarged schematic block diagram of the first connector station. The first
    connector station 110 encloses a first contactor 120, a first connector current sensor
    122, a first control-pilot interface 124, and a first electric-vehicle connector 126.
    A branch conductor 102 forms the power path. A horizontal isolated local bus 106 lies
    below the enclosing rectangle, extends to its left, rises through its lower side, and
    branches inside to the contactor, current sensor, and control-pilot interface.
    """

    png = draft_figures._deterministic_control_diagram_png(specification)
    assert png is not None
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert image.getpixel((150, 800)) < 64
        assert image.getpixel((1000, 800)) > 240
        assert image.getpixel((720, 535)) < 64
        assert image.getpixel((900, 535)) < 64
        assert image.getpixel((810, 620)) < 64
        assert image.getpixel((880, 400)) < 64


def test_split_clamp_renderer_accepts_current_filing_brief_wording():
    specification = """
    Plan view of the split pipe clamp closed around a pipe, viewed along the pipe axis.
    An annular frame body surrounds the pipe, bounded by an inner circle and by an outer
    circle. Two radial joint lines divide it into two semicircular bodies. At the left joint
    the hinge is shown schematically as a small circle on that joint line between the inner
    circle and the outer circle. At the right joint the latch is a rectangular block outside
    the frame body bridging both frame ends. Three jaw carriages are spaced around the body.
    Each jaw carriage carries a separate member, the jaw pad, whose inner face is a concave
    arc meeting the pipe.
    """

    png = draft_figures._deterministic_split_clamp_plan_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_certificate(
        png, specification)["ok"] is True


def test_segmented_cam_ring_renderer_accepts_current_filing_brief_wording():
    specification = """
    Plan view of the segmented cam ring removed from the frame, its two segments drawn in the
    relative positions they occupy when coupled. A flat annulus has one inner circular boundary
    and one outer circular boundary. Three elongated openings are formed in the annulus. Each is
    an oblique slot, and all three tilt the same way. The ring drive face is a short plain straight
    face on the end region of one segment at the right joint. It is drawn within the width of the
    annulus, and both circular boundaries of the annulus run unbroken. The face is shown
    schematically.
    """

    png = draft_figures._deterministic_segmented_cam_ring_plan_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_certificate(
        png, specification)["ok"] is True
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert image.getpixel((1030, 450)) < 64
        assert image.getpixel((970, 415)) < 64


def test_segmented_cam_ring_renderer_accepts_source_repaired_ring_without_drive_face():
    specification = """
    Plan view of the segmented cam ring removed from the frame, its two segments drawn in the
    relative positions they occupy when coupled in the closed condition, viewed along the ring
    axis. A flat annulus is bounded by one inner circular boundary and one outer circular
    boundary. Two joints, one at the left and one at the right, divide the annulus into two
    arcuate segments. Three elongated openings are formed in the annulus, one near the top, one
    at the lower left, one at the lower right. Each is an oblique slot, a long narrow opening
    lying within the annulus width and inclined to the radius at its own position. The three are
    alike and all tilt the same way. Features other than the two joints and three slots are not
    designated in this view.
    """

    png = draft_figures._deterministic_segmented_cam_ring_plan_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert image.getpixel((1030, 450)) < 64
        assert image.getpixel((1010, 563)) < 64


def test_chamber_renderer_accepts_current_two_hatched_legs_wording():
    specification = """
    The sheet shows four schematic bodies and one broken line: one hatched horizontal slab,
    the base 12; one closed loop cut twice, appearing as two hatched legs hanging from the
    underside of the slab; one hatched band across the bottom, the covering element 36; and
    one closed housing standing on the slab. One broken line runs from inside the housing to
    the chamber 22. The slab, the legs and the band each have hatching at different slopes.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    certificate = draft_figures._deterministic_section_hatch_certificate(png, specification)
    assert certificate is not None
    angles = {
        item["angle_degrees"]
        for item in certificate["components"]
        if item["component"] in {
            "base slab", "left perimeter leg", "covering-element band"}
    }
    assert len(angles) == 3


def test_chamber_renderer_accepts_source_repaired_four_body_inventory_without_line():
    specification = """
    The sheet shows four schematic bodies: one hatched horizontal slab, the base 12; one closed
    loop cut twice, appearing as two hatched legs hanging from the underside of the slab, one at
    each end and flush with it, so the loop runs at the perimeter of the underside; one hatched
    band across the bottom, the covering element 36, on which the legs stand; and one closed
    housing standing on the slab, the air-extraction mechanism 20.

    The slab, the legs and the band are the cut bodies, each filled with regularly spaced parallel
    hatching. Where two of them meet, a plain solid line is drawn along the join, so the slab, each
    leg and the band each read as a separate body with its own hatched interior. The housing lies
    outside the cut, in plain outline with open paper inside. The slab is drawn thick, its upper
    face visible each side of the housing, and each leg rests on the band.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert sum(image.getpixel((865, y)) < 32 for y in range(120, 205)) == 0
        assert sum(image.getpixel((865, y)) < 32 for y in range(380, 560)) == 0


def test_chamber_renderer_accepts_current_brief_with_a_physical_through_slab_gap():
    specification = """
    The sheet shows four schematic bodies: one hatched horizontal slab, the base 12; one closed
    loop cut twice, appearing as two hatched legs hanging from the underside of the slab, one at
    each end; one hatched band across the bottom, the covering element 36, on which the legs
    stand; and one housing standing on the slab, the air-extraction mechanism 20.

    The slab, the legs and the band are the cut bodies. Where two meet, a plain solid line runs
    along the join, so each reads as a separate hatched body. The housing lies outside the cut,
    in plain unhatched outline. A plain unhatched gap runs through the slab beneath the housing,
    from the inside of the housing to the chamber 22, so that the air-extraction mechanism 20 is
    in fluid communication with the chamber 22. The slab is hatched falling to the right, both
    legs are hatched rising to the right, and the band is hatched falling to the right more
    steeply than the slab.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert image.getpixel((865, 290)) == 255
        assert image.getpixel((835, 290)) < 64
        assert image.getpixel((895, 290)) < 64
    certificate = draft_figures._deterministic_section_hatch_certificate(png, specification)
    assert [item["angle_degrees"] for item in certificate["components"]] == [45, -45, -45, 70]


def test_fragmentary_renderer_accepts_current_complete_lower_area_wording():
    specification = """
    The sheet shows four hatched bodies: one upright column and three horizontal bands beneath
    it. The three bands are stacked in the lower part of the drawing area. Each runs across the
    drawing area, ending just inside its left and right limits, filled with regularly spaced
    parallel hatching continuous from side to side, including beneath the column. The uppermost
    band is the covering element 36, hatched falling to the right; the middle band is the bonding
    material 40, hatched rising to the right more steeply; the lowest is the substrate 42, hatched
    falling to the right less steeply than the covering element. The column is the perimeter
    member 24. It stands above the uppermost band, an open stretch of that band on each side of
    it, rising from one horizontal line closing it below to just inside the upper limit of the
    drawing area, hatched rising to the right. Between the bottom line of the column and the top
    line of the uppermost band lies open unhatched space.
    """

    png = draft_figures._deterministic_fragmentary_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    certificate = draft_figures._deterministic_section_hatch_certificate(png, specification)
    assert [item["angle_degrees"] for item in certificate["components"]] == [-45, 45, -70, 30]


def test_chamber_renderer_accepts_two_explicit_fluid_line_segments():
    specification = """
    The sheet shows four schematic bodies and two broken lines: one hatched horizontal slab,
    the base 12; one closed loop cut twice, appearing as two hatched legs hanging from the
    underside of the slab, one at each end and flush with it; one hatched band across the
    bottom, the covering element 36, on which the legs stand; and one closed housing standing
    on the slab, the air-extraction mechanism 20.

    Two separate short broken lines together indicate schematically the fluid communication
    between the air-extraction mechanism 20 and the chamber 22. The upper one lies wholly
    within the housing, running down to the upper face of the base 12 and ending there. The
    lower one lies wholly within the chamber 22, beginning just below the lower face of the
    base 12. The hatched slab between them carries no broken line, so no passage through the
    base 12 is asserted.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    certificate = draft_figures._deterministic_chamber_constraint_certificate(
        png, specification)
    assert certificate["split_line"]["required"] is True
    assert certificate["split_line"]["ok"] is True
    with Image.open(io.BytesIO(png)).convert("L") as image:
        assert sum(image.getpixel((865, y)) < 32 for y in range(225, 356)) < 40
        assert sum(image.getpixel((865, y)) < 32 for y in range(369, 521)) >= 110


def test_deterministic_block_grip_uses_designated_left_device_boundary():
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    the left-hand part of the tile, leaving a wide open expanse of tile to the right. The machine
    is one plain rectangular slab standing on a band that runs round its underside, with two
    closed housings and a grip on the top face of the slab. The two housings stand on the top
    face of the slab, one at the left and one at the right. The grip stands on the top face
    between them and is a closed block of the same kind. The band meets the underside of the
    slab and follows the same rectangular run.
    - The vibration device 10 is the whole machine. Identified on the upright outline at its
      left-hand end, clear of the slab face.
    """
    png = draft_figures._deterministic_grip_scene_png(specification)
    initial = [{"numeral": "10", "x": 500, "y": 500, "visible": True}]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, ["10 = vibration device"],
        {"ok": True, "anchors": initial})

    position = grounded["anchors"][0]
    assert (position["x"], position["y"]) == (
        draft_figures._pixel_to_normalized(185, 1400),
        draft_figures._pixel_to_normalized(365, 900),
    )
    assert position["target_evidence"] == "on the outer left boundary of the whole machine"


def test_deterministic_block_grip_accepts_source_clean_three_block_wording():
    specification = """
    The covering element 36 is one large plain tile in perspective. The machine stands on its
    left-hand part, leaving a wide open expanse of tile to the right. The machine is one plain
    rectangular slab, the base 12, standing on a band that runs round its underside. Three
    closed blocks stand side by side on its top face, clear of one another: the
    left-hand block is the vibration motor 18, the middle block is the handle 44, and the
    right-hand block is the air-extraction mechanism 20. The perimeter member 24 is the band
    beneath the slab.
    """
    numerals = [
        "10 = vibration device", "12 = base", "18 = vibration motor",
        "20 = air-extraction mechanism", "24 = perimeter member",
        "36 = covering element", "44 = handle",
    ]

    png = draft_figures._deterministic_grip_scene_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]
    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    assert grounded["deterministic_anchor_certificate"]["renderer"] == "block_grip_scene"
    assert {item["numeral"] for item in
            grounded["deterministic_anchor_certificate"]["anchors"]} == {
                "10", "12", "18", "20", "24", "36", "44"}


def test_deterministic_pulling_scene_uses_exact_band_and_path_anchors():
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    its right-hand part, leaving a wide open expanse of tile to the left. The machine is one
    plain rectangular body standing on a band that runs round its underside. The body and the
    band are the whole of the machine drawn on this sheet. The flexible pulling element 46 is
    drawn as one slack curved path, a single continuous curved line. It runs away to the left,
    sagging gently over the open expanse of tile.
    - The vibration device 10 is the whole machine. Identified on its outer boundary.
    - The perimeter member 24 is the band. Identified well inside its broad front strip.
    - The covering element 36 is the tile. Identified well inside the open tile in front.
    - The flexible pulling element 46 is the path. Identified on that path clear of the machine.
    """
    numerals = [
        "10 = vibration device", "24 = perimeter member",
        "36 = covering element", "46 = flexible pulling element",
    ]
    png = draft_figures._deterministic_pulling_scene_png(specification)
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, grounded)

    positions = {
        item["numeral"]: (item["x"], item["y"])
        for item in grounded["anchors"]
    }
    assert positions["24"] == (
        draft_figures._pixel_to_normalized(850, 1400),
        draft_figures._pixel_to_normalized(468, 900),
    )
    assert positions["46"] == (
        draft_figures._pixel_to_normalized(445, 1400),
        draft_figures._pixel_to_normalized(489, 900),
    )
    assert grounded["pixel_anchor_audit"]["ok"] is True
    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["renderer"] == "pulling_scene"
    assert {item["numeral"] for item in certificate["anchors"]} == {
        "10", "24", "36", "46"}


def test_deterministic_chamber_section_uses_exact_component_anchors():
    specification = """
    The sheet shows four bodies, one broken line, and nothing else: one horizontal
    hatched slab, the base 12; one closed loop cut twice, appearing as two short hatched legs
    hanging from the underside of the slab, one at each end; one hatched band across the bottom,
    the covering element 36; and one closed housing standing on the upper face of the slab, the
    air-extraction mechanism 20. One broken line runs from inside the housing to the chamber 22,
    that broken line being all that is drawn for the fluid communication.
    - The base 12 is the slab. Identified well inside its hatching.
    - The first side 14 is the upper face of the base 12. Identified on its upper edge line clear
      of the housing.
    - The air-extraction mechanism 20 is the housing. Identified well inside that housing.
    - The chamber 22 is the broad open space between the two legs. Identified well inside that
      open space, away from the broken line.
    - The perimeter member 24 appears as the two legs. Identified well inside the hatching of the
      right-hand leg.
    - The covering element 36 is the bottom band. Identified well inside its hatching.
    """
    numerals = [
        "12 = base", "14 = first side", "20 = air-extraction mechanism",
        "22 = chamber", "24 = perimeter member", "36 = covering element",
    ]
    png = draft_figures._deterministic_chamber_section_png(specification)
    assert png is not None
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, grounded)

    positions = {
        item["numeral"]: (item["x"], item["y"])
        for item in grounded["anchors"]
    }
    assert positions["14"] == (
        draft_figures._pixel_to_normalized(470, 1400),
        draft_figures._pixel_to_normalized(222, 900),
    )
    assert positions["22"] == (
        draft_figures._pixel_to_normalized(500, 1400),
        draft_figures._pixel_to_normalized(475, 900),
    )
    assert positions["24"] == (
        draft_figures._pixel_to_normalized(1080, 1400),
        draft_figures._pixel_to_normalized(475, 900),
    )
    assert grounded["pixel_anchor_audit"]["ok"] is True
    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["renderer"] == "chamber_section"
    assert {item["numeral"] for item in certificate["anchors"]} == {
        "12", "14", "20", "22", "24", "36"}


def test_flush_chamber_section_moves_the_perimeter_anchor_with_the_leg():
    specification = """
    The sheet shows four bodies, one broken line, and nothing else: one horizontal hatched
    slab, the base 12; one closed loop cut twice, appearing as two short hatched legs hanging
    from the underside of the slab, one at each end of the slab, the outer side of each leg
    standing flush with the corresponding end of the slab; one hatched band across the bottom,
    the covering element 36; and one closed housing standing on the upper face of the slab, the
    air-extraction mechanism 20. One broken line runs from inside the housing to the chamber 22,
    that broken line being all that is drawn for the fluid communication.
    - The perimeter member 24 appears as the two legs. Identified well inside the hatching of the
      right-hand leg.
    """
    png = draft_figures._deterministic_chamber_section_png(specification)
    semantic = {
        "ok": True,
        "anchors": [{
            "numeral": "24", "x": 500, "y": 500,
            "visible": True, "evidence": "perimeter member",
        }],
    }

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, ["24 = perimeter member"], semantic)
    grounded = draft_figures._apply_pixel_grounding(
        png, ["24 = perimeter member"], grounded)

    assert grounded["anchors"][0]["x"] == draft_figures._pixel_to_normalized(1140, 1400)
    assert grounded["anchors"][0]["y"] == draft_figures._pixel_to_normalized(475, 900)
    assert grounded["pixel_anchor_audit"]["ok"] is True


def test_deterministic_fragmentary_section_uses_exact_component_anchors():
    specification = """
    The sheet shows four hatched bodies: one upright column, the perimeter member 24, and three
    horizontal bands lying one above another beneath it, all four shown schematically. Each band
    runs across the drawing area from side to side, with hatching continuous from side to side,
    including directly beneath the column. Open unhatched paper lies beneath the lowest band. The
    column stands above the uppermost band, with an open stretch of that band on each side of it.
    Between the bottom line of the column and the top line of the uppermost band lies open
    unhatched space.
    - The perimeter member 24 is the column. Identified well inside its hatching.
    - The bearing face 26 is the bottom line of the column. Identified on that line.
    - The clearance 34 is the open space. Identified well inside it.
    - The covering element 36 is the uppermost band. Identified well inside its hatching to the
      right of the column.
    - The exposed face 38 is the top line of the uppermost band. Identified on that line to the
      left of the column.
    - The bonding material 40 is the middle band. Identified well inside its hatching.
    - The substrate 42 is the lowest band. Identified well inside its hatching.
    """
    numerals = [
        "24 = perimeter member", "26 = bearing face", "34 = clearance",
        "36 = covering element", "38 = exposed face", "40 = bonding material",
        "42 = substrate",
    ]
    png = draft_figures._deterministic_fragmentary_section_png(specification)
    assert png is not None
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, grounded)

    positions = {
        item["numeral"]: (item["x"], item["y"])
        for item in grounded["anchors"]
    }
    assert positions["26"] == (
        draft_figures._pixel_to_normalized(700, 1400),
        draft_figures._pixel_to_normalized(320, 900),
    )
    assert positions["34"] == (
        draft_figures._pixel_to_normalized(700, 1400),
        draft_figures._pixel_to_normalized(365, 900),
    )
    assert positions["38"] == (
        draft_figures._pixel_to_normalized(250, 1400),
        draft_figures._pixel_to_normalized(410, 900),
    )
    assert grounded["pixel_anchor_audit"]["ok"] is True
    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["renderer"] == "fragmentary_section"
    assert {item["numeral"] for item in certificate["anchors"]} == {
        "24", "26", "34", "36", "38", "40", "42"}


def test_deterministic_nested_plan_uses_exact_ring_and_field_anchors():
    specification = """
    The sheet shows the perimeter member 24 as one rectangular ring, and within it the second
    side 16 as a plain open field; no other body is drawn. The ring is drawn as one rectangle
    with a smaller rectangle inside it, the inner rectangle standing well in from each of the
    four sides of the outer rectangle. The field enclosed by the inner rectangle is left entirely
    open paper. The ring stands well in from every side of the drawing area.
    - The perimeter member 24 is the band between the outer edge and inner edge. Identified well
      inside that band along the left-hand side of the ring.
    - The second side 16 is the plain field inside the inner edge. Identified well inside it.
    """
    numerals = ["16 = second side", "24 = perimeter member"]
    png = draft_figures._deterministic_nested_plan_png(specification)
    assert png is not None
    initial = [{
        "numeral": entry.split(" = ", 1)[0], "x": 500, "y": 500,
        "visible": True, "evidence": entry,
    } for entry in numerals]

    grounded = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, numerals, {"ok": True, "anchors": initial})
    grounded = draft_figures._apply_pixel_grounding(png, numerals, grounded)

    positions = {
        item["numeral"]: (item["x"], item["y"])
        for item in grounded["anchors"]
    }
    assert positions["16"] == (
        draft_figures._pixel_to_normalized(700, 1400),
        draft_figures._pixel_to_normalized(450, 900),
    )
    assert positions["24"] == (
        draft_figures._pixel_to_normalized(190, 1400),
        draft_figures._pixel_to_normalized(450, 900),
    )
    assert grounded["pixel_anchor_audit"]["ok"] is True
    certificate = grounded["deterministic_anchor_certificate"]
    assert certificate["renderer"] == "nested_plan"
    assert {item["numeral"] for item in certificate["anchors"]} == {"16", "24"}


def test_deterministic_block_grip_replaces_stale_durable_endpoint_progress(monkeypatch):
    specification = """
    The covering element 36 is one large plain tile seen in perspective. The machine stands on
    the left-hand part of the tile, leaving a wide open expanse of tile to the right. The machine
    is one plain rectangular slab standing on a band that runs round its underside, with two
    closed housings and a grip on the top face of the slab. The two housings stand on the top
    face of the slab, one at the left and one at the right. The grip stands on the top face
    between them and is a closed block of the same kind. The band meets the underside of the
    slab and follows the same rectangular run.
    - The base 12 is the slab. Identified well inside its broad front face.
    - The handle 44 is the grip. Identified well inside its front face.
    """
    numerals = ["12 = base", "44 = handle"]
    png = draft_figures._deterministic_grip_scene_png(specification)
    stale = [
        {"numeral": "12", "x": 900, "y": 100, "visible": True, "evidence": "stale"},
        {"numeral": "44", "x": 850, "y": 800, "visible": True, "evidence": "stale"},
    ]
    monkeypatch.setattr(draft_figures, "_marked_progress_get", lambda *a, **k: {
        "anchors": stale, "certificates": {}, "attempts": 0,
        "coordinate_history": {"12": [(900, 100)], "44": [(850, 800)]},
    })
    monkeypatch.setattr(draft_figures, "_marked_progress_put", lambda *a, **k: None)
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["12", "44"], "figure_label": "FIG. 1",
        "sheet_numbers": [], "other_text": [], "confidence": 0.99,
    })
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [], "missing": [],
        "labels": [],
    })

    def reject_unnecessary_same_provider_review(*_args, **_kwargs):
        raise AssertionError(
            "byte-exact deterministic component certificates must bypass noisy same-provider "
            "coordinate voting")

    monkeypatch.setattr(
        draft_figures, "inspect_marked_anchors", reject_unnecessary_same_provider_review)
    monkeypatch.setattr(
        draft_figures, "inspect_cross_provider_endpoints",
        lambda *a, **k: accepted_cross_provider_audit(labels=[{
            "numeral": numeral, "correct": True,
            "evidence": "the exact component center is correct",
        } for numeral in ("12", "44")]))

    _sheet, labels, leaders, anchors, pixel = draft_figures._compose_checked_sheet(
        png, label="FIG. 1", caption=specification, numerals=numerals,
        semantic={"anchors": stale, "pixel_anchor_audit": {"ok": True}})

    assert labels["ok"] is True and leaders["ok"] is True and pixel["ok"] is True
    marked = leaders["marked_anchor_audit"]
    assert marked["prompt_version"] == draft_figures.DETERMINISTIC_ANCHOR_CERTIFICATE_VERSION
    assert marked["review_count"] == 0
    assert marked["cross_provider_audit"]["ok"] is True
    assert draft_figures.current_marked_anchor_audit(marked) is True
    final_positions = {
        item["numeral"]: (item["x"], item["y"])
        for item in anchors
    }
    assert final_positions["12"] == (
        draft_figures._pixel_to_normalized(435, 1400),
        draft_figures._pixel_to_normalized(365, 900),
    )
    assert final_positions["44"] == (
        draft_figures._pixel_to_normalized(435, 1400),
        draft_figures._pixel_to_normalized(305, 900),
    )


def test_deterministic_grip_scene_accepts_source_clean_single_outline_wording():
    specification = """
    The covering element 36 is one large plain tile filling the lower part of the drawing
    area. The machine stands on its left-hand part, leaving a wide open expanse of tile to the
    right. The machine is a plain rectangular slab, the base 12, carrying two closed housings
    on its top face and a grip above them, and standing on a band round its underside, the band
    alone touching the tile. The handle 44 is a simple grip carried on the machine above the
    slab and clear of the housings, drawn as one closed outline enclosing an open area.
    """

    png = draft_figures._deterministic_grip_scene_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    for x in (350, 400, 435, 470, 520):
        ink = [y for y in range(50, 241) if image.getpixel((x, y)) < 32]
        runs = []
        for y in ink:
            if not runs or y > runs[-1][-1] + 1:
                runs.append([y])
            else:
                runs[-1].append(y)
        assert len(runs) == 1
    assert image.getpixel((435, 255)) < 32
    assert image.getpixel((285, 220)) < 32
    assert image.getpixel((585, 220)) < 32


def test_deterministic_grip_scene_draws_two_boundaries_for_a_finite_width_ring():
    specification = """
    The covering element 36 is one large plain tile, a flat rectangular panel seen in
    perspective. The machine stands on its left-hand part, leaving a wide open expanse of tile
    to the right. The machine is a plain rectangular slab standing on a band that runs round its
    underside. Two plain closed housings stand on the top face, one left and one right, and a
    grip stands above the slab between them. The handle 44 is drawn as a closed ring shape
    enclosing an open area, the bar forming that ring having its own width.
    """

    png = draft_figures._deterministic_grip_scene_png(specification)

    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("L")
    for x in (400, 435, 470):
        ink = [y for y in range(30, 241) if image.getpixel((x, y)) < 32]
        runs = []
        for y in ink:
            if not runs or y > runs[-1][-1] + 2:
                runs.append([y])
            else:
                runs[-1].append(y)
        assert len(runs) == 2
        assert image.getpixel((x, 275)) < 32
        assert image.getpixel((x, 300)) < 32
    for x in (245, 335, 535, 625):
        assert sum(image.getpixel((x, y)) < 32 for y in range(235, 351)) > 60
    assert image.getpixel((428, 560)) == 255
    assert image.getpixel((432, 537)) < 32


def test_deterministic_grip_scene_leaves_room_for_a_callout_inside_the_band():
    specification = """
    The covering element 36 is one large plain tile, a flat rectangular panel seen in
    perspective. The machine stands on its left-hand part, leaving a wide open expanse of tile
    to the right. The machine is a plain rectangular slab standing on a band that runs round its
    underside. Two plain closed housings stand on the top face, one left and one right, and a
    grip stands above the slab between them. The handle 44 is drawn as a closed ring shape
    enclosing an open area, the bar forming that ring having its own width.
    """

    png = draft_figures._deterministic_grip_scene_png(specification)

    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("L")
    # At the middle of the viewer-facing right band, leave enough white surface between
    # its two boundary strokes for an endpoint dot to sit clearly inside either boundary.
    ink_runs = []
    for y in range(390, 520):
        if image.getpixel((560, y)) >= 32:
            continue
        if not ink_runs or y > ink_runs[-1][-1] + 1:
            ink_runs.append([y])
        else:
            ink_runs[-1].append(y)
    assert len(ink_runs) >= 2
    assert ink_runs[-1][0] - ink_runs[-2][-1] >= 32


def test_deterministic_section_renderers_differentiate_touching_body_hatching(monkeypatch):
    original = draft_figures._paste_hatched_box
    observed = []

    def record(image, box, *, angle):
        observed.append(angle)
        return original(image, box, angle=angle)

    monkeypatch.setattr(draft_figures, "_paste_hatched_box", record)
    fragmentary = """
    The sheet shows four hatched bodies and nothing else: one upright column and three
    horizontal bands lying one above another beneath it. Each band runs the whole way across,
    from the left-hand limit of the drawing area to the right, each carrying regularly spaced
    oblique parallel hatching continuous from side to side, including directly beneath the
    column, each band reading as one whole hatched body, the hatching of each band being angled
    differently from that of the band it touches and from that of the column. Open unhatched
    paper lies beneath the lowest band. The column stands above the uppermost band, with an open
    stretch of that band and open paper on each side of it. Between the bottom line of the column
    and the top line of the uppermost band lies open unhatched space.
    """
    chamber = """
    The sheet shows four bodies, one broken line, and nothing else: one horizontal hatched slab;
    one closed loop cut twice, appearing as two short hatched legs hanging from the underside of
    the slab, one at each end; one hatched band across the bottom on which both legs stand; and
    one closed housing standing on the upper face of the slab. One broken line runs from inside
    the housing to the chamber, and no passage, duct, opening or other structure is depicted.
    """

    assert draft_figures._deterministic_fragmentary_section_png(fragmentary) is not None
    assert observed == [45, -45, 60, -60]
    observed.clear()
    assert draft_figures._deterministic_chamber_section_png(chamber) is not None
    assert observed == [45, -45, -45, 60]


def test_deterministic_fragmentary_section_accepts_explicit_filing_inventory(monkeypatch):
    original = draft_figures._paste_hatched_box
    observed = []

    def record(image, box, *, angle):
        observed.append(angle)
        return original(image, box, angle=angle)

    monkeypatch.setattr(draft_figures, "_paste_hatched_box", record)
    specification = """
    The sheet shows four hatched bodies: one upright column and three horizontal bands lying
    one above another beneath it, all four shown schematically. Each band runs across the
    drawing area from side to side, with regularly spaced oblique parallel hatching continuous
    from side to side, including directly beneath the column. The uppermost band is hatched
    rising to the right at a shallow slope; the middle band is hatched falling to the right at
    a shallow slope; the lowest band is hatched rising to the right at about 45 degrees; and the
    column is hatched falling to the right at about 45 degrees. Open unhatched paper lies beneath
    the lowest band. The column stands above the uppermost band, with an open stretch of that
    band on each side of it. Between the bottom line of the column and the top line of the
    uppermost band lies open unhatched space.
    """

    assert draft_figures._deterministic_fragmentary_section_png(specification) is not None
    assert observed == [45, -20, 20, -45]


def test_deterministic_fragmentary_section_accepts_hatching_lines_continuous_wording():
    specification = """
    The sheet shows four hatched bodies: one upright column and three horizontal bands lying
    one above another beneath it. Each band runs across the drawing area from side to side and
    is filled with regularly spaced straight parallel hatching lines continuous from side to
    side, including directly beneath the column. Open unhatched paper lies beneath the lowest
    band. The column is the perimeter member. It stands above the uppermost band, with an open
    stretch of that band on each side of it. Between the bottom line of the column and the top
    line of the uppermost band lies open unhatched space.
    """

    png = draft_figures._deterministic_fragmentary_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png


def test_deterministic_fragmentary_section_accepts_complete_lower_area_inventory():
    specification = """
    The sheet shows four hatched bodies: one upright column and three horizontal bands lying one
    above another beneath it, all four shown schematically. The three bands are stacked one on
    another in the lower part of the drawing area. Each band runs across the drawing area,
    ending just inside its left-hand and right-hand limits, and each is filled with
    regularly spaced straight parallel hatching lines continuous from side to side, including
    directly beneath the column. The column stands above the uppermost band, with an open stretch
    of that band on each side of it. Between the bottom line of the column and the top line of the
    uppermost band lies open unhatched space, shown enlarged for clarity.
    """

    png = draft_figures._deterministic_fragmentary_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png


def test_deterministic_chamber_section_accepts_explicit_filing_inventory(monkeypatch):
    original = draft_figures._paste_hatched_box
    observed = []

    def record(image, box, *, angle):
        observed.append(angle)
        return original(image, box, angle=angle)

    monkeypatch.setattr(draft_figures, "_paste_hatched_box", record)
    specification = """
    The sheet shows four bodies, all shown schematically, and one broken line: one horizontal
    hatched slab, the base; one closed loop cut twice, appearing as two short hatched legs
    hanging from the underside of the slab, one at each end; one hatched band across the bottom
    on which both legs stand; and one closed housing standing on the upper face of the slab.
    The slab, the two legs and the band are the cut bodies. The slab is filled with regularly
    spaced parallel hatching rising to the right at about 45 degrees, both legs are filled with
    such hatching falling to the right at about 45 degrees, and the band is filled with such
    hatching rising to the right at about 75 degrees. One broken line runs from inside the housing
    to the chamber. The housing lies outside the cut and is drawn in plain outline without
    hatching.
    """

    assert draft_figures._deterministic_chamber_section_png(specification) is not None
    assert observed == [-45, 45, 45, -75]


def test_deterministic_chamber_section_accepts_agent_rephrased_inventory(monkeypatch):
    original = draft_figures._paste_hatched_box
    observed = []

    def record(image, box, *, angle):
        observed.append(angle)
        return original(image, box, angle=angle)

    monkeypatch.setattr(draft_figures, "_paste_hatched_box", record)
    specification = """
    The sheet shows four schematic bodies and one broken line: one hatched horizontal slab, the
    base 12; one closed loop cut twice, appearing as two short hatched legs hanging from the
    underside of the slab, one at each end and flush with it; one hatched band across the bottom,
    the covering element 36, on which the legs stand; and one closed housing standing on the slab,
    the air-extraction mechanism 20.
    The slab, the legs and the band are the cut bodies. In the slab each stroke starts low on the
    left and ends high on the right, like a forward slash. In both legs each stroke starts high on
    the left and ends low on the right, like a backslash. In the band each stroke is steep, close
    to upright and leaning slightly to the right, much nearer to vertical than the legs' strokes.
    One broken line runs from inside the housing to the chamber 22. That line stops at the upper
    face of the base 12 and resumes below its lower face, no passage through the base being drawn.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    assert observed == [-45, 45, 45, -75]
    image = Image.open(io.BytesIO(png)).convert("L")
    assert sum(image.getpixel((865, y)) < 32 for y in range(225, 356)) < 40
    assert image.getpixel((865, 215)) < 32
    assert sum(image.getpixel((200, y)) < 32 for y in range(360, 621)) > 240
    assert sum(image.getpixel((1200, y)) < 32 for y in range(360, 621)) > 240


def test_deterministic_section_certificate_records_exact_raw_pixel_hatch_angles():
    specification = """
    The sheet shows four bodies, all shown schematically, and one broken line: one horizontal
    hatched slab, the base 12; one closed loop cut twice, appearing as two short hatched legs
    hanging from the underside of the slab, one at each end; one hatched band across the bottom
    on which both legs stand, the covering element 36; and one closed housing standing on the
    upper face of the slab. The slab is filled with regularly spaced parallel hatching rising to
    the right at about 45 degrees, both legs are filled with such hatching falling to the right
    at about 45 degrees, and the band is filled with such hatching rising to the right at about
    75 degrees. One broken line runs from inside the housing to the chamber 22, and no passage,
    duct, opening or other structure is depicted.
    """
    png = draft_figures._deterministic_chamber_section_png(specification)

    semantic = draft_figures._apply_deterministic_anchor_certificate(
        png, specification, [], {"ok": True, "anchors": []})

    certificate = semantic["deterministic_section_hatch_certificate"]
    assert certificate["ok"] is True
    assert certificate["exact_renderer_match"] is True
    assert certificate["renderer"] == "chamber_section"
    assert certificate["coordinate_space"] == "raw_pixels_origin_upper_left_y_down"
    assert certificate["raw_png_sha256"] == hashlib.sha256(png).hexdigest()
    assert certificate["components"] == [
        {"component": "base slab", "angle_degrees": -45, "direction": "rises_to_right"},
        {"component": "left perimeter leg", "angle_degrees": 45,
         "direction": "falls_to_right"},
        {"component": "right perimeter leg", "angle_degrees": 45,
         "direction": "falls_to_right"},
        {"component": "covering-element band", "angle_degrees": -75,
         "direction": "rises_to_right"},
    ]


def test_fragmentary_section_certificate_distinguishes_column_and_all_three_bands():
    specification = """
    The sheet shows four hatched bodies: one upright column and three horizontal bands lying one
    above another beneath it, all four shown schematically. Each band runs across the drawing
    area from side to side, with hatching continuous from side to side, including directly
    beneath the column. The column is hatched falling to the right at about 45 degrees, the
    uppermost band is hatched rising to the right at about 45 degrees, the middle band is hatched
    falling to the right at about 60 degrees, and the lowest band is hatched rising to the right
    at about 60 degrees. Open unhatched paper lies beneath the lowest band. The column stands
    above the uppermost band, with an open stretch of that band on each side of it. Between the
    bottom line of the column and the top line of the uppermost band lies open unhatched space.
    """
    png = draft_figures._deterministic_fragmentary_section_png(specification)

    certificate = draft_figures._deterministic_section_hatch_certificate(png, specification)

    assert certificate["renderer"] == "fragmentary_section"
    assert [item["angle_degrees"] for item in certificate["components"]] == [45, -45, 60, -60]
    assert [item["direction"] for item in certificate["components"]] == [
        "falls_to_right", "rises_to_right", "falls_to_right", "rises_to_right",
    ]


def test_deterministic_chamber_section_splits_schematic_line_around_solid_base():
    inventory = """
    The sheet shows four bodies, all shown schematically, and one broken line: one horizontal
    hatched slab, the base 12; one closed loop cut twice, appearing as two short hatched legs
    hanging from the underside of the slab, one at each end; one hatched band across the bottom
    on which both legs stand; and one closed housing standing on the upper face of the slab.
    """
    continuous = inventory + """
    One broken line runs from inside the housing to the chamber 22. That broken line is all that
    is drawn for the fluid communication.
    """
    split = inventory + """
    One broken line runs from inside the housing to the chamber 22, the broken line stopping at
    the upper face of the base 12 and resuming below its lower face, so that no form of passage
    through the base 12 is drawn and none is asserted.
    """

    continuous_image = Image.open(io.BytesIO(
        draft_figures._deterministic_chamber_section_png(continuous))).convert("L")
    split_image = Image.open(io.BytesIO(
        draft_figures._deterministic_chamber_section_png(split))).convert("L")
    continuous_ink = sum(continuous_image.getpixel((865, y)) < 32 for y in range(225, 356))
    split_ink = sum(split_image.getpixel((865, y)) < 32 for y in range(225, 356))

    assert split_ink + 40 < continuous_ink
    assert any(split_image.getpixel((865, y)) < 32 for y in range(145, 211))
    assert any(split_image.getpixel((865, y)) < 32 for y in range(365, 521))


def test_deterministic_chamber_section_splits_that_line_wording_used_by_agent():
    inventory = """
    The sheet shows four bodies, all shown schematically, and one broken line: one horizontal
    hatched slab, the base 12; one closed loop cut twice, appearing as two short hatched legs
    hanging from the underside of the slab, one at each end; one hatched band across the bottom
    on which both legs stand; and one closed housing standing on the upper face of the slab.
    """
    continuous = inventory + """
    One broken line runs from inside the housing to the chamber 22.
    """
    filing_wording = inventory + """
    One broken line runs from inside the housing to the chamber 22. That line stops at the upper
    face of the base 12 and resumes below its lower face, no passage through the base 12 being
    drawn or asserted.
    """

    continuous_image = Image.open(io.BytesIO(
        draft_figures._deterministic_chamber_section_png(continuous))).convert("L")
    filing_image = Image.open(io.BytesIO(
        draft_figures._deterministic_chamber_section_png(filing_wording))).convert("L")
    continuous_ink = sum(continuous_image.getpixel((865, y)) < 32 for y in range(225, 356))
    filing_ink = sum(filing_image.getpixel((865, y)) < 32 for y in range(225, 356))

    assert filing_ink + 40 < continuous_ink
    resumed_ink = sum(filing_image.getpixel((865, y)) < 32 for y in range(369, 521))
    assert resumed_ink >= 110


def test_deterministic_fragmentary_section_preserves_open_clearance_and_four_bodies():
    specification = """
    The sheet shows four hatched bodies and nothing else: one upright column and three
    horizontal bands lying one above another beneath it. The column stands in the left-hand
    part. Its two side lines run straight down from the upper limit to one horizontal line that
    closes it below. Each band is one single hatched rectangle running the whole way from the
    left-hand limit to the right-hand limit. The two side lines of the column are the only
    vertical lines anywhere on the sheet. Between the line closing the bottom of the column and
    the top line of the uppermost band lies open unhatched space, plainly apart and not touching.
    Open unhatched paper lies beneath the lowest band.
    """

    png = draft_figures._deterministic_fragmentary_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.getpixel((700, 365)) == 255
    assert image.getpixel((700, 850)) == 255
    assert min(image.crop((20, 420, 1380, 790)).getextrema()) == 0
    vertical_runs = []
    vertical_x = [
        x for x in range(image.width)
        if sum(image.getpixel((x, y)) < 32 for y in range(20, 301)) > 250
    ]
    for x in vertical_x:
        if not vertical_runs or x > vertical_runs[-1][-1] + 1:
            vertical_runs.append([x])
        else:
            vertical_runs[-1].append(x)
    assert len(vertical_runs) == 2


def test_deterministic_fragmentary_section_centres_column_over_unbroken_bands():
    specification = """
    The sheet shows four hatched bodies and nothing else: one upright column and three
    horizontal bands lying one above another beneath it. Each band is one rectangle running the
    whole way across from the left-hand limit to the right, with hatching continuous from side
    to side including directly beneath the column. No band is interrupted, broken or partly
    unhatched. The column stands above the uppermost band midway across the drawing area, with
    open unhatched paper on both sides. Its two side lines run straight down to one horizontal
    line closing the column below. Between the bottom line of the column and the top line of the
    uppermost band lies open unhatched space. Open unhatched paper lies beneath the lowest band.
    """

    png = draft_figures._deterministic_fragmentary_section_png(specification)

    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("L")
    assert min(image.crop((650, 150, 750, 250)).getextrema()) == 0
    assert image.getpixel((375, 200)) == 255
    assert min(image.crop((650, 420, 750, 540)).getextrema()) == 0
    vertical_x = [
        x for x in range(image.width)
        if sum(image.getpixel((x, y)) < 32 for y in range(20, 301)) > 250
    ]
    assert min(vertical_x) > 500
    assert max(vertical_x) < 900


def test_deterministic_fragmentary_section_accepts_positive_open_sides_wording():
    specification = """
    The sheet shows four hatched bodies and nothing else: one upright column and three
    horizontal bands lying one above another beneath it. Each band runs the whole way across,
    from the left-hand limit of the drawing area to the right, each carrying plain even
    hatching continuous from side to side, including directly beneath the column, each band
    reading as one whole hatched body. Open unhatched paper lies beneath the lowest band. The
    column stands above the uppermost band, with an open stretch of that band and open paper on
    each side of it. Its two side lines run straight down from the upper limit of the drawing
    area to one horizontal line closing it below. Between the bottom line of the column and the
    top line of the uppermost band lies open unhatched space, the two lines standing plainly
    apart.
    """

    png = draft_figures._deterministic_fragmentary_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    assert min(image.crop((650, 150, 750, 250)).getextrema()) == 0
    assert image.getpixel((375, 200)) == 255
    assert min(image.crop((650, 420, 750, 540)).getextrema()) == 0


def test_deterministic_chamber_section_has_two_legs_and_one_broken_line():
    specification = """
    The sheet shows four bodies, one broken line, and nothing else: one horizontal hatched
    slab; one closed loop cut twice, appearing as two short hatched legs hanging from the
    underside of the slab, one at each end; one hatched band across the bottom on which both
    legs stand; and one closed housing standing on the upper face of the slab. One broken line
    runs from inside the housing to the chamber, indicating fluid communication, and no
    passage, duct, opening or other structure is depicted. The chamber is the broad open space
    between the two legs, below the slab and above the band.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png
    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.getpixel((650, 480)) == 255
    assert min(image.crop((270, 390, 370, 590)).getextrema()) == 0
    assert min(image.crop((1030, 390, 1130, 590)).getextrema()) == 0
    assert image.getpixel((300, 620)) < 32
    assert image.getpixel((1080, 620)) < 32
    broken_runs = []
    for y in (value for value in range(380, 601)
              if image.getpixel((865, value)) < 32):
        if not broken_runs or y > broken_runs[-1][-1] + 1:
            broken_runs.append([y])
        else:
            broken_runs[-1].append(y)
    assert 4 <= len(broken_runs) <= 8


def test_deterministic_chamber_section_honors_flush_perimeter_legs():
    specification = """
    The sheet shows four bodies, one broken line, and nothing else: one horizontal hatched
    slab; one closed loop cut twice, appearing as two short hatched legs hanging from the
    underside of the slab, one at each end of the slab, the outer side of each leg standing
    flush with the corresponding end of the slab; one hatched band across the bottom on which
    both legs stand; and one closed housing standing on the upper face of the slab. One broken
    line runs from inside the housing to the chamber, indicating fluid communication, and no
    passage, duct, opening or other structure is depicted.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("L")
    assert sum(image.getpixel((200, y)) < 32 for y in range(370, 611)) > 230
    assert sum(image.getpixel((1200, y)) < 32 for y in range(370, 611)) > 230


def test_deterministic_chamber_section_honors_compact_flush_wording():
    specification = """
    The sheet shows four bodies, all shown schematically, and one broken line: one horizontal
    hatched slab; one closed loop cut twice, appearing as two short hatched legs hanging from the
    underside of the slab, one at each end of the slab, the outer side of each leg flush with that
    end so that the loop runs at the perimeter of the underside; one hatched band across the bottom
    on which both legs stand; and one closed housing standing on the upper face of the slab. One
    broken line runs from inside the housing to the chamber, indicating fluid communication, and no
    passage, duct, opening or other structure is depicted.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    image = Image.open(io.BytesIO(png)).convert("L")
    assert sum(image.getpixel((200, y)) < 32 for y in range(370, 611)) > 230
    assert sum(image.getpixel((1200, y)) < 32 for y in range(370, 611)) > 230


def test_deterministic_chamber_section_accepts_positive_single_line_wording():
    specification = """
    The sheet shows four bodies, all shown schematically, one broken line, and nothing else:
    one horizontal hatched slab; one closed loop cut twice, appearing as two short hatched legs
    hanging from the underside of the slab, one at each end; one hatched band across the bottom,
    on which both legs stand; and one closed housing standing on the upper face of the slab.
    One broken line runs from inside the housing to the chamber, indicating schematically the
    fluid communication between the housing and the chamber, that broken line being all that is
    drawn for it.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png


def test_deterministic_chamber_section_accepts_exact_one_line_inventory_wording():
    specification = """
    The sheet shows four bodies, all shown schematically, one broken line, and nothing else:
    one horizontal hatched slab; one closed loop cut twice, appearing as two short hatched legs
    hanging from the underside of the slab, one at each end; one hatched band across the bottom;
    and one closed housing. One broken line runs from inside the housing to the chamber.
    """

    png = draft_figures._deterministic_chamber_section_png(specification)

    assert png is not None
    assert draft_figures._deterministic_geometry_png(specification) == png


def test_deterministic_nested_plan_uses_even_width_corridors():
    specification = (
        "The whole sheet contains four outlines and nothing else. From the outside inward, "
        "draw three nested rectangles and one circle at the centre. Each outline is drawn "
        "once as one closed line. The corridors between rectangles have even width."
    )
    image = Image.open(io.BytesIO(
        draft_figures._deterministic_nested_plan_png(specification))).convert("L")

    def centers(values):
        runs = []
        for value in values:
            if not runs or value > runs[-1][-1] + 1:
                runs.append([value])
            else:
                runs[-1].append(value)
        return [round((run[0] + run[-1]) / 2) for run in runs]

    horizontal = centers(
        x for x in range(image.width) if image.getpixel((x, image.height // 2)) < 32)
    vertical = centers(
        y for y in range(image.height) if image.getpixel((image.width // 2, y)) < 32)

    assert horizontal[1] - horizontal[0] == vertical[1] - vertical[0]
    assert horizontal[2] - horizontal[1] == vertical[2] - vertical[1]


def test_deterministic_nested_plan_scales_the_circle_to_one_third_of_inner_rectangle():
    specification = (
        "The whole sheet contains four outlines and nothing else. From the outside inward, "
        "draw three nested rectangles and one circle at the centre. The diameter of the circle "
        "is about one-third of the width of the third rectangle. Each outline is drawn once as "
        "one closed line."
    )

    image = Image.open(io.BytesIO(
        draft_figures._deterministic_nested_plan_png(specification))).convert("L")
    row = [x for x in range(image.width) if image.getpixel((x, image.height // 2)) < 32]
    runs = []
    for x in row:
        if not runs or x > runs[-1][-1] + 1:
            runs.append([x])
        else:
            runs[-1].append(x)
    centers = [round((run[0] + run[-1]) / 2) for run in runs]
    inner_width = centers[5] - centers[2]
    circle_diameter = centers[4] - centers[3]

    assert 0.30 <= circle_diameter / inner_width <= 0.36


def test_render_uses_deterministic_nested_plan_before_raster_generation(monkeypatch):
    bad = blank_png(1000, 800)
    specification = (
        "The whole sheet contains four outlines and nothing else. From the outside inward, "
        "draw three nested rectangles and one circle at the centre. Each outline is drawn "
        "once as one closed line."
    )
    monkeypatch.setattr(draft_figures, "MAX_SEMANTIC_ATTEMPTS", 1)
    generated = []
    monkeypatch.setattr(
        draft_figures, "_cached_generate",
        lambda *a, **k: generated.append((a, k)) or bad)
    monkeypatch.setattr(draft_figures, "_discard_cached_generation", lambda *a, **k: None)

    def inspect(png, **_kwargs):
        if png == bad:
            return {"ok": False, "missing": [], "errors": ["wrong outline count"]}
        return {
            "ok": True,
            "anchors": [
                {"numeral": "16", "x": 500, "y": 850, "visible": True,
                 "evidence": "outer margin"},
                {"numeral": "24", "x": 800, "y": 500, "visible": True,
                 "evidence": "inner band"},
                {"numeral": "30", "x": 500, "y": 500, "visible": True,
                 "evidence": "circle center"},
            ],
        }

    monkeypatch.setattr(draft_figures, "inspect_semantics", inspect)
    accepted_pixel = {
        "ok": True, "inspected": True, "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(
        draft_figures, "_apply_pixel_grounding",
        lambda _png, _numerals, semantic: {
            **semantic, "pixel_anchor_audit": dict(accepted_pixel),
        })
    accepted_labels = {
        "ok": True, "detected": ["16", "24", "30"],
        "expected": ["16", "24", "30"], "correct_figure_label": True,
        "other_text": [], "confidence": 0.99,
    }
    accepted_leaders = {
        "ok": True, "inspected": True, "errors": [], "incorrect": [],
        "marked_anchor_audit": accepted_marked_anchor_audit(),
    }
    monkeypatch.setattr(
        draft_figures, "_compose_checked_sheet",
        lambda png, **kwargs: (
            png, dict(accepted_labels), dict(accepted_leaders),
            list(kwargs["semantic"]["anchors"]), dict(accepted_pixel)))
    monkeypatch.setattr(draft_figures, "create_figure", lambda *a, **k: {"id": 44})
    saved = []

    def save(_figure_id, **kwargs):
        saved.append(kwargs)
        return {
            "version_no": 1, "audit": kwargs["ocr_audit"],
            "semantic_audit": kwargs["semantic_audit"],
            "leader_audit": kwargs["leader_audit"],
            "detected_numerals": ["16", "24", "30"],
        }

    monkeypatch.setattr(draft_figures, "_audited_version", save)

    result = draft_figures.render_figure(
        7, 91, label="FIG. 3", caption=specification,
        numerals=["16 = second side", "24 = perimeter member", "30 = extraction opening"])

    assert result["semantic_audit"]["ok"] is True
    assert generated == []
    assert saved[0]["source_kind"] == "deterministic"
    assert draft_figures.closed_region_audit(
        saved[0]["base_png"], specification)["observed"] == 4


def test_render_never_publishes_generated_pixels_for_a_supported_exact_brief(monkeypatch):
    specification = (
        "The whole sheet contains four outlines and nothing else. From the outside inward, "
        "draw three nested rectangles and one circle at the centre. Each outline is drawn "
        "once as one closed line."
    )
    exact = draft_figures._deterministic_geometry_png(specification)
    altered = Image.open(io.BytesIO(exact)).convert("RGB")
    altered.putpixel((10, 10), (0, 0, 0))
    generated_buffer = io.BytesIO()
    altered.save(generated_buffer, format="PNG")
    generated = generated_buffer.getvalue()
    assert generated != exact
    exact_reviews = 0
    generation_calls = []
    monkeypatch.setattr(draft_figures, "MAX_SEMANTIC_ATTEMPTS", 2)
    monkeypatch.setattr(
        draft_figures, "_cached_generate",
        lambda *args, **kwargs: generation_calls.append((args, kwargs)) or generated)
    monkeypatch.setattr(draft_figures, "_discard_cached_generation", lambda *a, **k: None)

    def inspect(png, **_kwargs):
        nonlocal exact_reviews
        if png == exact:
            exact_reviews += 1
            if exact_reviews == 1:
                return {"ok": False, "missing": [], "errors": ["reviewer false negative"]}
        return {
            "ok": True,
            "anchors": [
                {"numeral": "16", "x": 500, "y": 850, "visible": True,
                 "evidence": "outer margin"},
                {"numeral": "24", "x": 800, "y": 500, "visible": True,
                 "evidence": "inner band"},
                {"numeral": "30", "x": 500, "y": 500, "visible": True,
                 "evidence": "circle center"},
            ],
        }

    monkeypatch.setattr(draft_figures, "inspect_semantics", inspect)
    accepted_pixel = {
        "ok": True, "inspected": True, "version": draft_figures.PIXEL_ANCHOR_VERSION,
        "adjusted": [], "allowed_spaces": [], "ungrounded": [],
    }
    monkeypatch.setattr(
        draft_figures, "_apply_pixel_grounding",
        lambda _png, _numerals, semantic: {
            **semantic, "pixel_anchor_audit": dict(accepted_pixel),
        })
    monkeypatch.setattr(
        draft_figures, "_compose_checked_sheet",
        lambda png, **kwargs: (
            png,
            {"ok": True, "detected": ["16", "24", "30"],
             "expected": ["16", "24", "30"], "correct_figure_label": True,
             "other_text": [], "confidence": 0.99},
            {"ok": True, "inspected": True, "errors": [], "incorrect": [],
             "marked_anchor_audit": accepted_marked_anchor_audit()},
            list(kwargs["semantic"]["anchors"]), dict(accepted_pixel)))
    monkeypatch.setattr(draft_figures, "create_figure", lambda *a, **k: {"id": 46})
    saved = []

    def save(_figure_id, **kwargs):
        saved.append(kwargs)
        return {
            "version_no": 1, "audit": kwargs["ocr_audit"],
            "semantic_audit": kwargs["semantic_audit"],
            "leader_audit": kwargs["leader_audit"],
            "detected_numerals": ["16", "24", "30"],
        }

    monkeypatch.setattr(draft_figures, "_audited_version", save)

    draft_figures.render_figure(
        7, 91, label="FIG. 3", caption=specification,
        numerals=["16 = second side", "24 = perimeter member", "30 = extraction opening"])

    assert generation_calls
    assert exact_reviews == 2
    assert saved[0]["source_kind"] == "deterministic"
    assert saved[0]["base_png"] == exact


def test_render_keeps_an_exact_deterministic_ring_when_vision_anchor_is_near_its_edge(
        monkeypatch):
    specification = (
        "The sheet shows one rectangular ring and nothing else. The ring is drawn with two "
        "closed thin lines and those two alone: its outer edge and its inner edge. Both are "
        "rectangular, and they are spaced apart on all four sides. The opening inside the ring "
        "is left plain, so that the finished sheet carries just those two closed lines."
    )
    generated = []
    monkeypatch.setattr(
        draft_figures, "_cached_generate",
        lambda *args, **kwargs: generated.append((args, kwargs)) or blank_png())
    monkeypatch.setattr(draft_figures, "_discard_cached_generation", lambda *a, **k: None)
    monkeypatch.setattr(
        draft_figures, "inspect_semantics",
        lambda *_args, **_kwargs: {
            "ok": True,
            "anchors": [
                {"numeral": "16", "x": 500, "y": 500, "visible": True,
                 "evidence": "the plain field inside the inner rectangular outline"},
                {"numeral": "24", "x": 500, "y": 200, "visible": True,
                 "evidence": (
                     "the band-like surface between the outer and inner rectangular outlines")},
            ],
        })
    accepted_labels = {
        "ok": True, "detected": ["16", "24"], "expected": ["16", "24"],
        "correct_figure_label": True, "other_text": [], "confidence": 0.99,
    }
    accepted_leaders = {
        "ok": True, "inspected": True, "errors": [], "incorrect": [],
        "marked_anchor_audit": accepted_marked_anchor_audit(),
    }
    captured = {}

    def compose(png, **kwargs):
        captured["semantic"] = kwargs["semantic"]
        return (
            png, dict(accepted_labels), dict(accepted_leaders),
            list(kwargs["semantic"]["anchors"]),
            dict(kwargs["semantic"]["pixel_anchor_audit"]),
        )

    monkeypatch.setattr(draft_figures, "_compose_checked_sheet", compose)
    monkeypatch.setattr(draft_figures, "create_figure", lambda *a, **k: {"id": 45})
    saved = []

    def save(_figure_id, **kwargs):
        saved.append(kwargs)
        return {
            "version_no": 1, "audit": kwargs["ocr_audit"],
            "semantic_audit": kwargs["semantic_audit"],
            "leader_audit": kwargs["leader_audit"],
            "detected_numerals": ["16", "24"],
        }

    monkeypatch.setattr(draft_figures, "_audited_version", save)

    draft_figures.render_figure(
        7, 91, label="FIG. 3", caption=specification,
        numerals=["16 = second side", "24 = perimeter member"])

    assert generated == []
    assert saved[0]["source_kind"] == "deterministic"
    assert captured["semantic"]["pixel_anchor_audit"]["ok"] is True
    anchor = next(item for item in captured["semantic"]["anchors"]
                  if item["numeral"] == "24")
    assert (anchor["x"], anchor["y"]) == (
        draft_figures._pixel_to_normalized(190, 1400),
        draft_figures._pixel_to_normalized(450, 900),
    )


def test_closed_region_audit_is_not_required_without_an_exact_closed_shape_clause():
    audit = draft_figures.closed_region_audit(blank_png(), "A perspective view of a housing.")
    assert audit["ok"] is True and audit["required"] is False


def test_pixel_grounding_snaps_an_enclosed_line_feature_to_its_stroke():
    image = Image.new("RGB", (1000, 1000), "white")
    ImageDraw.Draw(image).rectangle((200, 200, 800, 600), outline="black", width=8)
    out = io.BytesIO()
    image.save(out, format="PNG")

    anchors, audit = draft_figures._ground_anchors_to_pixels(
        out.getvalue(), ["10 = handle"], [{
            "numeral": "10", "x": 500, "y": 400, "visible": True,
            "evidence": "inside the handle arch",
        }])

    assert audit["ok"] is True and audit["adjusted"][0]["numeral"] == "10"
    assert anchors[0]["y"] <= 210 or anchors[0]["y"] >= 590


def test_pixel_grounding_fails_closed_when_an_object_has_no_nearby_geometry():
    anchors, audit = draft_figures._ground_anchors_to_pixels(
        blank_png(1000, 1000), ["10 = base"], [{
            "numeral": "10", "x": 500, "y": 500, "visible": True,
            "evidence": "claimed base",
        }])

    assert anchors[0]["x"] == 500
    assert audit["ok"] is False and audit["ungrounded"][0]["numeral"] == "10"


def test_leader_audit_rejects_converged_or_wrong_endpoints():
    expected = ["10 = body", "12 = pump", "14 = outlet"]
    passing = draft_figures.leader_audit(expected, {
        "matches_spec": True, "summary": "all leaders are grounded", "errors": [],
        "labels": [
            {"numeral": "10", "correct": True, "evidence": "leader ends on the body"},
            {"numeral": "12", "correct": True, "evidence": "leader ends on the pump"},
            {"numeral": "14", "correct": True, "evidence": "leader ends on the outlet"},
        ],
    })
    assert passing["ok"] is True

    failed = draft_figures.leader_audit(expected, {
        "matches_spec": False, "summary": "leaders converge", "errors": [
            "Numerals 12 and 14 converge on the body."],
        "labels": [
            {"numeral": "10", "correct": True, "evidence": "leader ends on the body"},
            {"numeral": "12", "correct": False, "evidence": "leader ends on the body"},
            {"numeral": "14", "correct": False, "evidence": "leader ends in blank space"},
        ],
    })
    assert failed["ok"] is False
    assert failed["incorrect"] == ["12", "14"]
    assert "converge" in failed["errors"][0]


def test_leader_consensus_fails_when_the_adversarial_trace_disagrees():
    expected = ["10 = body", "12 = pump"]
    primary = {
        "matches_spec": True, "summary": "appears correct", "errors": [],
        "labels": [
            {"numeral": "10", "correct": True, "evidence": "body",
             "suggested_x": 300, "suggested_y": 400},
            {"numeral": "12", "correct": True, "evidence": "pump",
             "suggested_x": 700, "suggested_y": 400},
        ],
    }
    adversarial = {
        "matches_spec": False, "summary": "12 ends on the body",
        "errors": ["Numeral 12 ends on the neighboring body."],
        "labels": [
            {"numeral": "10", "correct": True, "evidence": "body",
             "suggested_x": 300, "suggested_y": 400},
            {"numeral": "12", "correct": False, "evidence": "neighboring body",
             "suggested_x": 760, "suggested_y": 420},
        ],
    }
    consensus = draft_figures.leader_consensus(expected, [primary, adversarial])
    assert consensus["ok"] is False and consensus["incorrect"] == ["12"]
    assert consensus["review_count"] == 2
    assert consensus["labels"][1]["suggested_x"] == 760


def test_only_the_current_two_trace_leader_review_is_accepted():
    current = {
        "ok": True, "inspected": True,
        "model_name": draft_figures.vision_model(),
        "prompt_version": draft_figures.LEADER_PROMPT_VERSION,
        "review_count": 2,
        "section_mark_anchor_audit": draft_figures._section_mark_anchor_audit([], []),
    }
    assert draft_figures.current_leader_audit(current) is True
    assert draft_figures.current_leader_audit({
        key: value for key, value in current.items()
        if key != "section_mark_anchor_audit"
    }) is False
    assert draft_figures.current_leader_audit({**current, "review_count": 1}) is False
    assert draft_figures.current_leader_audit({**current, "prompt_version": "old"}) is False
    assert draft_figures.current_leader_audit({"ok": True}) is False


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


def test_sheet_number_is_overlaid_at_top_center_and_larger_than_reference_numerals():
    raw = blank_png()
    anchors = [
        {"numeral": "10", "x": 200, "y": 300, "visible": True, "evidence": "body"},
    ]
    output = draft_figures.annotate_png(
        raw, "FIG. 1", anchors, sheet_number="1/5")
    layout = draft_figures._annotation_layout(raw, anchors, 1.0, sheet_number="1/5")
    image = Image.open(io.BytesIO(output)).convert("L")
    top_band = image.crop((0, 0, image.width, layout["source_y"]))
    ink_x = [x for y in range(top_band.height) for x in range(top_band.width)
             if top_band.getpixel((x, y)) < 32]

    assert ink_x
    assert abs(((min(ink_x) + max(ink_x)) / 2) - (image.width / 2)) < 3
    assert layout["sheet_font_size"] > layout["font_size"]
    assert output != draft_figures.annotate_png(raw, "FIG. 1", anchors, sheet_number="2/5")


def test_cutting_plane_arrows_and_both_section_designations_are_overlaid_deterministically():
    raw = blank_png()
    anchors = [{"numeral": "10", "x": 250, "y": 500, "visible": True}]
    marks = [{
        "designation": "3", "start_x": 500, "start_y": 100,
        "end_x": 500, "end_y": 800, "view_dx": -1000, "view_dy": 0,
    }]
    plain = draft_figures.annotate_png(raw, "FIG. 1", anchors)
    marked = draft_figures.annotate_png(
        raw, "FIG. 1", anchors, section_marks=marks)
    layout = draft_figures._annotation_layout(raw, anchors, 1.0)
    image = Image.open(io.BytesIO(marked)).convert("L")
    line_x = layout["source_x"] + round(500 * layout["source"].width / 1000)
    start_y = layout["source_y"] + round(100 * layout["source"].height / 1000)
    end_y = layout["source_y"] + round(800 * layout["source"].height / 1000)
    line_ink = sum(
        image.getpixel((line_x, y)) < 32 for y in range(start_y, end_y + 1))
    left_end_ink = sum(
        image.getpixel((x, y)) < 32
        for x in range(max(0, line_x - 80), line_x)
        for y in range(max(0, start_y - 45), min(image.height, start_y + 46)))

    assert marked != plain
    assert line_ink > 80
    assert left_end_ink > 25
    assert marked == draft_figures.annotate_png(
        raw, "FIG. 1", anchors, section_marks=marks)


def test_section_designation_is_separated_from_arrowhead_for_ocr():
    text_x, text_y = draft_figures._section_mark_designation_position(
        tip=(100, 100), view=(1.0, 0.0), line=(0.0, 1.0), outward=1,
        font_size=26, text_size=(16, 24), canvas_size=(400, 400))

    assert text_x >= 120
    assert 0 <= text_y <= 376


def test_deterministic_leader_endpoint_has_a_vision_visible_dot():
    raw = blank_png()
    anchors = [{"numeral": "10", "x": 500, "y": 500,
                "visible": True, "evidence": "body"}]
    layout = draft_figures._annotation_layout(raw, anchors, 1.0)
    output = Image.open(io.BytesIO(
        draft_figures.annotate_png(raw, "FIG. 1", anchors)))
    target_x = layout["source_x"] + round(500 * layout["source"].width / 1000)
    target_y = layout["source_y"] + round(500 * layout["source"].height / 1000)

    assert output.getpixel((target_x, target_y + 6))[0] < 32


def test_a_leader_route_never_runs_through_another_terminal_dot():
    raw = blank_png()
    anchors = [
        {"numeral": "16", "x": 100, "y": 500, "visible": True},
        {"numeral": "24", "x": 200, "y": 500, "visible": True},
        {"numeral": "30", "x": 500, "y": 500, "visible": True},
    ]
    layout = draft_figures._annotation_layout(raw, anchors, 2.2)
    output = Image.open(io.BytesIO(
        draft_figures.annotate_png(raw, "FIG. 3", anchors, scale=2.2)))
    first_x = layout["source_x"] + round(100 * layout["source"].width / 1000)
    target_y = layout["source_y"] + round(500 * layout["source"].height / 1000)

    # The line for numeral 24 used to run horizontally through numeral 16's dot.
    assert output.getpixel((first_x + 35, target_y))[0] > 240


def test_leader_row_optimizer_removes_endpoint_collisions_and_crossings():
    routes = [
        {"line_x": 0, "y": 0, "target_x": 100, "target_y": 100, "side": "left"},
        {"line_x": 0, "y": 100, "target_x": 200, "target_y": 100, "side": "left"},
        {"line_x": 0, "y": 200, "target_x": 500, "target_y": 100, "side": "left"},
    ]

    optimized = draft_figures._optimize_leader_rows(routes, clearance=15)

    assert draft_figures._leader_layout_score(optimized, 15)[:2] == (0, 0)


def test_terminal_dot_has_a_white_halo_when_it_lands_on_black_geometry():
    image = Image.new("RGB", (640, 420), "white")
    ImageDraw.Draw(image).line((0, 210, 639, 210), fill="black", width=18)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    anchors = [{"numeral": "10", "x": 500, "y": 500, "visible": True}]
    layout = draft_figures._annotation_layout(raw.getvalue(), anchors, 1.0)

    output = Image.open(io.BytesIO(
        draft_figures.annotate_png(raw.getvalue(), "FIG. 1", anchors)))
    target_x = layout["source_x"] + round(500 * layout["source"].width / 1000)
    target_y = layout["source_y"] + round(500 * layout["source"].height / 1000)

    assert output.getpixel((target_x, target_y))[0] < 32
    assert output.getpixel((target_x, target_y + 8))[0] > 240


@pytest.mark.parametrize("evidence", [
    "a point on the contact line where the leg meets the base",
    "the top horizontal line of the uppermost hatched layer",
])
def test_terminal_dot_does_not_erase_an_explicit_boundary_target(evidence):
    image = Image.new("RGB", (640, 420), "white")
    ImageDraw.Draw(image).line((0, 210, 639, 210), fill="black", width=6)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    anchors = [{
        "numeral": "26", "x": 500, "y": 500, "visible": True,
        "evidence": evidence,
    }]
    layout = draft_figures._annotation_layout(raw.getvalue(), anchors, 1.0)

    output = Image.open(io.BytesIO(
        draft_figures.annotate_png(raw.getvalue(), "FIG. 2", anchors)))
    target_x = layout["source_x"] + round(500 * layout["source"].width / 1000)
    target_y = layout["source_y"] + round(500 * layout["source"].height / 1000)

    assert output.getpixel((target_x, target_y))[0] < 32
    assert output.getpixel((target_x + 9, target_y))[0] < 32


def test_terminal_dot_keeps_a_halo_when_line_geometry_is_only_an_exclusion():
    image = Image.new("RGB", (640, 420), "white")
    ImageDraw.Draw(image).line((0, 210, 639, 210), fill="black", width=18)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    anchors = [{
        "numeral": "10", "x": 500, "y": 500, "visible": True,
        "evidence": "inside the base face, not on the ring or its outline",
    }]
    layout = draft_figures._annotation_layout(raw.getvalue(), anchors, 1.0)

    output = Image.open(io.BytesIO(
        draft_figures.annotate_png(raw.getvalue(), "FIG. 1", anchors)))
    target_x = layout["source_x"] + round(500 * layout["source"].width / 1000)
    target_y = layout["source_y"] + round(500 * layout["source"].height / 1000)

    assert output.getpixel((target_x, target_y))[0] < 32
    assert output.getpixel((target_x + 9, target_y))[0] > 240


def test_center_endpoints_route_opposite_a_right_endpoint_on_the_same_row():
    anchors = [
        {"numeral": "16", "x": 500, "y": 900, "visible": True},
        {"numeral": "24", "x": 845, "y": 500, "visible": True},
        {"numeral": "30", "x": 500, "y": 500, "visible": True},
    ]

    layout = draft_figures._annotation_layout(blank_png(), anchors, 1.0)

    assert {item["numeral"] for item in layout["left_items"]} == {"16", "30"}
    assert {item["numeral"] for item in layout["right_items"]} == {"24"}


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


def test_geometry_prompt_strips_a_complete_hyphenated_cutting_plane_paragraph():
    caption = (
        "The upper frame half 10 surrounds the pipe 90.\n\n"
        "A short broken cutting-plane line lies on the radial centre line. Its outer end lies "
        "outside the frame and its inner end stops at the jaw pad 40. A short arrow at each end "
        "points left. Each end carries 3 as the section designation, so it reads as line 3-3; "
        "it is not a reference numeral.\n\n"
        "The latch 16 bridges the frame ends.")

    cleaned = draft_figures._geometry_text(
        caption, ["10 = upper frame half", "90 = pipe", "40 = jaw pad", "16 = latch"])

    assert "upper frame half" in cleaned and "latch" in cleaned
    assert "cutting" not in cleaned.lower()
    assert "outer end" not in cleaned.lower() and "inner end" not in cleaned.lower()
    assert "arrow" not in cleaned.lower() and "designation" not in cleaned.lower()
    assert "line -" not in cleaned.lower()


def test_geometry_prompt_strips_adjacent_paragraphs_for_two_cutting_planes():
    caption = (
        "The underside is one rectangular ring around a plain inner face.\n\n"
        "Two straight broken section lines cross the whole view, one clear of the other.\n\n"
        "Each enters beyond the outer edge on one side, crosses the ring and inner face, "
        "leaves beyond the opposite edge, and carries a short arrow at each end.\n\n"
        "One runs horizontally and reads as section line 2-2. It marks the plane of FIG. 2.\n\n"
        "The other runs vertically and reads as section line 4-4. It marks the plane of FIG. 4, "
        "at a place where a clearance is present.\n\n"
        "The ring surface remains continuous between its outer and inner edges.")

    cleaned = draft_figures._geometry_text(caption)

    assert draft_figures.section_designations(caption) == ["2", "4"]
    assert "rectangular ring" in cleaned and "ring surface remains continuous" in cleaned
    assert "each enters" not in cleaned.lower()
    assert "arrow" not in cleaned.lower()
    assert "marks the plane" not in cleaned.lower()
    assert "section line" not in cleaned.lower()
    assert " two" not in cleaned.lower() and " four" not in cleaned.lower()
    assert "at a place" not in cleaned.lower()


def test_geometry_spec_strips_arbitrary_annotation_point_placement():
    caption = (
        "The second side 16 is the straight lower edge of the slab. "
        "The extraction opening 30 breaks through that edge at the horizontal center. "
        "The second side 16 is identified at one point on that lower edge, at the exact "
        "horizontal center of the sheet. "
        "The covering element 36 is identified by a point inside its right-hand quarter.")

    cleaned = draft_figures._geometry_text(
        caption, ["16 = second side", "30 = extraction opening", "36 = covering element"])
    specification = json.loads(draft_figures._review_specification(
        "FIG. 2", caption,
        ["16 = second side", "30 = extraction opening", "36 = covering element"],
        geometry_only=True))

    assert "straight lower edge" in cleaned
    assert "breaks through that edge" in cleaned
    assert "identified at" not in cleaned and "identified by a point" not in cleaned
    assert specification["caption"] == cleaned
    assert specification["endpoint_targets"] == [
        {
            "numeral": "16",
            "part": "second side",
            "definition": "The second side 16 is the straight lower edge of the slab.",
            "target": (
                "The second side 16 is identified at one point on that lower edge, at the exact "
                "horizontal center of the sheet."
            ),
        },
        {
            "numeral": "30",
            "part": "extraction opening",
            "definition": (
                "The extraction opening 30 breaks through that edge at the horizontal center."
            ),
            "target": "On the visible extraction opening geometry.",
        },
        {
            "numeral": "36",
            "part": "covering element",
            "definition": "covering element",
            "target": (
                "The covering element 36 is identified by a point inside its right-hand quarter."
            ),
        },
    ]


def test_long_geometry_prompt_keeps_components_change_request_and_no_text_rule():
    caption = "A rigid frame surrounds a central opening with parallel bearing faces. " * 180
    prompt = draft_figures.build_prompt(
        "FIG. 1 - perspective view", caption,
        ["10 = rigid base", "12 = transverse pump", "14 = square fastener"],
        instruction="Move the square fastener onto the open top face.",
        spec_context="The rigid base carries the transverse pump.")

    assert len(prompt) <= draft_figures.MAX_PROMPT_CHARS
    assert "the rigid base" in prompt
    assert "the transverse pump" in prompt
    assert "the square fastener" in prompt
    assert "CHANGE REQUESTED" in prompt
    assert prompt.endswith("Return geometry only, without text or digits.")


def test_visual_review_spec_keeps_the_full_realistic_brief_and_every_part():
    caption = (
        "A rectangular frame supports a transverse pump above an open central chamber. " * 85 +
        "The terminal flange remains below the upper plate and touches neither side wall.")
    numerals = ["10 = rectangular frame", "12 = transverse pump", "14 = terminal flange"]

    geometry = json.loads(draft_figures._review_specification(
        "FIG. 4 - sectional view", caption, numerals, geometry_only=True))
    endpoints = json.loads(draft_figures._review_specification(
        "FIG. 4 - sectional view", caption, numerals, geometry_only=False))

    for specification in (geometry, endpoints):
        assert specification["figure_label"] == "FIG. 4"
        assert "terminal flange remains below the upper plate" in specification["caption"].lower()
        assert [item["numeral"] for item in specification["parts"]] == ["10", "12", "14"]


def test_leader_review_spec_contains_only_annotation_routing():
    specification = json.loads(draft_figures._leader_routing_spec(
        "FIG. 2 - sectional view",
        ["24 = perimeter member on the right leg", "36 = covering element on the right side"]))

    assert specification == {
        "figure_label": "FIG. 2",
        "expected_numerals": ["24", "36"],
        "section_designations": [],
    }
    assert "perimeter member" not in json.dumps(specification).lower()


def test_marked_endpoint_spec_contains_local_part_definitions_and_targets():
    caption = (
        "The complete sheet contains three concentric rectangles and one central circle. "
        "The base 12 is the plate itself, whose edge is the largest rectangle. "
        "The second side 16 is the plain margin between the first and second rectangles. "
        "The base 12 is identified at a point on the left-hand quarter.")
    specification = json.loads(draft_figures._marked_endpoint_specification(
        "FIG. 3", caption, ["12 = base", "16 = second side"]))

    assert specification["figure_label"] == "FIG. 3"
    assert specification["parts"] == [
        {
            "numeral": "12", "part": "base",
            "definition": "The base 12 is the plate itself, whose edge is the largest rectangle.",
            "target": "The base 12 is identified at a point on the left-hand quarter.",
        },
        {
            "numeral": "16", "part": "second side",
            "definition": (
                "The second side 16 is the plain margin between the first and second rectangles."),
            "target": "On the visible second side geometry.",
        },
    ]
    encoded = json.dumps(specification).lower()
    assert "complete sheet" not in encoded and "identified at" in encoded


def test_marked_endpoint_spec_prefers_points_to_target_over_later_cross_reference():
    caption = (
        "A fragmentary portion of the rail 10 lies on the workpiece. The numeral 10 points to "
        "the upper face of the rail 10, well inside its outline. A transverse center index 24 "
        "is a straight line segment on the rail 10. The numeral 24 and its leader are to the "
        "left of the rail 10, and the leader points to the transverse center index.")

    specification = json.loads(draft_figures._marked_endpoint_specification(
        "FIG. 7", caption, ["10 = rail", "24 = transverse center index"]))
    parts = {item["numeral"]: item for item in specification["parts"]}

    assert parts["10"]["target"] == (
        "The numeral 10 points to the upper face of the rail 10, well inside its outline.")
    assert parts["24"]["target"].startswith("The numeral 24 and its leader")


def test_marked_endpoint_spec_keeps_a_following_target_sentence_in_the_same_bullet():
    caption = (
        "- The vibration device 10 is the whole rectangular assembly. "
        "Identified on the open upper surface of its slab, not on a component block.\n"
        "- The motor 18 is the left rectangular block. It is taller than the base. "
        "Identified on its front face.")

    specification = json.loads(draft_figures._marked_endpoint_specification(
        "FIG. 1", caption, ["10 = vibration device", "18 = motor"]))

    assert specification["parts"][0]["target"] == (
        "Identified on the open upper surface of its slab, not on a component block.")
    assert specification["parts"][1]["target"] == "Identified on its front face."


def test_marked_endpoint_spec_splits_inline_bullets_before_matching_targets():
    caption = (
        "Geometry to be drawn - The base 12 is a broad rectangular slab. "
        "Identified in the open stretch midway between the column and the nearer handle upright. "
        "- The handle 44 is an inverted U with two uprights and one crossbar. "
        "Identified on the crossbar, midway along it.")

    specification = json.loads(draft_figures._marked_endpoint_specification(
        "FIG. 1", caption, ["12 = base", "44 = handle"]))

    assert specification["parts"] == [
        {
            "numeral": "12", "part": "base",
            "definition": "The base 12 is a broad rectangular slab.",
            "target": (
                "Identified in the open stretch midway between the column and the nearer "
                "handle upright."),
        },
        {
            "numeral": "44", "part": "handle",
            "definition": (
                "The handle 44 is an inverted U with two uprights and one crossbar."),
            "target": "Identified on the crossbar, midway along it.",
        },
    ]


def test_marked_endpoint_spec_uses_each_parts_own_bullet_not_cross_references():
    caption = (
        "Perspective view from above onto the covering element 36. The artwork contains only "
        "the listed reference numerals and leader lines.\n"
        "- The vibration device 10 is the whole machine. Identified on its outer silhouette, "
        "clear of the handle 44 and the perimeter member 24.\n"
        "- The perimeter member 24 is the broad band beneath the machine. Identified deep "
        "inside the front surface of that band.\n"
        "- The handle 44 is the closed grip above the machine. Identified on the drawn outline "
        "of the grip.\n"
        "- The covering element 36 is the large plain tile. Identified well inside the open "
        "tile surface to the right of the machine.")

    specification = json.loads(draft_figures._marked_endpoint_specification(
        "FIG. 1", caption,
        ["10 = vibration device", "24 = perimeter member", "36 = covering element",
         "44 = handle"]))
    parts = {item["numeral"]: item for item in specification["parts"]}

    assert parts["10"]["target"].startswith("Identified on its outer silhouette")
    assert parts["24"]["target"] == (
        "Identified deep inside the front surface of that band.")
    assert parts["36"]["definition"] == "The covering element 36 is the large plain tile."
    assert parts["36"]["target"].startswith("Identified well inside the open tile surface")
    assert parts["44"]["target"] == "Identified on the drawn outline of the grip."


def test_current_visual_audits_are_bound_to_the_configured_review_model(monkeypatch):
    monkeypatch.setattr(draft_figures, "vision_model", lambda: "gemini-2.5-pro")
    digest = "a" * 64
    semantic = accepted_semantic_audit(
        specification_hash=digest, model_name="gemini-2.5-flash")
    leader = accepted_leader_audit(
        specification_hash=digest, model_name="gemini-2.5-flash")

    assert draft_figures.current_semantic_audit(semantic) is False
    assert draft_figures.current_leader_audit(leader) is False
    assert draft_figures.current_marked_anchor_audit(
        semantic["marked_anchor_audit"], specification_hash=digest) is False


def test_endpoint_review_specs_strip_geometry_only_annotation_prohibitions(monkeypatch):
    caption = ("No word, letter, caption, label, digit, or other writing is drawn. "
               "The body 10 is visible on the upper surface.")
    endpoint_spec = json.loads(draft_figures._review_specification(
        "FIG. 1", caption, ["10 = body"], geometry_only=True))
    assert "no word" not in endpoint_spec["caption"].lower()
    assert "body is visible" in endpoint_spec["caption"].lower()

    modes = []
    monkeypatch.setattr(
        draft_figures, "_review_specification",
        lambda *args, **kwargs: modes.append(kwargs["geometry_only"]) or "{}")
    cached = iter([accepted_leader_audit(), accepted_marked_anchor_audit()])
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *args: next(cached))

    draft_figures.inspect_leaders(
        blank_png(), label="FIG. 1", caption=caption,
        numerals=["10 = body"])
    draft_figures.inspect_marked_anchors(
        blank_png(), label="FIG. 1", caption=caption,
        numerals=["10 = body"],
        anchors=[{"numeral": "10", "x": 500, "y": 500, "visible": True}])

    assert modes == []


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


def test_render_refuses_to_store_a_sheet_with_a_misplaced_leader(monkeypatch):
    accept_pixel_grounding(monkeypatch)
    discarded = []
    monkeypatch.setattr(draft_figures, "_cached_generate", lambda *a, **k: blank_png())
    monkeypatch.setattr(
        draft_figures, "_discard_cached_generation",
        lambda prompt, previous=None: discarded.append((prompt, previous)))
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": True, "anchors": [{"numeral": "10", "x": 200, "y": 300,
                                    "visible": True, "evidence": "body"}]})
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99})
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": False, "inspected": True, "errors": ["10 ends in blank space"],
        "incorrect": ["10"], "missing": [], "labels": [
            {"numeral": "10", "correct": False, "evidence": "blank space",
             "suggested_x": 0, "suggested_y": 0}]})
    monkeypatch.setattr(
        draft_figures, "create_figure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not persist")))

    with pytest.raises(draft_figures.FigureError, match="leader placement"):
        draft_figures.render_figure(
            7, 91, label="FIG. 1", caption="side view of body", numerals=["10 = body"])
    assert len(discarded) == 1


def test_render_refuses_a_false_positive_when_marked_endpoint_is_on_neighboring_geometry(
        monkeypatch):
    accept_pixel_grounding(monkeypatch)
    discarded = []
    monkeypatch.setattr(draft_figures, "_cached_generate", lambda *a, **k: blank_png())
    monkeypatch.setattr(
        draft_figures, "_discard_cached_generation",
        lambda prompt, previous=None: discarded.append((prompt, previous)))
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": True, "anchors": [{"numeral": "26", "x": 220, "y": 650,
                                    "visible": True, "evidence": "bearing face"}]})
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["26"], "figure_label": "FIG. 2",
        "other_text": [], "confidence": 0.99})
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [],
        "labels": [{"numeral": "26", "correct": True,
                    "evidence": "the black leader appears plausible"}]})
    monkeypatch.setattr(draft_figures, "inspect_marked_anchors", lambda *a, **k: {
        "ok": False, "inspected": True,
        "prompt_version": draft_figures.MARKED_ANCHOR_PROMPT_VERSION,
        "review_count": draft_figures.MARKED_ANCHOR_REVIEW_COUNT,
        "errors": ["The marked center is inside the lower band."],
        "incorrect": ["26"], "missing": [], "labels": []})
    monkeypatch.setattr(
        draft_figures, "create_figure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not persist")))

    with pytest.raises(draft_figures.FigureError, match="marked endpoint"):
        draft_figures.render_figure(
            7, 91, label="FIG. 2", caption="bearing face at the upper boundary of the band",
            numerals=["26 = bearing face"])
    assert len(discarded) == 1


def test_render_reroutes_a_rejected_leader_without_moving_the_reviewed_feature(monkeypatch):
    accept_pixel_grounding(monkeypatch)
    raw = blank_png()
    initial = [{"numeral": "10", "x": 200, "y": 300,
                "visible": True, "evidence": "body"}]
    layout = draft_figures._annotation_layout(raw, initial, 1.0)
    suggested_x = round((layout["source_x"] + layout["source"].width * 0.8) *
                        1000 / layout["canvas_width"])
    suggested_y = round((layout["source_y"] + layout["source"].height * 0.5) *
                        1000 / layout["canvas_height"])
    monkeypatch.setattr(draft_figures, "_cached_generate", lambda *a, **k: raw)
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": True, "anchors": initial})
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99})
    calls = []

    def inspect(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            return {"ok": False, "inspected": True, "errors": ["wrong endpoint"],
                    "incorrect": ["10"], "missing": [], "labels": [{
                        "numeral": "10", "correct": False, "evidence": "wrong endpoint",
                        "suggested_x": suggested_x, "suggested_y": suggested_y}]}
        return {"ok": True, "inspected": True, "errors": [], "incorrect": [],
                "labels": [{"numeral": "10", "correct": True, "evidence": "body",
                            "suggested_x": suggested_x, "suggested_y": suggested_y}]}

    monkeypatch.setattr(draft_figures, "inspect_leaders", inspect)
    monkeypatch.setattr(draft_figures, "create_figure", lambda *a, **k: {"id": 44})
    saved = []
    monkeypatch.setattr(draft_figures, "_audited_version", lambda *a, **k: saved.append(k) or {
        "version_no": 1, "audit": k["ocr_audit"], "semantic_audit": k["semantic_audit"],
        "leader_audit": k["leader_audit"], "detected_numerals": ["10"]})

    result = draft_figures.render_figure(
        7, 91, label="FIG. 1", caption="body", numerals=["10 = body"])
    assert result["leader_audit"]["ok"] is True and len(calls) == 2
    assert saved[0]["semantic_audit"]["anchors"][0]["x"] == 200


def test_leader_routing_cannot_move_a_grounded_endpoint_into_blank_paper(monkeypatch):
    image = Image.new("RGB", (1000, 1000), "white")
    ImageDraw.Draw(image).rectangle((200, 200, 800, 600), outline="black", width=8)
    out = io.BytesIO()
    image.save(out, format="PNG")
    raw = out.getvalue()
    initial = [{"numeral": "10", "x": 500, "y": 400,
                "visible": True, "evidence": "inside the body"}]
    layout = draft_figures._annotation_layout(raw, initial, 1.0)
    suggested_x = round((layout["source_x"] + layout["source"].width * 0.5) *
                        1000 / layout["canvas_width"])
    suggested_y = round((layout["source_y"] + layout["source"].height * 0.8) *
                        1000 / layout["canvas_height"])
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99})
    calls = []

    def inspect(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            return {"ok": False, "inspected": True, "errors": ["wrong endpoint"],
                    "incorrect": ["10"], "missing": [], "labels": [{
                        "numeral": "10", "correct": False, "evidence": "blank paper",
                        "suggested_x": suggested_x, "suggested_y": suggested_y}]}
        return {"ok": True, "inspected": True, "errors": [], "incorrect": [],
                "labels": [{"numeral": "10", "correct": True, "evidence": "body",
                            "suggested_x": suggested_x, "suggested_y": suggested_y}]}

    monkeypatch.setattr(draft_figures, "inspect_leaders", inspect)
    monkeypatch.setattr(draft_figures, "inspect_marked_anchors", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [], "missing": [],
        "review_count": 3, "labels": [{
            "numeral": "10", "correct": True, "repairable": True,
            "evidence": "the endpoint remains on the grounded body",
            "suggested_x": 500, "suggested_y": 400,
            "correct_votes": 3, "incorrect_votes": 0,
        }],
    })
    _png, _labels, leaders, anchors, pixel = draft_figures._compose_checked_sheet(
        raw, label="FIG. 1", caption="body", numerals=["10 = body"],
        semantic={"anchors": initial, "pixel_anchor_audit": accepted_semantic_audit()[
            "pixel_anchor_audit"]})

    assert leaders["ok"] is True and len(calls) == 2
    assert pixel["ok"] is True
    assert (anchors[0]["x"], anchors[0]["y"]) == (500, 400)


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


def test_uninspected_semantic_review_is_transient_and_preserves_the_generation(monkeypatch):
    generated = []
    discarded = []

    def generate(prompt, previous=None):
        generated.append((prompt, previous))
        return blank_png()

    monkeypatch.setattr(draft_figures, "_cached_generate", generate)
    monkeypatch.setattr(
        draft_figures, "_discard_cached_generation",
        lambda prompt, previous=None: discarded.append((prompt, previous)))
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": False, "inspected": False, "missing": ["10"],
        "errors": ["Semantic inspection failed: upstream reset"], "anchors": [],
    })

    with pytest.raises(draft_figures.FigureTransientError, match="temporarily unavailable"):
        draft_figures.render_figure(
            7, 91, label="FIG. 1", caption="side view of body",
            numerals=["10 = body"])

    assert len(generated) == 1
    assert discarded == []


def test_structural_surplus_resets_once_then_uses_targeted_edits(monkeypatch):
    generated = []

    def generate(_prompt, previous=None):
        png = blank_png(width=640 + len(generated))
        generated.append((previous, png))
        return png

    monkeypatch.setattr(draft_figures, "_cached_generate", generate)
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": False, "inspected": True, "missing": [],
        "errors": [
            "Unexpected extra concentric ring and a doubled pipe boundary are visible."
        ],
        "unexpected": ["unsupported internal slot"], "anchors": [],
    })

    with pytest.raises(draft_figures.FigureError, match="semantic"):
        draft_figures.render_figure(
            7, 91, label="FIG. 1", caption="plan view of a split clamp",
            numerals=["10 = body"])

    previous_images = [previous for previous, _png in generated]
    assert len(previous_images) == draft_figures.DEFAULT_SEMANTIC_ATTEMPTS
    assert previous_images[:2] == [None, None]
    assert previous_images[2:] == [
        generated[index][1] for index in range(1, len(generated) - 1)
    ]


def test_default_semantic_attempt_budget_allows_two_more_progressive_repairs():
    assert draft_figures._semantic_attempt_limit(None) == 8
    assert draft_figures._semantic_attempt_limit("") == 8
    assert draft_figures._semantic_attempt_limit("6") == 6
    assert draft_figures._semantic_attempt_limit("99") == 8
    assert draft_figures._semantic_attempt_limit("invalid") == 8


def test_changed_nonstructural_failure_keeps_corrected_canvas(monkeypatch):
    generated = []
    reviews = iter((
        "the jaw pads do not meet the pipe",
        "the latch does not bridge both frame ends",
        "the hinge blocks do not reach the pivot",
    ))

    def generate(_prompt, previous=None):
        png = blank_png(width=640 + len(generated))
        generated.append((previous, png))
        return png

    monkeypatch.setattr(draft_figures, "MAX_SEMANTIC_ATTEMPTS", 3)
    monkeypatch.setattr(draft_figures, "_cached_generate", generate)
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": False, "inspected": True, "missing": [],
        "errors": [next(reviews)], "unexpected": [], "anchors": [],
    })

    with pytest.raises(draft_figures.FigureError, match="semantic"):
        draft_figures.render_figure(
            7, 91, label="FIG. 1", caption="plan view of a split clamp",
            numerals=["10 = body"])

    assert [previous for previous, _png in generated] == [
        None,
        generated[0][1],
        generated[1][1],
    ]


def test_third_semantic_attempt_resets_repeatedly_rejected_geometry(monkeypatch):
    generated = []

    def generate(_prompt, previous=None):
        png = blank_png(width=640 + len(generated))
        generated.append((previous, png))
        return png

    monkeypatch.setattr(draft_figures, "_cached_generate", generate)
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": False, "missing": [],
        "errors": ["the required support is disconnected from the slab"], "anchors": [],
    })

    with pytest.raises(draft_figures.FigureError, match="semantic"):
        draft_figures.render_figure(
            7, 91, label="FIG. 2", caption="sectioned slab",
            numerals=["12 = base"])

    assert generated[0][0] is None
    assert generated[1][0] == generated[0][1]
    assert generated[2][0] is None
    assert generated[3][0] == generated[2][1]


def test_semantically_rejected_generation_is_evicted_from_cache(monkeypatch):
    discarded = []
    monkeypatch.setattr(draft_figures, "_cached_generate", lambda *a, **k: blank_png())
    monkeypatch.setattr(
        draft_figures, "_discard_cached_generation",
        lambda prompt, previous=None: discarded.append((prompt, previous)))
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": False, "missing": [], "errors": ["wrong geometry"], "anchors": []})

    with pytest.raises(draft_figures.FigureError, match="semantic"):
        draft_figures.render_figure(
            7, 91, label="FIG. 1", caption="side view of body",
            numerals=["10 = body"])

    assert len(discarded) == draft_figures.MAX_SEMANTIC_ATTEMPTS


def test_ocr_detected_geometry_text_regenerates_from_a_clean_canvas(monkeypatch):
    accept_pixel_grounding(monkeypatch)
    generated = []

    def generate(prompt, previous=None):
        generated.append((prompt, previous))
        return blank_png(width=640 + len(generated))

    monkeypatch.setattr(draft_figures, "_cached_generate", generate)
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": True, "anchors": [{"numeral": "10", "x": 200, "y": 300,
                                    "visible": True, "evidence": "body"}]})
    inspections = []

    def inspect_labels(*_args, **_kwargs):
        inspections.append(True)
        if len(generated) == 1:
            return {"ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
                    "other_text": ["BODY"], "confidence": 0.99}
        return {"ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
                "other_text": [], "confidence": 0.99}

    monkeypatch.setattr(draft_figures, "inspect_labels", inspect_labels)
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [], "labels": []})
    monkeypatch.setattr(draft_figures, "create_figure", lambda *a, **k: {"id": 44})
    monkeypatch.setattr(draft_figures, "_audited_version", lambda *a, **k: {
        "version_no": 1, "audit": k["ocr_audit"], "semantic_audit": k["semantic_audit"],
        "leader_audit": k["leader_audit"], "detected_numerals": ["10"]})

    result = draft_figures.render_figure(
        7, 91, label="FIG. 1", caption="body", numerals=["10 = body"])
    assert result["numeral_audit"]["ok"] is True
    assert len(generated) == 2 and generated[1][1] is None
    assert "forbidden writing" in generated[1][0].lower()


def test_render_stores_only_after_semantic_and_ocr_gates_pass(monkeypatch):
    accept_pixel_grounding(monkeypatch)
    events = []
    monkeypatch.setattr(draft_figures, "_cached_generate", lambda *a, **k: blank_png())
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": True, "missing": [], "errors": [], "unexpected": [],
        "anchors": [{"numeral": "10", "x": 200, "y": 300, "visible": True,
                     "evidence": "body"}]})
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: {
        "ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
        "other_text": [], "confidence": 0.99})
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [],
        "labels": [{"numeral": "10", "correct": True, "evidence": "body"}]})
    monkeypatch.setattr(draft_figures, "create_figure", lambda *a, **k: {"id": 44})

    def save(figure_id, **kwargs):
        events.append((figure_id, kwargs))
        return {"version_no": 1, "audit": kwargs["ocr_audit"],
                "semantic_audit": kwargs["semantic_audit"],
                "leader_audit": kwargs["leader_audit"], "detected_numerals": ["10"]}

    monkeypatch.setattr(draft_figures, "_audited_version", save)
    out = draft_figures.render_figure(
        7, 91, label="FIG. 1 - side view", caption="side view of the body",
        numerals=["10 = body"])
    assert out["numeral_audit"]["ok"] is True
    assert out["semantic_audit"]["ok"] is True
    assert out["leader_audit"]["ok"] is True
    assert events and events[0][0] == 44


def test_render_increases_deterministic_label_size_until_ocr_is_exact(monkeypatch):
    accept_pixel_grounding(monkeypatch)
    scales = []
    original = draft_figures.annotate_png
    monkeypatch.setattr(draft_figures, "_cached_generate", lambda *a, **k: blank_png())
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": True, "anchors": [{"numeral": "10", "x": 200, "y": 300,
                                    "visible": True, "evidence": "body"}]})
    monkeypatch.setattr(
        draft_figures, "annotate_png",
        lambda png, label, anchors, scale=1.0, sheet_number="": scales.append(scale) or
        original(png, label, anchors, scale=scale, sheet_number=sheet_number))
    inspections = iter([
        {"ok": True, "numerals": ["10", "10"], "figure_label": "FIG. 1",
         "other_text": [], "confidence": 0.96},
        {"ok": True, "numerals": ["10", "10"], "figure_label": "FIG. 1",
         "other_text": [], "confidence": 0.96},
        {"ok": True, "numerals": ["10"], "figure_label": "FIG. 1",
         "other_text": [], "confidence": 0.98},
    ])
    monkeypatch.setattr(draft_figures, "inspect_labels", lambda *a, **k: next(inspections))
    monkeypatch.setattr(draft_figures, "inspect_leaders", lambda *a, **k: {
        "ok": True, "inspected": True, "errors": [], "incorrect": [], "labels": []})
    monkeypatch.setattr(draft_figures, "create_figure", lambda *a, **k: {"id": 44})
    monkeypatch.setattr(draft_figures, "_audited_version", lambda *a, **k: {
        "version_no": 1, "audit": k["ocr_audit"], "semantic_audit": k["semantic_audit"],
        "leader_audit": k["leader_audit"],
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
        "semantic_audit": accepted_semantic_audit(),
        "leader_audit": accepted_leader_audit()})
    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="a body carrying a pump",
        numeral_table=[{"numeral": "10", "part": "body"},
                       {"numeral": "12", "part": "pump"}],
        figure_specs=[
            {"label": "FIG. 1 - side view", "caption": "body and pump", "numerals": ["10", "12"]},
            {"label": "FIG. 2", "caption": "pump detail", "numerals": ["12"]},
        ])
    assert out["ok"] is True and out["generated"] == 2 and len(calls) == 2
    assert calls[0]["numerals"] == ["10 = body", "12 = pump"]
    assert [call["sort_order"] for call in calls] == [1, 2]
    assert [call["sheet_number"] for call in calls] == ["1/2", "2/2"]


def test_ensure_project_figures_preserves_complete_geometry_brief(monkeypatch):
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [])
    calls = []
    monkeypatch.setattr(draft_figures, "render_figure", lambda *a, **values: (
        calls.append(values) or {
            "figure_id": 1, "numeral_audit": {"ok": True},
            "semantic_audit": accepted_semantic_audit(),
            "leader_audit": accepted_leader_audit(),
        }))
    geometry_brief = "opening geometry. " + ("precise relationship. " * 260) + "terminal geometry."

    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body",
        numeral_table=[{"numeral": "10", "part": "body"}],
        figure_specs=[{
            "label": "FIG. 1", "caption": geometry_brief, "numerals": ["10"],
        }])

    assert out["ok"] is True
    assert len(geometry_brief) > 4000
    assert calls[0]["caption"] == geometry_brief


def test_ensure_project_figures_rechecks_cancellation_before_each_sheet(monkeypatch):
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [])
    rendered = []
    monkeypatch.setattr(draft_figures, "render_figure", lambda *a, **values: (
        rendered.append(values["label"]) or {
            "figure_id": len(rendered), "numeral_audit": {"ok": True},
            "semantic_audit": accepted_semantic_audit(),
            "leader_audit": accepted_leader_audit(),
        }))
    checks = []

    def check_cancel():
        checks.append(len(checks) + 1)
        if len(checks) == 2:
            raise RuntimeError("turn cancelled")

    with pytest.raises(RuntimeError, match="turn cancelled"):
        draft_figures.ensure_project_figures(
            7, 91, sections={}, disclosure="body",
            numeral_table=[{"numeral": "10", "part": "body"}],
            figure_specs=[
                {"label": "FIG. 1", "caption": "first view", "numerals": ["10"]},
                {"label": "FIG. 2", "caption": "second view", "numerals": ["10"]},
            ], check_cancel=check_cancel)

    assert checks == [1, 2]
    assert rendered == ["FIG. 1"]


def test_ensure_project_figures_returns_collected_faults_when_time_expires(monkeypatch):
    """A bounded pass must not discard a real sheet defect before automatic repair sees it."""
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [])
    rendered = []

    def render(*_args, **values):
        rendered.append(values["label"])
        raise draft_figures.FigureError("the disclosed linkage is missing")

    checks = []

    def check_budget():
        checks.append(len(checks) + 1)
        return len(checks) < 2

    monkeypatch.setattr(draft_figures, "render_figure", render)
    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body and linkage",
        numeral_table=[{"numeral": "10", "part": "body"}],
        figure_specs=[
            {"label": "FIG. 1", "caption": "side view", "numerals": ["10"]},
            {"label": "FIG. 2", "caption": "bottom view", "numerals": ["10"]},
        ], check_cancel=check_budget)

    assert checks == [1, 2]
    assert rendered == ["FIG. 1"]
    assert out["budget_spent"] is True
    assert out["errors"] == ["FIG. 1: the disclosed linkage is missing"]


def test_ensure_project_figures_collects_every_failed_sheet_before_repair(monkeypatch):
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [])
    attempted = []

    def render(*_args, **values):
        attempted.append(values["label"])
        if "1" in values["label"]:
            raise draft_figures.FigureError("wrong motor axis")
        return {"figure_id": 2, "numeral_audit": {"ok": True},
                "semantic_audit": accepted_semantic_audit(),
                "leader_audit": accepted_leader_audit()}

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


def test_ensure_project_figures_defers_transient_capacity_errors(monkeypatch):
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [])
    monkeypatch.setattr(
        draft_figures, "render_figure",
        lambda *a, **k: (_ for _ in ()).throw(
            draft_figures.FigureTransientError("429 RESOURCE_EXHAUSTED")))

    with pytest.raises(draft_figures.FigureTransientError, match="RESOURCE_EXHAUSTED"):
        draft_figures.ensure_project_figures(
            7, 91, sections={}, disclosure="body",
            numeral_table=[{"numeral": "10", "part": "body"}],
            figure_specs=[{
                "label": "FIG. 1", "caption": "side view", "numerals": ["10"],
            }])


def test_changed_figure_spec_is_reinspected_even_when_its_numerals_are_unchanged(monkeypatch):
    spec = {"label": "FIG. 1", "caption": "new sectional view", "numerals": ["10"]}
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 1", "active_version": 1,
        "versions": [{"version_no": 1, "numeral_audit": {"ok": True, "expected": ["10"]},
                      "semantic_audit": {"ok": True, "specification_hash": "stale"}}],
    }])
    calls = []
    monkeypatch.setattr(draft_figures, "render_figure", lambda *a, **k: calls.append(k) or {
        "figure_id": 8, "numeral_audit": {"ok": True},
        "semantic_audit": accepted_semantic_audit(),
        "leader_audit": accepted_leader_audit()})
    monkeypatch.setattr(draft_figures, "archive_figure", lambda *a: True)
    draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body", numeral_table=[{"numeral": "10", "part": "body"}],
        figure_specs=[spec])
    assert len(calls) == 1


def test_wrong_sheet_total_is_recomposed_even_when_geometry_is_current(monkeypatch):
    spec = {"label": "FIG. 1", "caption": "side view", "numerals": ["10"]}
    digest = draft_figures.specification_hash("FIG. 1", "side view", ["10 = body"])
    active = {
        "version_no": 1,
        "numeral_audit": accepted_ocr_audit("1/2"),
        "semantic_audit": accepted_semantic_audit(specification_hash=digest),
        "leader_audit": accepted_leader_audit(specification_hash=digest),
    }
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 1", "caption": "side view", "sort_order": 1,
        "active_version": 1, "versions": [active],
    }])
    calls = []
    monkeypatch.setattr(draft_figures, "render_figure", lambda *a, **values: (
        calls.append(values) or {
            "figure_id": 8, "numeral_audit": {"ok": True},
            "semantic_audit": accepted_semantic_audit(),
            "leader_audit": accepted_leader_audit(),
        }))
    monkeypatch.setattr(draft_figures, "archive_figure", lambda *a: True)

    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body",
        numeral_table=[{"numeral": "10", "part": "body"}], figure_specs=[spec])

    assert out["generated"] == 1 and out["reused"] == 0
    assert calls[0]["sheet_number"] == "1/1"


def test_new_deterministic_renderer_replaces_a_previously_checked_generated_sheet(monkeypatch):
    spec = {"label": "FIG. 1", "caption": "current chamber section", "numerals": ["10"]}
    digest = draft_figures.specification_hash(
        "FIG. 1", "current chamber section", ["10 = chamber"])
    active = {
        "version_no": 3,
        "source_kind": "generated",
        "numeral_audit": accepted_ocr_audit(),
        "semantic_audit": accepted_semantic_audit(specification_hash=digest),
        "leader_audit": accepted_leader_audit(specification_hash=digest),
    }
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 1", "caption": "current chamber section",
        "sort_order": 1, "active_version": 3, "versions": [active],
    }])
    monkeypatch.setattr(
        draft_figures, "_deterministic_geometry_png",
        lambda caption: b"current deterministic renderer pixels")
    monkeypatch.setattr(
        draft_figures, "png_bytes", lambda *a, **k: ("image/png", b"older generated pixels"))
    calls = []
    monkeypatch.setattr(draft_figures, "render_figure", lambda *a, **values: (
        calls.append(values) or {
            "figure_id": 8, "numeral_audit": {"ok": True},
            "semantic_audit": accepted_semantic_audit(),
            "leader_audit": accepted_leader_audit(),
        }))
    monkeypatch.setattr(draft_figures, "archive_figure", lambda *a: True)

    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="chamber",
        numeral_table=[{"numeral": "10", "part": "chamber"}], figure_specs=[spec])

    assert out["generated"] == 1 and out["reused"] == 0
    assert len(calls) == 1


def test_obsolete_and_duplicate_sheets_are_archived_without_losing_history(monkeypatch):
    spec = {"label": "FIG. 1", "caption": "side view", "numerals": ["10"]}
    digest = draft_figures.specification_hash("FIG. 1", "side view", ["10 = body"])
    active = {"version_no": 1, "numeral_audit": accepted_ocr_audit(),
              "semantic_audit": accepted_semantic_audit(specification_hash=digest),
              "leader_audit": accepted_leader_audit(specification_hash=digest)}
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [
        {"id": 2, "figure_label": "FIG. 1", "active_version": 1, "versions": [active]},
        {"id": 3, "figure_label": "FIG. 1", "active_version": 1, "versions": [active]},
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


def test_existing_figure_metadata_is_refreshed_from_the_current_spec(monkeypatch):
    spec = {"label": "FIG. 2 - current sectional view", "caption": "current caption",
            "numerals": ["10"]}
    digest = draft_figures.specification_hash(
        spec["label"], spec["caption"], ["10 = body"])
    active = {
        "version_no": 4,
        "numeral_audit": accepted_ocr_audit(),
        "semantic_audit": accepted_semantic_audit(specification_hash=digest),
        "leader_audit": accepted_leader_audit(specification_hash=digest),
    }
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 2 - stale label", "caption": "stale caption",
        "sort_order": 99, "active_version": 4, "versions": [active],
    }])
    updates = []
    monkeypatch.setattr(
        draft_figures, "update_figure_metadata",
        lambda *args: updates.append(args), raising=False)
    monkeypatch.setattr(
        draft_figures, "render_figure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("checked sheet should be reused")))
    monkeypatch.setattr(draft_figures, "archive_figure", lambda *a: True)

    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body",
        numeral_table=[{"numeral": "10", "part": "body"}], figure_specs=[spec])

    assert out["ok"] is True and out["reused"] == 1
    assert updates == [(8, 91, "FIG. 2", "current caption", 1)]


def test_exact_checked_historical_sheet_is_reactivated_after_a_rollback(monkeypatch):
    spec = {"label": "FIG. 1", "caption": "side view", "numerals": ["10"]}
    digest = draft_figures.specification_hash("FIG. 1", "side view", ["10 = body"])
    stale = {
        "version_no": 1,
        "numeral_audit": accepted_ocr_audit(),
        "semantic_audit": accepted_semantic_audit(specification_hash="old-spec"),
        "leader_audit": accepted_leader_audit(specification_hash="old-spec"),
    }
    checked = {
        "version_no": 4,
        "numeral_audit": accepted_ocr_audit(),
        "semantic_audit": accepted_semantic_audit(specification_hash=digest),
        "leader_audit": accepted_leader_audit(specification_hash=digest),
    }
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 1", "caption": "side view", "sort_order": 1,
        "active_version": 1, "versions": [checked, stale],
    }])
    activations = []
    monkeypatch.setattr(
        draft_figures, "set_active",
        lambda figure_id, user_id, version_no, **values: (
            activations.append((figure_id, user_id, version_no, values)) or True))
    monkeypatch.setattr(
        draft_figures, "render_figure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("checked sheet should be reused")))
    monkeypatch.setattr(draft_figures, "archive_figure", lambda *a: True)

    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body",
        numeral_table=[{"numeral": "10", "part": "body"}], figure_specs=[spec])

    assert out["ok"] is True and out["reused"] == 1 and out["generated"] == 0
    assert activations == [(8, 91, 4, {"expected_specification_hash": digest})]


def test_a_previous_leader_review_is_never_reused(monkeypatch):
    spec = {"label": "FIG. 1", "caption": "side view", "numerals": ["10"]}
    digest = draft_figures.specification_hash("FIG. 1", "side view", ["10 = body"])
    active = {
        "version_no": 1,
        "numeral_audit": {"ok": True, "expected": ["10"]},
        "semantic_audit": accepted_semantic_audit(specification_hash=digest),
        "leader_audit": {
            "ok": True, "inspected": True, "specification_hash": digest,
            "prompt_version": "figure-leader-v2-single-review", "review_count": 1,
        },
    }
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 3, "figure_label": "FIG. 1", "active_version": 1, "versions": [active],
    }])
    calls = []
    monkeypatch.setattr(draft_figures, "render_figure", lambda *a, **k: calls.append(k) or {
        "figure_id": 3, "numeral_audit": {"ok": True},
        "semantic_audit": accepted_semantic_audit(),
        "leader_audit": accepted_leader_audit(),
    })
    monkeypatch.setattr(draft_figures, "archive_figure", lambda *a: True)
    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body",
        numeral_table=[{"numeral": "10", "part": "body"}], figure_specs=[spec])
    assert len(calls) == 1 and out["generated"] == 1 and out["reused"] == 0


def test_a_previous_semantic_review_is_never_reused(monkeypatch):
    spec = {"label": "FIG. 1", "caption": "side view", "numerals": ["10"]}
    digest = draft_figures.specification_hash("FIG. 1", "side view", ["10 = body"])
    active = {
        "version_no": 1,
        "numeral_audit": {"ok": True, "expected": ["10"]},
        "semantic_audit": {
            "ok": True, "inspected": True, "specification_hash": digest,
            "prompt_version": "figure-semantic-v5-single-review", "review_count": 1,
        },
        "leader_audit": accepted_leader_audit(specification_hash=digest),
    }
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 3, "figure_label": "FIG. 1", "active_version": 1, "versions": [active],
    }])
    calls = []
    monkeypatch.setattr(draft_figures, "render_figure", lambda *a, **k: calls.append(k) or {
        "figure_id": 3, "numeral_audit": {"ok": True},
        "semantic_audit": accepted_semantic_audit(),
        "leader_audit": accepted_leader_audit(),
    })
    monkeypatch.setattr(draft_figures, "archive_figure", lambda *a: True)
    out = draft_figures.ensure_project_figures(
        7, 91, sections={}, disclosure="body",
        numeral_table=[{"numeral": "10", "part": "body"}], figure_specs=[spec])
    assert len(calls) == 1 and out["generated"] == 1 and out["reused"] == 0


def test_checked_images_are_materialized_for_the_independent_reviewer(monkeypatch, tmp_path):
    figures_dir = tmp_path / "figures"
    figures_dir.mkdir()
    (figures_dir / "rendered-old.png").write_bytes(b"old")
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 1: side view", "active_version": 2,
        "versions": [{"version_no": 2, "numeral_audit": accepted_ocr_audit(),
                      "semantic_audit": accepted_semantic_audit(),
                      "leader_audit": accepted_leader_audit()}],
    }])
    monkeypatch.setattr(draft_figures, "png_bytes", lambda *a, **k: ("image/png", b"checked"))
    count = draft_figures.materialize_review_images(7, 91, tmp_path)
    assert count == 1
    assert not (figures_dir / "rendered-old.png").exists()
    assert (figures_dir / "rendered-FIG-1.png").read_bytes() == b"checked"


def test_checked_images_include_exact_audit_evidence_for_the_independent_reviewer(
        monkeypatch, tmp_path):
    digest = "b" * 64
    geometry = accepted_cross_provider_geometry_audit(
        specification_hash=digest,
        summary="The sectional hatches and every solid boundary match the brief.",
    )
    endpoints = accepted_cross_provider_audit(
        specification_hash=digest,
        summary="The numeral 10 terminal dot lands inside the body.",
        coordinate_space="raw_pixels",
        coordinate_width=1400,
        coordinate_height=900,
        labels=[{
            "numeral": "10", "correct": True,
            "evidence": "Dot at raw (700, 400) is inside the body.",
        }],
    )
    marked = accepted_marked_anchor_audit(
        specification_hash=digest,
        cross_provider_audit=endpoints,
    )
    semantic = accepted_semantic_audit(
        specification_hash=digest,
        cross_provider_geometry_audit=geometry)
    specification = """
    The sheet shows four bodies, all shown schematically, and one broken line: one horizontal
    hatched slab, the base; one closed loop cut twice, appearing as two short hatched legs
    hanging from the underside of the slab, one at each end; one hatched band across the bottom
    on which both legs stand; and one closed housing standing on the upper face of the slab. The
    slab is filled with hatching rising to the right at about 45 degrees, both legs are filled
    with hatching falling to the right at about 45 degrees, and the band is filled with hatching
    rising to the right at about 75 degrees. One broken line runs from inside the housing to the
    chamber, and no passage, duct, opening or other structure is depicted.
    """
    raw = draft_figures._deterministic_chamber_section_png(specification)
    section_certificate = draft_figures._deterministic_section_hatch_certificate(
        raw, specification)
    draft_workspace.write_figures(tmp_path, [{
        "label": "FIG. 1: sectional view", "caption": specification, "numerals": ["10"],
    }])
    active = {
        "version_no": 2,
        "source_kind": "deterministic",
        "numeral_audit": accepted_ocr_audit(
            sheet_number="1/1", expected=("10",), detected=["10"],
            detected_sheet_number="1/1", detected_figure_label="FIG. 1"),
        "semantic_audit": semantic,
        "leader_audit": accepted_leader_audit(
            specification_hash=digest,
            marked_anchor_audit=marked),
    }
    assert draft_figures.current_ocr_audit(
        active["numeral_audit"], expected_sheet_number="1/1")
    assert draft_figures.current_semantic_audit(active["semantic_audit"])
    assert draft_figures.current_leader_audit(active["leader_audit"])
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 1: sectional view", "active_version": 2,
        "versions": [active],
    }])
    monkeypatch.setattr(
        draft_figures, "png_bytes",
        lambda *a, **k: ("image/png", raw if k.get("base") else b"checked"))

    assert draft_figures.materialize_review_images(7, 91, tmp_path) == 1

    evidence = json.loads((tmp_path / "review" / "figure-audit-evidence.json").read_text())
    assert evidence["schema_version"] == 1
    assert evidence["figures"] == [{
        "figure_label": "FIG. 1", "rendered_file": "rendered-FIG-1.png",
        "rendered_sha256": hashlib.sha256(b"checked").hexdigest(),
        "specification_hash": digest,
            "ocr": {
                "ok": True, "expected_numerals": ["10"], "detected_numerals": ["10"],
                "expected_section_designations": [], "detected_section_designations": [],
                "expected_sheet_number": "1/1", "detected_sheet_number": "1/1",
            "detected_figure_label": "FIG. 1",
        },
        "geometry": {
            "ok": True, "reviewer": draft_figures.cross_provider_model(),
            "prompt_version": draft_figures.CROSS_PROVIDER_GEOMETRY_PROMPT_VERSION,
            "summary": "The sectional hatches and every solid boundary match the brief.",
            "missing": [], "unexpected": [], "errors": [],
        },
            "deterministic_section_hatching": section_certificate,
            "section_marks": {
                "ok": True, "required": False, "reviewer": "deterministic-parser",
                "prompt_version": draft_figures.SECTION_MARK_PROMPT_VERSION,
                "review_count": 0,
                "summary": "No cutting-plane designation is required.",
                "marks": [],
            },
        "leaders": {
            "ok": True,
            "prompt_version": draft_figures.LEADER_PROMPT_VERSION,
            "marked_prompt_version": draft_figures.MARKED_ANCHOR_PROMPT_VERSION,
            "section_mark_anchor_clearance":
                draft_figures._section_mark_anchor_audit([], []),
        },
        "endpoints": {
            "ok": True, "reviewer": draft_figures.cross_provider_model(),
            "prompt_version": draft_figures.CROSS_PROVIDER_PROMPT_VERSION,
            "summary": "The numeral 10 terminal dot lands inside the body.",
            "coordinate_space": "raw_pixels", "coordinate_width": 1400,
            "coordinate_height": 900,
            "labels": [{
                "numeral": "10", "correct": True,
                "evidence": "Dot at raw (700, 400) is inside the body.",
            }],
        },
    }]


def test_old_leader_reviews_are_not_materialized_for_independent_review(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_figures, "listing", lambda *a: [{
        "id": 8, "figure_label": "FIG. 1", "active_version": 2,
        "versions": [{"version_no": 2, "numeral_audit": {"ok": True},
                      "semantic_audit": accepted_semantic_audit(),
                      "leader_audit": {"ok": True, "inspected": True,
                                         "prompt_version": "old", "review_count": 1}}],
    }])
    monkeypatch.setattr(
        draft_figures, "png_bytes",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not copy stale pixels")))
    assert draft_figures.materialize_review_images(7, 91, tmp_path) == 0
