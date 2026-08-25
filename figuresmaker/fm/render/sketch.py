"""Compiling a traced sketch into a figure.

The applicant drew it; this redraws it at a uniform weight, in black, at a size that fits the
sheet, with the reference characters placed by the solver and the whole thing checked. The lines
are theirs. What changes is the pen.

The numeral assignment works the same way it does for CAD, and for the same reason: a numeral
points at a part, so it is attached to a component of the drawing that genuinely exists rather
than to a region a model decided to call the housing. On a sketch a component is a run of strokes
that touch each other, which is usually one shape drawn without lifting the pen.
"""
from __future__ import annotations

from typing import Any

from ..drawing import Anchor, Figure, W_OUTLINE, polyline
from ..geom import Point, unit
from ..importers import trace as trace_import
from ..schemas import FigurePlan, SketchScene
from ..sources import Source


class SketchError(RuntimeError):
    """The sketch could not be compiled into a figure."""


def describe(source: Source, blob: bytes, limit: int = 24) -> list[dict[str, Any]]:
    """What is in a traced sketch, in terms a planner can attach numerals to."""
    traced = trace_import.trace(source.filename, blob)
    out: list[dict[str, Any]] = []
    for index in range(min(limit, len(traced.components))):
        box = traced.component_bounds(index)
        out.append({
            "component": index,
            "strokes": len(traced.components[index]),
            "size_mm": [round(box[2] - box[0], 1), round(box[3] - box[1], 1)],
            "centre_mm": [round((box[0] + box[2]) / 2.0, 1),
                          round((box[1] + box[3]) / 2.0, 1)],
            "position": _where(box, traced),
        })
    return out


def _where(box, traced: trace_import.Traced) -> str:
    """Where a component sits on the sheet, in words, from its own coordinates."""
    across = ("left", "centre", "right")
    down = ("top", "middle", "bottom")
    cx = (box[0] + box[2]) / 2.0 / max(traced.width_mm, 1e-6)
    cy = (box[1] + box[3]) / 2.0 / max(traced.height_mm, 1e-6)
    words = [down[0 if cy < 0.34 else (1 if cy < 0.67 else 2)],
             across[0 if cx < 0.34 else (1 if cx < 0.67 else 2)]]
    unique = [w for w in words if w not in ("middle", "centre")]
    return " ".join(unique) if unique else "centre"


def render_sketch(plan: FigurePlan, scene: SketchScene, source: Source, blob: bytes,
                  appearance=None) -> Figure:
    traced = trace_import.trace(source.filename, blob)
    owners = {part.component: (part.numeral or "") for part in scene.parts}

    figure = Figure(label=plan.label, kind=plan.kind, title=plan.title,
                    scene=scene.model_dump())
    per_owner: dict[str, list[list[Point]]] = {}
    for index, component in enumerate(traced.components):
        owner = owners.get(index, "")
        for points in component:
            figure.prims.append(polyline(points, role="outline", owner=owner,
                                         width=W_OUTLINE))
            if owner:
                per_owner.setdefault(owner, []).append(points)

    if not figure.prims:
        raise SketchError(f"{plan.label}: the trace produced no lines.")

    for owner, strokes in per_owner.items():
        figure.anchors[owner] = _anchors(owner, strokes)
    return figure


def _anchors(numeral: str, strokes: list[list[Point]], want: int = 12) -> list[Anchor]:
    """Points on the applicant's own strokes that a lead line may land on.

    Only points that were actually drawn. 37 CFR 1.84(q) wants the lead line to reach the
    feature, and on a traced sketch the feature is exactly the ink that was traced.
    """
    import math

    points = [p for stroke in strokes for p in stroke]
    if not points:
        return []
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)

    scored = sorted(points, key=lambda p: -math.hypot(p[0] - cx, p[1] - cy))
    spread = max(3.0, math.hypot(scored[0][0] - cx, scored[0][1] - cy) * 0.3)
    chosen: list[Point] = []
    for point in scored:
        if all(math.dist(point, other) > spread for other in chosen):
            chosen.append(point)
        if len(chosen) >= want:
            break
    return [Anchor(numeral, point, unit(point[0] - cx, point[1] - cy), 1.0)
            for point in (chosen or [scored[0]])]


def unassigned(scene: SketchScene, count: int) -> list[int]:
    """Pieces of the drawing that no numeral points at."""
    claimed = {part.component for part in scene.parts if part.numeral}
    return [i for i in range(count) if i not in claimed]
