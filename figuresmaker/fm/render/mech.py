"""Perspective, exploded and cross-sectional views.

This is where a patent drawing stops being a diagram and becomes a picture of a thing, and it is
done the way a draughtsman would do it rather than the way an image model would: the solids are
real meshes, the view is a real orthographic projection, and a line is drawn only where it can
actually be seen.

The hidden-line removal is exact rather than sampled. An edge and a triangle both project to the
plane of the sheet as straight things, and the depth of each is an affine function of position on
that plane, so the interval of the edge that the triangle covers, and the part of that interval
where the triangle is nearer the eye, can both be solved rather than tested at points. That
matters: a sampled test drops the last half millimetre of a line at every crossing, and a hundred
of those is what makes a generated drawing look generated.

Silhouettes are found without reference to winding order. On a curved surface the outline you see
is where the surface folds away from you, and there both faces that meet at the fold project to
the same side of it. That test needs no normals to be pointing the right way, which is what keeps
a mesh built by one of forty different builders from having to agree about handedness.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .. import geom
from ..drawing import (Anchor, DASH_CENTRE, DASH_HIDDEN, Figure, W_HATCH, W_HIDDEN,
                       W_OUTLINE, polyline)
from ..geom import Point
from ..schemas import FigurePlan, MechScene
from . import solid as solidlib
from .solid import Mesh, V3

EPS = 1e-9
MAX_SOLIDS = 48
MAX_TRIANGLES = 60000
HATCH_SPACING = 1.5
GRID_TARGET = 24


class MechError(RuntimeError):
    """The assembly could not be built into something drawable."""


# ------------------------------------------------------------------------------------- camera

_VIEWS: dict[str, tuple[float, float]] = {
    "isometric": (45.0, 35.264389682754654),
    "dimetric": (45.0, 20.0),
    "trimetric": (33.0, 24.0),
    "front": (0.0, 0.0),
    "top": (0.0, 89.9),
    "right": (90.0, 0.0),
}


@dataclass
class Camera:
    """An orthographic camera. ``depth`` grows towards the eye."""
    right: V3
    up: V3
    eye: V3

    @staticmethod
    def named(name: str) -> "Camera":
        az, el = _VIEWS.get((name or "isometric").lower(), _VIEWS["isometric"])
        return Camera.from_angles(az, el)

    @staticmethod
    def from_angles(az_deg: float, el_deg: float) -> "Camera":
        az, el = math.radians(az_deg), math.radians(el_deg)
        eye = (math.sin(az) * math.cos(el), math.sin(el), math.cos(az) * math.cos(el))
        right = (math.cos(az), 0.0, -math.sin(az))
        up = _cross(eye, right)
        return Camera(right=_norm(right), up=_norm(up), eye=_norm(eye))

    def project(self, v: V3) -> tuple[float, float, float]:
        """Screen x, screen y with y downwards, and depth."""
        return (_dot(v, self.right), -_dot(v, self.up), _dot(v, self.eye))


def _dot(a: V3, b: V3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: V3, b: V3) -> V3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(v: V3) -> V3:
    length = math.sqrt(_dot(v, v)) or 1.0
    return (v[0] / length, v[1] / length, v[2] / length)


# ------------------------------------------------------------------------------------- scene


@dataclass
class Scene3D:
    """Everything to be drawn, flattened into one vertex list."""
    verts: list[V3]
    tris: list[tuple[int, int, int]]
    tri_owner: list[str]
    edges: list[tuple[int, int, str]]          # a, b, owner
    smooth: list[tuple[int, int, str]]
    caps: list[tuple[list[V3], str]]           # a cut face and whose it is

    def size(self) -> float:
        if not self.verts:
            return 1.0
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        zs = [v[2] for v in self.verts]
        return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0


def assemble(scene: MechScene, kind: str) -> Scene3D:
    solids = list(scene.solids or [])[:MAX_SOLIDS]
    if not solids:
        raise MechError("the scene has no solids")

    meshes = [solidlib.build_solid(s) for s in solids]
    if scene.explode or kind == "exploded":
        meshes = _explode(scene, solids, meshes)

    total = sum(len(m.tris) for m in meshes)
    if total > MAX_TRIANGLES:
        raise MechError(f"the assembly needs {total} triangles, above the {MAX_TRIANGLES} this "
                        "renderer will project. Split the view into two figures")

    caps: list[tuple[list[V3], str]] = []
    if scene.section or kind == "cross_section":
        spec = scene.section
        if spec is None:
            raise MechError("a cross-sectional figure needs a cutting plane; the scene gave none")
        cut: list[Mesh] = []
        for mesh in meshes:
            clipped, loops = _cut_mesh(mesh, spec)
            if clipped.tris:
                cut.append(clipped)
            for loop in loops:
                caps.append((loop, mesh.owner))
        if not cut:
            raise MechError("the cutting plane misses every solid, so the section is empty")
        meshes = cut

    verts: list[V3] = []
    tris: list[tuple[int, int, int]] = []
    tri_owner: list[str] = []
    edges: list[tuple[int, int, str]] = []
    smooth: list[tuple[int, int, str]] = []
    for mesh in meshes:
        offset = len(verts)
        verts.extend(mesh.verts)
        for a, b, c in mesh.tris:
            tris.append((a + offset, b + offset, c + offset))
            tri_owner.append(mesh.owner)
        for a, b in mesh.edges:
            edges.append((a + offset, b + offset, mesh.owner))
        for a, b in mesh.smooth:
            smooth.append((a + offset, b + offset, mesh.owner))
    return Scene3D(verts=verts, tris=tris, tri_owner=tri_owner, edges=edges, smooth=smooth,
                   caps=caps)


def _explode(scene: MechScene, solids, meshes: list[Mesh]) -> list[Mesh]:
    spec = scene.explode
    axis = (spec.axis if spec else "y")
    gap = (spec.gap if spec else 30.0)
    index = {"x": 0, "y": 1, "z": 2}[axis]
    order = list(spec.order) if spec and spec.order else []
    if not order:
        # No stated order: separate along the axis in the order the parts already sit on it.
        order = [s.id for s in sorted(solids, key=lambda s: (s.at or [0, 0, 0])[index])]
    rank = {sid: i for i, sid in enumerate(order)}
    middle = (len(order) - 1) / 2.0
    out: list[Mesh] = []
    for mesh, spec_solid in zip(meshes, solids):
        shift = (rank.get(spec_solid.id, middle) - middle) * gap
        delta = [0.0, 0.0, 0.0]
        delta[index] = shift
        out.append(mesh.translated(*delta))
    return out


# ------------------------------------------------------------------------------ plane cutting


def _cut_mesh(mesh: Mesh, spec) -> tuple[Mesh, list[list[V3]]]:
    """Keep the half of a mesh on one side of a plane, and return the loops it was cut along."""
    index = {"x": 0, "y": 1, "z": 2}[spec.axis]
    sign = 1.0 if spec.keep == "negative" else -1.0
    offset = spec.offset

    def side(v: V3) -> float:
        return sign * (v[index] - offset)

    verts: list[V3] = list(mesh.verts)
    keep_tris: list[tuple[int, int, int]] = []
    segments: list[tuple[V3, V3]] = []
    cache: dict[tuple[int, int], int] = {}

    def crossing(i: int, j: int) -> int:
        key = (i, j) if i < j else (j, i)
        if key in cache:
            return cache[key]
        a, b = mesh.verts[i], mesh.verts[j]
        da, db = side(a), side(b)
        t = da / (da - db) if abs(da - db) > EPS else 0.5
        point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)
        verts.append(point)
        cache[key] = len(verts) - 1
        return cache[key]

    for tri in mesh.tris:
        d = [side(mesh.verts[i]) for i in tri]
        inside = [i for i, value in zip(tri, d) if value <= EPS]
        outside = [i for i, value in zip(tri, d) if value > EPS]
        if not outside:
            keep_tris.append(tri)
            continue
        if not inside:
            continue
        if len(inside) == 1:
            a = inside[0]
            p = crossing(a, outside[0])
            q = crossing(a, outside[1])
            keep_tris.append((a, p, q))
            segments.append((verts[p], verts[q]))
        else:
            a, b = inside
            c = outside[0]
            p = crossing(a, c)
            q = crossing(b, c)
            keep_tris.append((a, b, q))
            keep_tris.append((a, q, p))
            segments.append((verts[p], verts[q]))

    kept = set()
    for tri in keep_tris:
        kept.update(tri)
    edges = [(a, b) for a, b in mesh.edges if a in kept and b in kept]
    smooth = {(a, b) for a, b in mesh.smooth if a in kept and b in kept}
    loops = _chain(segments)
    # The cut face itself is a surface: without it, everything behind the plane stays visible
    # through the hole the cut made.
    for loop in loops:
        if len(loop) < 3:
            continue
        centre = _centroid(loop)
        base = len(verts)
        verts.append(centre)
        start = len(verts)
        verts.extend(loop)
        for i in range(len(loop)):
            keep_tris.append((base, start + i, start + (i + 1) % len(loop)))
        for i in range(len(loop)):
            edges.append((start + i, start + (i + 1) % len(loop)))
    return (Mesh(verts=verts, tris=keep_tris, edges=edges, smooth=smooth, owner=mesh.owner,
                 sid=mesh.sid), loops)


def _centroid(points: Sequence[V3]) -> V3:
    n = float(len(points)) or 1.0
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n,
            sum(p[2] for p in points) / n)


def _chain(segments: list[tuple[V3, V3]], tolerance: float = 1e-4) -> list[list[V3]]:
    """Cut segments joined into closed loops.

    Only closed loops are returned. An open run is not a face, and hatching one would put oblique
    lines across empty space, which reads as a mistake to anyone who has seen a section drawing.
    """
    if not segments:
        return []

    def key(v: V3) -> tuple[int, int, int]:
        return (round(v[0] / tolerance), round(v[1] / tolerance), round(v[2] / tolerance))

    points: dict[tuple, V3] = {}
    incident: dict[tuple, list[int]] = defaultdict(list)
    ends: list[tuple[tuple, tuple]] = []
    for a, b in segments:
        ka, kb = key(a), key(b)
        if ka == kb:
            continue
        points[ka], points[kb] = a, b
        edge_id = len(ends)
        ends.append((ka, kb))
        incident[ka].append(edge_id)
        incident[kb].append(edge_id)

    used: set[int] = set()
    loops: list[list[V3]] = []
    for seed in range(len(ends)):
        if seed in used:
            continue
        start, cursor = ends[seed]
        used.add(seed)
        loop_keys = [start, cursor]
        closed = False
        while True:
            nxt_edge = None
            for edge_id in incident[cursor]:
                if edge_id in used:
                    continue
                a, b = ends[edge_id]
                other = b if a == cursor else a
                nxt_edge = (edge_id, other)
                break
            if nxt_edge is None:
                break
            used.add(nxt_edge[0])
            cursor = nxt_edge[1]
            if cursor == start:
                closed = True
                break
            loop_keys.append(cursor)
            if len(loop_keys) > 20000:
                break
        if closed and len(loop_keys) >= 3:
            loops.append([points[k] for k in loop_keys])
    return loops


# ------------------------------------------------------------------- hidden line removal


@dataclass
class Projected:
    xy: list[Point]
    depth: list[float]
    planes: list[Optional[tuple[float, float, float]]]
    boxes: list[geom.BBox]
    scene: Scene3D
    scale: float


def project(scene: Scene3D, camera: Camera) -> Projected:
    xy: list[Point] = []
    depth: list[float] = []
    for v in scene.verts:
        x, y, z = camera.project(v)
        xy.append((x, y))
        depth.append(z)
    planes: list[Optional[tuple[float, float, float]]] = []
    boxes: list[geom.BBox] = []
    for a, b, c in scene.tris:
        planes.append(_plane(xy[a], xy[b], xy[c], depth[a], depth[b], depth[c]))
        boxes.append((min(xy[a][0], xy[b][0], xy[c][0]), min(xy[a][1], xy[b][1], xy[c][1]),
                      max(xy[a][0], xy[b][0], xy[c][0]), max(xy[a][1], xy[b][1], xy[c][1])))
    return Projected(xy=xy, depth=depth, planes=planes, boxes=boxes, scene=scene,
                     scale=scene.size())


def _plane(p0: Point, p1: Point, p2: Point, d0: float, d1: float,
           d2: float) -> Optional[tuple[float, float, float]]:
    """depth = a*x + b*y + c over the projected triangle, or None if it projects to a line."""
    x0, y0 = p0
    det = ((p1[0] - x0) * (p2[1] - y0)) - ((p2[0] - x0) * (p1[1] - y0))
    if abs(det) < 1e-12:
        return None
    u1, v1, w1 = p1[0] - x0, p1[1] - y0, d1 - d0
    u2, v2, w2 = p2[0] - x0, p2[1] - y0, d2 - d0
    a = (w1 * v2 - w2 * v1) / det
    b = (u1 * w2 - u2 * w1) / det
    c = d0 - a * x0 - b * y0
    return (a, b, c)


class TriangleIndex:
    """A uniform grid over the projected triangles, so an edge tests dozens, not thousands."""

    def __init__(self, projected: Projected):
        self.projected = projected
        boxes = projected.boxes
        if not boxes:
            self.cell = 1.0
            self.origin = (0.0, 0.0)
            self.cols = self.rows = 1
            self.buckets: dict[tuple[int, int], list[int]] = {}
            return
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        span = max(x1 - x0, y1 - y0, 1e-6)
        self.cell = span / GRID_TARGET
        self.origin = (x0, y0)
        self.cols = int((x1 - x0) / self.cell) + 1
        self.rows = int((y1 - y0) / self.cell) + 1
        self.buckets = defaultdict(list)
        for index, box in enumerate(boxes):
            if projected.planes[index] is None:
                continue
            for cell in self._cells(box):
                self.buckets[cell].append(index)

    def _cells(self, box: geom.BBox):
        cx0 = int((box[0] - self.origin[0]) / self.cell)
        cx1 = int((box[2] - self.origin[0]) / self.cell)
        cy0 = int((box[1] - self.origin[1]) / self.cell)
        cy1 = int((box[3] - self.origin[1]) / self.cell)
        for cx in range(max(0, cx0), min(self.cols, cx1 + 1)):
            for cy in range(max(0, cy0), min(self.rows, cy1 + 1)):
                yield (cx, cy)

    def candidates(self, a: Point, b: Point) -> set[int]:
        box = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        out: set[int] = set()
        for cell in self._cells(box):
            hit = self.buckets.get(cell)
            if hit:
                out.update(hit)
        return out


def visible_intervals(a: Point, b: Point, depth_a: float, depth_b: float,
                      index: TriangleIndex, *, skip: Iterable[int] = (),
                      bias: float = 1e-4) -> list[tuple[float, float]]:
    """The parts of an edge no triangle covers, as parameter intervals."""
    projected = index.projected
    eps = bias * max(1.0, projected.scale)
    hidden: list[tuple[float, float]] = []
    skip_set = set(skip)

    for tri_index in index.candidates(a, b):
        tri = projected.scene.tris[tri_index]
        if skip_set & set(tri):
            continue
        plane = projected.planes[tri_index]
        if plane is None:
            continue
        span = _clip_to_triangle(a, b, projected.xy[tri[0]], projected.xy[tri[1]],
                                 projected.xy[tri[2]])
        if span is None:
            continue
        t0, t1 = span
        # depth of the covering triangle, and of the edge, are both affine in t.
        pa = plane[0] * a[0] + plane[1] * a[1] + plane[2]
        pb = plane[0] * b[0] + plane[1] * b[1] + plane[2]
        f0 = (pa + (pb - pa) * t0) - (depth_a + (depth_b - depth_a) * t0) - eps
        f1 = (pa + (pb - pa) * t1) - (depth_a + (depth_b - depth_a) * t1) - eps
        if f0 <= 0 and f1 <= 0:
            continue
        if f0 > 0 and f1 > 0:
            hidden.append((t0, t1))
            continue
        cut = t0 + (t1 - t0) * (f0 / (f0 - f1))
        hidden.append((cut, t1) if f1 > 0 else (t0, cut))

    return _subtract(hidden)


def _subtract(hidden: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not hidden:
        return [(0.0, 1.0)]
    hidden.sort()
    merged: list[list[float]] = [list(hidden[0])]
    for start, end in hidden[1:]:
        if start <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor + 1e-6:
            out.append((cursor, min(start, 1.0)))
        cursor = max(cursor, end)
        if cursor >= 1.0:
            break
    if cursor < 1.0 - 1e-6:
        out.append((cursor, 1.0))
    return [(s, e) for s, e in out if e - s > 1e-6]


def _clip_to_triangle(p0: Point, p1: Point, a: Point, b: Point,
                      c: Point) -> Optional[tuple[float, float]]:
    area = (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
    if abs(area) < 1e-12:
        return None
    sign = 1.0 if area > 0 else -1.0
    t0, t1 = 0.0, 1.0
    for u, v in ((a, b), (b, c), (c, a)):
        nx = -(v[1] - u[1]) * sign
        ny = (v[0] - u[0]) * sign
        d0 = nx * (p0[0] - u[0]) + ny * (p0[1] - u[1])
        d1 = nx * (p1[0] - u[0]) + ny * (p1[1] - u[1])
        denom = d1 - d0
        if abs(denom) < 1e-15:
            if d0 < 0:
                return None
            continue
        t = -d0 / denom
        if denom > 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return (t0, t1)


# ------------------------------------------------------------------------------- silhouettes


def silhouette_edges(scene: Scene3D, projected: Projected) -> list[tuple[int, int, str]]:
    """Edges on a curved surface where the surface folds away from the eye.

    Found without normals: at a fold, the two faces meeting at the edge project to the same side
    of it. A mesh whose builders disagree about winding still gives the right answer.
    """
    if not scene.smooth:
        return []
    smooth = {(a, b) if a < b else (b, a): owner for a, b, owner in scene.smooth}
    adjacency: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (a, b, c) in enumerate(scene.tris):
        for u, v in ((a, b), (b, c), (c, a)):
            key = (u, v) if u < v else (v, u)
            if key in smooth:
                adjacency[key].append(index)

    xy = projected.xy
    out: list[tuple[int, int, str]] = []
    for key, faces in adjacency.items():
        if len(faces) != 2:
            if len(faces) == 1:
                out.append((key[0], key[1], smooth[key]))     # a boundary is always an outline
            continue
        a, b = key
        third = []
        for face in faces:
            tri = scene.tris[face]
            third.append(next(i for i in tri if i not in (a, b)))
        s1 = _side(xy[a], xy[b], xy[third[0]])
        s2 = _side(xy[a], xy[b], xy[third[1]])
        if s1 == 0.0 or s2 == 0.0:
            continue
        if (s1 > 0) == (s2 > 0):
            out.append((a, b, smooth[key]))
    return out


def _side(a: Point, b: Point, p: Point) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


# ------------------------------------------------------------------------------------ render


def assembly_components(scene: MechScene, meshes: Sequence[Mesh]) -> list[list[str]]:
    """Groups of solids that touch. More than one group means the assembly is in pieces.

    A perspective view is a picture of the thing assembled. A model that gives every part its own
    coordinates without making them meet produces a set of objects floating in space, which looks
    like an exploded view that forgot its alignment lines. It is worth catching, because it is
    invisible to every other check: the geometry is valid, the numerals are right, and the
    drawing is wrong.
    """
    boxes: list[tuple[str, tuple[V3, V3]]] = []
    for solid_spec, mesh in zip(scene.solids or [], meshes):
        if mesh.verts:
            boxes.append((solid_spec.id, mesh.bounds()))
    if len(boxes) < 2:
        return [[b[0] for b in boxes]] if boxes else []

    span = max(
        max(hi[axis] for _sid, (_lo, hi) in boxes) - min(lo[axis] for _sid, (lo, _hi) in boxes)
        for axis in range(3)) or 1.0
    slack = span * 0.02

    parent = {sid: sid for sid, _ in boxes}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (sid_a, (lo_a, hi_a)), (sid_b, (lo_b, hi_b)) = boxes[i], boxes[j]
            if all(lo_a[axis] - slack <= hi_b[axis] and lo_b[axis] - slack <= hi_a[axis]
                   for axis in range(3)):
                parent[find(sid_a)] = find(sid_b)

    groups: dict[str, list[str]] = {}
    for sid, _box in boxes:
        groups.setdefault(find(sid), []).append(sid)
    return list(groups.values())


def render_mech(plan: FigurePlan, scene: MechScene, appearance=None) -> Figure:
    scene3d = assemble(scene, plan.kind)
    camera = Camera.named(scene.camera or _default_camera(plan))
    projected = project(scene3d, camera)
    index = TriangleIndex(projected)

    figure = Figure(label=plan.label, kind=plan.kind, title=plan.title, scene=scene.model_dump())
    draw_hidden = bool(scene.hidden_lines or plan.conventions.hidden_lines)

    edges = list(scene3d.edges) + silhouette_edges(scene3d, projected)
    seen: set[tuple[int, int]] = set()
    visible_by_owner: dict[str, list[list[Point]]] = defaultdict(list)

    for a, b, owner in edges:
        key = (a, b) if a < b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        pa, pb = projected.xy[a], projected.xy[b]
        if math.dist(pa, pb) < 1e-6:
            continue
        spans = visible_intervals(pa, pb, projected.depth[a], projected.depth[b], index,
                                  skip=(a, b))
        for t0, t1 in spans:
            segment = [_lerp(pa, pb, t0), _lerp(pa, pb, t1)]
            figure.prims.append(polyline(segment, role="outline", owner=owner, width=W_OUTLINE))
            if owner:
                visible_by_owner[owner].append(segment)
        if draw_hidden:
            for t0, t1 in _invert(spans):
                figure.prims.append(polyline([_lerp(pa, pb, t0), _lerp(pa, pb, t1)],
                                             role="hidden", owner=owner, width=W_HIDDEN,
                                             dash=DASH_HIDDEN))

    _draw_hatching(figure, scene3d, camera, index, appearance)
    if plan.kind == "exploded":
        _draw_explode_lines(figure, scene, scene3d, camera)

    if not figure.prims:
        raise MechError(f"{plan.label}: the projection produced no visible lines")

    for owner, segments in visible_by_owner.items():
        figure.anchors[owner] = _anchors_for(owner, segments)
    return figure


def _default_camera(plan: FigurePlan) -> str:
    view = (plan.view or "").lower()
    for name in ("front", "top", "right", "isometric", "dimetric", "trimetric"):
        if name in view:
            return name
    if "plan" in view:
        return "top"
    if "side" in view or "elevation" in view:
        return "right"
    return "isometric"


def _lerp(a: Point, b: Point, t: float) -> Point:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _invert(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in spans:
        if start > cursor + 1e-6:
            out.append((cursor, start))
        cursor = end
    if cursor < 1.0 - 1e-6:
        out.append((cursor, 1.0))
    return out


# 37 CFR 1.84(h)(3): the hatching of a sectional view is regularly spaced oblique parallel lines.
# Two parts cut by the same plane are told apart by giving each a different angle, which is the
# convention the rule's "different elements" sentence is about.
_HATCH_ANGLES = (45.0, 135.0, 30.0, 150.0, 60.0, 120.0, 15.0, 165.0)


def _draw_hatching(figure: Figure, scene: Scene3D, camera: Camera, index: TriangleIndex,
                   appearance=None) -> None:
    if not scene.caps:
        return
    owners: list[str] = []
    for _loop, owner in scene.caps:
        if owner not in owners:
            owners.append(owner)
    for loop, owner in scene.caps:
        if len(loop) < 3:
            continue
        flat = [camera.project(v) for v in loop]
        ring = [(p[0], p[1]) for p in flat]
        if abs(_ring_area(ring)) < 0.5:
            continue
        # The angle a part is hatched at is remembered across the drawing set, so the same part
        # sectioned in two figures is hatched the same way in both.
        angle = (appearance.hatch_angle(owner) if appearance is not None
                 else _HATCH_ANGLES[owners.index(owner) % len(_HATCH_ANGLES)])
        depth_plane = _plane(ring[0], ring[1], ring[2], flat[0][2], flat[1][2], flat[2][2])
        for start, end in geom.hatch_polygon(ring, HATCH_SPACING, angle):
            if depth_plane is None:
                figure.prims.append(polyline([start, end], role="hatch", owner=owner,
                                             width=W_HATCH))
                continue
            da = depth_plane[0] * start[0] + depth_plane[1] * start[1] + depth_plane[2]
            db = depth_plane[0] * end[0] + depth_plane[1] * end[1] + depth_plane[2]
            for t0, t1 in visible_intervals(start, end, da, db, index):
                figure.prims.append(polyline([_lerp(start, end, t0), _lerp(start, end, t1)],
                                             role="hatch", owner=owner, width=W_HATCH))


def _ring_area(ring: Sequence[Point]) -> float:
    total = 0.0
    for i in range(len(ring)):
        j = (i + 1) % len(ring)
        total += ring[i][0] * ring[j][1] - ring[j][0] * ring[i][1]
    return total / 2.0


def _draw_explode_lines(figure: Figure, scene: MechScene, scene3d: Scene3D,
                        camera: Camera) -> None:
    """The dash-dot line that says which exploded part came from where."""
    box = figure.content_bbox(include_labels=False)
    axis = (scene.explode.axis if scene.explode else "y")
    vector = camera.project({"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0),
                             "z": (0.0, 0.0, 1.0)}[axis])
    direction = geom.unit(vector[0], vector[1])
    centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    reach = math.hypot(box[2] - box[0], box[3] - box[1]) * 0.6
    figure.prims.append(polyline(
        [(centre[0] - direction[0] * reach, centre[1] - direction[1] * reach),
         (centre[0] + direction[0] * reach, centre[1] + direction[1] * reach)],
        role="centre", width=W_HIDDEN, dash=DASH_CENTRE))


# ------------------------------------------------------------------------------------ anchors


def _anchors_for(numeral: str, segments: list[list[Point]], want: int = 10) -> list[Anchor]:
    """Points on a part's own visible outline that a lead line may land on.

    A lead line has to touch the thing it indicates, so an anchor is never invented: it is a
    point that is actually on a line that was actually drawn.
    """
    points = [p for segment in segments for p in segment]
    if not points:
        return []
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    scored: list[tuple[float, Point]] = []
    for segment in segments:
        mid = ((segment[0][0] + segment[1][0]) / 2.0, (segment[0][1] + segment[1][1]) / 2.0)
        for point in (segment[0], segment[1], mid):
            scored.append((math.hypot(point[0] - cx, point[1] - cy), point))
    scored.sort(key=lambda item: -item[0])

    chosen: list[Point] = []
    spread = max(4.0, scored[0][0] * 0.35) if scored else 4.0
    for _distance, point in scored:
        if all(math.dist(point, other) > spread for other in chosen):
            chosen.append(point)
        if len(chosen) >= want:
            break
    if not chosen:
        chosen = [scored[0][1]]
    return [Anchor(numeral, point, geom.unit(point[0] - cx, point[1] - cy), 1.0)
            for point in chosen]
