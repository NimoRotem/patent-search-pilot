"""Reading a supplied mesh, and compiling it into views."""
from __future__ import annotations

import struct

import pytest

from fm.importers import mesh as mesh_import
from fm.render import cad, solid
from fm.schemas import CadPart, CadScene, ExplodeSpec, FigurePlan, FigureSource, SectionSpec
from fm.sources import Source


def _stl(meshes) -> bytes:
    triangles = [(m.verts[a], m.verts[b], m.verts[c]) for m in meshes for a, b, c in m.tris]
    out = [b"test".ljust(80, b"\0"), struct.pack("<I", len(triangles))]
    for va, vb, vc in triangles:
        out.append(struct.pack("<3f", 0.0, 0.0, 0.0))
        for v in (va, vb, vc):
            out.append(struct.pack("<3f", *v))
        out.append(struct.pack("<H", 0))
    return b"".join(out)


@pytest.fixture(scope="module")
def assembly() -> bytes:
    return _stl([
        solid.build("housing", {"w": 90, "h": 40, "d": 60, "t": 5}),
        solid.build("cylinder", {"r": 14, "h": 26}).translated(0, 2, 0),
        solid.build("plate", {"w": 100, "d": 70, "t": 6}).translated(0, 25, 0),
    ])


@pytest.fixture(scope="module")
def source() -> Source:
    return Source(id="t", kind="cad", filename="assembly.stl", suffix=".stl", bytes=0)


def test_a_binary_stl_is_read(assembly):
    info = mesh_import.probe("assembly.stl", assembly)
    assert info["triangles"] > 100
    assert info["format"] == "stl"


def test_welding_turns_a_triangle_soup_back_into_a_surface(assembly):
    """An STL shares no vertices, so nothing has a dihedral angle until it is welded."""
    raw_verts, raw_tris = mesh_import._parse("assembly.stl", assembly)
    assert len(raw_verts) == len(raw_tris) * 3, "an STL should start as unshared triangles"
    welded = mesh_import.load("assembly.stl", assembly)
    assert len(welded.verts) < len(raw_verts) / 2
    assert welded.edges, "welding must produce drawable feature edges"


def test_flat_faces_do_not_produce_tessellation_edges(assembly):
    """Drawing every mesh edge is what makes converted CAD look converted."""
    built = mesh_import.load("assembly.stl", assembly)
    assert len(built.edges) < len(built.tris), "far fewer drawn edges than triangles"


def test_a_curved_wall_is_left_to_the_silhouette(assembly):
    built = mesh_import.load("assembly.stl", assembly)
    assert built.smooth, "the cylinder's wall must be marked smooth, not drawn as facets"


def test_components_are_recovered_from_one_file(assembly):
    built = mesh_import.load("assembly.stl", assembly)
    parts = mesh_import.split_components(built)
    assert len(parts) == 3, "three separate solids in one STL are three parts"
    assert all(p.tris for p in parts)


def test_components_come_back_largest_first(assembly):
    built = mesh_import.load("assembly.stl", assembly)
    parts = mesh_import.split_components(built)
    sizes = [len(p.tris) for p in parts]
    assert sizes == sorted(sizes, reverse=True)


def test_a_component_description_is_numeric_not_a_guess(assembly, source):
    described = cad.describe(source, assembly)
    assert len(described) == 3
    for item in described:
        assert item["triangles"] > 0
        assert len(item["size_mm"]) == 3
        assert item["position"]


def _plan(label: str, kind: str, numerals) -> FigurePlan:
    from fm.schemas import PlanElement
    return FigurePlan(label=label, kind=kind, source=FigureSource(kind="cad", source_id="t"),
                      elements=[PlanElement(numeral=n) for n in numerals])


def _parts(numerals):
    return [CadPart(component=i, numeral=n) for i, n in enumerate(numerals)]


def test_a_perspective_view_compiles_from_the_mesh(assembly, source):
    scene = CadScene(source_id="t", camera="isometric", parts=_parts(["102", "104", "106"]))
    figure = cad.render_cad(_plan("FIG. 1", "perspective", ["102", "104", "106"]),
                            scene, source, assembly)
    assert figure.prims
    assert figure.anchors, "numerals must land on geometry that exists in the mesh"


def test_a_section_cuts_the_supplied_mesh_and_hatches_it(assembly, source):
    scene = CadScene(source_id="t", camera="front", parts=_parts(["102", "104", "106"]),
                     section=SectionSpec(axis="z", offset=0.0, keep="negative", name="A-A"))
    figure = cad.render_cad(_plan("FIG. 2", "cross_section", ["102", "104", "106"]),
                            scene, source, assembly)
    hatch = [p for p in figure.prims if p.role == "hatch"]
    assert hatch, "a section of supplied CAD must be hatched like any other section"
    assert len({p.owner for p in hatch}) > 1, "each part hatched separately"


