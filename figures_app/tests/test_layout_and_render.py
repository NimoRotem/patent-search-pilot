"""Layout, rendering and the determinism the validators depend on."""
from __future__ import annotations

import re

from conftest import HOUSING_SYSTEM, make_document, make_graph

from pfc import plan as planning
from pfc import spec as speccing
from pfc.layout import build_scene
from pfc.render import render_svg
from pfc.schemas import Evidence, FigureSpec, FlowEdge, FlowStep


def _figure_one(document, graph, registry):
    item = next(row for row in planning.discover_figures(document)
                if row.figure_number == "1")
    spec, _notes = speccing.build_spec(document, graph, registry, item, None)
    return spec


def test_a_scene_places_every_specified_entity(housing_document, profile):
    graph, registry = make_graph(housing_document, [
        ("110", "contains", "120", "none"),
        ("130", "controls", "140", "subject_to_object"),
    ])
    spec = _figure_one(housing_document, graph, registry)
    scene = build_scene(spec, graph, profile)
    drawn = {node.entity_id for node in scene.nodes}
    assert drawn == {entity.entity_id for entity in spec.entities}
    assert {label.reference_numeral for label in scene.labels} == {
        entity.reference_numeral for entity in spec.entities}


def test_everything_stays_inside_the_drawing_area(housing_document, profile):
    graph, registry = make_graph(housing_document, [
        ("110", "contains", "120", "none"),
        ("130", "controls", "140", "subject_to_object"),
    ])
    spec = _figure_one(housing_document, graph, registry)
    scene = build_scene(spec, graph, profile)
    area = scene.drawing_area
    for node in scene.nodes:
        assert area.x <= node.box.x and node.box.right <= area.right
        assert area.y <= node.box.y and node.box.bottom <= area.bottom
    for label in scene.labels:
        assert area.x <= label.box.x and label.box.right <= area.right
        assert area.y <= label.box.y and label.box.bottom <= area.bottom


def test_containment_nests_rather_than_drawing_a_line(housing_document, profile):
    graph, registry = make_graph(housing_document, [("110", "contains", "120", "none")])
    spec = _figure_one(housing_document, graph, registry)
    scene = build_scene(spec, graph, profile)
    housing = scene.node("e110")
    sensor = scene.node("e120")
    assert housing is not None and sensor is not None
    assert housing.box.x <= sensor.box.x and sensor.box.right <= housing.box.right
    assert housing.box.y <= sensor.box.y and sensor.box.bottom <= housing.box.bottom
    assert not [edge for edge in scene.edges
                if {edge.from_entity, edge.to_entity} == {"e110", "e120"}]


def test_rendering_is_deterministic(housing_document, profile):
    graph, registry = make_graph(housing_document, [
        ("110", "contains", "120", "none"),
        ("130", "controls", "140", "subject_to_object"),
    ])
    spec = _figure_one(housing_document, graph, registry)
    first = render_svg(build_scene(spec, graph, profile), profile)
    second = render_svg(build_scene(spec, graph, profile), profile)
    assert first == second
    assert len(first) > 500


def test_the_drawing_is_monochrome_vector_only(housing_document, profile):
    graph, registry = make_graph(housing_document, [("110", "contains", "120", "none")])
    spec = _figure_one(housing_document, graph, registry)
    svg = render_svg(build_scene(spec, graph, profile), profile)
    assert "<image" not in svg
    assert "rgb(" not in svg
    assert set(colour.lower() for colour in re.findall(r"#[0-9A-Fa-f]{6}", svg)) <= {
        "#000000", "#ffffff"}


def test_every_drawn_object_carries_its_semantic_identity(housing_document, profile):
    graph, registry = make_graph(housing_document, [("130", "controls", "140",
                                                     "subject_to_object")])
    spec = _figure_one(housing_document, graph, registry)
    svg = render_svg(build_scene(spec, graph, profile), profile)
    for entity in spec.entities:
        assert f'data-entity-id="{entity.entity_id}"' in svg
    for relation in spec.relations:
        assert f'data-relation-id="{relation.relation_id}"' in svg


def test_an_arrow_appears_only_where_a_direction_was_disclosed(housing_document, profile):
    graph, registry = make_graph(housing_document, [
        ("130", "controls", "140", "subject_to_object"),
        ("120", "adjacent_to", "130", "none"),
    ])
    spec = _figure_one(housing_document, graph, registry)
    scene = build_scene(spec, graph, profile)
    directed = {edge.relation_id for edge in scene.edges if edge.arrow_at_end}
    assert "rel_130_controls_140" in directed
    assert "rel_120_adjacent_to_130" not in directed


