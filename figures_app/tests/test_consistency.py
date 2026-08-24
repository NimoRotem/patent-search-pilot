"""Cross-figure consistency: the guardrail that pays for the freedom to draw recognisable parts.

The freedom is only safe if the compiler draws the same part the same way everywhere. These
tests break that on purpose, one way at a time, and check the rule that owns each break fires.
"""
from __future__ import annotations

import pytest
from conftest import make_document, make_graph

from pfc import appearance, plan as planning, spec as speccing
from pfc.layout import build_scene
from pfc.providers.mock import ScriptedTextReasoner
from pfc.render import render_svg
from pfc.validate import FigureBundle, ValidationContext, blocking, validate_job, warnings

TWO_VIEWS = """A GLUING DEVICE

BRIEF DESCRIPTION OF THE DRAWINGS
FIG. 1 is a perspective view of the housing 110, the coil 120 and the substrate 130.
FIG. 2 is a perspective view of the housing 110, the coil 120 and the sensor 140.

DETAILED DESCRIPTION
The housing 110 contains the coil 120 and the substrate 130. The housing 110 also contains
the sensor 140. The coil 120 heats the substrate 130.

As shown in FIG. 1, the housing 110 contains the coil 120 and the substrate 130.

As shown in FIG. 2, the housing 110 contains the coil 120 and the sensor 140.
"""


def _bundles(profile, relations=(("110", "contains", "120", "none"),
                                 ("110", "contains", "130", "none"),
                                 ("110", "contains", "140", "none"))):
    document = make_document(TWO_VIEWS)
    graph, registry = make_graph(document, list(relations))
    appearance.decide(graph, None, [])
    plan = planning.build_plan(document, registry, None)
    bundles = []
    for sheet, item in enumerate(plan.figures, 1):
        spec, _notes = speccing.build_spec(document, graph, registry, item, None)
        if spec is None:
            continue
        scene = build_scene(spec, graph, profile, sheet_number=sheet,
                            sheet_total=len(plan.figures))
        bundles.append(FigureBundle(spec=spec, scene=scene, svg=render_svg(scene, profile)))
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figures=bundles)
    return graph, bundles, context


def _ids(issues):
    return {issue.rule_id for issue in issues}


def test_the_same_part_is_drawn_the_same_way_on_every_sheet(profile):
    graph, bundles, context = _bundles(profile)
    assert len(bundles) == 2
    drawn = {}
    for bundle in bundles:
        for node in bundle.scene.nodes:
            drawn.setdefault(node.entity_id, set()).add(node.symbol)
    for entity_id, used in drawn.items():
        assert len(used) == 1, f"{entity_id} was drawn as {used}"
    assert blocking(validate_job(context)) == []


def test_a_part_drawn_two_different_ways_is_caught(profile):
    graph, bundles, context = _bundles(profile)
    # As if a correction pass had redrawn the coil on the second sheet.
    node = next(n for n in bundles[1].scene.nodes if n.entity_id == "e120")
    node.symbol = "spring"
    issues = validate_job(context)
    assert "CON001" in _ids(blocking(issues))
    message = next(i for i in issues if i.rule_id == "CON001").message
    assert "coil" in message or "spring" in message


def test_two_parts_of_one_kind_drawn_differently_is_a_warning_not_a_refusal(profile):
    document = make_document(
        "A DEVICE\n\nBRIEF DESCRIPTION OF THE DRAWINGS\n"
        "FIG. 1 is a view of the first sensor 120 and the second sensor 122.\n\n"
        "DETAILED DESCRIPTION\n"
        "The first sensor 120 measures pressure. The second sensor 122 measures temperature. "
        "As shown in FIG. 1, the first sensor 120 is beside the second sensor 122.\n")
    graph, registry = make_graph(document, [("120", "adjacent_to", "122", "none")])
    appearance.decide(graph, None, [])
    plan = planning.build_plan(document, registry, None)
    item = plan.figures[0]
    spec, _ = speccing.build_spec(document, graph, registry, item, None)
    scene = build_scene(spec, graph, profile)
    node = next(n for n in scene.nodes if n.entity_id == "e122")
    node.symbol = "gauge_that_is_not_a_sensor"
    bundle = FigureBundle(spec=spec, scene=scene, svg=render_svg(scene, profile))
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figures=[bundle])
    issues = validate_job(context)
    assert "CON002" in _ids(warnings(issues))
    assert "CON002" not in _ids(blocking(issues))


def test_two_parts_swapping_which_is_larger_is_caught(profile):
    graph, bundles, context = _bundles(profile)
    first = {n.entity_id: n for n in bundles[0].scene.nodes}
    second = {n.entity_id: n for n in bundles[1].scene.nodes}
    shared = [e for e in first if e in second and not first[e].is_container]
    assert len(shared) >= 2, "the fixture needs two non-container parts on both sheets"
    a, b = shared[0], shared[1]
    from pfc.schemas import Box

    first[a].box = Box(x=first[a].box.x, y=first[a].box.y, width=400.0, height=300.0)
    first[b].box = Box(x=first[b].box.x, y=first[b].box.y, width=100.0, height=80.0)
    second[a].box = Box(x=second[a].box.x, y=second[a].box.y, width=100.0, height=80.0)
    second[b].box = Box(x=second[b].box.x, y=second[b].box.y, width=400.0, height=300.0)
    assert "CON003" in _ids(warnings(validate_job(context)))