def test_each_part_is_hatched_at_its_own_angle(assembly, source):
    import math

    scene = CadScene(source_id="t", camera="front", parts=_parts(["102", "104", "106"]),
                     section=SectionSpec(axis="z", offset=0.0, keep="negative", name="A-A"))
    figure = cad.render_cad(_plan("FIG. 2", "cross_section", ["102", "104", "106"]),
                            scene, source, assembly)
    angles: dict[str, set] = {}
    for prim in figure.prims:
        if prim.role != "hatch" or len(prim.pts) < 2:
            continue
        a, b = prim.pts[0], prim.pts[-1]
        angles.setdefault(prim.owner, set()).add(
            round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180, 1))
    assert len(angles) > 1
    flat = [next(iter(v)) for v in angles.values() if len(v) == 1]
    assert len(set(flat)) == len(flat), "37 CFR 1.84(h)(3): adjacent parts at different angles"


def test_an_exploded_view_separates_the_supplied_parts(assembly, source):
    scene = CadScene(source_id="t", camera="isometric", parts=_parts(["102", "104", "106"]),
                     explode=ExplodeSpec(axis="y", gap=60.0))
    figure = cad.render_cad(_plan("FIG. 3", "exploded", ["102", "104", "106"]),
                            scene, source, assembly)
    box = figure.content_bbox()
    assert box[3] - box[1] > 150, "the parts must actually come apart"


def test_a_component_no_numeral_names_is_reported(assembly, source):
    scene = CadScene(source_id="t", parts=_parts(["102"]))
    assert cad.unassigned(scene, 3) == [1, 2]


def test_step_is_refused_by_name_rather_than_half_read():
    with pytest.raises(mesh_import.MeshError) as caught:
        mesh_import.probe("part.step", b"ISO-10303-21;")
    assert "STL" in str(caught.value)


def test_an_empty_mesh_is_refused():
    with pytest.raises(mesh_import.MeshError):
        mesh_import.probe("empty.stl", _stl([]))


def test_the_appearance_store_leaves_a_cad_scene_alone():
    """A CAD scene has no primitives to pin: every view is a projection of one file.

    Dispatching the appearance constraint on the figure KIND rather than the scene type is what
    made every CAD-backed figure fail with "'CadScene' object has no attribute 'solids'".
    """
    from fm import appearance as appearance_mod
    from fm.pipeline import _constrain, _learn

    store = appearance_mod.Appearance()
    scene = CadScene(source_id="t", parts=[CadPart(component=0, numeral="102")])
    _constrain("perspective", scene, store)
    _learn("perspective", scene, store)
    assert scene.parts[0].numeral == "102"
    assert not store.parts


def test_the_appearance_store_still_pins_a_blockout():
    from fm import appearance as appearance_mod
    from fm.pipeline import _constrain, _learn
    from fm.schemas import MechScene, Solid

    store = appearance_mod.Appearance()
    first = MechScene(solids=[Solid(id="a", numeral="102", part="cylinder",
                                    params={"r": 10, "h": 20})])
    _learn("perspective", first, store)
    second = MechScene(solids=[Solid(id="b", numeral="102", part="box",
                                     params={"w": 99, "h": 99, "d": 99})])
    _constrain("perspective", second, store)
    assert second.solids[0].part == "cylinder", "a part must look like itself in every view"


def test_a_part_absent_from_the_mesh_is_blamed_on_the_geometry_not_the_planner(assembly, source):
    """Asking the planner to fix this would be asking a model to invent the missing part."""
    from fm.render import _missing_numerals
    from fm.schemas import PlanElement

    scene = CadScene(source_id="t", camera="isometric", parts=_parts(["102"]))
    plan = _plan("FIG. 1", "perspective", ["102"])
    plan.elements.append(PlanElement(numeral="999", term="flux capacitor"))
    figure = cad.render_cad(plan, scene, source, assembly)
    found = _missing_numerals(plan, figure)
    hit = [f for f in found if f.numeral == "999"]
    assert hit, "a promised part that is not in the mesh must be reported"
    assert hit[0].code == "part_not_in_supplied_geometry"
    assert hit[0].stage == "draft", "the draft owns this, not the planner"
    assert "will not be invented" not in hit[0].message
    assert "coverage matrix" in hit[0].message


def test_a_blockout_still_sends_a_missing_part_back_to_the_planner():
    from fm.render import _missing_numerals
    from fm.drawing import Figure
    from fm.schemas import PlanElement

    plan = FigurePlan(label="FIG. 1", kind="perspective",
                      source=FigureSource(kind="blockout"),
                      elements=[PlanElement(numeral="102", term="housing")])
    found = _missing_numerals(plan, Figure(label="FIG. 1", kind="perspective"))
    assert found[0].code == "element_not_drawn"
    assert found[0].stage == "planner"
