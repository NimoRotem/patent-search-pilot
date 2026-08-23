"""Drawing operations the three renderers share, and the sheet furniture every figure carries.

What is shared here is deliberately only the mechanical part: how a numeral and its leader are
drawn, how an arrowhead is attached, what the sheet's own caption and number look like. The
decisions that differ between a block diagram, a flowchart and a mechanical view — which
primitive a component becomes, whether a caption goes inside it, whether a relationship is an
arrow or a plain line — live in the renderer for that figure type, because sharing those is how
every figure ends up looking like the same wrong thing.
"""
from __future__ import annotations

from ..profiles import DrawingProfile
from ..schemas import LayoutEdge, LayoutLabel, LayoutScene, Point
from .svgdoc import BLACK, SvgDocument, number

RENDERER_VERSION = "pfc-svg-1.0.0"


def open_artwork(doc: SvgDocument, scene: LayoutScene) -> None:
    """The single stroked group all geometry lives in: black, uniform width, no fill."""
    doc.open_group(id=scene.figure_id, fill="none", stroke=BLACK,
                   stroke_width=number(doc.profile.stroke),
                   stroke_linecap="round", stroke_linejoin="round")


def draw_edge(doc: SvgDocument, edge: LayoutEdge, *, thin: bool = False) -> None:
    """One connection, with an arrowhead only where the direction was disclosed."""
    profile = doc.profile
    doc.polyline(
        edge.points,
        stroke_width=number(profile.thin_stroke if thin else profile.stroke),
        data_relation_id=edge.relation_id,
        data_from=edge.from_entity,
        data_to=edge.to_entity,
        data_edge_type=edge.edge_type,
        data_directed="1" if edge.arrow_at_end else "0")
    if edge.arrow_at_end and len(edge.points) >= 2:
        doc.arrowhead(edge.points[-1], edge.points[-2], data_arrow_for=edge.relation_id)
    if edge.arrow_at_start and len(edge.points) >= 2:
        doc.arrowhead(edge.points[0], edge.points[1], data_arrow_for=edge.relation_id)
    if edge.label:
        middle = edge.points[len(edge.points) // 2]
        doc.text(middle.x + profile.reference_height * 0.4,
                 middle.y - profile.reference_height * 0.3, edge.label,
                 height=profile.reference_height, data_edge_label=edge.relation_id)


def draw_labels(doc: SvgDocument, scene: LayoutScene) -> None:
    """Every reference numeral, each with the leader that binds it to one object.

    The leader is drawn first so the numeral sits on top of it, and both carry the entity they
    belong to, so a validator reading the finished sheet can check the binding rather than
    trusting the intention.
    """
    profile = doc.profile
    for label in scene.labels:
        target = label.leader_points[-1]
        doc.polyline(label.leader_points,
                     stroke_width=number(profile.thin_stroke),
                     data_leader_for=label.entity_id,
                     data_leader_reference=label.reference_numeral)
        doc.text(label.position.x, label.position.y, label.reference_numeral,
                 height=profile.reference_height,
                 data_reference_label=label.reference_numeral,
                 data_entity_id=label.entity_id,
                 data_leader_target=f"{number(target.x)},{number(target.y)}")


def draw_sheet_furniture(doc: SvgDocument, scene: LayoutScene) -> None:
    """The figure's own caption and the sheet number.

    Both sit in the margin the office reserves for them, and both are the only text on the sheet
    that is not a reference numeral or a component name.
    """
    profile = doc.profile
    sheet_text = profile.sheet_number_format.format(sheet=scene.sheet_number,
                                                    total=scene.sheet_total)
    doc.text(profile.sheet_width / 2, profile.margin_top - profile.caption_height * 0.6,
             sheet_text, height=profile.caption_height, anchor="middle",
             data_sheet_number="1")
    label = profile.label_format.format(number=scene.figure_number.upper())
    doc.text(profile.sheet_width / 2,
             profile.sheet_height - profile.margin_bottom + profile.caption_height * 1.2,
             label, height=profile.caption_height * 1.15, anchor="middle",
             data_figure_label="1")


def figure_metadata(scene: LayoutScene) -> dict:
    return {
        "figure_id": scene.figure_id,
        "figure_number": scene.figure_number,
        "figure_type": scene.figure_type,
        "profile": scene.profile_id,
        "renderer": RENDERER_VERSION,
    }