def test_a_flowchart_is_drawn_top_to_bottom(profile):
    document = make_document(HOUSING_SYSTEM)
    graph, _registry = make_graph(document, [])
    evidence = [Evidence(section_id="detailed_description",
                         paragraph_id=document.paragraphs[0].id, quote_start=0,
                         quote_end=10, quote="x")]
    spec = FigureSpec(
        figure_id="FIG_2", figure_number="2", figure_type="flowchart", view_type="flow",
        title="method", steps=[
            FlowStep(id="step_1", text="receiving sensor data", reference_numeral="502",
                     evidence=evidence),
            FlowStep(id="step_2", text="processing the sensor data", reference_numeral="504",
                     evidence=evidence),
            FlowStep(id="step_3", text="actuating the actuator", reference_numeral="506",
                     evidence=evidence)],
        step_edges=[FlowEdge(from_step="step_1", to_step="step_2"),
                    FlowEdge(from_step="step_2", to_step="step_3")])
    scene = build_scene(spec, graph, profile)
    ys = [node.box.y for node in scene.nodes]
    assert ys == sorted(ys)
    assert all(edge.arrow_at_end for edge in scene.edges)
    assert {label.reference_numeral for label in scene.labels} == {"502", "504", "506"}


def test_a_numeral_never_sits_on_the_outline_it_names(housing_document, profile):
    """A numeral printed on its own object's edge is as unreadable as one over its neighbour."""
    graph, registry = make_graph(housing_document, [
        ("110", "contains", "120", "none"),
        ("130", "controls", "140", "subject_to_object"),
    ])
    spec = _figure_one(housing_document, graph, registry)
    scene = build_scene(spec, graph, profile)
    by_entity = {node.entity_id: node for node in scene.nodes}
    for label in scene.labels:
        node = by_entity[label.entity_id]
        if node.is_container:
            continue        # a part's numeral legitimately sits inside its housing
        assert not label.box.overlaps(node.box), (
            f"{label.reference_numeral} is printed on the outline of the object it names")


def test_two_numerals_are_spread_when_there_is_room_to_spread_them():
    """Non-overlapping but shoulder to shoulder cost the same as opposite corners.

    On US-2024/0246200-A1 that put 350 and 314 side by side and an independent reader read 350
    as 390. The crowding term is a tie-breaker: it must never outrank a hard constraint.
    """
    from pfc.layout import leaders

    assert leaders.COST_CROWDING < leaders.COST_EDGE_CROSS
    assert leaders.COST_CROWDING < leaders.COST_LABEL_OVERLAP
    assert leaders.COST_CROWDING < leaders.COST_LEADER_CROSS
    assert leaders.COST_CROWDING < leaders.COST_HITS_NODE
    assert leaders.COST_CROWDING < leaders.COST_AMBIGUOUS
    assert leaders.COST_CROWDING < leaders.COST_ON_ARTWORK
    assert leaders.COST_CROWDING < leaders.COST_OUTSIDE


def test_the_crowding_term_falls_to_nothing_at_its_reach(profile):
    """It has to yield entirely where there is no room, or a crowded sheet gets no labels."""
    from pfc.layout import leaders
    from pfc.schemas import Box, LayoutLabel, Point

    def crowding_between(gap: float) -> float:
        here = Box(x=0.0, y=0.0, width=40.0, height=20.0)
        label = LayoutLabel(reference_numeral="1", entity_id="e1",
                            position=Point(x=gap, y=0.0),
                            leader_points=[Point(x=gap, y=0.0), Point(x=gap, y=1.0)],
                            text_width=40.0, text_height=20.0)
        reach = profile.reference_height * leaders.CROWDING_REACH
        distance = abs(label.box.cx - here.cx)
        return 0.0 if distance >= reach else leaders.COST_CROWDING * (1.0 - distance / reach)

    reach = profile.reference_height * leaders.CROWDING_REACH
    assert crowding_between(reach * 3) == 0.0
    assert crowding_between(reach * 0.5) > 0.0
    assert crowding_between(reach * 0.5) < leaders.COST_CROWDING


def test_a_missing_converter_is_reported_rather_than_dropping_the_pdf(tmp_path, profile,
                                                                      monkeypatch):
    """The PDF is what gets filed and the PNG is what the page shows. Losing them was silent.

    This host needed cairosvg installed by hand. Without it every figure would have shipped as
    an SVG alone, with a broken preview and nothing anywhere saying why.
    """
    from pfc import render

    def no_converter(*args, **kwargs):
        raise render.ExportUnavailable("cairosvg is not installed on this box")

    monkeypatch.setattr(render, "svg_to_pdf", no_converter)
    monkeypatch.setattr(render, "svg_to_png", no_converter)
    monkeypatch.setattr(render, "png_via_pdf", no_converter)

    written = render.export_all("<svg xmlns='http://www.w3.org/2000/svg'/>", profile,
                                tmp_path, "fig_1")
    assert written["svg"] == "fig_1.svg", "the canonical drawing is still written"
    assert "pdf" not in written and "png" not in written
    assert "cairosvg" in written["pdf_error"]
    assert "cairosvg" in written["png_error"]
    assert "via PDF" in written["png_error"], "both routes to a PNG are named"


