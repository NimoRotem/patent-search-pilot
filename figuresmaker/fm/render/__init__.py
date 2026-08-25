"""Scene in, figure out.

One entry point, because every figure type has to leave the same thing behind: primitives in
millimetres, anchors on real lines, numerals placed by the solver, and a list of what could not
be satisfied. A renderer that returned a picture instead would put the compliance checks back
into the realm of looking at it.
"""
from __future__ import annotations

from typing import Any, Optional

from ..drawing import Figure, normalise
from ..schemas import Finding, FigurePlan, GraphScene, MechScene, SeqScene, UIScene
from . import graphfig, leaders, mech, uifig
from .graphfig import LayoutUnavailable
from .mech import MechError
from .uifig import UIError


class RenderError(RuntimeError):
    """This figure could not be drawn. Carries the stage that has to fix it."""

    def __init__(self, message: str, *, stage: str = "renderer", figure: str = ""):
        super().__init__(message)
        self.stage = stage
        self.figure = figure


def render(plan: FigurePlan, scene: Any, appearance=None) -> tuple[Figure, list[Finding]]:
    """Draw one figure and place its numerals."""
    try:
        if plan.kind in ("block_diagram", "flowchart"):
            figure = graphfig.render_graph(plan, _as(scene, GraphScene))
        elif plan.kind == "sequence":
            figure = graphfig.render_sequence(plan, _as(scene, SeqScene))
        elif plan.kind in ("perspective", "exploded", "cross_section"):
            figure = mech.render_mech(plan, _as(scene, MechScene), appearance)
        elif plan.kind == "ui_screen":
            figure = uifig.render_ui(plan, _as(scene, UIScene))
        else:
            raise RenderError(f"{plan.label}: unknown figure kind {plan.kind!r}",
                              stage="planner", figure=plan.label)
    except (LayoutUnavailable, MechError, UIError) as exc:
        raise RenderError(f"{plan.label}: {exc}", stage="renderer", figure=plan.label) from exc

    normalise(figure, margin=1.0)
    findings = leaders.solve(figure)
    findings.extend(_missing_numerals(plan, figure))
    findings.extend(_illegible_elements(plan, figure))
    if plan.kind in ("perspective", "cross_section"):
        findings.extend(_disconnected(plan, _as(scene, MechScene)))
    return figure, findings


def _as(scene: Any, schema):
    if isinstance(scene, schema):
        return scene
    if isinstance(scene, dict):
        return schema.model_validate(scene)
    raise RenderError(f"the scene is a {type(scene).__name__}, not a {schema.__name__}",
                      stage="planner")


def _missing_numerals(plan: FigurePlan, figure: Figure) -> list[Finding]:
    """A numeral the plan promised and the scene never drew.

    This is the failure that matters most, because it is silent: the figure looks fine, and the
    only sign is that a part the description talks about is not in any view. It is reported
    against the planner, which is the stage that can put it back.
    """
    drawn = set(figure.anchors)
    out: list[Finding] = []
    for element in plan.elements:
        if element.numeral not in drawn:
            out.append(Finding(
                code="element_not_drawn", severity="error", stage="planner",
                figure=plan.label, numeral=element.numeral,
                message=(f"{plan.label} was planned to show {element.numeral} "
                         f"(\"{element.term}\") but the scene does not contain it."),
                cite="37 CFR 1.84(p)(4)"))
    return out


def _illegible_elements(plan: FigurePlan, figure: Figure) -> list[Finding]:
    """A part drawn so small that pointing a numeral at it means nothing.

    This is the failure a scene produces when a model loses track of scale: a part with a radius
    of a tenth of a millimetre next to one a hundred millimetres across. Every other check passes.
    The numeral is in the registry, it appears in the figure, its lead line touches the geometry,
    and what it touches is a speck.
    """
    import math

    from .. import geom

    box = figure.content_bbox(include_labels=False)
    diagonal = math.hypot(box[2] - box[0], box[3] - box[1]) or 1.0
    floor = max(1.0, diagonal * 0.008)

    extents: dict[str, list] = {}
    for prim in figure.prims:
        if not prim.owner or prim.role in ("leader", "arrow", "numeral", "caption"):
            continue
        for poly in prim.polys():
            extents.setdefault(prim.owner, []).extend(poly)

    out: list[Finding] = []
    for element in plan.elements:
        points = extents.get(element.numeral)
        if not points:
            continue
        part = geom.poly_bbox(points)
        size = math.hypot(part[2] - part[0], part[3] - part[1])
        if size >= floor:
            continue
        out.append(Finding(
            code="element_not_legible", severity="error", stage="renderer",
            figure=plan.label, numeral=element.numeral,
            message=(f"{element.numeral} (\"{element.term}\") is drawn {size:.2f} mm across in "
                     f"{plan.label}, against a figure {diagonal:.0f} mm on the diagonal. Give it "
                     "dimensions in proportion to the parts around it."),
            cite="37 CFR 1.84(l)", basis="practice",
            detail={"extent_mm": round(size, 3), "minimum_mm": round(floor, 2)}))
    return out


def _disconnected(plan: FigurePlan, scene: MechScene) -> list[Finding]:
    from . import solid as solidlib

    solids = list(scene.solids or [])
    if len(solids) < 2 or scene.explode:
        return []
    meshes = [solidlib.build_solid(s) for s in solids]
    groups = mech.assembly_components(scene, meshes)
    if len(groups) < 2:
        return []
    loose = ", ".join(sorted(g[0] for g in groups)[:8])
    return [Finding(
        code="assembly_disconnected", severity="error", stage="renderer", figure=plan.label,
        message=(f"{plan.label} is an assembled view but its parts fall into {len(groups)} "
                 f"groups that do not touch ({loose}). Place them so the parts that are joined "
                 "actually meet."),
        cite="", basis="practice", detail={"groups": len(groups)})]


def resolve(figure: Figure) -> list[Finding]:
    """Re-run the solver over a figure, keeping anything the user placed by hand."""
    return leaders.solve(figure)
