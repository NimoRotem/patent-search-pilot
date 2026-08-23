"""The symbol library, and the classifier that chooses between its entries."""
from __future__ import annotations

import re

import pytest

from pfc.profiles import load_profile
from pfc.render import symbols
from pfc.render.svgdoc import SvgDocument
from pfc.schemas import Box, VisualClass
from pfc.visualclass import classify, classify_all


@pytest.fixture
def profile():
    return load_profile("uspto_utility")


def _draw(profile, name: str, box: Box) -> str:
    doc = SvgDocument(profile, {})
    assert symbols.draw(doc, name, box)
    return doc.render()


ALL = sorted(symbols.SYMBOLS)


def _drawn_points(svg: str) -> list[tuple[float, float]]:
    """Every coordinate the symbol actually drew, from the geometry attributes only.

    Scraping numbers out of the raw SVG picks up stroke widths, font sizes and the viewBox and
    tells you nothing. The attributes are parsed instead.
    """
    import xml.etree.ElementTree as ET

    number = re.compile(r"-?\d+(?:\.\d+)?")
    points: list[tuple[float, float]] = []
    root = ET.fromstring(svg)
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        get = element.get
        if tag == "rect" and get("fill") != "#ffffff":
            x, y = float(get("x", 0)), float(get("y", 0))
            points += [(x, y), (x + float(get("width", 0)), y + float(get("height", 0)))]
        elif tag in {"ellipse", "circle"}:
            cx, cy = float(get("cx", 0)), float(get("cy", 0))
            rx = float(get("rx") or get("r") or 0)
            ry = float(get("ry") or get("r") or 0)
            points += [(cx - rx, cy - ry), (cx + rx, cy + ry)]
        elif tag in {"polygon", "polyline"}:
            pairs = [p.split(",") for p in (get("points") or "").split() if "," in p]
            points += [(float(a), float(b)) for a, b in pairs]
        elif tag == "path":
            points += _path_points(get("d") or "")
    return points


def _path_points(data: str) -> list[tuple[float, float]]:
    """Endpoints of an M/L/A/Z path.

    The arc command is why this cannot be a regular expression over the numbers: "A rx ry rot
    large-arc sweep x y" puts two radii and three flags BETWEEN the endpoints, so pairing the
    run off blindly reads a radius as an x coordinate. That is what made a coil look as though
    it were drawn at x=24.96.
    """
    tokens = re.findall(r"[MLAZmlaz]|-?\d+(?:\.\d+)?", data)
    out: list[tuple[float, float]] = []
    index = 0
    while index < len(tokens):
        command = tokens[index].upper()
        index += 1
        if command == "Z":
            continue
        if command in {"M", "L"}:
            out.append((float(tokens[index]), float(tokens[index + 1])))
            index += 2
        elif command == "A":
            out.append((float(tokens[index + 5]), float(tokens[index + 6])))
            index += 7
    return out


@pytest.mark.parametrize("name", ALL)
def test_every_symbol_draws_monochrome_line_art(profile, name):
    box = Box(x=400.0, y=400.0, width=300.0, height=200.0)
    svg = _draw(profile, name, box)
    assert "<image" not in svg
    assert "rgb(" not in svg
    assert set(c.lower() for c in re.findall(r"#[0-9A-Fa-f]{6}", svg)) <= {"#000000", "#ffffff"}
    # Something was actually drawn. Measured in geometry, not in characters: the magnet symbol
    # is legitimately two elements long.
    assert len(_drawn_points(svg)) >= 2


@pytest.mark.parametrize("name", ALL)
def test_every_symbol_stays_inside_its_box(profile, name):
    """The geometry validators measure the allotted box, so a symbol must not spill out of it."""
    box = Box(x=500.0, y=500.0, width=320.0, height=220.0)
    points = _drawn_points(_draw(profile, name, box))
    assert points, "the symbol emitted no geometry"
    # Leads, centrelines and a tube's mouth are conventionally drawn standing a little off the
    # body, so a small margin is allowed; spilling a whole box-width is not.
    slack = max(box.width, box.height) * 0.2
    for x, y in points:
        assert box.x - slack <= x <= box.right + slack, f"{name}: x={x} outside {box}"
        assert box.y - slack <= y <= box.bottom + slack, f"{name}: y={y} outside {box}"


