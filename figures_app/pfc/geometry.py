"""Geometry shared by the layout engine and the geometry validators.

Deliberately one module. If the router decided a leader clears a box using one intersection
test and the validator decided it does not using another, the correction loop would chase a
fault that only exists in the disagreement between them. Both call these functions.

Everything is in scene units and everything is exact: no tolerance is applied here, because the
tolerance a caller wants depends on what it is measuring. A crossing test wants an epsilon that
ignores a shared endpoint; a margin test wants none at all.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

from .schemas import Box, Point

EPSILON = 1e-7

Seg = tuple[tuple[float, float], tuple[float, float]]


def as_tuple(point: Point) -> tuple[float, float]:
    return (point.x, point.y)


def segments(points: Sequence[Point]) -> list[Seg]:
    return [(as_tuple(points[i]), as_tuple(points[i + 1])) for i in range(len(points) - 1)]


def _orientation(p: tuple[float, float], q: tuple[float, float],
                 r: tuple[float, float]) -> float:
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def segments_cross(a: Seg, b: Seg) -> bool:
    """A proper interior crossing only.

    Two lines that meet at a shared endpoint, or that touch, are not a crossing: a drawing whose
    connections meet at a node would otherwise report a crossing at every junction and the
    correction loop would never converge.
    """
    o1 = _orientation(a[0], a[1], b[0])
    o2 = _orientation(a[0], a[1], b[1])
    o3 = _orientation(b[0], b[1], a[0])
    o4 = _orientation(b[0], b[1], a[1])
    if any(abs(value) <= EPSILON for value in (o1, o2, o3, o4)):
        return False
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def polyline_crossings(left: Sequence[Point], right: Sequence[Point]) -> int:
    return sum(1 for a in segments(left) for b in segments(right) if segments_cross(a, b))


def box_segments(box: Box) -> list[Seg]:
    corners = [(box.x, box.y), (box.right, box.y), (box.right, box.bottom), (box.x, box.bottom)]
    return [(corners[i], corners[(i + 1) % 4]) for i in range(4)]


def segment_hits_box(seg: Seg, box: Box) -> bool:
    """True when a segment crosses a box edge or lies inside it."""
    if point_in_box(seg[0], box) or point_in_box(seg[1], box):
        return True
    return any(segments_cross(seg, edge) for edge in box_segments(box))


def point_in_box(point: tuple[float, float], box: Box) -> bool:
    return box.x <= point[0] <= box.right and box.y <= point[1] <= box.bottom


def polyline_hits_box(points: Sequence[Point], box: Box) -> bool:
    return any(segment_hits_box(seg, box) for seg in segments(points))


def boxes_overlap(first: Box, second: Box, gap: float = 0.0) -> bool:
    return first.overlaps(second, gap)


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polyline_length(points: Sequence[Point]) -> float:
    return sum(distance(a, b) for a, b in segments(points))


def boundary_point(box: Box, toward: tuple[float, float]) -> tuple[float, float]:
    """Where a line from the centre of ``box`` toward a point leaves the box.

    Used for both connection endpoints and leader endpoints, so a line always stops on the
    outline of the thing it refers to rather than in the middle of it.
    """
    cx, cy = box.cx, box.cy
    dx, dy = toward[0] - cx, toward[1] - cy
    if abs(dx) < EPSILON and abs(dy) < EPSILON:
        return (box.right, cy)
    half_w, half_h = box.width / 2, box.height / 2
    scale_x = half_w / abs(dx) if abs(dx) > EPSILON else math.inf
    scale_y = half_h / abs(dy) if abs(dy) > EPSILON else math.inf
    scale = min(scale_x, scale_y)
    return (cx + dx * scale, cy + dy * scale)


def ellipse_boundary_point(box: Box, toward: tuple[float, float]) -> tuple[float, float]:
    cx, cy = box.cx, box.cy
    dx, dy = toward[0] - cx, toward[1] - cy
    if abs(dx) < EPSILON and abs(dy) < EPSILON:
        return (box.right, cy)
    rx, ry = box.width / 2, box.height / 2
    scale = 1.0 / math.sqrt((dx / rx) ** 2 + (dy / ry) ** 2)
    return (cx + dx * scale, cy + dy * scale)


def contains(outer: Box, inner: Box, pad: float = 0.0) -> bool:
    return (outer.x + pad <= inner.x and outer.y + pad <= inner.y and
            inner.right <= outer.right - pad and inner.bottom <= outer.bottom - pad)


def union(boxes: Iterable[Box]) -> Optional[Box]:
    items = list(boxes)
    if not items:
        return None
    left = min(box.x for box in items)
    top = min(box.y for box in items)
    right = max(box.right for box in items)
    bottom = max(box.bottom for box in items)
    return Box(x=left, y=top, width=max(1.0, right - left), height=max(1.0, bottom - top))


def orthogonal_route(start: tuple[float, float], end: tuple[float, float],
                     prefer: str = "horizontal") -> list[Point]:
    """A two-segment right-angled path between two points.

    Orthogonal routing is the patent-drawing convention for block and data-flow figures, and it
    also makes crossings countable: two axis-aligned segments either cross or they do not.
    """
    if abs(start[1] - end[1]) < EPSILON or abs(start[0] - end[0]) < EPSILON:
        return [Point(x=start[0], y=start[1]), Point(x=end[0], y=end[1])]
    if prefer == "horizontal":
        elbow = (end[0], start[1])
    else:
        elbow = (start[0], end[1])
    return [Point(x=start[0], y=start[1]), Point(x=elbow[0], y=elbow[1]),
            Point(x=end[0], y=end[1])]


def clamp_box(box: Box, area: Box) -> Box:
    """Slide a box back inside the drawing area without resizing it."""
    x = min(max(box.x, area.x), max(area.x, area.right - box.width))
    y = min(max(box.y, area.y), max(area.y, area.bottom - box.height))
    return Box(x=x, y=y, width=box.width, height=box.height)
