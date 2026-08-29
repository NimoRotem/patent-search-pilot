"""Plane geometry, in millimetres, with y increasing downwards.

Everything downstream of a renderer is a polyline, because a polyline is the one shape you can
ask every question of: does this lead line cross that one, is this numeral sitting on top of ink,
does the drawing fit inside the sight of the sheet. Curves are kept as curves for the SVG and
flattened here for the arithmetic, so the picture stays smooth while the checks stay exact.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

Point = tuple[float, float]
Poly = list[Point]
BBox = tuple[float, float, float, float]  # x0, y0, x1, y1

EPS = 1e-9


# --------------------------------------------------------------------------------------------
# bounding boxes


def poly_bbox(points: Sequence[Point]) -> BBox:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_union(boxes: Iterable[BBox]) -> Optional[BBox]:
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def bbox_pad(box: BBox, pad: float) -> BBox:
    return (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)


def bbox_overlap(a: BBox, b: BBox) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def bbox_area(box: BBox) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def bbox_intersection_area(a: BBox, b: BBox) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h


def bbox_poly(box: BBox) -> Poly:
    x0, y0, x1, y1 = box
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def bbox_contains(outer: BBox, inner: BBox) -> bool:
    return (outer[0] <= inner[0] + EPS and outer[1] <= inner[1] + EPS
            and outer[2] >= inner[2] - EPS and outer[3] >= inner[3] - EPS)


# --------------------------------------------------------------------------------------------
# segments


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Point, b: Point, c: Point) -> bool:
    return (min(a[0], b[0]) - EPS <= c[0] <= max(a[0], b[0]) + EPS
            and min(a[1], b[1]) - EPS <= c[1] <= max(a[1], b[1]) + EPS)


def segments_cross(a: Point, b: Point, c: Point, d: Point, *, touching_counts: bool = False) -> bool:
    """True when segment ab meets segment cd.

    Lead lines are allowed to share an endpoint with the thing they point at, so by default a
    contact at an endpoint is not a crossing; only a genuine X counts. 37 CFR 1.84(q) forbids the
    X, not the touch.
    """
    d1, d2 = _orient(c, d, a), _orient(c, d, b)
    d3, d4 = _orient(a, b, c), _orient(a, b, d)
    if ((d1 > EPS and d2 < -EPS) or (d1 < -EPS and d2 > EPS)) and \
       ((d3 > EPS and d4 < -EPS) or (d3 < -EPS and d4 > EPS)):
        return True
    if not touching_counts:
        return False
    for p, q, r in ((c, d, a), (c, d, b), (a, b, c), (a, b, d)):
        if abs(_orient(p, q, r)) <= EPS and _on_segment(p, q, r):
            return True
    return False


def segment_intersection(a: Point, b: Point, c: Point, d: Point) -> Optional[Point]:
    """The crossing point of two segments, or None."""
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    denom = r[0] * s[1] - r[1] * s[0]
    if abs(denom) < EPS:
        return None
    t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / denom
    u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / denom
    if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS:
        return (a[0] + t * r[0], a[1] + t * r[1])
    return None


def dist_point_segment(p: Point, a: Point, b: Point) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    wx, wy = p[0] - a[0], p[1] - a[1]
    denom = vx * vx + vy * vy
    if denom < EPS:
        return math.hypot(wx, wy)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    return math.hypot(wx - t * vx, wy - t * vy)


def dist_point_polyline(p: Point, poly: Sequence[Point]) -> float:
    if len(poly) == 1:
        return math.hypot(p[0] - poly[0][0], p[1] - poly[0][1])
    return min(dist_point_segment(p, poly[i], poly[i + 1]) for i in range(len(poly) - 1))


def polyline_length(poly: Sequence[Point]) -> float:
    return sum(math.dist(poly[i], poly[i + 1]) for i in range(len(poly) - 1))


def point_in_polygon(p: Point, poly: Sequence[Point]) -> bool:
    """Even-odd rule. The polygon is treated as closed whether or not it repeats its first point."""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > p[1]) != (yj > p[1]):
            x_at = (xj - xi) * (p[1] - yi) / ((yj - yi) or EPS) + xi
            if p[0] < x_at:
                inside = not inside
        j = i
    return inside


def segment_crosses_polyline(a: Point, b: Point, poly: Sequence[Point],
                             *, closed: bool = False) -> bool:
    n = len(poly)
    if n < 2:
        return False
    last = n if closed else n - 1
    for i in range(last):
        c, d = poly[i], poly[(i + 1) % n]
        if segments_cross(a, b, c, d):
            return True
    return False


def segment_bbox_clip(a: Point, b: Point, box: BBox) -> Optional[tuple[float, float]]:
    """Liang-Barsky. Returns the parameter interval of ab inside box, or None."""
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - box[0]), (dx, box[2] - a[0]),
                 (-dy, a[1] - box[1]), (dy, box[3] - a[1])):
        if abs(p) < EPS:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return (t0, t1) if t1 >= t0 else None


# --------------------------------------------------------------------------------------------
# shape builders


def rect_poly(x: float, y: float, w: float, h: float) -> Poly:
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def rounded_rect_poly(x: float, y: float, w: float, h: float, r: float, steps: int = 6) -> Poly:
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= EPS:
        return rect_poly(x, y, w, h)
    pts: Poly = []
    corners = ((x + w - r, y + h - r, 0.0), (x + r, y + h - r, 90.0),
               (x + r, y + r, 180.0), (x + w - r, y + r, 270.0))
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = math.radians(start + 90.0 * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def circle_poly(cx: float, cy: float, r: float, steps: int = 48) -> Poly:
    return [(cx + r * math.cos(2 * math.pi * i / steps),
             cy + r * math.sin(2 * math.pi * i / steps)) for i in range(steps)]


def ellipse_poly(cx: float, cy: float, rx: float, ry: float, steps: int = 48) -> Poly:
    return [(cx + rx * math.cos(2 * math.pi * i / steps),
             cy + ry * math.sin(2 * math.pi * i / steps)) for i in range(steps)]


def arc_poly(cx: float, cy: float, r: float, a0: float, a1: float, steps: int = 24) -> Poly:
    """a0 and a1 in degrees, measured clockwise from east because y points down."""
    out: Poly = []
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def translate(points: Sequence[Point], dx: float, dy: float) -> Poly:
    return [(p[0] + dx, p[1] + dy) for p in points]


def scale_points(points: Sequence[Point], factor: float,
                 origin: Point = (0.0, 0.0)) -> Poly:
    return [(origin[0] + (p[0] - origin[0]) * factor,
             origin[1] + (p[1] - origin[1]) * factor) for p in points]


def unit(dx: float, dy: float) -> Point:
    length = math.hypot(dx, dy)
    if length < EPS:
        return (1.0, 0.0)
    return (dx / length, dy / length)


def arrow_head(tip: Point, from_point: Point, length: float = 2.4,
               half_angle: float = 14.0) -> Poly:
    """A closed triangular head at ``tip`` pointing away from ``from_point``."""
    ux, uy = unit(tip[0] - from_point[0], tip[1] - from_point[1])
    base = (tip[0] - ux * length, tip[1] - uy * length)
    spread = length * math.tan(math.radians(half_angle))
    return [tip, (base[0] - uy * spread, base[1] + ux * spread),
            (base[0] + uy * spread, base[1] - ux * spread)]


# --------------------------------------------------------------------------------------------
# hatching, 37 CFR 1.84(h)(3)


def hatch_polygon(loop: Sequence[Point], spacing: float = 1.4, angle_deg: float = 45.0,
                  holes: Sequence[Sequence[Point]] = ()) -> list[tuple[Point, Point]]:
    """Regularly spaced oblique parallel lines filling ``loop`` minus ``holes``.

    Section hatching has to be spaced far enough apart that the lines stay separate when the
    sheet is reduced, and it must not run under a reference character, which is why it comes back
    as segments the caller can still cut rather than as a fill.
    """
    if len(loop) < 3:
        return []
    box = poly_bbox(loop)
    theta = math.radians(angle_deg)
    ct, st = math.cos(theta), math.sin(theta)
    corners = bbox_poly(box)
    projected = [(-p[0] * st + p[1] * ct) for p in corners]
    lo, hi = min(projected), max(projected)
    # The family of hatch lines is anchored on the origin, so each line is built around the point
    # on it nearest the origin. Half its length therefore has to reach the far corner of the
    # SHAPE from there, not merely span the shape's own diagonal: a part sitting 200 mm from the
    # origin would otherwise be hatched with a set of segments 200 mm away from it, which comes
    # back as a section view with no hatching at all and no error.
    span = max(math.hypot(p[0], p[1]) for p in corners) + spacing
    out: list[tuple[Point, Point]] = []
    # Anchor the family of lines on the origin, not on the shape, so two parts cut by the same
    # plane get hatching that lines up instead of drifting a fraction of a space apart.
    k0 = math.floor(lo / spacing)
    k1 = math.ceil(hi / spacing)
    for k in range(k0, k1 + 1):
        offset = k * spacing
        # The line through (px, py) with direction (ct, st) is the one whose projection on the
        # hatch normal is exactly `offset`.
        px, py = -st * offset, ct * offset
        a = (px + ct * (-span), py + st * (-span))
        b = (px + ct * span, py + st * span)
        for seg in _clip_segment_to_ring(a, b, loop, holes):
            out.append(seg)
    return out


def _crossings(a: Point, b: Point, ring: Sequence[Point]) -> list[float]:
    """Parameters along ab where it crosses the closed ring."""
    ts: list[float] = []
    n = len(ring)
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom_len = math.hypot(dx, dy)
    if denom_len < EPS:
        return ts
    for i in range(n):
        c, d = ring[i], ring[(i + 1) % n]
        pt = segment_intersection(a, b, c, d)
        if pt is None:
            continue
        t = ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / (denom_len * denom_len)
        ts.append(t)
    return ts


def _clip_segment_to_ring(a: Point, b: Point, loop: Sequence[Point],
                          holes: Sequence[Sequence[Point]]) -> list[tuple[Point, Point]]:
    ts = [0.0, 1.0] + _crossings(a, b, loop)
    for hole in holes:
        ts += _crossings(a, b, hole)
    ts = sorted(t for t in ts if -EPS <= t <= 1 + EPS)
    out: list[tuple[Point, Point]] = []
    for i in range(len(ts) - 1):
        t0, t1 = ts[i], ts[i + 1]
        if t1 - t0 < 1e-6:
            continue
        tm = (t0 + t1) / 2.0
        mid = (a[0] + (b[0] - a[0]) * tm, a[1] + (b[1] - a[1]) * tm)
        if not point_in_polygon(mid, loop):
            continue
        if any(point_in_polygon(mid, hole) for hole in holes):
            continue
        out.append(((a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                    (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)))
    return out


# --------------------------------------------------------------------------------------------
# text metrics
#
# Reference characters and figure captions are drawn in one monospaced-ish sans face at a known
# size, so a width estimate from the glyph count is accurate to within a fraction of a
# millimetre. That is enough to keep a numeral off a line, and it means the placement solver
# needs no font machinery at all.

TEXT_ASPECT = 0.60          # advance width of a digit, as a fraction of the font size
TEXT_CAP_RATIO = 0.72       # cap height, as a fraction of the font size


def text_extent(text: str, size_mm: float) -> tuple[float, float]:
    """Width and cap height in millimetres for a string drawn at ``size_mm``."""
    return (max(1, len(text)) * size_mm * TEXT_ASPECT, size_mm * TEXT_CAP_RATIO)


def text_bbox(text: str, size_mm: float, x: float, y: float,
              anchor: str = "middle", baseline: str = "middle") -> BBox:
    w, h = text_extent(text, size_mm)
    if anchor == "middle":
        x0 = x - w / 2.0
    elif anchor == "end":
        x0 = x - w
    else:
        x0 = x
    if baseline == "middle":
        y0 = y - h / 2.0
    elif baseline == "hanging":
        y0 = y
    else:
        y0 = y - h
    return (x0, y0, x0 + w, y0 + h)
