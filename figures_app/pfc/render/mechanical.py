"""Physical views: the conventional symbol for each disclosed part, and its reference numeral.

This renderer is still defined by what it will not draw. No shading, no perspective, no
fasteners that were not described, no dimension or feature count taken from anywhere but the
document. What it does now draw is NOTATION: a coil as a coil, a cut substrate with hatching, a
suction cup as a bell over a stem.

That is a considered change from an earlier version that drew every part as a rectangle. The
rule against inventing a component's appearance is about geometry, and it stands; a symbol is
not geometry, it is the standard mark for the class of thing the applicant named, and a page of
identical rectangles was true and useless. Where the document does not settle what kind of thing
a part is, it is still a plain outline — see pfc/visualclass.py for where the line sits.

Names are not printed inside the outlines. A mechanical patent figure carries reference numerals
only, and the description is what says what each one is.
"""
from __future__ import annotations

from ..profiles import DrawingProfile
from ..schemas import Box, LayoutNode, LayoutScene, Point
from . import common, symbols
from .svgdoc import SvgDocument


def _draw_part(doc: SvgDocument, node: LayoutNode) -> None:
    box = node.box
    doc.open_group(data_entity_id=node.entity_id,
                   data_reference=node.reference_numeral or "",
                   data_shape=node.shape,
                   data_symbol=node.symbol or "",
                   data_role=node.role)
    # A container is always a plain outline whatever its class: it is holding other parts, and a
    # symbol drawn round them would read as a part of them.
    if node.is_container:
        doc.rect(box, radius=min(box.width, box.height) * 0.06)
    elif not symbols.draw(doc, node.symbol or "", box):
        # No symbol for this class, or the document never settled what kind of thing it is.
        if node.shape == "circle":
            doc.circle(box)
        elif node.shape == "ellipse":
            doc.ellipse(box)
        elif node.shape == "cylinder":
            doc.cylinder(box)
        else:
            doc.rect(box, radius=min(box.width, box.height) * 0.1)
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
