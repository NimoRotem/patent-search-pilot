"""Write a small multi-part STL, so the CAD path can be exercised without a CAD system.

Three separate solids in one file, which is what an assembly exported from CAD looks like: a
hollow housing, a cylinder inside it, and a plate on top. Nothing here is part of the product; it
is a fixture.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from fm.render import solid


def write_stl(meshes, path: Path) -> None:
    triangles: list[tuple] = []
    for mesh in meshes:
        for a, b, c in mesh.tris:
            triangles.append((mesh.verts[a], mesh.verts[b], mesh.verts[c]))
    with path.open("wb") as handle:
        handle.write(b"figuresmaker test assembly".ljust(80, b"\0"))
        handle.write(struct.pack("<I", len(triangles)))
        for va, vb, vc in triangles:
            handle.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in (va, vb, vc):
                handle.write(struct.pack("<3f", *v))
            handle.write(struct.pack("<H", 0))


def build() -> list:
    housing = solid.build("housing", {"w": 90, "h": 40, "d": 60, "t": 5})
    housing = housing.translated(0, 0, 0)
    motor = solid.build("motor", {"r": 14, "h": 26, "shaft_r": 3, "shaft_h": 12})
    motor = motor.translated(0, 2, 0)
    plate = solid.build("plate", {"w": 100, "d": 70, "t": 6})
    plate = plate.translated(0, 25, 0)
    return [housing, motor, plate]


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/assembly.stl")
    meshes = build()
    write_stl(meshes, target)
    print(f"{target}: {sum(len(m.tris) for m in meshes)} triangles, {len(meshes)} parts")