def test_turning_a_part_between_two_of_the_same_view_is_caught(profile):
    graph, bundles, context = _bundles(profile)
    assert bundles[0].spec.view_type == bundles[1].spec.view_type
    node = next(n for n in bundles[1].scene.nodes if n.entity_id == "e120")
    node.orientation = "vertical"
    assert "CON004" in _ids(warnings(validate_job(context)))


def test_turning_a_part_between_genuinely_different_views_is_allowed(profile):
    graph, bundles, context = _bundles(profile)
    bundles[1].spec.view_type = "plan"        # a plan and a perspective may differ
    node = next(n for n in bundles[1].scene.nodes if n.entity_id == "e120")
    node.orientation = "vertical"
    assert "CON004" not in _ids(warnings(validate_job(context)))


# ---------------------------------------------------------------------------
# the appearance decision itself
# ---------------------------------------------------------------------------
def test_an_appearance_is_settled_once_for_the_whole_document(profile):
    graph, _sheets, _context = _bundles(profile)
    coil = graph.entity("e120")
    assert coil.appearance.symbol == "coil"
    assert coil.appearance.source in {"keyword", "model", "disclosed"}
    assert coil.appearance.note


def test_a_container_is_drawn_large_and_what_is_inside_it_small(profile):
    graph, _sheets, _context = _bundles(profile)
    assert graph.entity("e110").appearance.size == "large"
    assert graph.entity("e120").appearance.size == "small"


def test_a_shape_the_description_states_outranks_the_model():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The housing 110 is cylindrical and contains the sensor 120.\n")
    graph, _registry = make_graph(document, [])
    housing = graph.entity("e110")
    housing.shape_hint = "cylindrical"
    housing.shape_hint_grounded = True
    reasoner = ScriptedTextReasoner({"component_appearance": {"components": [
        {"entity_id": "e110", "symbol": "antenna", "orientation": "vertical", "size": "small",
         "note": "a wrong idea"}]}})
    appearance.decide(graph, reasoner, [])
    assert housing.appearance.source == "disclosed"
    assert housing.appearance.symbol != "antenna"


def test_the_model_may_choose_from_the_library_and_nothing_else():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The widget 110 is connected to the doohickey 120 by a linkage.\n")
    graph, _registry = make_graph(document, [])
    reasoner = ScriptedTextReasoner({"component_appearance": {"components": [
        {"entity_id": "e110", "symbol": "pump", "orientation": "vertical", "size": "large",
         "note": "the description calls it a pump"},
        {"entity_id": "e120", "symbol": "a-lovely-swirl", "orientation": "horizontal",
         "size": "medium", "note": "invented"}]}})
    appearance.decide(graph, reasoner, [])
    assert graph.entity("e110").appearance.symbol == "pump"
    assert graph.entity("e110").appearance.source == "model"
    assert graph.entity("e110").appearance.orientation == "vertical"
    # Not in the library, so it is refused and the part stays as it was.
    assert graph.entity("e120").appearance.symbol != "a-lovely-swirl"


def test_a_part_the_document_does_not_settle_stays_a_plain_outline():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The arrangement 110 cooperates with the formation 120 in use.\n")
    graph, _registry = make_graph(document, [])
    appearance.decide(graph, None, [])
    for entity in graph.entities:
        assert entity.appearance.symbol == "generic_component"
        assert entity.appearance.source == "default"


def test_the_appearance_table_is_reviewable():
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The housing 110 contains the coil 120 and the pump 130.\n")
    graph, _registry = make_graph(document, [("110", "contains", "120", "none")])
    appearance.decide(graph, None, [])
    rows = {row["reference"]: row for row in appearance.summary(graph.entities)}
    assert rows["120"]["symbol"] == "coil"
    assert rows["130"]["symbol"] == "pump"
    assert rows["110"]["decided_by"]
    assert all(row["note"] for row in rows.values())


def test_the_summary_counts_what_was_drawn_not_who_decided():
    """Counting by author reported 19 recognisable and 0 outlines for a document with eight."""
    document = make_document(
        "A DEVICE\n\nDETAILED DESCRIPTION\n"
        "The coil 110 heats the arrangement 120. The formation 130 abuts the coil 110.\n")
    graph, _registry = make_graph(document, [])
    reasoner = ScriptedTextReasoner({"component_appearance": {"components": [
        {"entity_id": "e110", "symbol": "coil", "orientation": "horizontal", "size": "medium",
         "note": "called a coil"},
        # The model deliberately leaves these as outlines. That is a legitimate answer.
        {"entity_id": "e120", "symbol": "generic_component", "orientation": "horizontal",
         "size": "medium", "note": "the description does not say what it is"},
        {"entity_id": "e130", "symbol": "generic_component", "orientation": "horizontal",
         "size": "medium", "note": "the description does not say what it is"}]}})
    notes: list[str] = []
    appearance.decide(graph, reasoner, notes)
    summary = " ".join(notes)
    assert "1 part(s) are drawn as a recognisable element" in summary
    assert "2 as a plain outline" in summary
