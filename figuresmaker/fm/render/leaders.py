"""Where the numerals go, and how the lead lines get to them.

This is a separate stage on purpose. A renderer that places its own numerals places them where
they were convenient to compute, and the rules are not about convenience:

  37 CFR 1.84(p)(2)  reference characters must not be placed upon hatched or shaded surfaces,
                     and where that is unavoidable a blank space must be left in the hatching.
  37 CFR 1.84(p)(3)  characters at least 0.32 cm high.
  37 CFR 1.84(q)     lead lines originate immediately adjacent to the character, extend to the
                     feature indicated, should be as short as possible, and must not cross each
                     other.

So it is solved as a placement problem. Every numeral has a set of anchors, which are points that
are genuinely on lines the renderer drew, and a set of candidate positions around each. The cost
of a placement is what is wrong with it: ink under the numeral, a lead line across another part,
a lead line crossing another lead line. Most-constrained numerals are placed first, then the
worst-placed one is moved repeatedly until nothing improves.

It is deterministic. The same figure gives the same placement, which is what makes the editor's
"reset this numeral" button mean something and what keeps a re-run from producing a different
sheet for the same draft.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .. import geom
from ..drawing import Anchor, Figure, Leader, NumeralLabel, NUMERAL_SIZE
from ..geom import BBox, Point
from ..schemas import Finding

CLEARANCE = 1.0            # millimetres of white kept around a numeral
LEAD_GAP = 0.9             # gap between the numeral and the start of its lead line
DISTANCES = (5.0, 7.5, 10.5, 14.0, 18.5)
ANGLES = (0.0, -22.0, 22.0, -45.0, 45.0, -70.0, 70.0)
MAX_ANCHORS = 12
MAX_PASSES = 6

W_LENGTH = 1.0
W_ANGLE = 0.06
W_INK = 260.0              # a lead line across another part
W_ON_INK = 900.0           # the numeral itself sitting on a line
W_HATCH = 1400.0           # the numeral sitting on hatching, which the rule names
W_OVERLAP = 700.0          # two numerals on top of each other
W_CROSS = 1100.0           # two lead lines crossing, which the rule forbids
W_OUTSIDE = 30.0
W_INSIDE_OTHER = 950.0     # the numeral sitting inside a different part's outline
W_INSIDE_OWN = 130.0       # the numeral sitting inside its own outline rather than beside it
# A candidate costing more than this has something genuinely wrong with it rather than merely
# being longer than ideal. Counting those is how "most constrained first" is decided.
FEASIBLE = 400.0


@dataclass
class Candidate:
    numeral: str
    label: Point
    anchor: Point
    box: BBox
    tail: Point
    base: float             # everything about this placement that does not depend on the others


@dataclass
class Obstacles:
    """The figure's existing ink, in a grid so a numeral is tested against dozens of segments.

    ``regions`` is the part the segment grid cannot answer. A numeral dropped in the middle of an
    empty box crosses no line at all and still reads as being inside that box, which is exactly
    what 37 CFR 1.84(p) is about: characters go beside the part, not on it.
    """
    segments: list[tuple[Point, Point, str, str]] = field(default_factory=list)
    regions: list[tuple[list[Point], str, float]] = field(default_factory=list)
    cell: float = 8.0
    origin: Point = (0.0, 0.0)
    buckets: dict[tuple[int, int], list[int]] = field(default_factory=lambda: defaultdict(list))

    @staticmethod
    def build(figure: Figure) -> "Obstacles":
        segments: list[tuple[Point, Point, str, str]] = []
        regions: list[tuple[list[Point], str, float]] = []
        for prim in figure.prims:
            if prim.kind == "text":
                box = prim.bbox()
                ring = geom.bbox_poly(box)
                for i in range(4):
                    segments.append((ring[i], ring[(i + 1) % 4], prim.role, prim.owner))
                continue
            closed = prim.kind in ("polygon", "circle", "ellipse")
            for poly in prim.polys():
                for i in range(len(poly) - 1):
                    if geom.EPS < math.dist(poly[i], poly[i + 1]):
                        segments.append((poly[i], poly[i + 1], prim.role, prim.owner))
                if closed and prim.role == "outline" and len(poly) >= 4:
                    box = geom.poly_bbox(poly)
                    regions.append((poly, prim.owner, geom.bbox_area(box)))
        regions.sort(key=lambda item: item[2])
        out = Obstacles(segments=segments, regions=regions)
        if segments:
            xs = [p[0] for seg in segments for p in seg[:2]]
            ys = [p[1] for seg in segments for p in seg[:2]]
            span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
            out.cell = max(4.0, span / 28.0)
            out.origin = (min(xs), min(ys))
            for index, (a, b, _role, _owner) in enumerate(segments):
                for key in out._cells((min(a[0], b[0]), min(a[1], b[1]),
                                       max(a[0], b[0]), max(a[1], b[1]))):
                    out.buckets[key].append(index)
        return out

    def _cells(self, box: BBox):
        cx0 = int((box[0] - self.origin[0]) / self.cell)
        cx1 = int((box[2] - self.origin[0]) / self.cell)
        cy0 = int((box[1] - self.origin[1]) / self.cell)
        cy1 = int((box[3] - self.origin[1]) / self.cell)
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                yield (cx, cy)

    def near(self, box: BBox) -> list[int]:
        out: set[int] = set()
        for key in self._cells(box):
            hit = self.buckets.get(key)
            if hit:
                out.update(hit)
        return sorted(out)

    def under(self, box: BBox) -> tuple[int, int]:
        """How much ink, and how much of it is hatching, lies under a box."""
        ring = geom.bbox_poly(box)
        ink = hatch = 0
        for index in self.near(box):
            a, b, role, _owner = self.segments[index]
            if geom.segment_bbox_clip(a, b, box) is None:
                continue
            ink += 1
            if role == "hatch":
                hatch += 1
        _ = ring
        return (ink, hatch)

    def enclosing(self, point: Point) -> Optional[tuple[str, list[Point]]]:
        """The smallest closed outline the point falls inside, with its owner."""
        for poly, owner, _area in self.regions:
            box = geom.poly_bbox(poly)
            if not (box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]):
                continue
            if geom.point_in_polygon(point, poly):
                return (owner, poly)
        return None

    def enclosure_cost(self, point: Point, anchor: Point, numeral: str) -> float:
        """What it costs to put a numeral here, given what it is pointing at.

        A numeral inside a box that is not its own is wrong. A numeral inside a box that also
        contains the part it indicates is not: that is a subsystem's own numeral sitting inside
        the subsystem, which is how a real block diagram is drawn, and forcing it outside would
        buy a shorter enclosure at the price of a lead line dragged across two boundaries.
        """
        hit = self.enclosing(point)
        if hit is None:
            return 0.0
        owner, poly = hit
        if owner == numeral:
            return W_INSIDE_OWN
        if geom.point_in_polygon(anchor, poly):
            return W_INSIDE_OWN * 0.5
        return W_INSIDE_OTHER

    def crossings(self, a: Point, b: Point, owner: str) -> int:
        """How many lines a lead line crosses that do not belong to the part it points at."""
        box = (min(a[0], b[0]) - 0.1, min(a[1], b[1]) - 0.1,
               max(a[0], b[0]) + 0.1, max(a[1], b[1]) + 0.1)
        count = 0
        for index in self.near(box):
            c, d, role, seg_owner = self.segments[index]
            if seg_owner == owner:
                continue
            if role in ("leader", "arrow"):
                continue
            if geom.segments_cross(a, b, c, d):
                count += 1
        return count


# ------------------------------------------------------------------------------- candidates


def _candidates(numeral: str, anchors: Sequence[Anchor], obstacles: Obstacles,
                figure_box: BBox, size: float, effort: int = 1) -> list[Candidate]:
    width, height = geom.text_extent(numeral, size)
    half_w, half_h = width / 2.0 + CLEARANCE, height / 2.0 + CLEARANCE
    distances = DISTANCES if effort < 2 else DISTANCES + (23.0, 28.0, 34.0)
    angles = ANGLES if effort < 2 else ANGLES + (-90.0, 90.0, -110.0, 110.0, 180.0)
    anchor_cap = MAX_ANCHORS if effort < 2 else MAX_ANCHORS * 2
    keep = 60 if effort < 2 else 200
    out: list[Candidate] = []
    for anchor in list(anchors)[:anchor_cap]:
        nx, ny = anchor.normal if any(anchor.normal) else (0.0, -1.0)
        for angle in angles:
            radians = math.radians(angle)
            dx = nx * math.cos(radians) - ny * math.sin(radians)
            dy = nx * math.sin(radians) + ny * math.cos(radians)
            for distance in distances:
                cx = anchor.point[0] + dx * distance
                cy = anchor.point[1] + dy * distance
                box = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
                ink, hatch = obstacles.under(box)
                tail = _tail_point((cx, cy), anchor.point, half_w, half_h)
                crossings = obstacles.crossings(tail, anchor.point, numeral)
                enclosure = obstacles.enclosure_cost((cx, cy), anchor.point, numeral)
                base = (W_LENGTH * distance
                        + W_ANGLE * abs(angle)
                        + W_ON_INK * ink
                        + W_HATCH * hatch
                        + W_INK * crossings
                        + enclosure
                        + W_OUTSIDE * _outside(box, figure_box)
                        + 2.0 * (1.0 - anchor.weight) * 10.0)
                out.append(Candidate(numeral=numeral, label=(cx, cy), anchor=anchor.point,
                                     box=box, tail=tail, base=base))
    out.sort(key=lambda c: c.base)
    return out[:keep]


def _tail_point(label: Point, anchor: Point, half_w: float, half_h: float) -> Point:
    """Where the lead line starts: immediately adjacent to the character, not touching it."""
    dx, dy = anchor[0] - label[0], anchor[1] - label[1]
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    # Step out of the character's box along the direction of travel.
    scale = float("inf")
    if abs(ux) > 1e-9:
        scale = min(scale, half_w / abs(ux))
    if abs(uy) > 1e-9:
        scale = min(scale, half_h / abs(uy))
    if not math.isfinite(scale):
        scale = half_h
    step = min(scale + LEAD_GAP, length * 0.75)
    return (label[0] + ux * step, label[1] + uy * step)


def _outside(box: BBox, figure_box: BBox) -> float:
    """How far a numeral strays beyond the drawing it belongs to, in millimetres."""
    dx = max(0.0, figure_box[0] - box[0], box[2] - figure_box[2])
    dy = max(0.0, figure_box[1] - box[1], box[3] - figure_box[3])
    return max(0.0, math.hypot(dx, dy) - 6.0)


# ---------------------------------------------------------------------------------- solving


def _pair_cost(a: Candidate, b: Candidate) -> float:
    cost = 0.0
    overlap = geom.bbox_intersection_area(geom.bbox_pad(a.box, 0.4), geom.bbox_pad(b.box, 0.4))
    if overlap > 0:
        cost += W_OVERLAP * (1.0 + overlap)
    if geom.segments_cross(a.tail, a.anchor, b.tail, b.anchor):
        cost += W_CROSS
    # A lead line that runs through another numeral is as bad as running through its character.
    if geom.segment_bbox_clip(a.tail, a.anchor, geom.bbox_pad(b.box, 0.3)) is not None:
        cost += W_OVERLAP
    if geom.segment_bbox_clip(b.tail, b.anchor, geom.bbox_pad(a.box, 0.3)) is not None:
        cost += W_OVERLAP
    return cost


def solve(figure: Figure, *, size: float = NUMERAL_SIZE,
          keep: Optional[Iterable[str]] = None, effort: int = 1) -> list[Finding]:
    """Place every numeral the figure has anchors for. Returns what could not be satisfied.

    ``effort`` 2 is what the retry loop asks for when the first pass left a lead line crossing
    another: more candidate positions per anchor, further out, and more improvement passes. It
    is several times slower and is not the default for that reason.
    """
    numerals = [n for n in figure.anchors if figure.anchors[n]]
    if not numerals:
        return []
    fixed = {lab.numeral: lab for lab in figure.labels if lab.placed_by == "user"} \
        if keep is None else {lab.numeral: lab for lab in figure.labels
                              if lab.numeral in set(keep)}

    obstacles = Obstacles.build(figure)
    figure_box = figure.content_bbox(include_labels=False)
    pools: dict[str, list[Candidate]] = {}
    for numeral in numerals:
        if numeral in fixed:
            continue
        pool = _candidates(numeral, figure.anchors[numeral], obstacles, figure_box, size, effort)
        if pool:
            pools[numeral] = pool

    for numeral, label in fixed.items():
        anchor = _nearest_anchor(figure.anchors.get(numeral) or [], (label.x, label.y))
        if anchor is None:
            continue
        width, height = geom.text_extent(numeral, label.size)
        half_w, half_h = width / 2.0 + CLEARANCE, height / 2.0 + CLEARANCE
        box = (label.x - half_w, label.y - half_h, label.x + half_w, label.y + half_h)
        pools[numeral] = [Candidate(numeral=numeral, label=(label.x, label.y),
                                    anchor=anchor.point, box=box,
                                    tail=_tail_point((label.x, label.y), anchor.point,
                                                     half_w, half_h),
                                    base=0.0)]

    # Most constrained first: a numeral hemmed in on every side must choose before an easy one
    # takes the only space it had.
    order = sorted(pools, key=lambda n: (len([c for c in pools[n] if c.base < FEASIBLE]),
                                         pools[n][0].base if pools[n] else 0.0))
    chosen: dict[str, Candidate] = {}
    for numeral in order:
        chosen[numeral] = min(pools[numeral],
                              key=lambda c: c.base + sum(_pair_cost(c, other)
                                                         for other in chosen.values()))

    for _pass in range(MAX_PASSES if effort < 2 else MAX_PASSES * 3):
        improved = False
        for numeral in order:
            if len(pools[numeral]) <= 1:
                continue
            others = [c for n, c in chosen.items() if n != numeral]
            current = chosen[numeral]
            current_cost = current.base + sum(_pair_cost(current, o) for o in others)
            best, best_cost = current, current_cost
            for candidate in pools[numeral]:
                cost = candidate.base + sum(_pair_cost(candidate, o) for o in others)
                if cost < best_cost - 1e-6:
                    best, best_cost = candidate, cost
            if best is not current:
                chosen[numeral] = best
                improved = True
        if not improved:
            break

    figure.labels = []
    figure.leaders = []
    for numeral in sorted(chosen, key=lambda n: order.index(n)):
        candidate = chosen[numeral]
        figure.labels.append(NumeralLabel(numeral=numeral, x=candidate.label[0],
                                          y=candidate.label[1], size=size,
                                          placed_by="user" if numeral in fixed else "solver"))
        figure.leaders.append(Leader(numeral=numeral,
                                     points=[candidate.tail, candidate.anchor]))
    return _report(figure, chosen, obstacles)


def _nearest_anchor(anchors: Sequence[Anchor], point: Point) -> Optional[Anchor]:
    if not anchors:
        return None
    return min(anchors, key=lambda a: math.dist(a.point, point))


def _report(figure: Figure, chosen: dict[str, Candidate],
            obstacles: Obstacles) -> list[Finding]:
    """What the solver could not fix. Said out loud rather than left for the eye to find."""
    findings: list[Finding] = []
    items = list(chosen.items())
    for numeral, candidate in items:
        ink, hatch = obstacles.under(candidate.box)
        if hatch:
            findings.append(Finding(
                code="numeral_on_hatching", severity="error", stage="placement",
                figure=figure.label, numeral=numeral,
                message=(f"{numeral} sits on hatching in {figure.label} and no free position was "
                         "found. Move it, or leave a blank space in the hatching."),
                cite="37 CFR 1.84(p)(2)"))
        elif ink:
            findings.append(Finding(
                code="numeral_on_ink", severity="warning", stage="placement",
                figure=figure.label, numeral=numeral,
                message=f"{numeral} overlaps a line in {figure.label}.",
                cite="37 CFR 1.84(p)(1)"))
        if obstacles.enclosure_cost(candidate.label, candidate.anchor,
                                    numeral) >= W_INSIDE_OTHER:
            hit = obstacles.enclosing(candidate.label)
            findings.append(Finding(
                code="numeral_inside_other_part", severity="warning", stage="placement",
                figure=figure.label, numeral=numeral,
                message=(f"{numeral} sits inside the outline of "
                         + ((hit[0] if hit and hit[0] else "another element"))
                         + f" in {figure.label}; no free position outside it was found."),
                cite="37 CFR 1.84(p)(2)"))
        crossings = obstacles.crossings(candidate.tail, candidate.anchor, numeral)
        if crossings:
            findings.append(Finding(
                code="leader_crosses_geometry", severity="warning", stage="placement",
                figure=figure.label, numeral=numeral,
                message=(f"the lead line for {numeral} in {figure.label} crosses {crossings} "
                         "line(s) of another part."),
                cite="37 CFR 1.84(q)", detail={"crossings": crossings}))

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i][1], items[j][1]
            if geom.segments_cross(a.tail, a.anchor, b.tail, b.anchor):
                findings.append(Finding(
                    code="leaders_cross", severity="error", stage="placement",
                    figure=figure.label, numeral=f"{a.numeral}, {b.numeral}",
                    message=(f"the lead lines for {a.numeral} and {b.numeral} cross in "
                             f"{figure.label}. Lead lines must not cross each other."),
                    cite="37 CFR 1.84(q)"))
            elif geom.bbox_intersection_area(a.box, b.box) > 0:
                findings.append(Finding(
                    code="numerals_overlap", severity="error", stage="placement",
                    figure=figure.label, numeral=f"{a.numeral}, {b.numeral}",
                    message=f"{a.numeral} and {b.numeral} overlap in {figure.label}.",
                    cite="37 CFR 1.84(p)(1)"))
    return findings


def replace_one(figure: Figure, numeral: str, at: Point) -> list[Finding]:
    """Move one numeral to where the editor dropped it, and reattach its lead line."""
    anchors = figure.anchors.get(numeral) or []
    if not anchors:
        return [Finding(code="unknown_numeral", severity="error", stage="placement",
                        figure=figure.label, numeral=numeral,
                        message=f"{numeral} is not in {figure.label}.")]
    label = figure.label_for(numeral)
    size = label.size if label else NUMERAL_SIZE
    width, height = geom.text_extent(numeral, size)
    half_w, half_h = width / 2.0 + CLEARANCE, height / 2.0 + CLEARANCE
    # The lead line goes to whichever of the part's own anchors is now nearest, which is what a
    # draughtsman does when they drag a numeral to the other side of a part.
    anchor = min(anchors, key=lambda a: math.dist(a.point, at))
    figure.labels = [lab for lab in figure.labels if lab.numeral != numeral]
    figure.leaders = [ld for ld in figure.leaders if ld.numeral != numeral]
    figure.labels.append(NumeralLabel(numeral=numeral, x=at[0], y=at[1], size=size,
                                      placed_by="user"))
    figure.leaders.append(Leader(numeral=numeral,
                                 points=[_tail_point(at, anchor.point, half_w, half_h),
                                         anchor.point]))
    return []


def retarget(figure: Figure, numeral: str, tip: Point) -> list[Finding]:
    """Move the far end of a lead line to a point the user chose on the element."""
    anchors = figure.anchors.get(numeral) or []
    if not anchors:
        return [Finding(code="unknown_numeral", severity="error", stage="placement",
                        figure=figure.label, numeral=numeral,
                        message=f"{numeral} is not in {figure.label}.")]
    anchor = min(anchors, key=lambda a: math.dist(a.point, tip))
    label = figure.label_for(numeral)
    if label is None:
        return []
    width, height = geom.text_extent(numeral, label.size)
    figure.leaders = [ld for ld in figure.leaders if ld.numeral != numeral]
    figure.leaders.append(Leader(
        numeral=numeral,
        points=[_tail_point((label.x, label.y), anchor.point,
                            width / 2.0 + CLEARANCE, height / 2.0 + CLEARANCE),
                anchor.point]))
    return []
