"""The primitive library, as meshes.

The model is not allowed to write geometry. It picks a part by name from this list and gives it
numbers, and this module builds the mesh. That is what makes a mechanical figure repeatable: the
same scene gives the same triangles every time, a housing looks like a housing in figure 1 and in
figure 4, and a part that cannot be built is a schema error rather than a strange picture.

Every mesh carries three things: triangles, which are what occludes; feature edges, which are
what gets drawn; and an owner, which is the reference numeral the part belongs to. Curved
surfaces contribute no feature edges around their circumference, because the line you see there
is the silhouette, and the silhouette depends on where you are standing. It is worked out at
projection time in ``mech``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

V3 = tuple[float, float, float]

CIRCLE_SEGMENTS = 40
SMALL_CIRCLE_SEGMENTS = 20
SHARP_DEGREES = 22.0        # a profile corner sharper than this is drawn as an edge


@dataclass
class Mesh:
    verts: list[V3] = field(default_factory=list)
    tris: list[tuple[int, int, int]] = field(default_factory=list)
    edges: list[tuple[int, int]] = field(default_factory=list)
    smooth: set[tuple[int, int]] = field(default_factory=set)   # edges on a curved surface
    owner: str = ""
    sid: str = ""

    def transformed(self, matrix: "Matrix") -> "Mesh":
        return Mesh(verts=[matrix.apply(v) for v in self.verts], tris=list(self.tris),
                    edges=list(self.edges), smooth=set(self.smooth), owner=self.owner,
                    sid=self.sid)

    def translated(self, dx: float, dy: float, dz: float) -> "Mesh":
        return Mesh(verts=[(v[0] + dx, v[1] + dy, v[2] + dz) for v in self.verts],
                    tris=list(self.tris), edges=list(self.edges), smooth=set(self.smooth),
                    owner=self.owner, sid=self.sid)

    def bounds(self) -> tuple[V3, V3]:
        if not self.verts:
            return ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        zs = [v[2] for v in self.verts]
        return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))


@dataclass
class Matrix:
    """A 3x4 affine transform. Rotations are degrees about X, then Y, then Z."""
    rows: tuple[tuple[float, float, float, float], ...]

    @staticmethod
    def identity() -> "Matrix":
        return Matrix(((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0)))

    @staticmethod
    def from_euler(rx: float, ry: float, rz: float, at: Sequence[float]) -> "Matrix":
        ax, ay, az = (math.radians(rx), math.radians(ry), math.radians(rz))
        cx, sx = math.cos(ax), math.sin(ax)
        cy, sy = math.cos(ay), math.sin(ay)
        cz, sz = math.cos(az), math.sin(az)
        # Rz * Ry * Rx
        m00 = cz * cy
        m01 = cz * sy * sx - sz * cx
        m02 = cz * sy * cx + sz * sx
        m10 = sz * cy
        m11 = sz * sy * sx + cz * cx
        m12 = sz * sy * cx - cz * sx
        m20 = -sy
        m21 = cy * sx
        m22 = cy * cx
        tx, ty, tz = (list(at) + [0.0, 0.0, 0.0])[:3]
        return Matrix(((m00, m01, m02, tx), (m10, m11, m12, ty), (m20, m21, m22, tz)))

    def apply(self, v: V3) -> V3:
        r = self.rows
        return (r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2] + r[0][3],
                r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2] + r[1][3],
                r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2] + r[2][3])


# ------------------------------------------------------------------------------ mesh builders


def _ring(r: float, y: float, segments: int, phase: float = 0.0) -> list[V3]:
    return [(r * math.cos(2 * math.pi * i / segments + phase), y,
             r * math.sin(2 * math.pi * i / segments + phase)) for i in range(segments)]


def _corner_angle(prev: tuple[float, float], here: tuple[float, float],
                  nxt: tuple[float, float]) -> float:
    ax, ay = here[0] - prev[0], here[1] - prev[1]
    bx, by = nxt[0] - here[0], nxt[1] - here[1]
    la, lb = math.hypot(ax, ay), math.hypot(bx, by)
    if la < 1e-9 or lb < 1e-9:
        return 180.0
    cos = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
    return math.degrees(math.acos(cos))


def extrude(profile: Sequence[tuple[float, float]], height: float, *, owner: str = "",
            sid: str = "", closed: bool = True) -> Mesh:
    """Extrude a closed 2D profile in the XZ plane along Y, centred on y=0.

    The profile is taken as (x, z) pairs, counter-clockwise seen from +Y. Which of its corners
    are sharp decides which vertical edges get drawn, so a 40-sided circle extrudes into a
    cylinder with no visible facets and a hexagon extrudes into a nut with six.
    """
    n = len(profile)
    if n < 3:
        return Mesh(owner=owner, sid=sid)
    half = height / 2.0
    verts: list[V3] = [(p[0], -half, p[1]) for p in profile] + \
                      [(p[0], half, p[1]) for p in profile]
    tris: list[tuple[int, int, int]] = []
    edges: list[tuple[int, int]] = []
    smooth: set[tuple[int, int]] = set()

    for i in range(n):
        j = (i + 1) % n
        b0, b1, t0, t1 = i, j, i + n, j + n
        tris.append((b0, b1, t1))
        tris.append((b0, t1, t0))
        edges.append((b0, b1))
        edges.append((t0, t1))
        angle = _corner_angle(profile[(i - 1) % n], profile[i], profile[j])
        if angle >= SHARP_DEGREES:
            edges.append((b0, t0))
        else:
            smooth.add(_key(b0, t0))

    if closed:
        centre_b = len(verts)
        centre_t = centre_b + 1
        cx = sum(p[0] for p in profile) / n
        cz = sum(p[1] for p in profile) / n
        verts.append((cx, -half, cz))
        verts.append((cx, half, cz))
        for i in range(n):
            j = (i + 1) % n
            tris.append((centre_b, j, i))          # bottom, facing -Y
            tris.append((centre_t, i + n, j + n))  # top, facing +Y
    return Mesh(verts=verts, tris=tris, edges=_dedupe(edges), smooth=smooth, owner=owner, sid=sid)


def revolve(profile: Sequence[tuple[float, float]], segments: int = CIRCLE_SEGMENTS, *,
            owner: str = "", sid: str = "", close_profile: bool = False) -> Mesh:
    """Revolve a (radius, y) profile about the Y axis.

    A profile point at radius zero is a pole and is collapsed to a single vertex, which is what
    keeps a sphere from having a fan of degenerate triangles at its top.
    """
    pts = list(profile)
    if len(pts) < 2:
        return Mesh(owner=owner, sid=sid)
    verts: list[V3] = []
    ring_index: list[list[int]] = []
    for r, y in pts:
        if abs(r) < 1e-9:
            verts.append((0.0, y, 0.0))
            ring_index.append([len(verts) - 1] * segments)
        else:
            start = len(verts)
            verts.extend(_ring(r, y, segments))
            ring_index.append([start + i for i in range(segments)])

    tris: list[tuple[int, int, int]] = []
    edges: list[tuple[int, int]] = []
    smooth: set[tuple[int, int]] = set()
    rows = len(pts)
    for row in range(rows - 1):
        lower, upper = ring_index[row], ring_index[row + 1]
        for i in range(segments):
            j = (i + 1) % segments
            a, b, c, d = lower[i], lower[j], upper[j], upper[i]
            # A pole collapses one side of the quad, leaving a single triangle.
            if a != b:
                tris.append((a, b, c))
            if c != d:
                tris.append((a, c, d))
            # A meridian is only ever a silhouette, never a drawn edge.
            if a != d:
                smooth.add(_key(a, d))
    # Rings where the profile turns a corner are real edges, and so are its two ends.
    for row, (r, _y) in enumerate(pts):
        if abs(r) < 1e-9:
            continue
        sharp = row in (0, rows - 1)
        if not sharp and 0 < row < rows - 1:
            sharp = _corner_angle(pts[row - 1], pts[row], pts[row + 1]) >= SHARP_DEGREES
        if not sharp:
            for i in range(segments):
                smooth.add(_key(ring_index[row][i], ring_index[row][(i + 1) % segments]))
            continue
        for i in range(segments):
            edges.append((ring_index[row][i], ring_index[row][(i + 1) % segments]))

    if close_profile:
        first, last = ring_index[0], ring_index[-1]
        if first[0] != last[0] and abs(pts[0][0]) > 1e-9 and abs(pts[-1][0]) > 1e-9:
            for i in range(segments):
                j = (i + 1) % segments
                tris.append((last[i], last[j], first[j]))
                tris.append((last[i], first[j], first[i]))
    return Mesh(verts=verts, tris=tris, edges=_dedupe(edges), smooth=smooth, owner=owner, sid=sid)


def _key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _dedupe(edges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int]] = []
    for a, b in edges:
        if a == b:
            continue
        key = _key(a, b)
        if key in seen:
            continue
        seen.add(key)
        out.append((a, b))
    return out


def merge(meshes: Sequence[Mesh], owner: str = "", sid: str = "") -> Mesh:
    out = Mesh(owner=owner, sid=sid)
    for mesh in meshes:
        offset = len(out.verts)
        out.verts.extend(mesh.verts)
        out.tris.extend((a + offset, b + offset, c + offset) for a, b, c in mesh.tris)
        out.edges.extend((a + offset, b + offset) for a, b in mesh.edges)
        out.smooth.update(_key(a + offset, b + offset) for a, b in mesh.smooth)
    return out


# --------------------------------------------------------------------------------- profiles


def circle_profile(r: float, segments: int = CIRCLE_SEGMENTS) -> list[tuple[float, float]]:
    return [(r * math.cos(2 * math.pi * i / segments), r * math.sin(2 * math.pi * i / segments))
            for i in range(segments)]


def polygon_profile(r: float, sides: int) -> list[tuple[float, float]]:
    sides = max(3, min(24, int(sides)))
    phase = math.pi / sides
    return [(r * math.cos(2 * math.pi * i / sides + phase),
             r * math.sin(2 * math.pi * i / sides + phase)) for i in range(sides)]


def rect_profile(w: float, d: float) -> list[tuple[float, float]]:
    return [(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]


def ring_profile(r: float, ri: float, segments: int = CIRCLE_SEGMENTS) -> list[list]:
    return [circle_profile(r, segments), circle_profile(max(0.01, ri), segments)]


def gear_profile(r: float, teeth: int) -> list[tuple[float, float]]:
    teeth = max(6, min(48, int(teeth)))
    root = r * 0.86
    out: list[tuple[float, float]] = []
    for i in range(teeth):
        base = 2 * math.pi * i / teeth
        step = 2 * math.pi / teeth
        for frac, radius in ((0.00, root), (0.14, r), (0.36, r), (0.50, root)):
            angle = base + step * frac
            out.append((radius * math.cos(angle), radius * math.sin(angle)))
    return out


# ------------------------------------------------------------------------------- the library
#
# Each builder takes the clamped parameter dict and returns a mesh centred on its own origin.


def _p(params: dict, name: str, default: float, low: float = 0.05,
       high: float = 600.0) -> float:
    try:
        value = float(params.get(name, default))
    except (TypeError, ValueError):
        value = default
    if value <= 0 and default > 0:
        value = default
    return max(low, min(high, value))


def _tube_mesh(r: float, ri: float, h: float, owner: str, sid: str, segments: int) -> Mesh:
    """A tube is two extrusions plus two annular caps, so the bore is a real hole."""
    ri = max(0.02, min(ri, r * 0.98))
    outer = circle_profile(r, segments)
    inner = circle_profile(ri, segments)
    half = h / 2.0
    verts: list[V3] = []
    verts += [(p[0], -half, p[1]) for p in outer]
    verts += [(p[0], half, p[1]) for p in outer]
    verts += [(p[0], -half, p[1]) for p in inner]
    verts += [(p[0], half, p[1]) for p in inner]
    n = segments
    ob, ot, ib, it = 0, n, 2 * n, 3 * n
    tris: list[tuple[int, int, int]] = []
    edges: list[tuple[int, int]] = []
    smooth: set[tuple[int, int]] = set()
    for i in range(n):
        j = (i + 1) % n
        tris += [(ob + i, ob + j, ot + j), (ob + i, ot + j, ot + i)]          # outer wall
        tris += [(ib + j, ib + i, it + i), (ib + j, it + i, it + j)]          # bore, inward
        tris += [(ob + i, ib + i, ib + j), (ob + i, ib + j, ob + j)]          # bottom annulus
        tris += [(ot + j, it + j, it + i), (ot + j, it + i, ot + i)]          # top annulus
        edges += [(ob + i, ob + j), (ot + i, ot + j), (ib + i, ib + j), (it + i, it + j)]
        smooth.add(_key(ob + i, ot + i))
        smooth.add(_key(ib + i, it + i))
    return Mesh(verts=verts, tris=tris, edges=_dedupe(edges), smooth=smooth, owner=owner, sid=sid)


def _shell_mesh(w: float, h: float, d: float, t: float, owner: str, sid: str) -> Mesh:
    """A box hollowed out and opened at +Y: four walls and a floor."""
    t = max(0.4, min(t, min(w, d) / 2.2, h / 1.5))
    hw, hh, hd = w / 2.0, h / 2.0, d / 2.0
    iw, idp = hw - t, hd - t
    floor_top = -hh + t
    pieces = [
        _box_mesh(w, t, d, owner, sid, offset=(0.0, -hh + t / 2.0, 0.0)),
        _box_mesh(t, h - t, d, owner, sid, offset=(-hw + t / 2.0, floor_top + (h - t) / 2.0, 0.0)),
        _box_mesh(t, h - t, d, owner, sid, offset=(hw - t / 2.0, floor_top + (h - t) / 2.0, 0.0)),
        _box_mesh(w - 2 * t, h - t, t, owner, sid,
                  offset=(0.0, floor_top + (h - t) / 2.0, -hd + t / 2.0)),
        _box_mesh(w - 2 * t, h - t, t, owner, sid,
                  offset=(0.0, floor_top + (h - t) / 2.0, hd - t / 2.0)),
    ]
    _ = (iw, idp)
    return merge(pieces, owner=owner, sid=sid)


def _box_mesh(w: float, h: float, d: float, owner: str, sid: str,
              offset: V3 = (0.0, 0.0, 0.0)) -> Mesh:
    mesh = extrude(rect_profile(w, d), h, owner=owner, sid=sid)
    if offset != (0.0, 0.0, 0.0):
        mesh = mesh.translated(*offset)
    return mesh


def _bracket_mesh(w: float, h: float, d: float, t: float, owner: str, sid: str) -> Mesh:
    t = max(0.6, min(t, h / 2.0, d / 2.0))
    base = _box_mesh(w, t, d, owner, sid, offset=(0.0, -h / 2.0 + t / 2.0, 0.0))
    wall = _box_mesh(w, h - t, t, owner, sid,
                     offset=(0.0, -h / 2.0 + t + (h - t) / 2.0, -d / 2.0 + t / 2.0))
    return merge([base, wall], owner=owner, sid=sid)


def _spring_mesh(r: float, h: float, turns: float, wire: float, owner: str, sid: str) -> Mesh:
    """A helix swept as a thin square section: enough to read as a spring, cheap to occlude."""
    turns = max(2.0, min(24.0, turns))
    wire = max(0.3, min(wire, r / 2.0))
    steps = int(max(24, min(360, turns * 16)))
    profile: list[tuple[float, float]] = []
    verts: list[V3] = []
    tris: list[tuple[int, int, int]] = []
    edges: list[tuple[int, int]] = []
    rings: list[list[int]] = []
    for step in range(steps + 1):
        frac = step / steps
        angle = 2 * math.pi * turns * frac
        y = -h / 2.0 + h * frac
        cx, cz = r * math.cos(angle), r * math.sin(angle)
        nx, nz = math.cos(angle), math.sin(angle)
        ring = []
        for dx, dy in ((-wire / 2, -wire / 2), (wire / 2, -wire / 2),
                       (wire / 2, wire / 2), (-wire / 2, wire / 2)):
            ring.append(len(verts))
            verts.append((cx + nx * dx, y + dy, cz + nz * dx))
        rings.append(ring)
    for step in range(steps):
        a, b = rings[step], rings[step + 1]
        for i in range(4):
            j = (i + 1) % 4
            tris += [(a[i], a[j], b[j]), (a[i], b[j], b[i])]
            edges.append((a[i], b[i]))
    _ = profile
    return Mesh(verts=verts, tris=tris, edges=_dedupe(edges), owner=owner, sid=sid)


def _handle_mesh(r: float, length: float, owner: str, sid: str) -> Mesh:
    """A U-shaped grip: two legs down and a bar across, opening towards -Y."""
    leg = max(r * 2.0, length * 0.4)
    bar = extrude(circle_profile(r, SMALL_CIRCLE_SEGMENTS), length, owner=owner, sid=sid)
    bar = _rotate_z(bar, 90.0)
    left = extrude(circle_profile(r, SMALL_CIRCLE_SEGMENTS), leg, owner=owner, sid=sid)
    left = left.translated(-length / 2.0, -leg / 2.0, 0.0)
    right = extrude(circle_profile(r, SMALL_CIRCLE_SEGMENTS), leg, owner=owner, sid=sid)
    right = right.translated(length / 2.0, -leg / 2.0, 0.0)
    return merge([bar, left, right], owner=owner, sid=sid)


def _rotate_z(mesh: Mesh, degrees: float) -> Mesh:
    return mesh.transformed(Matrix.from_euler(0.0, 0.0, degrees, (0.0, 0.0, 0.0)))


def _rotate_x(mesh: Mesh, degrees: float) -> Mesh:
    return mesh.transformed(Matrix.from_euler(degrees, 0.0, 0.0, (0.0, 0.0, 0.0)))


def build(part: str, params: dict, owner: str = "", sid: str = "") -> Mesh:
    """One named part, at its own origin. An unknown name becomes a box, never nothing."""
    part = (part or "box").lower()
    seg = CIRCLE_SEGMENTS

    if part in ("box",):
        return _box_mesh(_p(params, "w", 40), _p(params, "h", 20), _p(params, "d", 30),
                         owner, sid)
    if part == "plate":
        return _box_mesh(_p(params, "w", 60), _p(params, "t", 3), _p(params, "d", 40),
                         owner, sid)
    if part == "pcb":
        return _box_mesh(_p(params, "w", 60), _p(params, "t", 1.6), _p(params, "d", 40),
                         owner, sid)
    if part in ("housing",):
        return _shell_mesh(_p(params, "w", 60), _p(params, "h", 30), _p(params, "d", 45),
                           _p(params, "t", 3), owner, sid)
    if part in ("cylinder", "rod", "shaft"):
        return extrude(circle_profile(_p(params, "r", 10), seg), _p(params, "h", 30),
                       owner=owner, sid=sid)
    if part in ("disc", "wheel"):
        return extrude(circle_profile(_p(params, "r", 20), seg), _p(params, "t", 4),
                       owner=owner, sid=sid)
    if part in ("tube", "bearing"):
        return _tube_mesh(_p(params, "r", 14), _p(params, "ri", 8), _p(params, "h", 12),
                          owner, sid, seg)
    if part == "washer":
        return _tube_mesh(_p(params, "r", 10), _p(params, "ri", 5), _p(params, "t", 1.5),
                          owner, sid, seg)
    if part == "cone":
        r1, r2 = _p(params, "r", 15), _p(params, "r2", 0.0, low=0.0)
        h = _p(params, "h", 25)
        return revolve([(0.0, -h / 2), (r1, -h / 2), (max(0.0, r2), h / 2), (0.0, h / 2)],
                       seg, owner=owner, sid=sid)
    if part in ("nozzle",):
        r1, r2 = _p(params, "r", 8), _p(params, "r2", 3)
        h = _p(params, "h", 18)
        return revolve([(0.0, -h / 2), (r1, -h / 2), (r2, h / 2), (0.0, h / 2)], seg,
                       owner=owner, sid=sid)
    if part == "sphere":
        r = _p(params, "r", 15)
        rows = 18
        profile = [(r * math.sin(math.pi * i / rows), -r * math.cos(math.pi * i / rows))
                   for i in range(rows + 1)]
        return revolve(profile, seg, owner=owner, sid=sid)
    if part == "dome":
        r = _p(params, "r", 15)
        rows = 10
        profile = [(r * math.cos(math.pi / 2 * i / rows), r * math.sin(math.pi / 2 * i / rows))
                   for i in range(rows + 1)]
        profile = [(0.0, 0.0)] + list(reversed([(x, y) for x, y in profile]))
        profile = sorted(profile, key=lambda p: p[1])
        return revolve(profile, seg, owner=owner, sid=sid)
    if part == "torus":
        big, small = _p(params, "R", 20), _p(params, "r", 5)
        rows = 20
        profile = [(big + small * math.cos(2 * math.pi * i / rows),
                    small * math.sin(2 * math.pi * i / rows)) for i in range(rows + 1)]
        return revolve(profile, seg, owner=owner, sid=sid, close_profile=True)
    if part == "prism":
        return extrude(polygon_profile(_p(params, "r", 15), int(_p(params, "n", 6, low=3,
                                                                  high=24))),
                       _p(params, "h", 25), owner=owner, sid=sid)
    if part == "nut":
        return extrude(polygon_profile(_p(params, "r", 6), 6), _p(params, "h", 4),
                       owner=owner, sid=sid)
    if part == "wedge":
        w, h, d = _p(params, "w", 30), _p(params, "h", 20), _p(params, "d", 30)
        return extrude([(-w / 2, -d / 2), (w / 2, -d / 2), (-w / 2, d / 2)], h,
                       owner=owner, sid=sid)
    if part in ("gear", "pulley"):
        r, h = _p(params, "r", 20), _p(params, "h", 6)
        if part == "pulley":
            return _tube_mesh(r, r * 0.25, h, owner, sid, seg)
        return extrude(gear_profile(r, int(_p(params, "teeth", 16, low=6, high=48))), h,
                       owner=owner, sid=sid)
    if part == "screw":
        r, h = _p(params, "r", 2.5), _p(params, "h", 16)
        hr, hh = _p(params, "head_r", r * 2.0), _p(params, "head_h", r * 1.2)
        shank = extrude(circle_profile(r, SMALL_CIRCLE_SEGMENTS), h, owner=owner, sid=sid)
        head = extrude(circle_profile(hr, SMALL_CIRCLE_SEGMENTS), hh, owner=owner, sid=sid)
        return merge([shank, head.translated(0.0, h / 2.0 + hh / 2.0, 0.0)], owner=owner, sid=sid)
    if part == "spring":
        return _spring_mesh(_p(params, "r", 10), _p(params, "h", 30),
                            _p(params, "turns", 6, low=2, high=24),
                            _p(params, "wire", 1.6), owner, sid)
    if part == "flange":
        r, ri, t = _p(params, "r", 30), _p(params, "ri", 12), _p(params, "t", 5)
        body = _tube_mesh(r, ri, t, owner, sid, seg)
        holes = int(_p(params, "bolts", 4, low=0, high=12))
        bolt_r = _p(params, "bolt_r", 2.5)
        pieces = [body]
        for i in range(holes):
            angle = 2 * math.pi * i / max(1, holes)
            at = ((r + ri) / 2.0 * math.cos(angle), 0.0, (r + ri) / 2.0 * math.sin(angle))
            pieces.append(extrude(circle_profile(bolt_r, SMALL_CIRCLE_SEGMENTS), t * 1.4,
                                  owner=owner, sid=sid).translated(*at))
        return merge(pieces, owner=owner, sid=sid)
    if part == "bracket":
        return _bracket_mesh(_p(params, "w", 40), _p(params, "h", 30), _p(params, "d", 30),
                             _p(params, "t", 3), owner, sid)
    if part == "connector":
        w, h, d = _p(params, "w", 20), _p(params, "h", 8), _p(params, "d", 10)
        pins = int(_p(params, "pins", 4, low=0, high=16))
        pieces = [_box_mesh(w, h, d, owner, sid)]
        for i in range(pins):
            x = -w / 2.0 + w * (i + 0.5) / max(1, pins)
            pieces.append(_box_mesh(w / (pins * 3.0), h * 0.5, d * 0.3, owner, sid,
                                    offset=(x, -h * 0.6, 0.0)))
        return merge(pieces, owner=owner, sid=sid)
    if part in ("button", "knob"):
        r, h = _p(params, "r", 6), _p(params, "h", 4)
        return extrude(circle_profile(r, SMALL_CIRCLE_SEGMENTS), h, owner=owner, sid=sid)
    if part == "handle":
        return _handle_mesh(_p(params, "r", 4), _p(params, "len", 50), owner, sid)
    if part == "lever":
        return _box_mesh(_p(params, "len", 50), _p(params, "t", 4), _p(params, "w", 8),
                         owner, sid)
    if part == "hose":
        r, length = _p(params, "r", 6), _p(params, "len", 60)
        mesh = extrude(circle_profile(r, SMALL_CIRCLE_SEGMENTS), length, owner=owner, sid=sid)
        return _rotate_z(mesh, 90.0)
    if part == "bellows":
        r, h = _p(params, "r", 12), _p(params, "h", 30)
        folds = int(_p(params, "folds", 5, low=2, high=14))
        profile: list[tuple[float, float]] = [(0.0, -h / 2)]
        for i in range(folds * 2 + 1):
            y = -h / 2 + h * i / (folds * 2)
            profile.append((r if i % 2 == 0 else r * 0.72, y))
        profile.append((0.0, h / 2))
        return revolve(profile, seg, owner=owner, sid=sid)
    if part == "suction_cup":
        r, h = _p(params, "r", 25), _p(params, "h", 14)
        profile = [(0.0, -h / 2), (r, -h / 2), (r * 0.92, -h / 2 + h * 0.22),
                   (r * 0.42, h * 0.1), (r * 0.34, h / 2), (0.0, h / 2)]
        return revolve(profile, seg, owner=owner, sid=sid)
    if part == "motor":
        r, h = _p(params, "r", 18), _p(params, "h", 40)
        sr, sh = _p(params, "shaft_r", r * 0.18), _p(params, "shaft_h", h * 0.35)
        body = extrude(circle_profile(r, seg), h, owner=owner, sid=sid)
        shaft = extrude(circle_profile(sr, SMALL_CIRCLE_SEGMENTS), sh, owner=owner, sid=sid)
        return merge([body, shaft.translated(0.0, h / 2.0 + sh / 2.0, 0.0)], owner=owner, sid=sid)
    if part == "piston":
        r, h = _p(params, "r", 18), _p(params, "h", 20)
        rr, rh = _p(params, "rod_r", r * 0.3), _p(params, "rod_h", h * 2.0)
        crown = extrude(circle_profile(r, seg), h, owner=owner, sid=sid)
        rod = extrude(circle_profile(rr, SMALL_CIRCLE_SEGMENTS), rh, owner=owner, sid=sid)
        return merge([crown, rod.translated(0.0, -h / 2.0 - rh / 2.0, 0.0)], owner=owner, sid=sid)
    if part == "valve":
        r, h = _p(params, "r", 14), _p(params, "h", 22)
        body = extrude(circle_profile(r, seg), h, owner=owner, sid=sid)
        stem = extrude(circle_profile(r * 0.22, SMALL_CIRCLE_SEGMENTS), h * 0.8,
                       owner=owner, sid=sid)
        return merge([body, stem.translated(0.0, h * 0.9, 0.0)], owner=owner, sid=sid)
    if part == "hinge":
        length, r = _p(params, "len", 40), _p(params, "r", 4)
        barrel = _rotate_z(extrude(circle_profile(r, SMALL_CIRCLE_SEGMENTS), length,
                                   owner=owner, sid=sid), 90.0)
        leaf_a = _box_mesh(length, r * 0.5, r * 5, owner, sid, offset=(0.0, 0.0, r * 2.8))
        leaf_b = _box_mesh(length, r * 5, r * 0.5, owner, sid, offset=(0.0, r * 2.8, 0.0))
        return merge([barrel, leaf_a, leaf_b], owner=owner, sid=sid)
    return _box_mesh(_p(params, "w", 30), _p(params, "h", 20), _p(params, "d", 20), owner, sid)


def build_solid(solid, owner: Optional[str] = None) -> Mesh:
    """A ``Solid`` from a scene, built and placed."""
    mesh = build(solid.part, solid.params or {}, owner=owner if owner is not None
                 else (solid.numeral or ""), sid=solid.id)
    rotate = list(solid.rotate or [0.0, 0.0, 0.0])
    at = list(solid.at or [0.0, 0.0, 0.0])
    matrix = Matrix.from_euler(rotate[0], rotate[1], rotate[2], at)
    return mesh.transformed(matrix)
