"""Vision verification, the semantic diff, and the correction loop.

The verifier here is the SVG-reading double, not a model. That is the point: corrupt the
drawing and the observation genuinely changes, so the comparison and the repair logic are
exercised end to end with no network and no cost.
"""
from __future__ import annotations

import copy

import pytest
from conftest import make_graph

from pfc import correct as correction
from pfc import plan as planning
from pfc import spec as speccing
from pfc import vision
from pfc.layout import build_scene
from pfc.providers.mock import SvgReadingVerifier
from pfc.render import render_svg
from pfc.schemas import Point
from pfc.validate import FigureBundle, ValidationContext, blocking, validate_figure


@pytest.fixture
def figure(housing_document, profile):
    graph, registry = make_graph(housing_document, [
        ("110", "contains", "120", "none"),
        ("130", "controls", "140", "subject_to_object"),
    ])
    plan = planning.build_plan(housing_document, registry, None)
    item = next(row for row in plan.figures if row.figure_number == "1")
    spec, _ = speccing.build_spec(housing_document, graph, registry, item, None)
    scene = build_scene(spec, graph, profile)
    svg = render_svg(scene, profile)
    return graph, plan, spec, scene, svg


def observe(svg):
    return SvgReadingVerifier().observe_svg(svg)


def test_a_correct_drawing_reads_back_as_specified(figure, profile):
    _graph, _plan, spec, scene, svg = figure
    observed = observe(svg)
    result = vision.diff(spec, scene, observed, profile)
    assert result.missing_references == []
    assert result.unexpected_references == []
    assert result.missing_connections == []
    assert result.unexpected_connections == []
    assert result.direction_mismatches == []
    assert result.unsupported_visible_text == []


def test_a_numeral_erased_from_the_sheet_is_noticed(figure, profile):
    _graph, _plan, spec, scene, svg = figure
    numeral = scene.labels[0].reference_numeral
    broken = svg.replace(f'data-reference-label="{numeral}"', 'data-removed="1"', 1)
    result = vision.diff(spec, scene, observe(broken), profile)
    assert numeral in result.missing_references
    issues = vision.issues_from_diff(result, observe(broken), spec.figure_id)
    assert any(issue.rule_id == "VIS001" for issue in issues)


def test_a_connection_erased_from_the_sheet_is_noticed(figure, profile):
    _graph, _plan, spec, scene, svg = figure
    edge = scene.edges[0]
    broken = svg.replace(f'data-relation-id="{edge.relation_id}"', 'data-gone="1"', 1)
    result = vision.diff(spec, scene, observe(broken), profile)
    assert result.missing_connections
    issues = vision.issues_from_diff(result, observe(broken), spec.figure_id)
    assert any(issue.rule_id == "VIS005" for issue in issues)


def test_an_arrowhead_that_should_not_be_there_is_noticed(housing_document, profile):
    """The document says two parts are adjacent. A reader who sees an arrow has seen a claim
    about flow that the patent never made."""
    graph, registry = make_graph(housing_document, [("120", "adjacent_to", "130", "none")])
    plan = planning.build_plan(housing_document, registry, None)
    item = next(row for row in plan.figures if row.figure_number == "1")
    spec, _ = speccing.build_spec(housing_document, graph, registry, item, None)
    scene = build_scene(spec, graph, profile)
    svg = render_svg(scene, profile)
    undirected = next(edge for edge in scene.edges if not edge.arrow_at_end)
    broken = svg.replace(f'data-edge-type="{undirected.edge_type}" data-directed="0"',
                         f'data-edge-type="{undirected.edge_type}" data-directed="1"')
    assert broken != svg
    result = vision.diff(spec, scene, observe(broken), profile)
    assert result.direction_mismatches


def test_stray_text_on_the_sheet_is_noticed(figure, profile):
    _graph, _plan, spec, scene, svg = figure
    broken = svg.replace("</svg>", '<text x="100" y="100">CONFIDENTIAL</text></svg>')
    result = vision.diff(spec, scene, observe(broken), profile)
    assert "CONFIDENTIAL" in result.unsupported_visible_text


def test_two_readers_must_agree_before_a_figure_is_failed(figure, profile):
    _graph, _plan, spec, scene, svg = figure
    numeral = scene.labels[0].reference_numeral
    broken = svg.replace(f'data-reference-label="{numeral}"', 'data-removed="1"', 1)
    seen = vision.diff(spec, scene, observe(broken), profile)
    clean = vision.diff(spec, scene, observe(svg), profile)

    agreed, disagreed = vision.reconcile(seen, clean)
    assert agreed.clean, "one reader alone must not fail a drawing"
    assert disagreed

    agreed_both, disagreed_both = vision.reconcile(seen, seen)
    assert not agreed_both.clean
    assert not disagreed_both


