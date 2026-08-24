"""A sheet whose artwork was generated and whose numerals were not.

The raster goes down first, the reference numerals and their leaders go on top, drawn by exactly
the same code that draws them on a deterministic sheet. That is the whole point of the split: an
image model can be trusted to draw a housing and cannot be trusted to write "112" next to it, so
it draws the housing and the compiler writes the numeral.

The artwork is embedded rather than linked, so one SVG is one complete figure. It is also the
reason the monochrome rule has to know about this mode: a raster IS the drawing here, where on a
deterministic sheet it would be a defect.
"""
from __future__ import annotations

import base64
import hashlib

from ..profiles import DrawingProfile
from ..schemas import Box, LayoutScene
from . import common
from .svgdoc import SvgDocument, number


def render(scene: LayoutScene, profile: DrawingProfile, artwork: bytes = b"") -> str:
    doc = SvgDocument(profile, {
        **common.figure_metadata(scene),
        "artwork": "generated",
        # The artwork's fingerprint travels with the sheet: a figure whose raster is replaced is
        # a different figure, and the manifest can say which one it was.
        "artwork_sha256": hashlib.sha256(artwork).hexdigest() if artwork else "",
    })

    box = scene.artwork_box or Box(x=profile.drawing_left, y=profile.drawing_top,
                                   width=profile.drawing_width,
                                   height=profile.drawing_height)
    if artwork:
        encoded = base64.b64encode(artwork).decode("ascii")
        doc.parts.append(
            f'<image x="{number(box.x)}" y="{number(box.y)}" width="{number(box.width)}" '
            f'height="{number(box.height)}" preserveAspectRatio="xMidYMid meet" '
            f'data-artwork="generated" '
            f'xlink:href="data:image/png;base64,{encoded}" '
            f'href="data:image/png;base64,{encoded}"/>')

    common.open_artwork(doc, scene)
    # No outlines: the raster already drew them. The nodes exist so the numerals have something
    # to point at and the geometry rules have something to measure.
    common.draw_labels(doc, scene)
    doc.close_group()
    common.draw_sheet_furniture(doc, scene)

    svg = doc.render()
    # xlink is only needed for the raster, and only some renderers still want it.
    return svg.replace(
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1"',
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1"', 1)
