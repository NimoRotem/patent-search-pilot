"""Reading a supplied mesh.

STL, OBJ, PLY and OFF, parsed here rather than through a library, for one reason: the projection,
the hidden-line removal and the plane cutting already work on ``solid.Mesh``, and anything that
lands in that type gets all of it for free. A converted mesh is sectioned and hatched by exactly
the code that sections and hatches a primitive.

Two things this does that a plain loader would not.

**It welds vertices.** An STL is a soup of unconnected triangles: every triangle carries its own
three vertices, so nothing shares an edge, and an edge that nothing shares cannot have a dihedral
angle, which means no feature edges and no silhouettes. Welding on a tolerance derived from the
model's own size is what turns the soup back into a surface.

**It decides which edges are drawn.** A mesh has no idea which of its edges are real. An edge
between two nearly coplanar triangles is a tessellation artefact and drawing it produces the
faceted mush that makes converted CAD look converted; an edge at a sharp fold is the outline of
the part. The dihedral angle separates them, and everything below the threshold is handed to the
silhouette test instead, which decides per view.

STEP and IGES are not read. They need a geometry kernel this host does not have, and saying so is
better than a loader that half works: export STL from the CAD system instead.
"""
from __future__ import annotations

import math
import re
import struct
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

from ..render.solid import Mesh, V3

# Two triangles meeting at less than this are the same surface tessellated, not an edge.
FEATURE_ANGLE = 24.0
# Vertices closer than this fraction of the model's diagonal are the same vertex.
WELD_FRACTION = 1e-5
MAX_TRIANGLES = int(__import__("os").environ.get("FM_MAX_MESH_TRIANGLES", "120000"))
# What a projection can actually be drawn from in reasonable time. Bigger meshes are decimated by
# dropping the smallest triangles, which changes the silhouette by less than a line width.
PROJECTION_TRIANGLES = int(__import__("os").environ.get("FM_PROJECT_TRIANGLES", "24000"))

_UNSUPPORTED = {".step", ".stp", ".iges", ".igs", ".sldprt", ".ipt", ".catpart", ".x_t", ".3dm"}


class MeshError(RuntimeError):
    """The mesh could not be read. Names the file and the reason."""


# ------------------------------------------------------------------------------------ parsing


def probe(filename: str, blob: bytes) -> dict[str, Any]:
    """What is in a mesh file, without building anything. Used at upload time."""
    suffix = Path(filename).suffix.lower()
    if suffix in _UNSUPPORTED:
        raise MeshError(
            f"{filename}: {suffix} needs a CAD kernel this host does not have. Export the part "
            "as STL, OBJ or PLY from the CAD system and upload that.")
    verts, tris = _parse(filename, blob)
    if not tris:
        raise MeshError(f"{filename}: no triangles.")
    lo, hi = _bounds(verts)
    return {
        "triangles": len(tris),
        "vertices": len(verts),
        "size_mm": [round(hi[i] - lo[i], 3) for i in range(3)],
        "format": suffix.lstrip("."),
    }


def load(filename: str, blob: bytes, *, owner: str = "", sid: str = "",
         target_size_mm: float = 120.0) -> Mesh:
    """A supplied mesh, welded, edge-detected, and scaled to something a sheet can hold."""
    verts, tris = _parse(filename, blob)
    if not tris:
        raise MeshError(f"{filename}: no triangles.")
    if len(tris) > MAX_TRIANGLES:
        raise MeshError(
            f"{filename}: {len(tris):,} triangles, above the {MAX_TRIANGLES:,} this reads. "
            "Export it with a coarser tessellation; a patent drawing needs the silhouette, not "
            "the surface finish.")

    verts, tris = _weld(verts, tris)
    tris = _drop_degenerate(verts, tris)
    if len(tris) > PROJECTION_TRIANGLES:
        tris = _decimate(verts, tris, PROJECTION_TRIANGLES)
    verts = _normalise(verts, target_size_mm)
    edges, smooth = _classify_edges(verts, tris)
    return Mesh(verts=verts, tris=tris, edges=edges, smooth=smooth, owner=owner,
                sid=sid or Path(filename).stem[:40])


def _parse(filename: str, blob: bytes) -> tuple[list[V3], list[tuple[int, int, int]]]:
    suffix = Path(filename).suffix.lower()
    if suffix in _UNSUPPORTED:
        raise MeshError(f"{filename}: {suffix} needs a CAD kernel this host does not have. "
                        "Export STL, OBJ or PLY instead.")
    if suffix == ".stl" or blob[:5].lower() == b"solid" or _looks_binary_stl(blob):
        return _stl(blob)
    if suffix == ".obj":
        return _obj(blob)
    if suffix == ".ply":
        return _ply(blob)
    if suffix == ".off":
        return _off(blob)
    raise MeshError(f"{filename}: not a mesh format this reads (STL, OBJ, PLY, OFF).")