def test_a_working_converter_reports_no_failure(tmp_path, profile):
    """The other half: a successful export must not carry an error key."""
    from pfc import render

    written = render.export_all(
        "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'></svg>",
        profile, tmp_path, "fig_1")
    assert "svg" in written
    if "pdf" in written:
        assert "pdf_error" not in written
    if "png" in written:
        assert "png_error" not in written


def test_reaching_further_never_costs_more_than_crossing_another_leader():
    """Distance is in sheet units; every other weight is a constant.

    So their ratio was a function of how big the drawing is. On a raster-backed sheet the
    numerals sit in the margin and leaders run 300 to 850 units, which made crossing another
    leader (400) cheaper than reaching 400 units further, and FIG. 4 of US-2024/0246200-A1 came
    back with four crossing pairs that no amount of re-placing would undo.
    """
    from pfc.layout import leaders

    assert leaders.DISTANCE_CEILING < leaders.COST_LEADER_CROSS, (
        "the largest possible distance difference has to lose to one crossing")
    assert leaders.DISTANCE_CEILING < leaders.COST_LABEL_OVERLAP
    assert leaders.DISTANCE_CEILING < leaders.COST_HITS_NODE
    assert leaders.DISTANCE_CEILING < leaders.COST_AMBIGUOUS
    assert leaders.DISTANCE_CEILING < leaders.COST_OUTSIDE
    # and it still discriminates below the ceiling, or numerals wander off their objects
    assert leaders.DISTANCE_CEILING > leaders.COST_BEND * 4


def test_the_distance_term_is_bounded_so_a_crossing_always_costs_more(profile):
    """The invariant, checked where it lives: in the cost function, not in a constant.

    An unbounded distance term is a crossing waiting to happen on any sheet big enough. On
    US-2024/0246200-A1 leaders ran 300 to 850 units against COST_LEADER_CROSS at 400.
    """
    from pfc.layout import leaders
    from pfc.schemas import Box, LayoutNode, Point

    area = Box(x=profile.drawing_left, y=profile.drawing_top,
               width=profile.drawing_width, height=profile.drawing_height)
    node = LayoutNode(entity_id="e1", reference_numeral="110", caption="", shape="box",
                      box=Box(x=area.x + 40, y=area.y + 40, width=200.0, height=140.0),
                      depth=1, role="primary")

    # A numeral as far from its object as the sheet allows, with nothing else on the sheet.
    far = (area.right - 200.0, area.bottom - 120.0)
    target = (node.box.right, node.box.bottom)
    route = [Point(x=far[0], y=far[1]), Point(x=target[0], y=target[1])]
    cost = leaders._cost(profile, node, far, route, target, [node], [], [], area, "110")

    span = ((area.width ** 2 + area.height ** 2) ** 0.5)
    assert span > 2000, "the fixture sheet has to be big enough for this to mean anything"
    assert cost < leaders.COST_LEADER_CROSS, (
        f"a leader spanning the sheet costs {cost:.0f}, so crossing one at "
        f"{leaders.COST_LEADER_CROSS:.0f} would be the cheaper choice")


def test_three_numerals_on_one_object_get_leaders_that_do_not_cross(profile):
    """The case the ceiling was for: several numerals pointing at one box in a perspective view.

    A reader that cannot separate three parts returns one box for all three. Their leaders have
    to approach it from different sides rather than converge and cross.
    """
    from pfc.layout.leaders import place_labels
    from pfc.geometry import segments, segments_cross
    from pfc.schemas import Box, LayoutNode, LayoutScene

    shared = Box(x=700.0, y=800.0, width=900.0, height=500.0)
    nodes = [LayoutNode(entity_id=f"e{n}", reference_numeral=str(n), caption="", shape="box",
                        box=shared.model_copy(), depth=1, role="primary")
             for n in (110, 120, 130)]
    scene = LayoutScene(
        figure_id="FIG_4", figure_number="4", figure_type="mechanical_schematic",
        profile_id=profile.version_tag, sheet_width=profile.sheet_width,
        sheet_height=profile.sheet_height,
        drawing_area=Box(x=profile.drawing_left, y=profile.drawing_top,
                         width=profile.drawing_width, height=profile.drawing_height),
        nodes=nodes, edges=[], labels=[], caption="t", sheet_number=1, sheet_total=1,
        artwork=True, artwork_box=Box(x=600.0, y=700.0, width=1100.0, height=700.0))

    placed = place_labels(scene, profile).labels
    assert len(placed) == 3
    crossings = 0
    for first in range(len(placed)):
        for second in range(first + 1, len(placed)):
            for a in segments(placed[first].leader_points):
                for b in segments(placed[second].leader_points):
                    if segments_cross(a, b):
                        crossings += 1
    assert crossings == 0, f"{crossings} crossing pair(s) among three leaders to one box"
