"""The checks on the sheet as it will be filed.

Two layers, and both are here because they catch different things. The geometric layer reads the
same numbers the SVG was written from, transformed into sheet millimetres, and answers the
questions the rules are actually stated in: is any ink in the margin, is any character under
0.32 cm, does a lead line reach the thing it points at, does a lead line cross another one.

The raster layer rasterises the sheet and looks at pixels. It exists because the geometric layer
can only find what it knows to look for. A font that failed to load, a converter that dropped a
group, a fill that came out grey: none of those change the geometry and all of them change the
sheet. If the raster layer cannot run it says so as a finding, because a check that quietly does
not happen is worse than no check at all.
"""
from __future__ import annotations

import io
import math
import re
from typing import Any, Iterable, Optional, Sequence

from .. import geom
from ..geom import BBox, Point
from ..schemas import Finding
from . import rules

DPI = 300.0
MM_PER_INCH = 25.4

# What a drawing sheet is allowed to carry as text. Anything else is reported, not removed.
_FIG_LABEL = re.compile(r"^FIG\.\s?\d+[A-Z]*$")
_SHEET_NUMBER = re.compile(r"^\d+\s?/\s?\d+$")
_SECTION_MARK = re.compile(r"^[A-Z]-[A-Z]$")
_ALWAYS_ALLOWED = {"PRIOR ART", "YES", "NO", "START", "END", "BEGIN", "STOP", "A", "B", "C",
                   "D", "E", "F", "N", "Y", "TRUE", "FALSE"}


def _finding(code: str, message: str, *, severity: str = "error", stage: str = "layout",
             figure: str = "", numeral: str = "", detail: Optional[dict] = None) -> Finding:
    cite, basis = rules.decorate(code)
    return Finding(code=code, severity=severity, message=message, stage=stage, figure=figure,
                   numeral=numeral, cite=cite, basis=basis, detail=detail or {})


# --------------------------------------------------------------------------- geometric layer


def check_geometry(sheet_number: int, data: dict[str, Any],
                   allowed_numerals: Iterable[str]) -> list[Finding]:
    out: list[Finding] = []
    sight = data["sight"]
    numerals = set(allowed_numerals)

    out += _check_margins(sheet_number, data, sight)
    out += _check_text(sheet_number, data, numerals)
    out += _check_strokes(sheet_number, data)
    out += _check_leaders(sheet_number, data)
    out += _check_placement(sheet_number, data, sight)
    return out


def _check_margins(sheet_number: int, data: dict[str, Any], sight: BBox) -> list[Finding]:
    out: list[Finding] = []
    worst: dict[str, tuple[float, Point]] = {}
    for record in data["lines"]:
        for point in record["points"]:
            over = _outside_by(point, sight)
            if over <= 0:
                continue
            key = record.get("figure") or ""
            if key not in worst or over > worst[key][0]:
                worst[key] = (over, point)
    for text in data["texts"]:
        box = text["bbox"]
        for point in (box[:2], (box[2], box[1]), box[2:], (box[0], box[3])):
            over = _outside_by(point, sight)
            if over <= 0:
                continue
            key = text.get("figure") or ""
            if key not in worst or over > worst[key][0]:
                worst[key] = (over, point)
    for figure, (over, point) in sorted(worst.items()):
        out.append(_finding(
            "outside_margins",
            f"on sheet {sheet_number}, {figure or 'the sheet'} puts ink {over:.1f} mm outside the "
            "sight of the sheet.",
            figure=figure, detail={"overrun_mm": round(over, 2),
                                   "at": [round(point[0], 2), round(point[1], 2)]}))
    return out


def _outside_by(point: Point, sight: BBox) -> float:
    dx = max(sight[0] - point[0], point[0] - sight[2], 0.0)
    dy = max(sight[1] - point[1], point[1] - sight[3], 0.0)
    return math.hypot(dx, dy)


