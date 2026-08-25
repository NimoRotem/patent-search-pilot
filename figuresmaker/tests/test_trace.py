"""Reading a sketch, and compiling it into a figure."""
from __future__ import annotations

import io
import math

import pytest

from fm import sources
from fm.importers import trace
from fm.render import sketch
from fm.schemas import CadPart, FigurePlan, FigureSource, PlanElement, SketchScene
from fm.sources import Source


@pytest.fixture(scope="module")
def drawing() -> bytes:
    from tests.makesketch import draw
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "sketch.png"
        draw(path)
        return path.read_bytes()


@pytest.fixture(scope="module")
def source() -> Source:
    return Source(id="s", kind="sketch", filename="sketch.png", suffix=".png", bytes=0)


# ------------------------------------------------------------------------------- binarising


def test_a_lighting_gradient_does_not_defeat_the_threshold(drawing):
    """A phone photograph of a sketch has a shadow across it; one global number cannot work."""
    image = trace.load_image("sketch.png", drawing)
    ink = trace.binarise(image)
    fraction = float(ink.mean())
    assert 0.002 < fraction < 0.20, f"ink fraction {fraction} is not line work"
    # The gradient runs across the sheet, so if it had beaten the threshold one side would be
    # solid ink and the other blank.
    left = ink[:, : ink.shape[1] // 2].mean()
    right = ink[:, ink.shape[1] // 2:].mean()
    assert max(left, right) < 6 * max(min(left, right), 1e-4)


def test_a_white_on_black_sketch_is_read_the_right_way_round():
    from PIL import Image, ImageDraw

    image = Image.new("L", (400, 300), 20)
    ImageDraw.Draw(image).rectangle([80, 60, 320, 240], outline=235, width=5)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    ink = trace.binarise(trace.load_image("x.png", buffer.getvalue()))
    assert 0.001 < float(ink.mean()) < 0.3


# --------------------------------------------------------------------------------- thinning


def test_thinning_reduces_a_stroke_to_its_spine():
    numpy = pytest.importorskip("numpy")
    mask = numpy.zeros((60, 200), dtype=bool)
    mask[26:34, 20:180] = True                     # a bar eight pixels thick
    skeleton = trace.thin(mask)
    assert skeleton.sum() < mask.sum() / 4
    # One pixel per column across the middle of the bar.
    for column in range(40, 160):
        assert 1 <= int(skeleton[:, column].sum()) <= 2


def test_a_boundary_trace_would_have_doubled_the_line():
    """The reason this uses a centreline. A stroke's outline is a loop around it."""
    numpy = pytest.importorskip("numpy")
    mask = numpy.zeros((60, 200), dtype=bool)
    mask[28:32, 20:180] = True
    skeleton = trace.thin(mask)
    paths, _degree = trace.walk(skeleton)
    assert len(paths) == 1, "a single bar is one line, not a loop around one"


# ---------------------------------------------------------------------------------- walking


def test_a_junction_cluster_is_one_node_not_many(drawing):
    """The regression that turned a sketch of six shapes into 980 fragments.

    Thinning leaves several touching pixels with three or more neighbours wherever lines meet.
    Treating each as its own junction emits a two-pixel path between every adjacent pair.
    """
    image = trace.load_image("sketch.png", drawing)
    skeleton = trace.thin(trace.binarise(image))
    paths, degree = trace.walk(skeleton)
    branch_pixels = sum(1 for count in degree.values() if count > 2)
    assert branch_pixels > 50, "this fixture should have plenty of branch pixels"
    assert len(paths) < branch_pixels / 4, (
        f"{len(paths)} paths from {branch_pixels} branch pixels: junctions are not being merged")
    tiny = sum(1 for path in paths if len(path) <= 2)
    assert tiny < len(paths) / 4


# ---------------------------------------------------------------------------- straightening


def test_a_wobbly_straight_line_collapses_to_two_points():
    rng = __import__("random").Random(3)
    points = [(float(i), 100.0 + rng.uniform(-2.0, 2.0)) for i in range(0, 300, 2)]
    out = trace.collapse_runs(points, 6.0, 1.5)
    assert len(out) == 2, f"a tremor is not a shape: got {len(out)} points"


def test_an_arc_keeps_its_curve():
    """A bow deviates one way; that is what tells it from a wobble of the same size."""
    points = [(80 * math.cos(t / 40.0), 80 * math.sin(t / 40.0)) for t in range(0, 130)]
    out = trace.collapse_runs(points, 6.0, 1.5)
    assert len(out) > 6, f"the arc was flattened to {len(out)} points"


def test_a_true_straight_line_is_two_points():
    points = [(float(i), 50.0) for i in range(0, 200, 2)]
    assert len(trace.collapse_runs(points, 6.0, 1.5)) == 2


def test_a_nearly_horizontal_segment_is_snapped():
    out = trace.straighten([(0.0, 100.0), (200.0, 103.0)], 1.0)
    assert out[-1][1] == pytest.approx(100.0), "on a hand sketch that was meant to be horizontal"


def test_a_clearly_sloped_segment_is_left_alone():
    out = trace.straighten([(0.0, 100.0), (200.0, 180.0)], 1.0)
    assert out[-1][1] == pytest.approx(180.0)


def test_simplify_keeps_the_corners():
    square = ([(float(i), 0.0) for i in range(0, 101, 5)]
              + [(100.0, float(i)) for i in range(0, 101, 5)])
    out = trace.simplify(square, 2.0)
    assert 3 <= len(out) <= 5, "a right angle is a corner, not noise"


# ------------------------------------------------------------------------------ end to end


def test_the_sketch_traces_to_the_shapes_that_were_drawn(drawing):
    traced = trace.trace("sketch.png", drawing)
    assert 2 <= len(traced.components) <= 6
    assert traced.width_mm == pytest.approx(150.0, abs=0.5)
    stats = traced.stats()
    assert stats["points"] < 400, "a clean trace, not a pixel-for-pixel copy"
    assert stats["polylines"] > 4


def test_a_photograph_is_refused_rather_than_traced():
    from PIL import Image
    import random

    rng = random.Random(1)
    image = Image.new("L", (300, 300))
    image.putdata([rng.randint(0, 255) for _ in range(300 * 300)])
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    with pytest.raises(trace.TraceError):
        trace.trace("photo.png", buffer.getvalue())


def test_a_blank_page_is_refused_by_name():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("L", (400, 400), 250).save(buffer, "PNG")
    with pytest.raises(trace.TraceError) as caught:
        trace.trace("blank.png", buffer.getvalue())
    assert "ink" in str(caught.value)


def test_a_sketch_is_checked_when_it_is_uploaded_not_when_it_is_drawn(drawing):
    """A file that cannot be traced should say so while the person who chose it is watching."""
    info = sources.inspect("sketch", "sketch.png", drawing)
    assert info["components"] >= 2
    assert info["width"] > 0


def test_an_untraceable_sketch_is_refused_at_upload():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("L", (400, 400), 250).save(buffer, "PNG")
    with pytest.raises(sources.SourceError):
        sources.inspect("sketch", "b.png", buffer.getvalue())


# ------------------------------------------------------------------------------- the figure


def test_a_traced_sketch_becomes_a_figure_with_numerals_on_real_strokes(drawing, source):
    traced = trace.trace("sketch.png", drawing)
    parts = [CadPart(component=i, numeral=n)
             for i, n in zip(range(len(traced.components)), ["102", "104", "106"])]
    plan = FigurePlan(label="FIG. 1", kind="perspective",
                      source=FigureSource(kind="sketch", source_id="s"),
                      elements=[PlanElement(numeral=p.numeral) for p in parts])
    figure = sketch.render_sketch(plan, SketchScene(source_id="s", parts=parts), source, drawing)
    assert figure.prims
    assert set(figure.anchors) == {p.numeral for p in parts}
    from fm import geom
    for numeral, anchors in figure.anchors.items():
        owned = [poly for prim in figure.prims if prim.owner == numeral
                 for poly in prim.polys()]
        for anchor in anchors[:4]:
            assert min(geom.dist_point_polyline(anchor.point, poly) for poly in owned) < 0.01, \
                "an anchor must be a point that was actually drawn"


def test_a_sketch_can_stand_behind_a_mechanical_view():
    assert sources.is_authoritative("perspective", "sketch")
    assert sources.is_authoritative("cross_section", "sketch")
    assert not sources.is_authoritative("perspective", "blockout")


def test_a_piece_no_numeral_names_is_reported(drawing, source):
    scene = SketchScene(source_id="s", parts=[CadPart(component=0, numeral="102")])
    assert sketch.unassigned(scene, 3) == [1, 2]
