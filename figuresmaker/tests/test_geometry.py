"""Plane geometry, projection and hidden-line removal."""
from __future__ import annotations

import math

import pytest

from fm import geom
from fm.render import mech, solid
from fm.schemas import FigurePlan, MechScene, Solid


# ------------------------------------------------------------------------------------ segments


def test_a_proper_crossing_is_a_crossing():
    assert geom.segments_cross((0, 0), (10, 10), (0, 10), (10, 0))


def test_a_shared_endpoint_is_not_a_crossing():
    """A lead line lands on the part it indicates. That touch must not read as a violation."""
    assert not geom.segments_cross((0, 0), (5, 5), (5, 5), (10, 0))
    assert geom.segments_cross((0, 0), (5, 5), (5, 5), (10, 0), touching_counts=True)


def test_parallel_segments_never_cross():
    assert not geom.segments_cross((0, 0), (10, 0), (0, 2), (10, 2))


def test_intersection_point():
    point = geom.segment_intersection((0, 0), (10, 10), (0, 10), (10, 0))
    assert point == pytest.approx((5.0, 5.0))


def test_point_in_polygon():
    square = geom.rect_poly(0, 0, 10, 10)
    assert geom.point_in_polygon((5, 5), square)
    assert not geom.point_in_polygon((15, 5), square)
    assert not geom.point_in_polygon((5, -1), square)


def test_bbox_clip_finds_the_interval_inside_a_box():
    span = geom.segment_bbox_clip((-5, 5), (15, 5), (0, 0, 10, 10))
    assert span is not None
    assert span[0] == pytest.approx(0.25)
    assert span[1] == pytest.approx(0.75)


def test_bbox_clip_returns_none_when_outside():
    assert geom.segment_bbox_clip((-5, 50), (15, 50), (0, 0, 10, 10)) is None


# ------------------------------------------------------------------------------------ hatching


def test_hatching_fills_a_square_with_parallel_lines():
    square = geom.rect_poly(0, 0, 20, 20)
    lines = geom.hatch_polygon(square, spacing=2.0, angle_deg=45.0)
    assert len(lines) > 5
    angles = {round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180, 1)
              for a, b in lines}
    assert len(angles) == 1, "hatching must be one family of parallel lines"
    for a, b in lines:
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        assert geom.point_in_polygon(mid, square)


def test_hatching_leaves_a_hole_alone():
    outer = geom.rect_poly(0, 0, 30, 30)
    hole = geom.rect_poly(10, 10, 10, 10)
    lines = geom.hatch_polygon(outer, spacing=1.5, angle_deg=45.0, holes=[hole])
    for a, b in lines:
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        assert not geom.point_in_polygon(mid, hole)


def test_two_shapes_hatched_at_the_same_angle_line_up():
    """Cuts through one part in two places must not drift out of register."""
    left = geom.hatch_polygon(geom.rect_poly(0, 0, 10, 10), 2.0, 45.0)
    right = geom.hatch_polygon(geom.rect_poly(40, 0, 10, 10), 2.0, 45.0)
    theta = math.radians(45.0)

    def offsets(lines):
        return {round((-a[0] * math.sin(theta) + a[1] * math.cos(theta)) / 2.0, 4)
                for a, _b in lines}

    assert offsets(left) and offsets(right)
    assert all(abs(value - round(value)) < 1e-6 for value in offsets(left) | offsets(right))


# ------------------------------------------------------------------------------------- text


def test_character_height_meets_the_rule():
    from fm.drawing import MIN_CHAR_MM, NUMERAL_SIZE

    _width, height = geom.text_extent("102", NUMERAL_SIZE)
    assert height >= MIN_CHAR_MM - 1e-9


# -------------------------------------------------------------------------------- projection


def test_camera_axes_are_orthonormal():
    for name in ("isometric", "front", "top", "right", "dimetric"):
        camera = mech.Camera.named(name)
        for axis in (camera.right, camera.up, camera.eye):
            assert math.sqrt(sum(v * v for v in axis)) == pytest.approx(1.0, abs=1e-9)
        assert mech._dot(camera.right, camera.up) == pytest.approx(0.0, abs=1e-9)
        assert mech._dot(camera.right, camera.eye) == pytest.approx(0.0, abs=1e-9)


def test_front_camera_puts_y_up_on_the_page():
    camera = mech.Camera.named("front")
    high = camera.project((0.0, 10.0, 0.0))
    low = camera.project((0.0, -10.0, 0.0))
    assert high[1] < low[1], "a higher point must draw further up the sheet"


def test_depth_grows_towards_the_eye():
    camera = mech.Camera.named("front")
    near = camera.project((0.0, 0.0, 10.0))
    far = camera.project((0.0, 0.0, -10.0))
    assert near[2] > far[2]


# --------------------------------------------------------------------- hidden line removal


def _scene(solids: list[Solid], kind: str = "perspective", **kw) -> mech.Scene3D:
    return mech.assemble(MechScene(solids=solids, **kw), kind)


