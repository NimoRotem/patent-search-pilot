"""Scene in, figure out.

One entry point, because every figure type has to leave the same thing behind: primitives in
millimetres, anchors on real lines, numerals placed by the solver, and a list of what could not
be satisfied. A renderer that returned a picture instead would put the compliance checks back
into the realm of looking at it.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..drawing import Figure, normalise
from ..schemas import (CadScene, Finding, FigurePlan, GraphScene, MechScene, SeqScene,
                       SketchScene, UIScene)
from .. import sources as sources_mod
from . import cad, graphfig, leaders, mech, sketch, uifig
from .cad import CadError
from .sketch import SketchError
from .graphfig import LayoutUnavailable
from .mech import MechError
from .uifig import UIError

# Given a source id, return the record and its bytes. The renderer is handed one of these rather
# than a path, so it never has to know where a job keeps its files.
SourceResolver = Callable[[str], tuple[Any, bytes]]


class RenderError(RuntimeError):
    """This figure could not be drawn. Carries the stage that has to fix it."""

    def __init__(self, message: str, *, stage: str = "renderer", figure: str = ""):
        super().__init__(message)
        self.stage = stage
        self.figure = figure


def render(plan: FigurePlan, scene: Any, appearance=None,
           resolve_source: Optional[SourceResolver] = None) -> tuple[Figure, list[Finding]]:
    """Draw one figure and place its numerals.

    Which renderer runs is decided by the figure's SOURCE first and its kind second. A
    perspective view compiled from the applicant's mesh and one blocked out from prose are the
    same kind of figure and not remotely the same claim about the invention, and the dispatcher
    is where that distinction has to be made or it will not be made anywhere.
    """
    source_kind = (plan.source.kind if plan.source else "blockout") or "blockout"
    from_cad = source_kind == "cad" and plan.kind in ("perspective", "exploded", "cross_section")
    # A traced sketch backs any kind of figure, because the applicant already chose the view when
    # they drew it. There is nothing left to project.
    from_sketch = source_kind == "sketch"
    try:
        if from_sketch:
            if resolve_source is None:
                raise RenderError(f"{plan.label}: this figure is compiled from a sketch but no "
                                  "source store was supplied", stage="renderer",
                                  figure=plan.label)
            record, blob = resolve_source(plan.source.source_id)
            figure = sketch.render_sketch(plan, _as(scene, SketchScene), record, blob, appearance)
        elif from_cad:
            if resolve_source is None:
                raise RenderError(f"{plan.label}: this figure is compiled from CAD but no source "
                                  "store was supplied", stage="renderer", figure=plan.label)
            record, blob = resolve_source(plan.source.source_id)
            figure = cad.render_cad(plan, _as(scene, CadScene), record, blob, appearance)
        elif plan.kind in ("block_diagram", "flowchart"):
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
    except (LayoutUnavailable, MechError, UIError, CadError, SketchError) as exc:
        raise RenderError(f"{plan.label}: {exc}", stage="renderer", figure=plan.label) from exc
    except sources_mod.SourceError as exc:
        raise RenderError(f"{plan.label}: {exc}", stage="draft", figure=plan.label) from exc

    figure.provenance = {"source_kind": source_kind, "source_id": plan.source.source_id,
                         "filing_ready": sources_mod.is_authoritative(plan.kind, source_kind)}
    normalise(figure, margin=1.0)
    findings = leaders.solve(figure)
    findings.extend(_provenance(plan))
    findings.extend(_missing_numerals(plan, figure))
    findings.extend(_illegible_elements(plan, figure))
    if not from_cad and not from_sketch and plan.kind in ("perspective", "cross_section"):
        findings.extend(_disconnected(plan, _as(scene, MechScene)))
    return figure, findings


def _provenance(plan: FigurePlan) -> list[Finding]:
    """Say, once per figure, whether it is a drawing of the invention or a blockout of it.

    This is the finding the product turns on. It is an error rather than a warning because a
    blockout that reaches a filing is worse than no drawing at all: it is a statement about the
    invention that nobody made.
    """
    source_kind = (plan.source.kind if plan.source else "blockout") or "blockout"
    if sources_mod.is_authoritative(plan.kind, source_kind):
        return []
    return [Finding(
        code="geometry_not_authoritative", severity="error", stage="draft", figure=plan.label,
        message=f"{plan.label} is a draft: " + sources_mod.draft_reason(plan.kind, source_kind),
        cite="", basis="practice",
        detail={"source_kind": source_kind, "figure_kind": plan.kind})]


def _as(scene: Any, schema):
    if isinstance(scene, schema):
        return scene
    if isinstance(scene, dict):
        return schema.model_validate(scene)
    raise RenderError(f"the scene is a {type(scene).__name__}, not a {schema.__name__}",
                      stage="planner")


def _missing_numerals(plan: FigurePlan, figure: Figure) -> list[Finding]:
    """A numeral the plan promised and the drawing does not carry.

    This is the failure that matters most, because it is silent: the figure looks fine, and the
    only sign is that a part the description talks about is not in any view.

    Who it is reported against depends on where the geometry came from. In a blockout or a
    diagram the planner could have included the part and did not, so the planner is asked again.
    In a view compiled from the applicant's CAD there is nothing to ask: the part is not in the
    file. Sending that to the planner would be asking a model to invent the very thing this
    product refuses to invent, so it goes to the draft instead, where a person can model the
    part, split the mesh, or accept that this view does not show it.
    """
    drawn = set(figure.anchors)
    from_cad = (plan.source.kind if plan.source else "") in ("cad", "sketch")
    out: list[Finding] = []
    for element in plan.elements:
        if element.numeral in drawn:
            continue
        if from_cad:
            out.append(Finding(
                code="part_not_in_supplied_geometry", severity="error", stage="draft",
                figure=plan.label, numeral=element.numeral, basis="practice",
                message=(f"{element.numeral} (\"{element.term}\") was to be shown in "
                         f"{plan.label}, and the supplied "
                         + ("sketch" if (plan.source.kind if plan.source else "") == "sketch"
                            else "mesh")
                         + " has no piece that could be identified as it. Draw it, separate it "
                           "in the source, or take it out of this view in the coverage matrix."),
                cite="37 CFR 1.84(p)(4)"))
        else:
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
