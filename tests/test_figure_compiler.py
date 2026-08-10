"""Patent figure compiler contracts.

These tests exercise the filing-artifact path, not the existing raster concept-sketch path.  A
compiler output must be deterministic, traceable to the draft, gated by approvals, and reject a
text/drawing numeral mismatch rather than hoping a vision model notices it later.
"""
import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

import figure_compiler

SECTIONS = {
    "title": "Vacuum Lifting Tool With Interchangeable Sealing Ring",
    "drawing_descriptions": (
        "FIG. 1 is a side elevation of the vacuum lifting tool.\n"
        "FIG. 2 is an exploded view of the sealing ring and the body."
    ),
    "detailed_description": (
        "Referring to FIG. 1, a vacuum lifting tool 10 has a body 12 that carries a pump 14. "
        "A sealing ring 16 is received in a groove 18 in the body 12. As shown in FIG. 2, "
        "the pump 14 draws air through a passage 20 in the body 12."
    ),
    "claims": (
        "1. A vacuum lifting tool comprising a body, a pump carried by the body, a groove in "
        "the body, and a sealing ring received in the groove.\n\n"
        "2. The vacuum lifting tool of claim 1, wherein the body defines a passage between the "
        "pump and the groove."
    ),
}
NUMERALS = [
    {"numeral": "10", "part": "vacuum lifting tool"},
    {"numeral": "12", "part": "body"},
    {"numeral": "14", "part": "pump"},
    {"numeral": "16", "part": "sealing ring"},
    {"numeral": "18", "part": "groove"},
    {"numeral": "20", "part": "passage"},
]
FIGURES = [
    {"label": "FIG. 1", "caption": "side elevation of the vacuum lifting tool",
     "numerals": ["10 vacuum lifting tool", "12 body", "14 pump"]},
    {"label": "FIG. 2", "caption": "exploded view of the sealing ring and body",
     "numerals": ["16 sealing ring", "18 groove", "20 passage"]},
]


def approved(value, kind="figure_manifest"):
    return figure_compiler.approve_artifact(value, artifact_type=kind, user_id=91)


def compiled_fixture():
    pir = approved(figure_compiler.build_pir(SECTIONS, NUMERALS, FIGURES), "canonical_model")
    manifest = approved(figure_compiler.plan_manifest(pir, FIGURES))
    return pir, manifest, figure_compiler.compile_package(pir, manifest, "uspto-letter-2026.1")


def test_pir_has_stable_source_provenance_registry_relations_and_claim_coverage():
    first = figure_compiler.build_pir(SECTIONS, NUMERALS, FIGURES)
    second = figure_compiler.build_pir(SECTIONS, NUMERALS, FIGURES)

    assert first == second
    assert first["schema_version"] == "pir-1"
    assert {e["reference"] for e in first["entities"]} == {
        "10", "12", "14", "16", "18", "20"
    }
    assert all(e["source_span_ids"] for e in first["entities"])
    assert all(span["sha256"] for span in first["source_spans"])
    assert any(r["predicate"] == "carried_by" for r in first["relations"])
    assert first["reference_conflicts"] == []
    assert first["claim_coverage"]
    assert not [c for c in first["claim_coverage"] if c["drawable"] and not c["figure_ids"]]
    assert figure_compiler.validate_pir_contract(first) is None


def test_typed_stage_contract_rejects_an_incomplete_pir():
    with pytest.raises(figure_compiler.FigureCompilerError, match="PIR contract"):
        figure_compiler.validate_pir_contract({"schema_version": "pir-1"})


def test_material_reference_conflict_is_a_hard_blocker_and_is_not_silently_resolved():
    conflicted = NUMERALS + [{"numeral": "14", "part": "battery"}]
    pir = figure_compiler.build_pir(SECTIONS, conflicted, FIGURES)

    assert pir["reference_conflicts"]
    conflict = pir["reference_conflicts"][0]
    assert conflict["material"] is True
    assert conflict["status"] == "unresolved"
    assert {"pump", "battery"}.issubset(set(conflict["candidates"]))
    assert "unresolved_reference_conflict" in {b["code"] for b in pir["hard_blockers"]}