def _check_text(sheet_number: int, data: dict[str, Any], numerals: set[str]) -> list[Finding]:
    out: list[Finding] = []
    legends: dict[str, list[str]] = {}
    for text in data["texts"]:
        body = (text.get("text") or "").strip()
        if not body:
            continue
        cap = text["size"] * geom.TEXT_CAP_RATIO
        role = text.get("role") or ""
        if cap < rules.MIN_CHARACTER_MM - 0.02:
            out.append(_finding(
                "numeral_too_small",
                f"on sheet {sheet_number}, {body!r} is {cap:.2f} mm high; the minimum is "
                f"{rules.MIN_CHARACTER_MM:.2f} mm (0.32 cm).",
                figure=text.get("figure", ""), numeral=body if role == "numeral" else "",
                detail={"height_mm": round(cap, 3)}))
        if role in ("numeral",):
            if body not in numerals:
                out.append(_finding(
                    "numeral_not_in_registry",
                    f"the character {body!r} on sheet {sheet_number} is not in the registry.",
                    stage="renderer", figure=text.get("figure", ""), numeral=body))
            continue
        if role in ("caption", "sheet_number"):
            if role == "caption" and not _FIG_LABEL.match(body):
                out.append(_finding(
                    "figure_label_malformed",
                    f"the caption {body!r} on sheet {sheet_number} is not of the form \"FIG. 1\".",
                    figure=text.get("figure", "")))
            if role == "sheet_number" and not _SHEET_NUMBER.match(body):
                out.append(_finding(
                    "sheet_number_missing",
                    f"the sheet number {body!r} is not of the form \"1/3\".", severity="warning"))
            continue
        upper = body.upper()
        if upper in _ALWAYS_ALLOWED or _SECTION_MARK.match(upper) or upper in numerals:
            continue
        legends.setdefault(text.get("figure", ""), []).append(body)

    for figure, items in sorted(legends.items()):
        unique = sorted(set(items))
        out.append(_finding(
            "legend_used", severity="info", stage="layout", figure=figure,
            message=(f"{figure or 'the sheet'} carries {len(unique)} descriptive legend(s), which "
                     "are permitted but subject to approval by the Office: "
                     + ", ".join(f'"{x}"' for x in unique[:8])
                     + ("..." if len(unique) > 8 else "")),
            detail={"legends": unique}))

    out += _check_text_collisions(sheet_number, data)
    out += _check_legend_overflow(sheet_number, data)
    return out


def _check_text_collisions(sheet_number: int, data: dict[str, Any]) -> list[Finding]:
    """Two characters on top of each other on the sheet.

    The solver keeps numerals apart in the figure's own coordinates. Reducing a figure to fit the
    sheet moves them closer together without shrinking them, so the check has to be made again
    where it matters, on the sheet.
    """
    marks = [t for t in data["texts"] if t.get("role") in ("numeral", "caption")]
    out: list[Finding] = []
    for i in range(len(marks)):
        for j in range(i + 1, len(marks)):
            a, b = marks[i], marks[j]
            if geom.bbox_intersection_area(a["bbox"], b["bbox"]) <= 0.05:
                continue
            out.append(_finding(
                "numerals_overlap",
                f"on sheet {sheet_number}, {a['text']!r} and {b['text']!r} overlap.",
                stage="layout", figure=a.get("figure", ""),
                numeral=f"{a['text']}, {b['text']}"))
    return out


def _check_legend_overflow(sheet_number: int, data: dict[str, Any]) -> list[Finding]:
    """A legend that no longer fits the shape it names.

    This is the price of keeping characters full size on a reduced view: the box shrinks and the
    words in it do not. It is worth saying out loud, because the alternative was lettering the
    box below 0.32 cm, which is a rule rather than a matter of taste.
    """
    owners: dict[tuple[str, str], BBox] = {}
    for record in data["lines"]:
        if not record["owner"] or record["role"] not in ("outline",):
            continue
        key = (record["figure"], record["owner"])
        box = geom.poly_bbox(record["points"])
        owners[key] = geom.bbox_union([owners[key], box]) if key in owners else box

    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for text in data["texts"]:
        if text.get("role") != "legend" or not text.get("owner"):
            continue
        key = (text.get("figure", ""), text["owner"])
        shape = owners.get(key)
        if shape is None or key in seen:
            continue
        box = text["bbox"]
        centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        inside_shape = (shape[0] <= centre[0] <= shape[2] and shape[1] <= centre[1] <= shape[3])
        if not inside_shape or geom.bbox_contains(geom.bbox_pad(shape, 0.3), box):
            continue
        seen.add(key)
        out.append(_finding(
            "legend_overflows", severity="warning", stage="layout",
            figure=text.get("figure", ""), numeral=text["owner"],
            message=(f"on sheet {sheet_number}, the legend {text['text']!r} is wider than the "
                     f"block it names. The view was reduced to fit and its characters were not, "
                     "because they may not go below 0.32 cm. Shorten the legend or split the "
                     "view.")))
    return out


def _check_strokes(sheet_number: int, data: dict[str, Any]) -> list[Finding]:
    widths = [record["width"] for record in data["lines"] if record["width"] > 0]
    if not widths:
        return []
    out: list[Finding] = []
    thinnest = min(widths)
    if thinnest < rules.MIN_STROKE_MM:
        out.append(_finding(
            "line_too_thin",
            f"on sheet {sheet_number} the thinnest line is {thinnest:.3f} mm, below the "
            f"{rules.MIN_STROKE_MM} mm this checker requires for a line that must stay dense and "
            "well-defined after the Office reduces the sheet.",
            detail={"thinnest_mm": round(thinnest, 4)}))
    distinct = sorted({round(w, 3) for w in widths})
    if len(distinct) > 5:
        out.append(_finding(
            "lines_not_uniform",
            f"sheet {sheet_number} uses {len(distinct)} different line weights.",
            severity="warning", detail={"weights": distinct[:12]}))
    return out


