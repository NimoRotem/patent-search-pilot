"""Physical views: outlines and reference numerals, and nothing the text did not disclose.

This renderer is defined as much by what it will not draw. No shading, no hatching, no fasteners
that were not described, no rounded industrial-looking profiles chosen because the component is
called a housing. A part whose shape the patent never states is a plain rectangle, and a plain
rectangle with the right numeral on it is a true statement about the invention where a
convincing-looking gripper would be a false one.

Names are not printed inside the outlines. A mechanical patent figure carries reference numerals
only, and the description is what says what each one is.
"""
from __future__ import annotations

from ..profiles import DrawingProfile
from ..schemas import Box, LayoutNode, LayoutScene, Point
from . import common
from .svgdoc import SvgDocument


def _draw_part(doc: SvgDocument, node: LayoutNode) -> None:
    profile = doc.profile
    box = node.box
    doc.open_group(data_entity_id=node.entity_id,
                   data_reference=node.reference_numeral or "",
                   data_shape=node.shape,
                   data_role=node.role)
    if node.shape in {"container", "chamber"}:
        doc.rect(box)
    elif node.shape == "circle":
        doc.circle(box)
    elif node.shape == "ellipse":
        doc.ellipse(box)
    elif node.shape == "cylinder":
        doc.cylinder(box)
    elif node.shape == "tube":
        # Two parallel outlines: the only thing "tubular" discloses is that it is open through.
        inset = min(box.height * 0.28, profile.min_node_height * 0.3)
        doc.rect(box)
        doc.line(Point(x=box.x, y=box.y + inset), Point(x=box.right, y=box.y + inset))
        doc.line(Point(x=box.x, y=box.bottom - inset), Point(x=box.right, y=box.bottom - inset))
    elif node.shape == "shaft":
        doc.rect(box, radius=box.height / 2)
    elif node.shape == "plate":
        doc.rect(box)
    elif node.shape == "opening":
        # An opening is an absence of material, so it is drawn as a broken outline: the same
        # convention a draughtsman uses for an edge that is not solid.
        doc.ellipse(box, stroke_dasharray=f"{doc.profile.stroke * 5:.0f} "
                                          f"{doc.profile.stroke * 3:.0f}")
    else:
        doc.rect(box)
    doc.close_group()


def render(scene: LayoutScene, profile: DrawingProfile) -> str:
    doc = SvgDocument(profile, common.figure_metadata(scene))
    common.open_artwork(doc, scene)
    for node in sorted(scene.nodes, key=lambda item: (item.depth, item.entity_id)):
        _draw_part(doc, node)
    for edge in scene.edges:
        # A physical connection is a plain line. An arrow on it would assert a flow the
        # description did not state.
        common.draw_edge(doc, edge, thin=edge.edge_type == "association")
    common.draw_labels(doc, scene)
    doc.close_group()
    common.draw_sheet_furniture(doc, scene)
    return doc.render()
