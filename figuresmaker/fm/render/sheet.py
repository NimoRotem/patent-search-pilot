"""Putting the figures on paper.

37 CFR 1.84 is specific about the paper and what may be on it:

  (f) sheets are 21.0 x 29.7 cm (A4) or 21.6 x 27.9 cm (8 1/2 by 11 inches).
  (g) margins of 2.5 cm at the top, 2.5 cm on the left, 1.5 cm on the right and 1.0 cm at the
      bottom, and the sight must not exceed 17.0 x 26.2 cm.
  (i) views are arranged upright, not crowded, and separated by adequate space.
  (t) sheets are numbered in consecutive Arabic numerals within the sight, in the middle of the
      top of the sheet, not in the margin.
  (u) views are numbered consecutively in Arabic numerals preceded by the abbreviation FIG.

So the packer here is not a general-purpose one. It fits each figure to the sight, keeps them in
label order because the numbering has to be consecutive, and starts a new sheet rather than
shrink a figure past the point where its reference characters would fall below 0.32 cm.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .. import geom
from ..drawing import CAPTION_SIZE, Figure, normalise
from ..geom import BBox

# Paper, in millimetres.
PAPERS: dict[str, tuple[float, float]] = {
    "a4": (210.0, 297.0),
    "letter": (215.9, 279.4),
}

MARGIN_TOP = 25.0
MARGIN_LEFT = 25.0
MARGIN_RIGHT = 15.0
MARGIN_BOTTOM = 10.0
SIGHT_MAX_W = 170.0
SIGHT_MAX_H = 262.0

FIGURE_GAP = 12.0
CAPTION_GAP = 6.0
# 37 CFR 1.84(t) puts the sheet number in the middle of the top of the sheet and explicitly NOT
# in the margin, so the band it needs is taken out of the top of the sight rather than borrowed
# from the 2.5 cm above it.
SHEET_NUMBER_BAND = CAPTION_SIZE + 3.0
# Characters stay full size however far the geometry is reduced, so the limit is not the rule any
# more, it is legibility: below about half size the drawing's own detail is smaller than the
# characters written on it. A figure that still does not fit is reported, not squeezed.
MIN_SCALE = 0.5


@dataclass
class Placed:
    """One figure on one sheet: where it sits, and how much it was reduced.

    ``origin`` is the top-left of the figure's footprint in scaled figure coordinates. It is not
    simply the geometry's bounding box times the scale, because the characters do not scale with
    the drawing: a reduced view keeps its reference characters at full size, so they stick out
    further than the geometry does and the footprint has to be measured, not calculated.
    """
    label: str
    index: int
    x: float
    y: float
    scale: float
    width: float
    height: float
    origin: tuple[float, float] = (0.0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "index": self.index, "x": round(self.x, 3),
                "y": round(self.y, 3), "scale": round(self.scale, 5),
                "width": round(self.width, 3), "height": round(self.height, 3),
                "origin": [round(self.origin[0], 3), round(self.origin[1], 3)]}


@dataclass
class Sheet:
    number: int
    total: int
    paper: str
    placed: list[Placed] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "total": self.total, "paper": self.paper,
                "placed": [p.to_dict() for p in self.placed]}


def sight(paper: str) -> BBox:
    width, height = PAPERS.get(paper, PAPERS["a4"])
    x1 = min(width - MARGIN_RIGHT, MARGIN_LEFT + SIGHT_MAX_W)
    y1 = min(height - MARGIN_BOTTOM, MARGIN_TOP + SIGHT_MAX_H)
    return (MARGIN_LEFT, MARGIN_TOP, x1, y1)


# A line placed exactly on the boundary of the sight has half its width outside it. The rule is
# about ink, not about centre lines, so the packer keeps clear of the edge by more than the
# widest stroke.
EDGE_SAFETY = 0.6


def content_area(paper: str) -> BBox:
    """Where a figure may be placed: the sight, less the sheet number band and the edge safety."""
    area = sight(paper)
    return (area[0] + EDGE_SAFETY, area[1] + SHEET_NUMBER_BAND,
            area[2] - EDGE_SAFETY, area[3] - EDGE_SAFETY)


def sheet_number_point(paper: str) -> tuple[float, float]:
    area = sight(paper)
    return ((area[0] + area[2]) / 2.0, area[1] + CAPTION_SIZE * 0.6)


def footprint(figure: Figure, scale: float) -> BBox:
    """What a figure occupies at a given reduction, in scaled figure coordinates.

    Geometry is multiplied by the scale; text is not, because 37 CFR 1.84(p)(3) sets a floor on
    the height of a character on the sheet and a reduced view is lettered at the same size as a
    full one. So the footprint has to be assembled from both.
    """
    boxes: list[BBox] = []
    for prim in figure.decorated_prims():
        if prim.kind == "text":
            at = prim.pts[0]
            boxes.append(geom.text_bbox(prim.text, prim.size, at[0] * scale, at[1] * scale,
                                        prim.anchor, prim.baseline))
            continue
        box = prim.bbox()
        boxes.append((box[0] * scale, box[1] * scale, box[2] * scale, box[3] * scale))
    for leader in figure.leaders:
        if leader.points:
            box = geom.poly_bbox(leader.points)
            boxes.append((box[0] * scale, box[1] * scale, box[2] * scale, box[3] * scale))
    body = geom.bbox_union(boxes) or (0.0, 0.0, 1.0, 1.0)
    # The caption sits under the figure, centred, and is also full size.
    caption_w, _height = geom.text_extent(figure.label, CAPTION_SIZE)
    centre = (body[0] + body[2]) / 2.0
    return (min(body[0], centre - caption_w / 2.0), body[1],
            max(body[2], centre + caption_w / 2.0),
            body[3] + CAPTION_GAP + CAPTION_SIZE)


def fit_scale(figure: Figure, avail_w: float, avail_h: float) -> float:
    """The largest reduction that fits, found by bisection because text does not scale."""
    def fits(scale: float) -> bool:
        box = footprint(figure, scale)
        return (box[2] - box[0]) <= avail_w + 1e-6 and (box[3] - box[1]) <= avail_h + 1e-6

    if fits(1.0):
        return 1.0
    low, high = MIN_SCALE, 1.0
    if not fits(low):
        return MIN_SCALE
    for _ in range(24):
        mid = (low + high) / 2.0
        if fits(mid):
            low = mid
        else:
            high = mid
    return low


def pack(figures: Sequence[Figure], paper: str = "a4") -> list[Sheet]:
    """Figures onto sheets, in label order, shelf by shelf.

    Order is not negotiable: 37 CFR 1.84(u) requires the views to be numbered consecutively, and
    a reader looking for FIG. 5 expects it after FIG. 4 rather than wherever it fitted best.
    """
    area = content_area(paper)
    avail_w = area[2] - area[0]
    avail_h = area[3] - area[1]
    sheets: list[Sheet] = []
    current: list[Placed] = []
    shelf_y = 0.0
    shelf_h = 0.0
    cursor_x = 0.0

    def flush() -> None:
        nonlocal current, shelf_y, shelf_h, cursor_x
        if current:
            sheets.append(Sheet(number=len(sheets) + 1, total=0, paper=paper, placed=current))
        current = []
        shelf_y = shelf_h = cursor_x = 0.0

    for index, figure in enumerate(figures):
        normalise(figure, margin=0.0)
        scale = fit_scale(figure, avail_w, avail_h)
        box = footprint(figure, scale)
        width, height = box[2] - box[0], box[3] - box[1]

        if cursor_x > 0 and cursor_x + FIGURE_GAP + width > avail_w + 1e-6:
            shelf_y += shelf_h + FIGURE_GAP
            shelf_h = 0.0
            cursor_x = 0.0
        if shelf_y + height > avail_h + 1e-6 and current:
            flush()
        x = area[0] + cursor_x
        y = area[1] + shelf_y
        current.append(Placed(label=figure.label, index=index, x=x, y=y, scale=scale,
                              width=width, height=height, origin=(box[0], box[1])))
        cursor_x += width + FIGURE_GAP
        shelf_h = max(shelf_h, height)

    flush()
    for sheet in sheets:
        sheet.total = len(sheets)
    return sheets


# ------------------------------------------------------------------------------------ output


def sheet_svg(sheet: Sheet, figures: Sequence[Figure], *, show_margins: bool = False) -> str:
    """One sheet as SVG, in real millimetres.

    The width and height carry their unit and the viewBox is the sheet in millimetres, so one
    user unit is one millimetre everywhere: the browser, the PDF and the rasteriser all agree,
    and a 0.45 mm line is 0.45 mm on paper rather than 0.45 of whatever the converter assumed.
    """
    width, height = PAPERS.get(sheet.paper, PAPERS["a4"])
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'width="{width:.2f}mm" height="{height:.2f}mm" '
        f'viewBox="0 0 {width:.3f} {height:.3f}" '
        f'data-sheet="{sheet.number}" data-paper="{sheet.paper}">',
        f'<rect x="0" y="0" width="{width:.3f}" height="{height:.3f}" fill="#fff" '
        f'stroke="none"/>']

    if show_margins:
        area = sight(sheet.paper)
        parts.append(
            f'<rect x="{area[0]:.3f}" y="{area[1]:.3f}" width="{area[2] - area[0]:.3f}" '
            f'height="{area[3] - area[1]:.3f}" fill="none" stroke="#7aa7d8" '
            f'stroke-width="0.2" stroke-dasharray="1.5 1.5" data-role="guide"/>')

    # 37 CFR 1.84(t): the sheet number sits within the sight, in the middle of the top.
    number = f"{sheet.number}/{sheet.total}"
    at = sheet_number_point(sheet.paper)
    parts.append(_text_svg(at[0], at[1], number, CAPTION_SIZE, "sheet_number"))

    for placed in sheet.placed:
        figure = figures[placed.index]
        scale = placed.scale
        dx = placed.x - placed.origin[0]
        dy = placed.y - placed.origin[1]
        parts.append(f'<g data-figure="{figure.label}" data-scale="{scale:.5f}">')
        for prim in figure.decorated_prims():
            svg = prim.svg(scale, dx, dy, text_scale=1.0, stroke_scale=1.0)
            if svg:
                parts.append(svg)
        parts.append(_text_svg(placed.x + placed.width / 2.0,
                               placed.y + placed.height - CAPTION_SIZE * 0.25,
                               figure.label, CAPTION_SIZE, "caption"))
        parts.append("</g>")

    parts.append("</svg>")
    return "\n".join(parts)


def figure_svg(figure: Figure, *, px_per_mm: float = 3.2, pad: float = 6.0,
               caption: bool = True) -> str:
    """One figure on its own, for the editor.

    The coordinate system is millimetres, the same as everywhere else; ``px_per_mm`` only sets
    how large the browser draws it. The editor converts a mouse position back into millimetres
    through the SVG's own screen matrix, so it never has to know the scale.
    """
    box = figure.content_bbox()
    width = (box[2] - box[0]) + 2 * pad
    height = (box[3] - box[1]) + 2 * pad + (CAPTION_SIZE + CAPTION_GAP if caption else 0.0)
    dx = pad - box[0]
    dy = pad - box[1]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'width="{width * px_per_mm:.1f}" height="{height * px_per_mm:.1f}" '
        f'viewBox="0 0 {width:.3f} {height:.3f}" '
        f'data-figure="{figure.label}" data-dx="{dx:.4f}" data-dy="{dy:.4f}" '
        f'class="fm-figure-svg">',
        f'<rect x="0" y="0" width="{width:.3f}" height="{height:.3f}" fill="#fff" '
        f'stroke="none"/>']
    for prim in figure.prims:
        svg = prim.svg(1.0, dx, dy)
        if svg:
            parts.append(svg)
    for leader in figure.leaders:
        points = " ".join(f"{p[0] + dx:.3f},{p[1] + dy:.3f}" for p in leader.points)
        tip = leader.points[-1]
        parts.append(f'<polyline points="{points}" stroke="#000" fill="none" '
                     f'stroke-width="0.35" stroke-linecap="round" '
                     f'data-role="leader" data-owner="{leader.numeral}"/>')
        parts.append(f'<circle cx="{tip[0] + dx:.3f}" cy="{tip[1] + dy:.3f}" r="1.1" '
                     f'class="fm-tip" data-owner="{leader.numeral}" fill="transparent" '
                     f'stroke="none"/>')
    for label in figure.labels:
        parts.append(
            f'<text x="{label.x + dx:.3f}" y="{label.y + dy:.3f}" '
            f'font-family="DejaVu Sans, Liberation Sans, Helvetica, Arial, sans-serif" '
            f'font-size="{label.size:.3f}" text-anchor="middle" '
            f'dominant-baseline="central" fill="#000" data-role="numeral" '
            f'data-owner="{label.numeral}" data-placed="{label.placed_by}" '
            f'class="fm-numeral">{label.numeral}</text>')
    if caption:
        parts.append(_text_svg((box[2] - box[0]) / 2.0 + pad, height - pad * 0.5, figure.label,
                               CAPTION_SIZE, "caption"))
    parts.append("</svg>")
    return "\n".join(parts)


def _text_svg(x: float, y: float, body: str, size: float, role: str) -> str:
    return (f'<text x="{x:.3f}" y="{y:.3f}" '
            f'font-family="DejaVu Sans, Liberation Sans, Helvetica, Arial, sans-serif" '
            f'font-size="{size:.3f}" text-anchor="middle" dominant-baseline="central" '
            f'fill="#000" stroke="none" data-role="{role}">{body}</text>')


# ---------------------------------------------------------------------------- what was drawn


def sheet_geometry(sheet: Sheet, figures: Sequence[Figure]) -> dict[str, Any]:
    """Everything on a sheet in sheet millimetres, for the render checks.

    The validator never reads the SVG back. It is given the same numbers the SVG was written
    from, transformed into the coordinate system the rule is stated in.
    """
    lines: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    leaders: list[dict[str, Any]] = []
    area = sight(sheet.paper)

    number = f"{sheet.number}/{sheet.total}"
    at = sheet_number_point(sheet.paper)
    texts.append({"text": number, "role": "sheet_number", "size": CAPTION_SIZE, "owner": "",
                  "figure": "", "bbox": geom.text_bbox(number, CAPTION_SIZE, at[0], at[1])})

    for placed in sheet.placed:
        figure = figures[placed.index]
        scale = placed.scale

        def to_sheet(point, _placed=placed, _scale=scale):
            return (_placed.x - _placed.origin[0] + point[0] * _scale,
                    _placed.y - _placed.origin[1] + point[1] * _scale)

        for prim in figure.decorated_prims():
            if prim.kind == "text":
                point = to_sheet(prim.pts[0])
                texts.append({"text": prim.text, "role": prim.role, "size": prim.size,
                              "owner": prim.owner, "figure": figure.label,
                              "bbox": geom.text_bbox(prim.text, prim.size, point[0], point[1],
                                                     prim.anchor, prim.baseline)})
                continue
            for poly in prim.polys():
                lines.append({"points": [to_sheet(p) for p in poly], "role": prim.role,
                              "owner": prim.owner, "figure": figure.label,
                              "width": prim.width})
        for leader in figure.leaders:
            leaders.append({"numeral": leader.numeral, "figure": figure.label,
                            "points": [to_sheet(p) for p in leader.points]})
        texts.append({"text": figure.label, "role": "caption", "size": CAPTION_SIZE, "owner": "",
                      "figure": figure.label,
                      "bbox": geom.text_bbox(figure.label, CAPTION_SIZE,
                                             placed.x + placed.width / 2.0,
                                             placed.y + placed.height - CAPTION_SIZE * 0.25)})
    return {"lines": lines, "texts": texts, "leaders": leaders, "sight": area,
            "content": content_area(sheet.paper), "paper": sheet.paper,
            "placed": [p.to_dict() for p in sheet.placed]}
