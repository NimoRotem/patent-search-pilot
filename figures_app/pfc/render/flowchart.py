"""Method figures.

A patent flowchart has its own conventions and they are not the block diagram's: the step text
goes inside the box because the step IS the text, a test is a diamond, a start or end is a
stadium, every arrow carries a head because a method has a direction by definition, and a
branch is labelled with the drafter's own word for the outcome.

The step numerals sit beside their boxes on leaders, like any other reference numeral, so a
figure whose steps are numbered 502, 504, 506 reads the way the description that names them
does.
"""
from __future__ import annotations

from .. import textfit
from ..profiles import DrawingProfile
from ..schemas import LayoutNode, LayoutScene, Point
from . import common
from .svgdoc import SvgDocument

MAX_STEP_LINES = 4


def _draw_step(doc: SvgDocument, node: LayoutNode) -> None:
    profile = doc.profile
    doc.open_group(data_entity_id=node.entity_id,
                   data_reference=node.reference_numeral or "",
                   data_shape=node.shape,
                   data_step="1")
    if node.shape == "diamond":
        doc.polygon([Point(x=node.box.cx, y=node.box.y),
                     Point(x=node.box.right, y=node.box.cy),
                     Point(x=node.box.cx, y=node.box.bottom),
                     Point(x=node.box.x, y=node.box.cy)])
        inner_width = node.box.width * 0.55
    elif node.shape == "stadium":
        doc.rect(node.box, radius=node.box.height / 2)
        inner_width = node.box.width - node.box.height
    else:
        doc.rect(node.box, radius=profile.caption_height * 0.5)
        inner_width = node.box.width - profile.caption_height * 1.4

    height = profile.caption_height
    inner_height = (node.box.height * 0.55 if node.shape == "diamond"
                    else node.box.height - height * 0.6)
    lines, size = textfit.fit(profile, node.caption, max(height * 4, inner_width),
                              inner_height, height, profile.min_reference_height,
                              MAX_STEP_LINES)
    doc.text_block(lines, node.box.cx, node.box.cy, height=size,
                   data_caption_for=node.entity_id)
    doc.close_group()


def render(scene: LayoutScene, profile: DrawingProfile) -> str:
    doc = SvgDocument(profile, common.figure_metadata(scene))
    common.open_artwork(doc, scene)
    for node in scene.nodes:
        _draw_step(doc, node)
    for edge in scene.edges:
        common.draw_edge(doc, edge)
    common.draw_labels(doc, scene)
    doc.close_group()
    common.draw_sheet_furniture(doc, scene)
    return doc.render()