def test_user_choice_resolves_one_reference_conflict_in_a_new_pir_value():
    pir = figure_compiler.build_pir(
        SECTIONS, NUMERALS + [{"numeral": "14", "part": "battery"}], FIGURES)
    before = deepcopy(pir)
    conflict = pir["reference_conflicts"][0]

    resolved = figure_compiler.resolve_reference_conflict(
        pir, conflict_id=conflict["id"], choice="pump", resolved_by_user_id=91)

    assert pir == before
    assert resolved["reference_conflicts"][0]["status"] == "resolved"
    assert resolved["reference_conflicts"][0]["resolution"]["choice"] == "pump"
    assert next(e for e in resolved["entities"] if e["reference"] == "14")["name"] == "pump"
    assert "unresolved_reference_conflict" not in {b["code"] for b in resolved["hard_blockers"]}


def test_manifest_approval_is_a_real_compilation_gate():
    raw_pir = figure_compiler.build_pir(SECTIONS, NUMERALS, FIGURES)
    manifest = approved(figure_compiler.plan_manifest(raw_pir, FIGURES))

    with pytest.raises(figure_compiler.ApprovalRequired, match="canonical model"):
        figure_compiler.compile_package(raw_pir, manifest, "uspto-letter-2026.1")

    pir = approved(raw_pir, "canonical_model")
    manifest = figure_compiler.plan_manifest(pir, FIGURES)

    with pytest.raises(figure_compiler.ApprovalRequired, match="manifest"):
        figure_compiler.compile_package(pir, manifest, "uspto-letter-2026.1")

    package = figure_compiler.compile_package(pir, approved(manifest),
                                              "uspto-letter-2026.1")
    assert package["renderer_version"] == figure_compiler.RENDERER_VERSION
    assert package["sheets"]


def test_semantic_svg_is_monochrome_traceable_and_uses_each_reference_once():
    pir, _manifest, package = compiled_fixture()
    svg = "\n".join(sheet["svg"] for sheet in package["sheets"])

    assert svg.startswith("<svg")
    assert 'data-entity-id="entity-12"' in svg
    assert 'data-reference="12"' in svg
    assert 'data-source-spans="' in svg
    assert "#000000" in svg and "#ffffff" in svg
    assert "rgb(" not in svg and "<image" not in svg
    for entity in pir["entities"]:
        assert svg.count(f'data-reference-label="{entity["reference"]}"') == 1


def test_same_component_can_be_labeled_once_in_each_figure_without_false_duplicate():
    sections = {
        "drawing_descriptions": (
            "FIG. 1 is a block diagram of the controller. "
            "FIG. 2 is another state of the controller."
        ),
        "detailed_description": "A controller 10 contains a processor 12.",
        "claims": "1. A controller comprising a processor.",
    }
    numerals = [
        {"numeral": "10", "part": "controller"},
        {"numeral": "12", "part": "processor"},
    ]
    figures = [
        {"label": "FIG. 1", "caption": "block diagram of the controller",
         "numerals": ["10 controller", "12 processor"]},
        {"label": "FIG. 2", "caption": "another state of the controller",
         "numerals": ["10 controller", "12 processor"]},
    ]
    pir = approved(figure_compiler.build_pir(sections, numerals, figures), "canonical_model")
    manifest = approved(figure_compiler.plan_manifest(pir, figures))
    package = figure_compiler.compile_package(pir, manifest, "uspto-letter-2026.1")

    result = figure_compiler.validate_package(pir, manifest, package,
                                               "uspto-letter-2026.1")

    assert result["approved_for_export"] is True
    assert "reference_label_count" not in {issue["code"] for issue in result["issues"]}
    assert sum(sheet["svg"].count('data-reference-label="10"')
               for sheet in package["sheets"]) == 2


def test_validator_blocks_a_label_bound_to_the_wrong_registry_entity():
    pir, manifest, package = compiled_fixture()
    broken = deepcopy(package)
    broken["figures"][0]["labels"][0]["entity_id"] = "entity-12"

    result = figure_compiler.validate_package(pir, manifest, broken,
                                               "uspto-letter-2026.1")

    assert "label_registry_mismatch" in {issue["code"] for issue in result["issues"]}
    assert result["approved_for_export"] is False


def test_validator_rejects_payload_drift_even_when_rendered_svg_is_unchanged():
    pir, manifest, package = compiled_fixture()
    broken = deepcopy(package)
    broken["figures"][0]["caption"] = "A silently altered caption"

    result = figure_compiler.validate_package(pir, manifest, broken,
                                               "uspto-letter-2026.1")

    assert "package_content_hash_mismatch" in {issue["code"] for issue in result["issues"]}
    assert result["approved_for_export"] is False