def _looks_binary_stl(blob: bytes) -> bool:
    if len(blob) < 84:
        return False
    count = struct.unpack("<I", blob[80:84])[0]
    return len(blob) == 84 + count * 50


def _stl(blob: bytes) -> tuple[list[V3], list[tuple[int, int, int]]]:
    if _looks_binary_stl(blob):
        count = struct.unpack("<I", blob[80:84])[0]
        verts: list[V3] = []
        tris: list[tuple[int, int, int]] = []
        offset = 84
        for _ in range(count):
            values = struct.unpack_from("<12fH", blob, offset)
            offset += 50
            base = len(verts)
            verts.extend(((values[3], values[4], values[5]),
                          (values[6], values[7], values[8]),
                          (values[9], values[10], values[11])))
            tris.append((base, base + 1, base + 2))
        return verts, tris

    text = blob.decode("utf-8", "replace")
    numbers = re.findall(r"vertex\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)", text)
    if not numbers:
        raise MeshError("this ASCII STL has no vertex lines.")
    verts = [(float(a), float(b), float(c)) for a, b, c in numbers]
    tris = [(i, i + 1, i + 2) for i in range(0, len(verts) - 2, 3)]
    return verts, tris


def _obj(blob: bytes) -> tuple[list[V3], list[tuple[int, int, int]]]:
    verts: list[V3] = []
    tris: list[tuple[int, int, int]] = []
    for line in blob.decode("utf-8", "replace").splitlines():
        if line.startswith("v "):
            parts = line.split()
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif line.startswith("f "):
            # A face may be a polygon and may carry texture and normal indices; both are ignored.
            indices = []
            for token in line.split()[1:]:
                raw = token.split("/")[0]
                if not raw:
                    continue
                value = int(raw)
                indices.append(value - 1 if value > 0 else len(verts) + value)
            for k in range(1, len(indices) - 1):
                tris.append((indices[0], indices[k], indices[k + 1]))
    return verts, tris


def _ply(blob: bytes) -> tuple[list[V3], list[tuple[int, int, int]]]:
    text = blob.decode("utf-8", "replace")
    head, _, body = text.partition("end_header")
    if "format ascii" not in head:
        raise MeshError("only ASCII PLY is read. Re-export as ASCII, or as STL.")
    vertex_count = int(re.search(r"element vertex (\d+)", head).group(1))
    face_count = int(re.search(r"element face (\d+)", head).group(1))
    lines = [ln for ln in body.splitlines() if ln.strip()]
    verts = [tuple(float(v) for v in lines[i].split()[:3]) for i in range(vertex_count)]
    tris: list[tuple[int, int, int]] = []
    for line in lines[vertex_count:vertex_count + face_count]:
        parts = [int(v) for v in line.split()]
        for k in range(1, parts[0] - 1):
            tris.append((parts[1], parts[1 + k], parts[2 + k]))
    return verts, tris  # type: ignore[return-value]


def _off(blob: bytes) -> tuple[list[V3], list[tuple[int, int, int]]]:
    lines = [ln for ln in blob.decode("utf-8", "replace").splitlines()
             if ln.strip() and not ln.startswith("#")]
    if not lines or not lines[0].upper().startswith("OFF"):
        raise MeshError("this does not begin with OFF.")
    header = lines[1].split() if lines[0].strip().upper() == "OFF" else lines[0][3:].split()
    start = 2 if lines[0].strip().upper() == "OFF" else 1
    vertex_count, face_count = int(header[0]), int(header[1])
    verts = [tuple(float(v) for v in lines[start + i].split()[:3]) for i in range(vertex_count)]
    tris: list[tuple[int, int, int]] = []
    for line in lines[start + vertex_count:start + vertex_count + face_count]:
        parts = [int(v) for v in line.split()]
        for k in range(1, parts[0] - 1):
            tris.append((parts[1], parts[1 + k], parts[2 + k]))
    return verts, tris  # type: ignore[return-value]


# ------------------------------------------------------------------------------ conditioning


def _bounds(verts: Sequence[V3]) -> tuple[V3, V3]:
    lo = (min(v[0] for v in verts), min(v[1] for v in verts), min(v[2] for v in verts))
    hi = (max(v[0] for v in verts), max(v[1] for v in verts), max(v[2] for v in verts))
    return lo, hi


def _weld(verts: list[V3], tris: list[tuple[int, int, int]]
          ) -> tuple[list[V3], list[tuple[int, int, int]]]:
    """Merge coincident vertices, which is what makes an STL a surface rather than a soup."""
    lo, hi = _bounds(verts)
    diagonal = math.dist(lo, hi) or 1.0
    grid = max(diagonal * WELD_FRACTION, 1e-9)

    index: dict[tuple[int, int, int], int] = {}
    out: list[V3] = []
    remap: list[int] = []
    for v in verts:
        key = (round(v[0] / grid), round(v[1] / grid), round(v[2] / grid))
        found = index.get(key)
        if found is None:
            found = len(out)
            index[key] = found
            out.append(v)
        remap.append(found)
    return out, [(remap[a], remap[b], remap[c]) for a, b, c in tris]