@pytest.mark.parametrize("name", ALL)
def test_every_symbol_is_a_declared_visual_class_or_an_alias(name):
    """A symbol nothing can be classified as is a symbol that never gets drawn."""
    declared = set(VisualClass.__args__)  # type: ignore[attr-defined]
    assert name in declared


def test_every_visual_class_has_a_symbol_or_is_flowchart_only():
    flowchart_only = {"process_step", "decision", "terminator"}
    for name in VisualClass.__args__:  # type: ignore[attr-defined]
        if name in flowchart_only:
            continue
        assert symbols.has_symbol(name), f"{name} has no symbol"


def test_every_symbol_has_a_proportion():
    for name in ALL:
        assert symbols.aspect(name) > 0


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("induction coil", "coil"),
    ("the coils", "coil"),
    ("vacuum pump", "pump"),
    ("impedance sensor", "sensor"),
    ("power supply", "power"),
    ("memory device", "memory"),
    ("display interface", "interface"),
    ("first conductive substrate", "substrate"),
    ("heat-activated adhesive substrate", "substrate"),
    ("suction cup", "suction_cup"),
    ("robotic arm", "arm"),
    ("magnetic field generator", "magnet"),
    ("housing", "housing"),
    ("control unit", "processor"),
])
def test_a_disclosed_name_chooses_its_symbol(name, expected):
    assert classify(name) == expected


@pytest.mark.parametrize("name", [
    # Every one of these contains a keyword as a SUBSTRING and must not match it.
    "induction-assisted adhesive activation system",   # "duct" inside "induction"
    "excellent surface finish",                        # "cell"
    "spinning operation",                              # "pin"
    "barrier arrangement",                             # "bar"
    "produced article",                                # "rod" — and "article" is a workpiece,
    "scoring position",                                # "core"
    "abandoned attempt",                               # "band"
    "staircase region",                                # "case"
])
def test_a_keyword_hiding_inside_a_word_does_not_choose_a_symbol(name):
    """Substring matching drew an "induction-assisted activation system" as a pipe."""
    got = classify(name)
    assert got in {"generic_component", "workpiece"}, f"{name!r} came out as {got}"


@pytest.mark.parametrize("name", ["score line", "production line", "lifting unit",
                                  "adhesive module", "frequency band", "widget",
                                  "internal surface", "arrangement"])
def test_a_name_that_settles_nothing_stays_a_plain_outline(name):
    assert classify(name) == "generic_component"


def test_the_extractor_always_wins(simple_graph):
    """The keyword table is the floor. A class read out of the paragraphs is not overwritten."""
    graph, _registry = simple_graph
    sensor = next(e for e in graph.entities if e.reference_numeral == "120")
    sensor.visual_class = "housing"          # as if the extractor had said so
    classify_all(graph.entities)
    assert sensor.visual_class == "housing"


def test_classification_fills_only_the_defaults(simple_graph):
    graph, _registry = simple_graph
    for entity in graph.entities:
        entity.visual_class = "generic_component"
    changed = classify_all(graph.entities)
    assert changed >= 1
    by_numeral = {e.reference_numeral: e.visual_class for e in graph.entities}
    assert by_numeral["120"] == "sensor"
    assert by_numeral["130"] == "processor"


@pytest.mark.parametrize("name", ["plate", "substrate", "workpiece", "filter", "electrode"])
def test_a_hatched_symbol_is_hatched_and_is_quick_about_it(profile, name):
    """A negative hatch angle once ran the loop thirteen million times for no output."""
    import time

    box = Box(x=500.0, y=500.0, width=320.0, height=220.0)
    started = time.monotonic()
    points = _drawn_points(_draw(profile, name, box))
    assert time.monotonic() - started < 0.5, "hatching should be instant"
    # An outline plus a real run of hatch lines, not an outline on its own.
    assert len(points) >= 8, f"{name} drew {len(points)} points, so it is not hatched"
