"""The specification's end-to-end acceptance tests, one test function each.

These are the traps the compiler exists to avoid. They are written as the specification states
them, in the specification's order, so a change in behaviour shows up here as a named failure
rather than as a subtly different drawing.
"""
from __future__ import annotations

import pytest
from conftest import make_document, make_graph

from pfc import plan as planning
from pfc import spec as speccing
from pfc.layout import build_scene
from pfc.render import render_svg
from pfc.validate import FigureBundle, ValidationContext, blocking, validate_figure


def compile_figure(text: str, relations, profile, figure_number: str = "1"):
    document = make_document(text)
    graph, registry = make_graph(document, relations)
    plan = planning.build_plan(document, registry, None)
    item = next(row for row in plan.figures if row.figure_number == figure_number)
    spec, notes = speccing.build_spec(document, graph, registry, item, None)
    if spec is None:
        return document, graph, plan, None, None, notes
    scene = build_scene(spec, graph, profile)
    return document, graph, plan, spec, scene, notes


# --- Test 1: a simple system ------------------------------------------------
TEST1 = """A SIMPLE SYSTEM

BRIEF DESCRIPTION OF THE DRAWINGS
FIG. 1 illustrates the system 100.

DETAILED DESCRIPTION
The system 100 includes the sensor 120 and the controller 130. The sensor 120 communicates
measurement data to the controller 130.
"""


def test_1_simple_system_draws_exactly_what_is_disclosed(profile):
    _doc, _graph, _plan, spec, scene, _notes = compile_figure(
        TEST1, [("100", "contains", "120", "none"),
                ("100", "contains", "130", "none"),
                ("120", "transmits_to", "130", "subject_to_object")], profile)
    assert {label.reference_numeral for label in scene.labels} == {"100", "120", "130"}
    boundary = scene.node("e100")
    assert boundary is not None and boundary.is_container
    for child in ("e120", "e130"):
        node = scene.node(child)
        assert boundary.box.x <= node.box.x and node.box.right <= boundary.box.right
    directed = [edge for edge in scene.edges if edge.arrow_at_end]
    assert len(directed) == 1
    assert directed[0].from_entity == "e120" and directed[0].to_entity == "e130"


# --- Test 2: the duplicate-numeral trap -------------------------------------
def test_2_a_numeral_is_never_printed_twice(profile):
    _doc, _graph, _plan, _spec, scene, _notes = compile_figure(
        TEST1, [("120", "transmits_to", "130", "subject_to_object")], profile)
    printed = [label.reference_numeral for label in scene.labels]
    assert len(printed) == len(set(printed))


# --- Test 3: the unsupported-component trap ---------------------------------
def test_3_a_component_the_patent_never_mentions_is_never_drawn(profile):
    _doc, _graph, _plan, spec, scene, _notes = compile_figure(
        TEST1, [("120", "transmits_to", "130", "subject_to_object")], profile)
    names = {node.caption.lower() for node in scene.nodes}
    assert not any("battery" in name or "power" in name for name in names)
    assert {node.entity_id for node in scene.nodes} <= {"e100", "e120", "e130"}


# --- Test 4: the direction trap ---------------------------------------------
TEST4 = """A CONTROL SYSTEM

BRIEF DESCRIPTION OF THE DRAWINGS
FIG. 1 illustrates the controller 130 and the actuator 140.

DETAILED DESCRIPTION
The controller 130 transmits a command to the actuator 140.
"""


def test_4_a_direction_is_drawn_the_way_the_patent_states_it(profile):
    _doc, _graph, _plan, _spec, scene, _notes = compile_figure(
        TEST4, [("130", "transmits_to", "140", "subject_to_object")], profile)
    directed = [edge for edge in scene.edges if edge.arrow_at_end]
    assert len(directed) == 1
    assert (directed[0].from_entity, directed[0].to_entity) == ("e130", "e140")


# --- Test 5: embodiment separation ------------------------------------------
TEST5 = """A SENSING DEVICE

BRIEF DESCRIPTION OF THE DRAWINGS
FIG. 1 illustrates the housing 110 and the sensor 120.

DETAILED DESCRIPTION
In a first embodiment, the sensor 120 is positioned within the housing 110.

In another embodiment, the sensor 120 is mounted externally to the housing 110.
"""


