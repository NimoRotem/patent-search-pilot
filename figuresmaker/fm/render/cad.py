"""Compiling a supplied mesh into a view.

This is the path the product is for. The geometry is the applicant's; nothing here decides a
shape, a size or a position. A model may choose which way to look at the assembly and say which
component carries which reference numeral, and that is the whole of its involvement. Everything
else is the same projection, hidden-line removal, plane cutting and hatching that a primitive
scene goes through, because the mesh was imported into the same type.

Components matter more than they look. An assembly exported as one STL is several parts in one
file, and a reference numeral points at a part. Splitting on connectivity recovers them, so a
numeral lands on a component that genuinely exists in the applicant's model rather than on a
region a model decided to call the housing.
"""
from __future__ import annotations

from typing import Any, Sequence

from ..drawing import Figure
from ..importers import mesh as mesh_import
from ..schemas import CadScene, FigurePlan
from ..sources import Source
from . import mech
from .solid import Mesh


class CadError(RuntimeError):
    """The view could not be compiled from the mesh."""


def describe(source: Source, blob: bytes, limit: int = 24) -> list[dict[str, Any]]:
    """What is in a supplied mesh, in terms a planner can assign numerals to.

    Deliberately numeric. Size, position and share of the model are facts about geometry that
    exists; a description in words would be a guess about what the parts are for, which is the
    thing this product refuses to do.
    """
    whole = mesh_import.load(source.filename, blob)
    parts = mesh_import.split_components(whole, limit=limit)
    lo, hi = whole.bounds()
    span = max(hi[i] - lo[i] for i in range(3)) or 1.0

    out: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        plo, phi = part.bounds()
        size = [round(phi[i] - plo[i], 1) for i in range(3)]
        centre = [round((plo[i] + phi[i]) / 2.0, 1) for i in range(3)]
        out.append({
            "component": index,
            "triangles": len(part.tris),
            "size_mm": size,
            "centre_mm": centre,
            "extent_fraction": round(max(size) / span, 3),
            "position": _where(centre, lo, hi),
        })
    return out


def _where(centre: Sequence[float], lo, hi) -> str:
    """A plain description of where a component sits in the whole, on each axis."""
    names = (("left", "middle", "right"), ("bottom", "middle", "top"), ("back", "middle", "front"))
    words: list[str] = []
    for axis in range(3):
        span = (hi[axis] - lo[axis]) or 1.0
        t = (centre[axis] - lo[axis]) / span
        words.append(names[axis][0 if t < 0.34 else (1 if t < 0.67 else 2)])
    unique = [w for w in words if w != "middle"]
    return " ".join(unique) if unique else "centre"


def build(scene: CadScene, source: Source, blob: bytes, kind: str) -> list[Mesh]:
    """The applicant's parts, named by the numerals the scene assigned to them."""
    whole = mesh_import.load(source.filename, blob)
    parts = mesh_import.split_components(whole)
    if not parts:
        raise CadError(f"{source.filename} has no connected geometry.")

    owners = {p.component: (p.numeral or "") for p in scene.parts}
    named: list[Mesh] = []
    for index, part in enumerate(parts):
        part.owner = owners.get(index, "")
        part.sid = f"c{index}"
        named.append(part)

    if scene.explode or kind == "exploded":
        axis = scene.explode.axis if scene.explode else "y"
        gap = scene.explode.gap if scene.explode else max(
            18.0, mech.Scene3D(verts=whole.verts, tris=[], tri_owner=[], edges=[], smooth=[],
                               caps=[]).size() * 0.35)
        named = mech.explode_meshes(named, axis, gap,
                                    list(scene.explode.order) if scene.explode else None)
    return named


def render_cad(plan: FigurePlan, scene: CadScene, source: Source, blob: bytes,
               appearance=None) -> Figure:
    parts = build(scene, source, blob, plan.kind)
    scene3d = mech.compose(parts, plan.kind, section=scene.section)
    camera = mech.Camera.named(scene.camera or "isometric")
    figure = mech.draw_projection(
        plan, scene3d, camera, appearance=appearance,
        draw_hidden=bool(scene.hidden_lines or plan.conventions.hidden_lines),
        exploded=(plan.kind == "exploded"),
        explode_axis=(scene.explode.axis if scene.explode else "y"))
    figure.scene = scene.model_dump()
    return figure


def unassigned(scene: CadScene, count: int) -> list[int]:
    """Components of the mesh that no numeral points at. Reported, not hidden.

    A part the applicant modelled and the drawing does not name is either a part the description
    forgot to number, or a component the splitter separated that is really one part with its
    neighbour. Both are worth a sentence to the attorney; neither is worth guessing about.
    """
    claimed = {p.component for p in scene.parts if p.numeral}
    return [i for i in range(count) if i not in claimed]