def _drop_degenerate(verts: Sequence[V3], tris: Sequence[tuple[int, int, int]]
                     ) -> list[tuple[int, int, int]]:
    return [t for t in tris if t[0] != t[1] and t[1] != t[2] and t[0] != t[2]
            and _area(verts[t[0]], verts[t[1]], verts[t[2]]) > 0.0]


def _area(a: V3, b: V3, c: V3) -> float:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)


def _decimate(verts: Sequence[V3], tris: Sequence[tuple[int, int, int]],
              keep: int) -> list[tuple[int, int, int]]:
    """Keep the largest triangles.

    Crude on purpose. A patent drawing needs the silhouette and the sharp edges; the small
    triangles that go are the ones that were describing surface curvature at a scale finer than a
    line width. Anything cleverer would be a mesh simplifier, which is a project, not a step.
    """
    ranked = sorted(tris, key=lambda t: -_area(verts[t[0]], verts[t[1]], verts[t[2]]))
    return ranked[:keep]


def _normalise(verts: list[V3], target: float) -> list[V3]:
    """Centre on the origin and scale the longest axis to ``target`` millimetres.

    Real CAD arrives in millimetres, inches, or whatever the exporter felt like, and at any
    origin. The drawing does not care: 37 CFR 1.84(k) says the scale must be large enough to be
    legible after reduction, not that it must be any particular number.
    """
    lo, hi = _bounds(verts)
    span = max(hi[i] - lo[i] for i in range(3)) or 1.0
    factor = target / span
    centre = tuple((lo[i] + hi[i]) / 2.0 for i in range(3))
    return [((v[0] - centre[0]) * factor, (v[1] - centre[1]) * factor,
             (v[2] - centre[2]) * factor) for v in verts]


def _classify_edges(verts: Sequence[V3], tris: Sequence[tuple[int, int, int]]
                    ) -> tuple[list[tuple[int, int]], set[tuple[int, int]]]:
    """Which edges are drawn, and which are left to the silhouette test.

    An edge between two nearly coplanar triangles is tessellation and drawing it is what makes
    converted CAD look converted. An edge at a fold is the part's outline. A boundary edge, with
    only one triangle, is always drawn: it is a real border or a hole in the mesh, and either way
    the reader should see it.
    """
    faces: dict[tuple[int, int], list[V3]] = defaultdict(list)
    for a, b, c in tris:
        normal = _normal(verts[a], verts[b], verts[c])
        for u, v in ((a, b), (b, c), (c, a)):
            faces[(u, v) if u < v else (v, u)].append(normal)

    edges: list[tuple[int, int]] = []
    smooth: set[tuple[int, int]] = set()
    limit = math.cos(math.radians(FEATURE_ANGLE))
    for key, normals in faces.items():
        if len(normals) == 1:
            edges.append(key)
            continue
        if len(normals) > 2:
            edges.append(key)          # non-manifold: err towards showing it
            continue
        dot = max(-1.0, min(1.0, sum(normals[0][i] * normals[1][i] for i in range(3))))
        if dot < limit:
            edges.append(key)
        else:
            smooth.add(key)
    return edges, smooth


def _normal(a: V3, b: V3, c: V3) -> V3:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / length, ny / length, nz / length)


# ---------------------------------------------------------------------------------- grouping


def split_components(mesh: Mesh, limit: int = 24) -> list[Mesh]:
    """Break a mesh into its connected pieces.

    An assembly exported as one STL is several parts in one file, and a part is what a reference
    numeral points at. Splitting on connectivity recovers them, which is what lets a numeral be
    attached to a component rather than to the whole model. Pieces come back largest first, so
    the ones that matter are the ones a caller keeps.
    """
    parent = list(range(len(mesh.verts)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for a, b, c in mesh.tris:
        for u, v in ((a, b), (b, c)):
            ru, rv = find(u), find(v)
            if ru != rv:
                parent[ru] = rv

    groups: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for tri in mesh.tris:
        groups[find(tri[0])].append(tri)

    out: list[Mesh] = []
    for root, tris in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:limit]:
        used = sorted({i for tri in tris for i in tri})
        remap = {old: new for new, old in enumerate(used)}
        piece = Mesh(
            verts=[mesh.verts[i] for i in used],
            tris=[(remap[a], remap[b], remap[c]) for a, b, c in tris],
            edges=[(remap[a], remap[b]) for a, b in mesh.edges
                   if a in remap and b in remap],
            smooth={(remap[a], remap[b]) for a, b in mesh.smooth
                    if a in remap and b in remap},
            owner=mesh.owner, sid=f"{mesh.sid}-{len(out)}")
        out.append(piece)
    return out
