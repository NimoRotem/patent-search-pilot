"""Block, data-flow, logical and network figures.

The convention this renderer follows is the one an examiner expects from a system figure: named
rectangles, orthogonal connectors, an arrowhead only where the description gave a direction, a
containing outline drawn as a plain rectangle around what it holds, and the reference numeral
beside each box on its own leader rather than crammed inside it.

Captions go inside the boxes because a block diagram of unnamed rectangles communicates
nothing, and the names used are the drafter's own words from the reference registry.
"""
from __future__ import annotations

from ..profiles import DrawingProfile
from ..schemas import Box, LayoutNode, LayoutScene
from . import common
from .svgdoc import SvgDocument, number

CORNER_RADIUS_RATIO = 0.10


def _draw_node(doc: SvgDocument, node: LayoutNode) -> None:
    profile = doc.profile
    doc.open_group(data_entity_id=node.entity_id,
                   data_reference=node.reference_numeral or "",
                   data_shape=node.shape,
                   data_role=node.role)
    if node.is_container:
        # A container is a plain rectangle. It is drawn first in the document order so the parts
        # inside it are painted over it, and it carries no fill so nothing is hidden.
        doc.rect(node.box, data_container="1")
    elif node.shape == "cylinder":
        doc.cylinder(node.box)
    elif node.shape in {"ellipse"}:
        doc.ellipse(node.box)
    elif node.shape == "circle":
        doc.circle(node.box)
    elif node.shape == "diamond":
        from ..schemas import Point
        doc.polygon([Point(x=node.box.cx, y=node.box.y),
                     Point(x=node.box.right, y=node.box.cy),
                     Point(x=node.box.cx, y=node.box.bottom),
                     Point(x=node.box.x, y=node.box.cy)])
    elif node.shape == "stadium":
        doc.rect(node.box, radius=node.box.height / 2)
    else:
        doc.rect(node.box, radius=min(node.box.width, node.box.height) * CORNER_RADIUS_RATIO)

    if node.caption:
        height = profile.caption_height
        if node.is_container:
            # A container's name belongs on its own top edge, where it cannot be read as the
            # name of something inside it.
            doc.text(node.box.x + profile.container_padding * 0.6,
                     node.box.y + height * 1.4, node.caption, height=height,
                     data_caption_for=node.entity_id)
        else:
            from .. import textfit
            lines = textfit.wrap(profile, node.caption,
                                 node.box.width - height * 1.2, height, 3)
            doc.text_block(lines, node.box.cx, node.box.cy, height=height,
                           data_caption_for=node.entity_id)
    doc.close_group()


def render(scene: LayoutScene, profile: DrawingProfile) -> str:
    doc = SvgDocument(profile, common.figure_metadata(scene))
    common.open_artwork(doc, scene)
    # Containers before their contents, so a nested part is never hidden behind its housing.
    for node in sorted(scene.nodes, key=lambda item: (item.depth, item.entity_id)):
        _draw_node(doc, node)
    for edge in scene.edges:
        common.draw_edge(doc, edge)
    common.draw_labels(doc, scene)
    doc.close_group()
    common.draw_sheet_furniture(doc, scene)
    return doc.render()
