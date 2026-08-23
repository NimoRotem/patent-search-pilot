"""Synthetic-error tests: take a valid figure, break it one way, prove it is caught.

This is the suite that matters most. Any validator can pass a correct drawing; the question is
whether it fails an incorrect one, and the only way to know is to introduce each defect
deliberately and check that the rule which owns it fires. A corruption that slips through here
is a class of wrong figure the compiler would call VALIDATED.
"""
from __future__ import annotations

import copy

import pytest
from conftest import make_graph

from pfc import plan as planning
from pfc import spec as speccing
from pfc.layout import build_scene
from pfc.render import render_svg
from pfc.schemas import LayoutEdge, LayoutLabel, Point, SpecRelation
from pfc.validate import FigureBundle, ValidationContext, blocking, validate_figure


@pytest.fixture
def valid_figure(housing_document, profile):
    graph, registry = make_graph(housing_document, [
        ("110", "contains", "120", "none"),
        ("130", "controls", "140", "subject_to_object"),
    ])
    plan = planning.build_plan(housing_document, registry, None)
    item = next(row for row in plan.figures if row.figure_number == "1")
    spec, _ = speccing.build_spec(housing_document, graph, registry, item, None)
    scene = build_scene(spec, graph, profile)
    bundle = FigureBundle(spec=spec, scene=scene, svg=render_svg(scene, profile))
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    return context, bundle


def _rerender(context, bundle, profile):
    bundle.svg = render_svg(bundle.scene, profile)
    context.figure = bundle
    return validate_figure(context)


def _rule_ids(issues):
    return {issue.rule_id for issue in issues if issue.severity == "blocking"}


def test_the_unbroken_figure_passes(valid_figure, profile):
    context, bundle = valid_figure
    issues = validate_figure(context)
    assert blocking(issues) == [], [issue.message for issue in blocking(issues)]


def test_a_duplicated_numeral_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    bundle.scene.labels.append(copy.deepcopy(bundle.scene.labels[0]))
    assert "REF001" in _rule_ids(_rerender(context, bundle, profile))


def test_one_numeral_on_two_objects_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    first, second = bundle.scene.labels[0], bundle.scene.labels[1]
    second.reference_numeral = first.reference_numeral
    assert "REF001" in _rule_ids(_rerender(context, bundle, profile))


def test_a_removed_numeral_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    bundle.scene.labels.pop()
    assert "REF004" in _rule_ids(_rerender(context, bundle, profile))


def test_an_invented_numeral_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    stray = copy.deepcopy(bundle.scene.labels[0])
    stray.reference_numeral = "999"
    bundle.scene.labels.append(stray)
    assert "REF003" in _rule_ids(_rerender(context, bundle, profile))


def test_a_reversed_arrow_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    edge = next(edge for edge in bundle.scene.edges if edge.arrow_at_end)
    edge.from_entity, edge.to_entity = edge.to_entity, edge.from_entity
    assert "SEM005" in _rule_ids(_rerender(context, bundle, profile))


def test_an_arrow_on_an_undirected_relationship_is_caught(housing_document, profile):
    graph, registry = make_graph(housing_document, [("120", "adjacent_to", "130", "none")])
    plan = planning.build_plan(housing_document, registry, None)
    item = next(row for row in plan.figures if row.figure_number == "1")
    spec, _ = speccing.build_spec(housing_document, graph, registry, item, None)
    scene = build_scene(spec, graph, profile)
    edge = next(edge for edge in scene.edges
                if {edge.from_entity, edge.to_entity} == {"e120", "e130"})
    edge.arrow_at_end = True
    bundle = FigureBundle(spec=spec, scene=scene, svg=render_svg(scene, profile))
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    assert "SEM005" in _rule_ids(validate_figure(context))


def test_a_removed_connection_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    assert bundle.scene.edges, "the fixture must have a connection to remove"
    bundle.scene.edges.pop()
    assert "SEM004" in _rule_ids(_rerender(context, bundle, profile))


def test_an_unsupported_connection_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    first, second = bundle.scene.nodes[0], bundle.scene.nodes[-1]
    bundle.scene.edges.append(LayoutEdge(
        relation_id="rel_invented", from_entity=first.entity_id, to_entity=second.entity_id,
        edge_type="data_flow",
        points=[Point(x=first.box.cx, y=first.box.cy),
                Point(x=second.box.cx, y=second.box.cy)], arrow_at_end=True))
    assert "SEM003" in _rule_ids(_rerender(context, bundle, profile))


def test_an_unsupported_object_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    stray = copy.deepcopy(bundle.scene.nodes[0])
    stray.entity_id = "e_invented"
    stray.reference_numeral = "777"
    bundle.scene.nodes.append(stray)
    assert "SEM001" in _rule_ids(_rerender(context, bundle, profile))


def test_a_leader_pointing_at_the_wrong_object_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    label = bundle.scene.labels[0]
    other = next(node for node in bundle.scene.nodes
                 if node.entity_id != label.entity_id and not node.is_container)
    label.leader_points[-1] = Point(x=other.box.cx, y=other.box.cy)
    ids = _rule_ids(_rerender(context, bundle, profile))
    assert "GEO004" in ids or "GEO009" in ids


def test_two_overlapping_numerals_are_caught(valid_figure, profile):
    context, bundle = valid_figure
    first, second = bundle.scene.labels[0], bundle.scene.labels[1]
    second.position = Point(x=first.position.x, y=first.position.y)
    assert "GEO002" in _rule_ids(_rerender(context, bundle, profile))


def test_an_object_pushed_off_the_sheet_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    node = bundle.scene.nodes[0]
    node.box.x = -500.0
    assert "GEO007" in _rule_ids(_rerender(context, bundle, profile))


def test_a_numeral_below_the_required_size_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    bundle.scene.labels[0].text_height = profile.min_reference_height / 2
    assert "GEO010" in _rule_ids(_rerender(context, bundle, profile))


def test_two_overlapping_components_are_caught(valid_figure, profile):
    context, bundle = valid_figure
    movable = [node for node in bundle.scene.nodes if not node.is_container]
    movable[1].box.x = movable[0].box.x
    movable[1].box.y = movable[0].box.y
    assert "GEO001" in _rule_ids(_rerender(context, bundle, profile))


def test_an_edited_svg_no_longer_matches_its_layout(valid_figure, profile):
    """Every semantic rule reads the scene, so this proves the scene describes the file."""
    context, bundle = valid_figure
    bundle.svg = bundle.svg.replace("</svg>", '<text x="10" y="10">999</text></svg>')
    context.figure = bundle
    assert "RND001" in _rule_ids(validate_figure(context))


def test_colour_in_the_artwork_is_caught(valid_figure, profile):
    context, bundle = valid_figure
    bundle.svg = bundle.svg.replace('stroke="#000000"', 'stroke="#ff0000"', 1)
    context.figure = bundle
    ids = _rule_ids(validate_figure(context))
    assert "JUR001" in ids or "RND001" in ids


def test_a_relationship_the_model_does_not_carry_cannot_be_specified(valid_figure, profile):
    context, bundle = valid_figure
    bundle.spec.relations.append(SpecRelation(relation_id="rel_nonexistent",
                                              visual_representation="data_flow"))
    ids = _rule_ids(_rerender(context, bundle, profile))
    assert "GRD001" in ids
