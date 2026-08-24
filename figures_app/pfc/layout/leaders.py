"""Reference numerals and their leader lines.

A reference numeral is the only thing in a patent drawing that is legally load-bearing on its
own: it is what the description points at. So a leader has to end on the object it names, on
that object's outline and not in the whitespace beside it, and it must not be possible to read
it as pointing at the neighbour.

Placement is a scored search rather than a rule of thumb. Each numeral gets a set of candidate
positions around its object, each candidate gets a straight, one-bend and two-bend route, and
every candidate is costed against everything already on the sheet: the other objects, the other
leaders, the connection lines, the other numerals, and the sheet margins. The cheapest wins.

The costs are ordered so that correctness beats tidiness. Landing outside the drawing area or
pointing at the wrong object is effectively infinite; crossing another leader is expensive;
being a long way from the object is merely a nuisance.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from ..geometry import (boundary_point, distance, ellipse_boundary_point, polyline_hits_box,
                        segments, segments_cross)
from ..numerals import sort_key
from ..profiles import DrawingProfile
from ..schemas import Box, LayoutEdge, LayoutLabel, LayoutNode, LayoutScene, Point

# Cost weights. Their ratios are the policy; their absolute values are arbitrary.
COST_OUTSIDE = 10_000.0
COST_AMBIGUOUS = 4_000.0
# A numeral printed ON generated artwork is unreadable in a way a numeral beside a schematic box
# is not: the artwork has lines everywhere. On a raster-backed sheet the numerals belong in the
# white margin around the drawing, with the leader crossing into it. That is also the convention
# a draughtsman uses.
COST_ON_ARTWORK = 2_500.0
COST_HITS_NODE = 1_200.0
COST_LABEL_OVERLAP = 900.0
COST_LEADER_CROSS = 400.0
COST_EDGE_CROSS = 120.0
COST_BEND = 18.0
COST_DISTANCE = 1.0
# Two numerals that do not overlap but sit a hair apart cost the same, under the rule above, as
# two at opposite corners, so the search has no reason to prefer the readable one. On
# US-2024/0246200-A1 that put 350 and 314 shoulder to shoulder and an independent reader read
# 350 as 390. A soft term, weighted well under every hard constraint, spreads them where there is
# room and yields immediately where there is not: a tie-breaker, never a veto.
COST_CROWDING = 60.0
# The separation, in numeral heights, at which crowding stops being charged for at all.
CROWDING_REACH = 2.2

# Eight directions, near then far. Near is preferred by the distance term; far exists so a
# crowded corner of the sheet still has somewhere to put a numeral.
_DIRECTIONS = [(1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1)]
_REACH = (1.6, 2.8, 4.2)

# A leader endpoint is ambiguous when another object's outline is nearly as close to it as its
# own. Expressed as a ratio so it scales with the drawing.
AMBIGUITY_RATIO = 1.35


def _anchor(node: LayoutNode, toward: tuple[float, float]) -> tuple[float, float]:
    if node.shape in {"circle", "ellipse", "cylinder"}:
        return ellipse_boundary_point(node.box, toward)
    return boundary_point(node.box, toward)


def _leader_origin(profile: DrawingProfile, numeral: str, label_point: tuple[float, float],
                   target: tuple[float, float]) -> tuple[float, float]:
    """Where the leader leaves the numeral: the side of the text facing the object.

    Starting it at the text's anchor point puts the line through the digits whenever the numeral
    sits to the left of what it names, which is half the labels on a sheet. It leaves from the
    edge of the text, slightly above the baseline, the way a draughtsman draws it.
    """
    width = profile.text_width(numeral)
    lift = profile.reference_height * 0.3
    if target[0] >= label_point[0]:
        return (label_point[0] + width + profile.min_label_gap * 0.5, label_point[1] - lift)
    return (label_point[0] - profile.min_label_gap * 0.5, label_point[1] - lift)


def _routes(origin: tuple[float, float], target: tuple[float, float]
            ) -> list[list[Point]]:
    """Straight, one-bend and two-bend candidates between a numeral and its anchor."""
    straight = [Point(x=origin[0], y=origin[1]), Point(x=target[0], y=target[1])]
    mid_x = (origin[0] + target[0]) / 2
    one_bend_h = [Point(x=origin[0], y=origin[1]),
                  Point(x=target[0], y=origin[1]),
                  Point(x=target[0], y=target[1])]
    one_bend_v = [Point(x=origin[0], y=origin[1]),
                  Point(x=origin[0], y=target[1]),
                  Point(x=target[0], y=target[1])]
    two_bend = [Point(x=origin[0], y=origin[1]),
                Point(x=mid_x, y=origin[1]),
                Point(x=mid_x, y=target[1]),
                Point(x=target[0], y=target[1])]
    return [straight, one_bend_h, one_bend_v, two_bend]


def _label_box(profile: DrawingProfile, numeral: str, x: float, y: float) -> Box:
    width = profile.text_width(numeral)
    height = profile.reference_height
    # The anchor point is the text baseline's left end, which is how the renderer places it.
    return Box(x=x - profile.min_label_gap / 2,
               y=y - height - profile.min_label_gap / 2,
               width=width + profile.min_label_gap,
               height=height + profile.min_label_gap)


def _ambiguous(target: tuple[float, float], owner: LayoutNode,
               nodes: Iterable[LayoutNode], clearance: float) -> bool:
    """True when a reader could not tell which object the leader ends on.

    The endpoint sits on its own object's outline by construction, so the test is not "is it
    nearest its own object" — it always is. The test is whether some OTHER object's outline is
    close enough to the same point to be a plausible alternative reading.

    Containers are excluded: a part's numeral necessarily ends inside its housing's outline, and
    treating that as ambiguous would make every nested figure unbuildable.
    """
    for node in nodes:
        if node.entity_id == owner.entity_id or node.is_container:
            continue
        if distance(target, _anchor(node, target)) < clearance * AMBIGUITY_RATIO:
            return True
    return False


def _cost(profile: DrawingProfile, node: LayoutNode, label_point: tuple[float, float],
          route: list[Point], target: tuple[float, float], nodes: list[LayoutNode],
          edges: list[LayoutEdge], placed: list[LayoutLabel], area: Box,
          numeral: str, artwork: Optional[Box] = None) -> float:
    box = _label_box(profile, numeral, label_point[0], label_point[1])
    if not (area.x <= box.x and area.y <= box.y and box.right <= area.right
            and box.bottom <= area.bottom):
        return COST_OUTSIDE
    if not (area.x <= target[0] <= area.right and area.y <= target[1] <= area.bottom):
        return COST_OUTSIDE

    cost = COST_DISTANCE * distance(label_point, target)
    cost += COST_BEND * (len(route) - 2)
    if artwork is not None and box.overlaps(artwork):
        cost += COST_ON_ARTWORK

    # A numeral printed ON the outline of the thing it names is as hard to read as one printed
    # over its neighbour, and the leader is what does the naming. The owner is NOT exempt from
    # this: exempting it let 112 sit on its own box edge with the leader stubbed into a corner.
    if box.overlaps(node.box.inflated(profile.min_label_gap)):
        cost += COST_HITS_NODE

    for other in nodes:
        if other.entity_id == node.entity_id:
            continue
        if box.overlaps(other.box.inflated(profile.min_label_gap)) and not other.is_container:
            cost += COST_HITS_NODE
        # The leader may leave its own object and may pass through a container it sits in; it
        # may not cut across an unrelated object.
        if other.is_container:
            continue
        if polyline_hits_box(route, other.box.inflated(profile.leader_clearance)):
            cost += COST_HITS_NODE

    for label in placed:
        if box.overlaps(label.box.inflated(profile.min_label_gap)):
            cost += COST_LABEL_OVERLAP
        else:
            gap = distance((box.cx, box.cy), (label.box.cx, label.box.cy))
            reach = profile.reference_height * CROWDING_REACH
            if gap < reach:
                cost += COST_CROWDING * (1.0 - gap / reach)
        for a in segments(route):
            for b in segments(label.leader_points):
                if segments_cross(a, b):
                    cost += COST_LEADER_CROSS

    for edge in edges:
        # A connection line running THROUGH the numeral is as bad as two numerals on top of
        # each other, and is a separate fault from a leader merely crossing a connection.
        if polyline_hits_box(edge.points, box):
            cost += COST_LABEL_OVERLAP
        for a in segments(route):
            for b in segments(edge.points):
                if segments_cross(a, b):
                    cost += COST_EDGE_CROSS

    if _ambiguous(target, node, nodes, profile.leader_clearance):
        cost += COST_AMBIGUOUS
    return cost


def place_labels(scene: LayoutScene, profile: DrawingProfile, *, seed: int = 0) -> LayoutScene:
    """Attach exactly one numeral to each object that has one.

    Objects are labelled outermost first, so a housing claims its position before the parts
    inside it compete for the space around it, and in numeral order so the same scene always
    produces the same sheet.
    """
    labelled = [node for node in scene.nodes if node.reference_numeral]
    labelled.sort(key=lambda node: (node.depth, sort_key(node.reference_numeral or "")))
    placed: list[LayoutLabel] = []
    area = scene.drawing_area
    artwork = scene.artwork_box if scene.artwork else None
    if artwork is not None:
        # Reach far enough to clear the artwork, whatever size it came out.
        reaches = _REACH + (max(artwork.width, artwork.height) / max(
            1.0, profile.reference_height) * 0.55,)
    else:
        reaches = _REACH

    for offset, node in enumerate(labelled):
        numeral = node.reference_numeral or ""
        best: Optional[tuple[float, tuple[float, float], list[Point]]] = None
        directions = _DIRECTIONS[(seed + offset) % len(_DIRECTIONS):] + \
            _DIRECTIONS[:(seed + offset) % len(_DIRECTIONS)]
        for reach in reaches:
            for dx, dy in directions:
                norm = math.hypot(dx, dy) or 1.0
                span = max(node.box.width, node.box.height) / 2 + profile.reference_height * reach
                label_point = (node.box.cx + dx / norm * span,
                               node.box.cy + dy / norm * span)
                if dx < 0:
                    # A numeral to the LEFT of its object is anchored so its text ENDS at the
                    # offset. Anchoring it at the start ran the digits into the outline.
                    label_point = (label_point[0] - profile.text_width(numeral),
                                   label_point[1])
                target = _anchor(node, label_point)
                origin = _leader_origin(profile, numeral, label_point, target)
                for route in _routes(origin, target):
                    cost = _cost(profile, node, label_point, route, target, scene.nodes,
                                 scene.edges, placed, area, numeral, artwork)
                    if best is None or cost < best[0]:
                        best = (cost, label_point, route)
            if best is not None and best[0] < COST_HITS_NODE:
                break
        if best is None:
            continue
        _, label_point, route = best
        placed.append(LayoutLabel(
            reference_numeral=numeral, entity_id=node.entity_id,
            position=Point(x=round(label_point[0], 2), y=round(label_point[1], 2)),
            leader_points=[Point(x=round(p.x, 2), y=round(p.y, 2)) for p in route],
            text_width=profile.text_width(numeral),
            text_height=profile.reference_height))

    scene.labels = placed
    return scene


def relocate(scene: LayoutScene, profile: DrawingProfile, numerals: Iterable[str]) -> LayoutScene:
    """Re-place named numerals only, leaving every other label byte-for-byte alone.

    This is the local repair the correction loop uses for an overlap or a crossing. Re-running
    the whole placement would move labels that were already correct, which turns one reported
    defect into an unpredictable diff.
    """
    wanted = {str(numeral) for numeral in numerals}
    if not wanted:
        return scene
    keep = [label for label in scene.labels if label.reference_numeral not in wanted]
    moving = [label for label in scene.labels if label.reference_numeral in wanted]
    by_entity = {node.entity_id: node for node in scene.nodes}
    area = scene.drawing_area
    artwork = scene.artwork_box if scene.artwork else None
    reaches = _REACH if artwork is None else _REACH + (
        max(artwork.width, artwork.height) / max(1.0, profile.reference_height) * 0.55,)

    for label in sorted(moving, key=lambda item: sort_key(item.reference_numeral)):
        node = by_entity.get(label.entity_id)
        if node is None:
            continue
        best: Optional[tuple[float, tuple[float, float], list[Point]]] = None
        for reach in reaches:
            for dx, dy in _DIRECTIONS:
                norm = math.hypot(dx, dy) or 1.0
                span = max(node.box.width, node.box.height) / 2 + profile.reference_height * reach
                label_point = (node.box.cx + dx / norm * span, node.box.cy + dy / norm * span)
                if dx < 0:
                    label_point = (label_point[0] - profile.text_width(label.reference_numeral),
                                   label_point[1])
                target = _anchor(node, label_point)
                origin = _leader_origin(profile, label.reference_numeral, label_point, target)
                for route in _routes(origin, target):
                    cost = _cost(profile, node, label_point, route, target, scene.nodes,
                                 scene.edges, keep, area, label.reference_numeral, artwork)
                    if best is None or cost < best[0]:
                        best = (cost, label_point, route)
        if best is None:
            keep.append(label)
            continue
        _, label_point, route = best
        keep.append(LayoutLabel(
            reference_numeral=label.reference_numeral, entity_id=label.entity_id,
            position=Point(x=round(label_point[0], 2), y=round(label_point[1], 2)),
            leader_points=[Point(x=round(p.x, 2), y=round(p.y, 2)) for p in route],
            text_width=label.text_width, text_height=label.text_height))

    scene.labels = sorted(keep, key=lambda item: sort_key(item.reference_numeral))
    return scene