def _check_leaders(sheet_number: int, data: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    leaders = data["leaders"]
    owned: dict[tuple[str, str], list[list[Point]]] = {}
    for record in data["lines"]:
        if record["role"] in ("leader", "arrow") or not record["owner"]:
            continue
        owned.setdefault((record["figure"], record["owner"]), []).append(record["points"])

    for leader in leaders:
        points = leader["points"]
        if len(points) < 2:
            continue
        tip = points[-1]
        target = owned.get((leader["figure"], leader["numeral"]))
        if not target:
            out.append(_finding(
                "leader_missing",
                f"the lead line for {leader['numeral']} in {leader['figure']} points at nothing: "
                "no line on the sheet belongs to that part.",
                stage="renderer", figure=leader["figure"], numeral=leader["numeral"]))
            continue
        nearest = min(geom.dist_point_polyline(tip, poly) for poly in target)
        if nearest > rules.LEADER_TOUCH_MM:
            out.append(_finding(
                "leader_not_touching",
                f"the lead line for {leader['numeral']} in {leader['figure']} stops "
                f"{nearest:.2f} mm short of the part it indicates.",
                stage="placement", figure=leader["figure"], numeral=leader["numeral"],
                detail={"gap_mm": round(nearest, 3)}))

    for i in range(len(leaders)):
        for j in range(i + 1, len(leaders)):
            a, b = leaders[i], leaders[j]
            if _polylines_cross(a["points"], b["points"]):
                out.append(_finding(
                    "leaders_cross",
                    f"on sheet {sheet_number} the lead lines for {a['numeral']} "
                    f"({a['figure']}) and {b['numeral']} ({b['figure']}) cross.",
                    stage="placement", figure=a["figure"],
                    numeral=f"{a['numeral']}, {b['numeral']}"))
    return out


def _polylines_cross(a: Sequence[Point], b: Sequence[Point]) -> bool:
    for i in range(len(a) - 1):
        for j in range(len(b) - 1):
            if geom.segments_cross(a[i], a[i + 1], b[j], b[j + 1]):
                return True
    return False


def _check_placement(sheet_number: int, data: dict[str, Any], sight: BBox) -> list[Finding]:
    out: list[Finding] = []
    placed = data.get("placed") or []
    for i in range(len(placed)):
        for j in range(i + 1, len(placed)):
            a, b = placed[i], placed[j]
            box_a = (a["x"], a["y"], a["x"] + a["width"], a["y"] + a["height"])
            box_b = (b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"])
            gap = _box_gap(box_a, box_b)
            if gap < rules.MIN_FIGURE_GAP_MM:
                out.append(_finding(
                    "figures_crowded",
                    f"on sheet {sheet_number}, {a['label']} and {b['label']} are {gap:.1f} mm "
                    f"apart; views need at least {rules.MIN_FIGURE_GAP_MM:.0f} mm between them.",
                    severity="warning", figure=a["label"], detail={"gap_mm": round(gap, 2)}))
        box = (placed[i]["x"], placed[i]["y"], placed[i]["x"] + placed[i]["width"],
               placed[i]["y"] + placed[i]["height"])
        if not geom.bbox_contains(geom.bbox_pad(sight, 0.05), box):
            out.append(_finding(
                "figure_overruns_sheet",
                f"{placed[i]['label']} does not fit within the sight of sheet {sheet_number} "
                "even at the smallest scale that keeps its reference characters legible. Split "
                "the view.",
                figure=placed[i]["label"]))
    return out


def _box_gap(a: BBox, b: BBox) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return math.hypot(dx, dy)


# ------------------------------------------------------------------------------ raster layer


def rasterise(svg: str, dpi: float = DPI):
    """The sheet as a PIL image, or (None, reason)."""
    try:
        import cairosvg
    except Exception as exc:                     # pragma: no cover - deployment dependent
        return None, f"cairosvg is not installed: {exc}"
    try:
        from PIL import Image
    except Exception as exc:                     # pragma: no cover
        return None, f"Pillow is not installed: {exc}"
    try:
        png = cairosvg.svg2png(bytestring=svg.encode("utf-8"), dpi=dpi,
                               background_color="white")
        return Image.open(io.BytesIO(png)).convert("RGB"), ""
    except Exception as exc:
        return None, f"the sheet could not be rasterised: {type(exc).__name__}: {exc}"


def check_raster(sheet_number: int, svg: str, paper_mm: tuple[float, float],
                 sight: BBox, dpi: float = DPI) -> list[Finding]:
    image, reason = rasterise(svg, dpi)
    if image is None:
        return [_finding(
            "raster_check_skipped", severity="warning", stage="layout",
            message=(f"the pixel checks did not run on sheet {sheet_number}: {reason}. Colour, "
                     "line density and stray ink in the margin were NOT verified."))]

    out: list[Finding] = []
    width, height = image.size
    pixels = image.load()
    px_per_mm = width / max(paper_mm[0], 1e-6)

    coloured = 0
    ink = 0
    first_colour: Optional[tuple[int, int]] = None
    margin_ink = 0
    first_margin: Optional[tuple[float, float]] = None

    # A whole-sheet scan at 300 dpi is nine million pixels; stepping by two costs nothing in
    # sensitivity for a check about colour and stray ink, and turns seconds into fractions.
    step = 2
    for y in range(0, height, step):
        for x in range(0, width, step):
            r, g, b = pixels[x, y]
            if r > 246 and g > 246 and b > 246:
                continue
            ink += 1
            if abs(r - g) > 12 or abs(g - b) > 12 or abs(r - b) > 12:
                coloured += 1
                if first_colour is None:
                    first_colour = (x, y)
            mx, my = x / px_per_mm, y / px_per_mm
            if _outside_by((mx, my), sight) > 0.3:
                margin_ink += 1
                if first_margin is None:
                    first_margin = (round(mx, 1), round(my, 1))

    if coloured:
        out.append(_finding(
            "not_black_and_white",
            f"sheet {sheet_number} has {coloured * step * step} coloured pixels; the drawing must "
            "be black on white.",
            detail={"first_at_px": list(first_colour or ())}))
    if margin_ink:
        out.append(_finding(
            "outside_margins",
            f"sheet {sheet_number} has ink in the margin, first seen at "
            f"{first_margin[0]} mm, {first_margin[1]} mm from the top left corner."
            if first_margin else f"sheet {sheet_number} has ink in the margin.",
            detail={"pixels": margin_ink * step * step}))

    out += _check_grey(image, sheet_number)
    out += _check_hairlines(image, sheet_number, px_per_mm)
    return out


def _check_grey(image, sheet_number: int) -> list[Finding]:
    """Grey that is shading, not the soft edge of a black line.

    Every antialiased line has grey along both edges, so counting grey pixels finds a defect in
    every drawing ever rendered. What is actually forbidden is grey used as tone: 37 CFR 1.84(m)
    allows shading only by line. So the grey next to black is discounted, and only grey that
    stands on its own is reported.
    """
    try:
        from PIL import ImageChops, ImageFilter
    except Exception:                            # pragma: no cover
        return []
    grey = image.convert("L")
    solid = grey.point(lambda v: 255 if v < 70 else 0, mode="L")
    midtone = grey.point(lambda v: 255 if 70 <= v < 215 else 0, mode="L")
    # Spreading the solid mask by two pixels covers the antialiased skirt of any real line.
    near_solid = solid.filter(ImageFilter.MaxFilter(5))
    orphan = ImageChops.subtract(midtone, near_solid)
    orphan_count = sum(orphan.histogram()[128:])
    total = sum(midtone.histogram()[128:]) + sum(solid.histogram()[128:])
    if not total:
        return []
    fraction = orphan_count / total
    if fraction <= rules.MAX_GREY_FRACTION:
        return []
    return [_finding(
        "shading_present", severity="warning",
        message=(f"{fraction * 100:.0f}% of the ink on sheet {sheet_number} is mid-grey and not "
                 "next to any black line, which reads as tonal shading rather than line work."),
        detail={"orphan_grey_fraction": round(fraction, 3)})]


def _check_hairlines(image, sheet_number: int, px_per_mm: float) -> list[Finding]:
    """Ink that survives no erosion at all is a hairline, however thick the vector said it was."""
    try:
        from PIL import ImageFilter
    except Exception:                            # pragma: no cover
        return []
    binary = image.convert("L").point(lambda v: 0 if v < 200 else 255, mode="L")
    before = _ink_count(binary)
    if not before:
        return [_finding("no_figures", f"sheet {sheet_number} rasterised to a blank page.",
                         stage="renderer")]
    # MaxFilter on a white-on-black-ink image erodes the ink by one pixel each way.
    eroded = binary.filter(ImageFilter.MaxFilter(3))
    after = _ink_count(eroded)
    minimum = rules.MIN_STROKE_MM * px_per_mm
    if before and after / before < 0.25 and minimum > 1.2:
        return [_finding(
            "line_too_thin", severity="warning",
            message=(f"on sheet {sheet_number}, {100 - after * 100 // before}% of the ink is one "
                     "pixel wide at 300 dpi and would not survive the Office's reduction."),
            detail={"kept_fraction": round(after / before, 3)})]
    return []


def _ink_count(image) -> int:
    histogram = image.histogram()
    return sum(histogram[:128])
