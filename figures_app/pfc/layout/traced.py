"""A scene laid out over generated artwork.

The artwork decides where the parts are; this turns that into the same ``LayoutScene`` every
other figure uses, so nothing downstream has to know the drawing came from an image model. The
numerals are placed by the same scored search, checked by the same geometry rules and rendered
by the same code as on a deterministic sheet.

Two things differ, and both are recorded on the scene rather than special-cased downstream:

* the nodes carry no shape, because the artwork already drew them. They exist so a numeral has
  something to point at and a validator has something to measure;
* parts legitimately overlap. In a perspective view a pump sits behind a housing wall, and the
  boxes a reader returns for them intersect. Treating that as a defect would refuse every
  drawing this mode produces, so the overlap rule stands down for a raster-backed scene and
  says so.
"""
from __future__ import annotations

from typing import Optional

from ..imagegrounding import Located
from ..numerals import sort_key
from ..profiles import DrawingProfile
from ..schemas import Box, FigureSpec, LayoutNode, LayoutScene, PatentGraph
from .leaders import place_labels


def build(spec: FigureSpec, graph: PatentGraph, located: Located, profile: DrawingProfile,
          *, artwork_box: Box, sheet_number: int = 1, sheet_total: int = 1,
          seed: int = 0) -> LayoutScene:
    area = Box(x=profile.drawing_left, y=profile.drawing_top,
               width=profile.drawing_width, height=profile.drawing_height)
    roles = {entity.entity_id: entity.role for entity in spec.entities}

    nodes: list[LayoutNode] = []
    for entity in sorted(spec.entities, key=lambda e: sort_key(e.reference_numeral or "")):
        box = located.boxes.get(entity.entity_id)
        if box is None:
            continue
        entity_node = graph.entity(entity.entity_id)
        encloses = entity.entity_id in located.encloses or entity.role == "boundary"
        nodes.append(LayoutNode(
            entity_id=entity.entity_id,
            reference_numeral=entity.reference_numeral,
            caption="", shape="box",
            symbol=(entity_node.appearance.symbol if entity_node else ""),
            orientation=(entity_node.appearance.orientation if entity_node else "horizontal"),
            box=box, depth=0 if encloses else 1, is_container=encloses,
            role=roles.get(entity.entity_id, "primary")))  # type: ignore[arg-type]

    scene = LayoutScene(
        figure_id=spec.figure_id, figure_number=spec.figure_number,
        figure_type=spec.figure_type, profile_id=profile.version_tag,
        sheet_width=profile.sheet_width, sheet_height=profile.sheet_height,
        drawing_area=area, nodes=nodes, edges=[], labels=[],
        caption=spec.title, sheet_number=sheet_number, sheet_total=sheet_total,
        artwork=True, artwork_box=artwork_box)
    return place_labels(scene, profile, seed=seed)


def fit_artwork(profile: DrawingProfile, image_size: tuple[int, int]) -> Box:
    """Where the generated raster sits on the sheet, at its own aspect ratio.

    Centred in the drawing area with a margin kept clear all round, because the numerals and
    their leaders are placed into that margin and a leader that has to start off the sheet has
    nowhere to go.
    """
    area_width = profile.drawing_width
    area_height = profile.drawing_height - profile.caption_height * 3
    reserve = profile.reference_height * 3.5
    usable_w = max(1.0, area_width - 2 * reserve)
    usable_h = max(1.0, area_height - 2 * reserve)

    width, height = image_size
    if width <= 0 or height <= 0:
        width, height = 4, 3
    scale = min(usable_w / width, usable_h / height)
    draw_w, draw_h = width * scale, height * scale
    return Box(x=profile.drawing_left + (area_width - draw_w) / 2,
               y=profile.drawing_top + (area_height - draw_h) / 2,
               width=max(1.0, draw_w), height=max(1.0, draw_h))