def test_validator_blocks_unsupported_visible_objects_and_bidirectional_numeral_drift():
    pir, manifest, package = compiled_fixture()
    broken = deepcopy(package)
    broken["figures"][0]["entities"].append({
        "entity_id": "entity-999", "reference": "999", "name": "invented sensor",
        "source_span_ids": ["span-forged"], "x": 100, "y": 100, "width": 80, "height": 40,
        "shape": "box",
    })
    broken["figures"][1]["entities"] = [
        item for item in broken["figures"][1]["entities"] if item["reference"] != "20"
    ]

    result = figure_compiler.validate_package(pir, manifest, broken,
                                               "uspto-letter-2026.1")
    codes = {issue["code"] for issue in result["issues"]}
    assert "unsupported_visible_entity" in codes
    assert "text_reference_missing_from_drawings" in codes
    assert "drawing_reference_missing_from_text" in codes
    assert result["hard_blockers"] >= 3
    assert result["approved_for_export"] is False


def test_formal_validator_rejects_labels_outside_the_versioned_usable_surface():
    pir, manifest, package = compiled_fixture()
    broken = deepcopy(package)
    broken["figures"][0]["labels"][0]["x"] = 10
    broken["figures"][0]["labels"][0]["y"] = 10

    result = figure_compiler.validate_package(pir, manifest, broken,
                                               "uspto-letter-2026.1")
    assert "content_outside_usable_area" in {issue["code"] for issue in result["issues"]}
    assert result["validator_counts"]["formal"] >= 1


def test_typed_patch_creates_a_new_package_without_mutating_the_old_one():
    _pir, _manifest, package = compiled_fixture()
    before = deepcopy(package)
    moved = figure_compiler.apply_typed_patch(package, {
        "type": "move_label", "figure_id": "figure-1", "reference": "12",
        "x": 705, "y": 250,
    })

    assert package == before
    assert moved["artifact_version"] == package["artifact_version"] + 1
    assert moved["parent_sha256"] == figure_compiler.content_hash(package)
    label = next(item for item in moved["figures"][0]["labels"]
                 if item["reference"] == "12")
    assert (label["x"], label["y"]) == (705, 250)
    assert moved["patch"]["type"] == "move_label"


def test_typed_patch_rejects_non_finite_coordinates():
    _pir, _manifest, package = compiled_fixture()
    with pytest.raises(figure_compiler.FigureCompilerError, match="Typed patch contract"):
        figure_compiler.apply_typed_patch(package, {
            "type": "move_label", "figure_id": "figure-1", "reference": "12",
            "x": float("nan"), "y": 250,
        })


def test_pdf_export_is_deterministic_and_contains_one_page_per_sheet():
    _pir, _manifest, package = compiled_fixture()
    first = figure_compiler.render_pdf(package, "uspto-letter-2026.1")
    second = figure_compiler.render_pdf(package, "uspto-letter-2026.1")

    assert first.startswith(b"%PDF-")
    assert len(first) > 1_000
    assert first == second


def test_versioned_rulesets_encode_current_uspto_and_pct_drawing_sights():
    letter = figure_compiler.load_ruleset("uspto-letter-2026.1")
    pct = figure_compiler.load_ruleset("pct-a4-2026.1")

    assert letter["sheet_mm"] == [216, 279]
    assert letter["margins_mm"] == {"top": 25, "left": 25, "right": 15, "bottom": 10}
    assert pct["sheet_mm"] == [210, 297]
    assert pct["usable_mm"] == [170, 262]
    assert pct["minimum_character_height_mm"] == 3.2


def test_golden_semantic_sheets_hold_their_visual_regression_hashes():
    pir = figure_compiler.approve_artifact(
        figure_compiler.build_pir(SECTIONS, NUMERALS, FIGURES),
        artifact_type="canonical_model", user_id=91,
        approved_at="2026-08-10T00:00:00+00:00")
    manifest = figure_compiler.approve_artifact(
        figure_compiler.plan_manifest(pir, FIGURES), artifact_type="figure_manifest",
        user_id=91, approved_at="2026-08-10T00:00:00+00:00")
    package = figure_compiler.compile_package(pir, manifest, "uspto-letter-2026.1")

    fixture_dir = Path(__file__).parent / "fixtures" / "figure_compiler"
    actual = [sheet["svg"] for sheet in package["sheets"]]
    expected = [(fixture_dir / f"golden-figure-{index}.svg").read_text().rstrip("\n")
                for index in (1, 2)]
    assert actual == expected
    assert [hashlib.sha256(svg.encode()).hexdigest() for svg in actual] == [
        "2e2f12c51991ac0c178eb25b59c001a0cb6662421b1cd9976cb372f151948f25",
        "ad275e78125e40acf9f8b224b5898ca95de93a5a116973a0968d1984fe6466e3",
    ]