def test_5_alternatives_are_not_drawn_at_the_same_time(profile):
    document = make_document(TEST5)
    graph, registry = make_graph(document, [])
    paragraphs = {p.id: p for p in document.paragraphs}
    inside = next(p for p in document.paragraphs if "within the housing" in p.text)
    outside = next(p for p in document.paragraphs if "externally" in p.text)
    from pfc.schemas import Evidence, Relation

    graph.relations = [
        Relation(id="rel_inside", subject="e120", predicate="inside", object="e110",
                 embodiment_scope=["a first embodiment"],
                 evidence=[Evidence(section_id=inside.section_id, paragraph_id=inside.id,
                                    quote_start=0, quote_end=40, quote=inside.text[:40])]),
        Relation(id="rel_outside", subject="e120", predicate="mounted_on", object="e110",
                 embodiment_scope=["another embodiment"],
                 evidence=[Evidence(section_id=outside.section_id, paragraph_id=outside.id,
                                    quote_start=0, quote_end=40, quote=outside.text[:40])]),
    ]
    plan = planning.build_plan(document, registry, None)
    item = next(row for row in plan.figures if row.figure_number == "1")
    spec, notes = speccing.build_spec(document, graph, registry, item, None)
    chosen = {relation.relation_id for relation in spec.relations}
    assert len(chosen) == 1, "one figure must not assert both alternatives at once"
    assert any("alternative" in note for note in notes)


# --- Test 6: a component with no reference numeral --------------------------
TEST6 = """A GRIPPING DEVICE

BRIEF DESCRIPTION OF THE DRAWINGS
FIG. 1 illustrates the vacuum pump and the housing 110.

DETAILED DESCRIPTION
The housing 110 encloses a vacuum pump, which is connected to the suction cup 130. The
suction cup 130 seals against a workpiece.
"""


def test_6_an_unnumbered_component_asks_for_the_text_to_change(profile):
    document = make_document(TEST6)
    graph, registry = make_graph(document, [])
    plan = planning.build_plan(document, registry, None)
    item = next(row for row in plan.figures if row.figure_number == "1")
    spec, notes = speccing.build_spec(document, graph, registry, item, None)
    assert any(annotation.startswith("needs-numeral:") for annotation in spec.annotations)
    scene = build_scene(spec, graph, profile)
    bundle = FigureBundle(spec=spec, scene=scene, svg=render_svg(scene, profile))
    context = ValidationContext(graph=graph, profile=profile, plan=plan, figure=bundle)
    issues = blocking(validate_figure(context))
    assert [issue.rule_id for issue in issues] == ["REF007"]
    assert all(issue.repair_action == "revise_text" for issue in issues)
    # And no numeral was invented for it.
    assert {label.reference_numeral for label in scene.labels} <= set(registry)


# --- Test 7: the described figure set is the produced figure set -------------
TEST7 = """A SYSTEM AND A METHOD

BRIEF DESCRIPTION OF THE DRAWINGS
FIG. 1 illustrates the system 100.
FIG. 2 is a flowchart of the method 500.

DETAILED DESCRIPTION
The system 100 includes the sensor 120. The method 500 includes receiving data 502.
"""


def test_7_the_figure_set_is_the_one_the_patent_describes():
    document = make_document(TEST7)
    graph, registry = make_graph(document, [])
    plan = planning.build_plan(document, registry, None)
    assert [item.figure_number for item in plan.figures] == ["1", "2"]
    assert plan.source == "explicit"
    assert plan.figures[1].figure_type == "flowchart"


def test_7b_a_range_of_figures_is_expanded():
    assert planning.expand_figure_reference("1A-1E") == ["1A", "1B", "1C", "1D", "1E"]
    assert planning.expand_figure_reference("4 and 5") == ["4", "5"]
    assert planning.expand_figure_reference("6 to 8") == ["6", "7", "8"]
    assert planning.expand_figure_reference("2") == ["2"]


# --- Test 8: cross-figure identity ------------------------------------------
TEST8 = """A SYSTEM IN TWO VIEWS

BRIEF DESCRIPTION OF THE DRAWINGS
FIG. 1 illustrates a block diagram of the system 100.
FIG. 3 illustrates a block diagram of the sensor 120.

DETAILED DESCRIPTION
As shown in FIG. 1, the system 100 includes the sensor 120 and the controller 130.

As shown in FIG. 3, the sensor 120 includes the detector 122 and communicates with the
controller 130.
"""


def test_8_a_numeral_means_the_same_thing_on_every_sheet(profile):
    document = make_document(TEST8)
    graph, registry = make_graph(document, [("120", "communicates_with", "130", "none")])
    plan = planning.build_plan(document, registry, None)
    bundles = []
    for sheet, item in enumerate(plan.figures, 1):
        spec, _ = speccing.build_spec(document, graph, registry, item, None)
        if spec is None:
            continue
        scene = build_scene(spec, graph, profile, sheet_number=sheet,
                            sheet_total=len(plan.figures))
        bundles.append(FigureBundle(spec=spec, scene=scene,
                                    svg=render_svg(scene, profile)))
    assert len(bundles) == 2
    from pfc.validate import validate_job

    context = ValidationContext(graph=graph, profile=profile, plan=plan, figures=bundles)
    assert blocking(validate_job(context)) == []
    meaning = {}
    for bundle in bundles:
        for label in bundle.scene.labels:
            meaning.setdefault(label.reference_numeral, label.entity_id)
            assert meaning[label.reference_numeral] == label.entity_id