def test_overlapping_numerals_are_repaired_rather_than_regenerated(figure, profile):
    graph, plan, spec, scene, _svg = figure
    scene.labels[1].position = Point(x=scene.labels[0].position.x,
                                     y=scene.labels[0].position.y)
    bundle = FigureBundle(spec=spec, scene=scene, svg=render_svg(scene, profile))
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    issues = validate_figure(context)
    assert "GEO002" in {issue.rule_id for issue in blocking(issues)}

    before = {label.reference_numeral: (label.position.x, label.position.y)
              for label in scene.labels}
    outcome = correction.correct(spec, graph, scene, profile, issues, attempt=0)
    assert outcome.changed
    assert outcome.applied and "re-placed" in outcome.applied[0]

    bundle.scene = outcome.scene
    bundle.svg = render_svg(bundle.scene, profile)
    context.figure = bundle
    assert "GEO002" not in {issue.rule_id for issue in blocking(validate_figure(context))}

    moved = {label.reference_numeral for label in outcome.scene.labels
             if before[label.reference_numeral] != (label.position.x, label.position.y)}
    untouched = set(before) - moved
    assert untouched, "a local repair must leave the labels that were already right alone"
    # The semantics are untouched: same objects, same numerals, same connections.
    assert {node.entity_id for node in outcome.scene.nodes} == {node.entity_id
                                                                for node in scene.nodes}
    assert {label.reference_numeral for label in outcome.scene.labels} == set(before)


def test_a_defect_that_needs_the_text_to_change_is_never_redrawn_away(figure, profile):
    graph, _plan, spec, scene, _svg = figure
    from pfc.schemas import ValidationIssue

    issue = ValidationIssue(
        rule_id="REF007", severity="blocking", category="reference",
        message="a part has no numeral", repair_action="revise_text")
    outcome = correction.correct(spec, graph, scene, profile, [issue], attempt=0)
    assert not outcome.changed
    assert outcome.escalated == [issue]


def test_correction_gives_up_rather_than_looping():
    assert correction.MAX_ATTEMPTS == 3


def test_a_readers_estimate_of_where_a_numeral_sits_does_not_block_the_figure(figure, profile):
    """The renderer placed it. A vision model's guess at the coordinate is not evidence.

    Measured on US-2024/0246200-A1: a purely deterministic run, whose numeral placement is
    checked by the geometry rules and re-verified by re-rendering, had the reader miss by 66 to
    139 mm on every figure and blocked all four.
    """
    _graph, _plan, spec, scene, svg = figure
    observed = observe(svg)
    reference = next(item for item in observed.visible_references
                     if item.reference in {label.reference_numeral for label in scene.labels})
    reference.confidence = 0.95
    reference.bbox = [10.0, 10.0, 40.0, 30.0]          # nowhere near where it was drawn
    reference.target_description = ""

    result = vision.diff(spec, scene, observed, profile, image_size=(1000, 1000))
    drifted = [item for item in result.reference_target_mismatches
               if item.get("measured") == "position"]
    assert drifted, "the drift was not measured at all"

    issues = vision.issues_from_diff(result, observed, spec.figure_id)
    offending = [issue for issue in issues
                 if issue.rule_id == "VIS003" and issue.detail.get("measured") == "position"]
    assert offending, "the drift was not reported"
    assert all(issue.severity == "warning" for issue in offending)
    assert not [issue for issue in blocking(issues) if issue.rule_id == "VIS003"]


def test_a_leader_reading_as_a_different_component_still_blocks(figure, profile):
    """The half of VIS003 that needs eyes: nothing deterministic can make this judgement."""
    _graph, _plan, spec, scene, svg = figure
    observed = observe(svg)
    labelled = {label.reference_numeral: label for label in scene.labels}
    captioned = next(node for node in scene.nodes if node.caption)
    numeral = next(numeral for numeral, label in labelled.items()
                   if label.entity_id == captioned.entity_id)

    reference = next(item for item in observed.visible_references if item.reference == numeral)
    reference.confidence = 0.95
    reference.target_description = "a completely unrelated widget nobody mentioned"

    result = vision.diff(spec, scene, observed, profile)
    issues = vision.issues_from_diff(result, observed, spec.figure_id)
    blockers = [issue for issue in blocking(issues)
                if issue.rule_id == "VIS003" and issue.detail.get("measured") == "target"]
    assert blockers, "a leader that reads as naming another part has to block"
