"""Autonomous patent-drawing generation and pixel-level filing gates."""
import io
import json
import re

import pytest
from PIL import Image, ImageDraw

import draft_figures


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
        "missing": [], "unexpected": [], "duplicates": [],
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
    coordinate_sheet = draft_figures._coordinate_grid_overlay(raw)
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


def test_current_ocr_audit_rejects_an_old_gate_or_different_sheet_total():
    current = accepted_ocr_audit("2/5")

    assert draft_figures.current_ocr_audit(
        current, expected_sheet_number="2/5") is True
    assert draft_figures.current_ocr_audit(
        current, expected_sheet_number="2/6") is False
    assert draft_figures.current_ocr_audit(
        {**current, "prompt_version": "old"}, expected_sheet_number="2/5") is False


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


def test_cross_provider_endpoint_review_uses_anthropic_pixels_and_normalizes_its_veto(
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

    raw = blank_png()
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
    assert audit["model_name"] == "claude-opus-5"
    assert len(calls) == 1 and calls[0][1] == "test-anthropic-key"
    assert calls[0][0]["thinking"] == {"type": "disabled"}
    assert calls[0][0]["messages"][0]["content"][0]["type"] == "image"
    assert [item["type"] for item in calls[0][0]["messages"][0]["content"]] == [
        "image", "image", "image", "text"]
    prompt = calls[0][0]["messages"][0]["content"][3]["text"]
    assert "suggested_x" in prompt and "raw geometry sheet" in prompt
    assert "CURRENT" in prompt and "0 to 1000" in prompt
    assert saved and saved[0][1]["provider"] == "anthropic"


def test_required_cross_provider_review_fails_closed_without_a_credential(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_REQUIRED", "1")
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)

    audit = draft_figures.inspect_cross_provider_endpoints(
        blank_png(), label="FIG. 1", caption="device", numerals=["10 = device"])

    assert audit["ok"] is False and audit["inspected"] is False
    assert audit["missing"] == ["10"]
    assert "not configured" in audit["errors"][0].lower()


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


def test_required_cross_provider_geometry_review_fails_closed_without_a_credential(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATENT_FIGURE_CROSSCHECK_REQUIRED", "1")
    monkeypatch.setattr(draft_figures, "_analysis_cache_get", lambda *_args: None)

    audit = draft_figures.inspect_cross_provider_geometry(
        blank_png(), label="FIG. 1", caption="housing", numerals=["10 = housing"])

    assert audit["ok"] is False and audit["inspected"] is False
    assert audit["missing"] == ["10"]
    assert "not configured" in audit["errors"][0].lower()


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


def test_marked_anchor_heading_states_the_exact_full_sheet_coordinate():
    heading = draft_figures._marked_anchor_heading(
        {"numeral": "26", "x": 501, "y": 502}, {"26": "bearing face"})

    assert heading == "26: bearing face | CURRENT (501, 502)"


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


def test_current_marked_audit_accepts_a_fully_certified_v9_review():
    audit = accepted_marked_anchor_audit(
        prompt_version=(
            "figure-anchor-v9-local-part-coordinate-certificate-majority-with-correction"
        ))

    assert draft_figures.current_marked_anchor_audit(audit) is True
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
    assert 120 <= anchor["y"] <= 180


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
    }
    assert draft_figures.current_leader_audit(current) is True
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


def test_third_semantic_attempt_resets_repeatedly_rejected_geometry(monkeypatch):
    generated = []

    def generate(_prompt, previous=None):
        png = blank_png(width=640 + len(generated))
        generated.append((previous, png))
        return png

    monkeypatch.setattr(draft_figures, "_cached_generate", generate)
    monkeypatch.setattr(draft_figures, "inspect_semantics", lambda *a, **k: {
        "ok": False, "missing": [],
        "errors": ["unexpected dashed line remains inside the slab"], "anchors": [],
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