def test_a_near_box_hides_the_far_one():
    """The whole point of the projection: a line behind a solid is not drawn."""
    scene = _scene([
        Solid(id="near", numeral="10", part="box", params={"w": 40, "h": 40, "d": 10},
              at=[0, 0, 20]),
        Solid(id="far", numeral="20", part="box", params={"w": 10, "h": 10, "d": 10},
              at=[0, 0, -20]),
    ])
    camera = mech.Camera.named("front")
    projected = mech.project(scene, camera)
    index = mech.TriangleIndex(projected)

    visible_far = 0.0
    total_far = 0.0
    for a, b, owner in scene.edges:
        if owner != "20":
            continue
        length = math.dist(projected.xy[a], projected.xy[b])
        if length < 1e-6:
            continue
        total_far += length
        for t0, t1 in mech.visible_intervals(projected.xy[a], projected.xy[b],
                                             projected.depth[a], projected.depth[b],
                                             index, skip=(a, b)):
            visible_far += (t1 - t0) * length
    assert total_far > 0
    assert visible_far / total_far < 0.01, "the small far box is entirely behind the near one"


def test_an_unobstructed_edge_is_fully_visible():
    scene = _scene([Solid(id="only", numeral="10", part="box",
                          params={"w": 20, "h": 20, "d": 20})])
    projected = mech.project(scene, mech.Camera.named("isometric"))
    index = mech.TriangleIndex(projected)
    a, b, _owner = scene.edges[0]
    spans = mech.visible_intervals(projected.xy[a], projected.xy[b], projected.depth[a],
                                   projected.depth[b], index, skip=(a, b))
    total = sum(t1 - t0 for t0, t1 in spans)
    assert 0.0 <= total <= 1.0
    assert total > 0.4, "an edge of a single convex solid is at least half visible"


def test_intervals_are_subtracted_correctly():
    assert mech._subtract([]) == [(0.0, 1.0)]
    assert mech._subtract([(0.0, 1.0)]) == []
    got = mech._subtract([(0.2, 0.4), (0.3, 0.6)])
    assert got == pytest.approx([(0.0, 0.2), (0.6, 1.0)])


def test_a_cut_produces_a_closed_cap_that_gets_hatched():
    plan = FigurePlan(label="FIG. 1", kind="cross_section", title="a cut cylinder")
    scene = MechScene(camera="front", solids=[
        Solid(id="a", numeral="10", part="cylinder", params={"r": 20, "h": 30})],
        section={"axis": "z", "offset": 0.0, "keep": "negative", "name": "A-A"})
    figure = mech.render_mech(plan, scene)
    hatch = [p for p in figure.prims if p.role == "hatch"]
    assert hatch, "a sectional view must hatch its cut surface"
    assert all(p.owner == "10" for p in hatch)


def test_the_cut_face_hides_what_is_behind_it():
    """Without a cap, a section is a hole you can see the far wall through."""
    scene = MechScene(camera="front", solids=[
        Solid(id="a", numeral="10", part="tube", params={"r": 20, "ri": 14, "h": 30})],
        section={"axis": "z", "offset": 0.0, "keep": "negative", "name": "A-A"})
    built = mech.assemble(scene, "cross_section")
    assert built.caps, "the cut must produce at least one cap loop"
    for loop, owner in built.caps:
        assert owner == "10"
        assert len(loop) >= 3


def test_explode_separates_along_the_axis():
    scene = MechScene(camera="front", solids=[
        Solid(id="a", numeral="10", part="box", params={"w": 10, "h": 10, "d": 10}),
        Solid(id="b", numeral="20", part="box", params={"w": 10, "h": 10, "d": 10}),
    ], explode={"axis": "y", "gap": 50.0, "order": ["a", "b"]})
    built = mech.assemble(scene, "exploded")
    ys = [v[1] for v in built.verts]
    assert max(ys) - min(ys) > 50.0


def test_an_empty_scene_is_reported_not_drawn_blank():
    with pytest.raises(mech.MechError):
        mech.assemble(MechScene(solids=[]), "perspective")


# ------------------------------------------------------------------------------------ solids


def test_every_named_part_builds_a_non_empty_mesh():
    from fm.schemas import PART_NAMES

    for name in PART_NAMES:
        mesh = solid.build(name, {}, owner="1", sid=name)
        assert mesh.verts, f"{name} produced no vertices"
        assert mesh.tris, f"{name} produced no triangles"
        assert mesh.edges or mesh.smooth, f"{name} produced nothing to draw"


def test_an_unknown_part_becomes_a_box_rather_than_nothing():
    mesh = solid.build("flux_capacitor", {"w": 10}, owner="1", sid="x")
    assert len(mesh.verts) >= 8


def test_a_cylinder_has_no_facet_lines_down_its_side():
    mesh = solid.build("cylinder", {"r": 10, "h": 20})
    assert mesh.smooth, "the curved wall must be marked smooth so only its silhouette is drawn"
    vertical = [(a, b) for a, b in mesh.edges
                if abs(mesh.verts[a][1] - mesh.verts[b][1]) > 1e-6]
    assert not vertical, "a cylinder must not draw a line at every mesh segment"
