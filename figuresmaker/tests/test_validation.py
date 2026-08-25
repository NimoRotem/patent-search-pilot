"""The compliance checker, proved against injected defects.

Every check here is exercised twice: once on a clean drawing set, where it must stay silent, and
once on a set with exactly one thing wrong with it, where it must fire. A guard that has only
ever been seen passing is a guard nobody has tested.
"""
from __future__ import annotations

import copy

import pytest

from fm import validate
from fm.drawing import Anchor, Figure, MIN_CHAR_MM, Prim, polygon, polyline
from fm.geom import rect_poly
from fm.render import leaders as leaders_mod, sheet as sheetmod
from fm.schemas import (BriefItem, Claim, ClaimElement, FigurePlan, Plan, PlanElement,
                        RefEntry, Registry, Sections)
from fm.validate import data as data_checks, raster as raster_checks, rules


# ---------------------------------------------------------------------------------- fixtures


def a_box(numeral: str, x: float, y: float, w: float = 30.0, h: float = 20.0) -> list[Prim]:
    return [polygon(rect_poly(x, y, w, h), role="outline", owner=numeral)]


def a_figure(label: str = "FIG. 1", numerals=("102", "104"), kind: str = "block_diagram"
             ) -> Figure:
    figure = Figure(label=label, kind=kind, title="a test view")
    for index, numeral in enumerate(numerals):
        x = 10.0 + index * 50.0
        figure.prims.extend(a_box(numeral, x, 10.0))
        box = rect_poly(x, 10.0, 30.0, 20.0)
        figure.anchors[numeral] = [
            Anchor(numeral, (x + 15.0, 10.0), (0.0, -1.0)),
            Anchor(numeral, (x + 30.0, 20.0), (1.0, 0.0)),
            Anchor(numeral, (x + 15.0, 30.0), (0.0, 1.0)),
            Anchor(numeral, (x, 20.0), (-1.0, 0.0)),
        ]
        _ = box
    leaders_mod.solve(figure)
    return figure


def a_registry(numerals=("102", "104")) -> Registry:
    terms = {"102": "housing", "104": "vacuum pump", "106": "controller", "108": "sensor"}
    return Registry(entries=[RefEntry(numeral=n, term=terms.get(n, f"part {n}"),
                                      figures=["FIG. 1"], mentions=3) for n in numerals])


def a_plan(figures) -> Plan:
    return Plan(figures=[
        FigurePlan(label=f.label, kind=f.kind, title=f.title,
                   elements=[PlanElement(numeral=n, term=a_registry().by_numeral().get(
                       n, RefEntry(numeral=n, term="")).term) for n in f.numerals()])
        for f in figures])


def a_claim(numerals=("102", "104")) -> list[Claim]:
    terms = a_registry().by_numeral()
    return [Claim(number=1, independent=True, text="A thing.", elements=[
        ClaimElement(text=f"a {terms[n].term}", term=terms[n].term, numeral=n)
        for n in numerals if n in terms])]


def a_sections(labels=("FIG. 1",)) -> Sections:
    return Sections(raw="", brief_items=[BriefItem(label=label, text=f"{label} shows a thing.")
                                         for label in labels])


def check(figures, plan=None, registry=None, claims=None, sections=None, **kw):
    figures = list(figures)
    return validate.validate(figures, plan or a_plan(figures), registry or a_registry(),
                             claims if claims is not None else a_claim(),
                             sections or a_sections([f.label for f in figures]), **kw)


def codes(report, severity=None):
    return {f.code for f in report.findings if severity is None or f.severity == severity}


# ---------------------------------------------------------------------------- the clean case


def test_a_clean_set_passes_everything():
    report = check([a_figure()])
    assert report.passed, [f.message for f in report.errors()]
    assert not report.errors()


def test_the_report_says_what_it_checked():
    report = check([a_figure()])
    assert len(report.checked) >= 12


# --------------------------------------------------------------- numerals and the description


def test_a_numeral_not_in_the_registry_is_caught():
    """37 CFR 1.84(p)(4): a character in a drawing must be mentioned in the description."""
    figure = a_figure(numerals=("102", "104", "999"))
    report = check([figure], registry=a_registry(("102", "104")), plan=a_plan([figure]))
    assert "numeral_not_in_registry" in codes(report, "error")


def test_a_registry_numeral_in_no_figure_is_caught():
    report = check([a_figure(numerals=("102",))], registry=a_registry(("102", "104")))
    assert "registry_numeral_undrawn" in codes(report, "error")
    hit = next(f for f in report.findings if f.code == "registry_numeral_undrawn")
    assert hit.cite == "37 CFR 1.84(p)(4)"
    assert hit.stage == "planner"


def test_one_numeral_for_two_parts_across_views_is_caught():
    """37 CFR 1.84(p)(5): a character must never designate different parts."""
    first, second = a_figure("FIG. 1"), a_figure("FIG. 2")
    plan = a_plan([first, second])
    plan.figures[1].elements[0].term = "gearbox"
    report = check([first, second], plan=plan, sections=a_sections(("FIG. 1", "FIG. 2")))
    assert "numeral_reused" in codes(report, "error")


# ------------------------------------------------------------------------------------ claims


def test_a_claimed_feature_in_no_figure_is_caught():
    """37 CFR 1.83(a): the drawing must show every feature the claims specify."""
    report = check([a_figure(numerals=("102",))],
                   registry=a_registry(("102", "104")), claims=a_claim(("102", "104")))
    assert "claim_element_not_depicted" in codes(report, "error")


def test_an_unmatched_claim_element_is_a_warning_not_an_error():
    """It cannot be checked, which is a different thing from being wrong."""
    claims = [Claim(number=1, independent=True, text="A thing.", elements=[
        ClaimElement(text="a flux capacitor", term="flux capacitor", numeral="")])]
    report = check([a_figure()], claims=claims)
    assert "claim_element_unmatched" in codes(report, "warning")
    assert "claim_element_unmatched" not in codes(report, "error")


# --------------------------------------------------------------------------- view numbering


def test_a_gap_in_the_figure_numbers_is_caught():
    report = check([a_figure("FIG. 1"), a_figure("FIG. 3")],
                   sections=a_sections(("FIG. 1", "FIG. 3")))
    assert "figures_not_sequential" in codes(report, "error")


def test_starting_at_figure_two_is_caught():
    report = check([a_figure("FIG. 2")], sections=a_sections(("FIG. 2",)))
    assert "figures_not_sequential" in codes(report, "error")


def test_a_gap_in_the_letters_is_caught():
    report = check([a_figure("FIG. 1A"), a_figure("FIG. 1C")],
                   sections=a_sections(("FIG. 1A", "FIG. 1C")))
    assert "figures_not_sequential" in codes(report, "error")


def test_consecutive_letters_are_accepted():
    report = check([a_figure("FIG. 1A"), a_figure("FIG. 1B")],
                   sections=a_sections(("FIG. 1A", "FIG. 1B")))
    assert "figures_not_sequential" not in codes(report)


def test_a_malformed_caption_is_caught():
    figure = a_figure("Figure One")
    report = check([figure], sections=Sections(raw=""))
    assert "figure_label_malformed" in codes(report, "error")


# ------------------------------------------------------------------------- sections and hatch


def test_a_sectional_view_with_no_hatching_is_caught():
    figure = a_figure("FIG. 1", kind="cross_section")
    plan = a_plan([figure])
    plan.figures[0].kind = "cross_section"
    plan.figures[0].parent = ""
    report = check([figure], plan=plan)
    assert "section_without_hatching" in codes(report, "error")


def test_a_sectional_view_with_hatching_is_accepted():
    figure = a_figure("FIG. 1", kind="cross_section")
    figure.prims.append(polyline([(12, 12), (26, 26)], role="hatch", owner="102"))
    plan = a_plan([figure])
    plan.figures[0].kind = "cross_section"
    plan.figures[0].parent = "FIG. 1"
    report = check([figure], plan=plan)
    assert "section_without_hatching" not in codes(report)


# ---------------------------------------------------------------------- the brief description


def test_a_promised_figure_that_was_not_drawn_is_caught():
    report = check([a_figure("FIG. 1")], sections=a_sections(("FIG. 1", "FIG. 2")))
    assert "brief_description_mismatch" in codes(report, "error")


def test_a_missing_brief_description_is_a_warning():
    report = check([a_figure()], sections=Sections(raw=""))
    assert "brief_description_missing" in codes(report, "warning")


# -------------------------------------------------------------------------------- the sheet


def _geometry(figures):
    sheets = sheetmod.pack(list(figures), "a4")
    return sheets, [sheetmod.sheet_geometry(s, figures) for s in sheets]


def test_a_clean_sheet_is_within_its_margins():
    sheets, geometries = _geometry([a_figure()])
    found = raster_checks.check_geometry(1, geometries[0], {"102", "104"})
    assert not [f for f in found if f.code == "outside_margins"]


def test_ink_in_the_margin_is_caught():
    sheets, geometries = _geometry([a_figure()])
    geometry = copy.deepcopy(geometries[0])
    geometry["lines"].append({"points": [(2.0, 2.0), (8.0, 8.0)], "role": "outline",
                              "owner": "102", "figure": "FIG. 1", "width": 0.45})
    found = raster_checks.check_geometry(1, geometry, {"102", "104"})
    assert any(f.code == "outside_margins" for f in found)


def test_a_character_below_the_minimum_height_is_caught():
    """37 CFR 1.84(p)(3): at least 0.32 cm."""
    sheets, geometries = _geometry([a_figure()])
    geometry = copy.deepcopy(geometries[0])
    for item in geometry["texts"]:
        if item["role"] == "numeral":
            item["size"] = MIN_CHAR_MM * 0.5
            break
    found = raster_checks.check_geometry(1, geometry, {"102", "104"})
    assert any(f.code == "numeral_too_small" for f in found)


def test_characters_at_the_minimum_height_are_accepted():
    sheets, geometries = _geometry([a_figure()])
    found = raster_checks.check_geometry(1, geometries[0], {"102", "104"})
    assert not [f for f in found if f.code == "numeral_too_small"]


def test_a_hairline_is_caught():
    sheets, geometries = _geometry([a_figure()])
    geometry = copy.deepcopy(geometries[0])
    geometry["lines"][0]["width"] = 0.02
    found = raster_checks.check_geometry(1, geometry, {"102", "104"})
    assert any(f.code == "line_too_thin" for f in found)


def test_text_that_is_not_permitted_is_reported_as_a_legend():
    sheets, geometries = _geometry([a_figure()])
    geometry = copy.deepcopy(geometries[0])
    geometry["texts"].append({"text": "Confidential draft", "role": "legend", "size": 4.4,
                              "owner": "", "figure": "FIG. 1",
                              "bbox": (50.0, 50.0, 80.0, 54.0)})
    found = raster_checks.check_geometry(1, geometry, {"102", "104"})
    legends = [f for f in found if f.code == "legend_used"]
    assert legends
    assert "Confidential draft" in str(legends[0].detail)


def test_a_figure_caption_is_not_reported_as_a_legend():
    sheets, geometries = _geometry([a_figure()])
    found = raster_checks.check_geometry(1, geometries[0], {"102", "104"})
    legends = [f for f in found if f.code == "legend_used"]
    assert not legends


def test_the_sheet_number_sits_inside_the_sight():
    """37 CFR 1.84(t): in the middle of the top of the sheet, and NOT in the margin."""
    point = sheetmod.sheet_number_point("a4")
    sight = sheetmod.sight("a4")
    assert sight[0] < point[0] < sight[2]
    assert sight[1] <= point[1] <= sight[3]


# ------------------------------------------------------------------------------ lead lines


def test_crossed_lead_lines_are_caught():
    """37 CFR 1.84(q): lead lines must not cross each other."""
    sheets, geometries = _geometry([a_figure()])
    geometry = copy.deepcopy(geometries[0])
    geometry["leaders"] = [
        {"numeral": "102", "figure": "FIG. 1", "points": [(40.0, 40.0), (80.0, 80.0)]},
        {"numeral": "104", "figure": "FIG. 1", "points": [(40.0, 80.0), (80.0, 40.0)]},
    ]
    found = raster_checks.check_geometry(1, geometry, {"102", "104"})
    assert any(f.code == "leaders_cross" for f in found)


def test_a_lead_line_that_stops_short_is_caught():
    sheets, geometries = _geometry([a_figure()])
    geometry = copy.deepcopy(geometries[0])
    for leader in geometry["leaders"]:
        leader["points"] = [leader["points"][0], (leader["points"][-1][0] + 25.0,
                                                  leader["points"][-1][1] + 25.0)]
    found = raster_checks.check_geometry(1, geometry, {"102", "104"})
    assert any(f.code == "leader_not_touching" for f in found)


def test_the_solver_produces_lead_lines_that_touch_their_parts():
    figure = a_figure()
    for leader in figure.leaders:
        target = [poly for prim in figure.prims if prim.owner == leader.numeral
                  for poly in prim.polys()]
        assert target
        from fm import geom
        assert min(geom.dist_point_polyline(leader.tip(), poly) for poly in target) < 0.05


def test_the_solver_keeps_numerals_off_the_geometry():
    figure = a_figure()
    obstacles = leaders_mod.Obstacles.build(figure)
    for label in figure.labels:
        ink, hatch = obstacles.under(label.bbox())
        assert ink == 0 and hatch == 0, f"{label.numeral} sits on a line"


def test_the_solver_never_crosses_its_own_lead_lines():
    from fm import geom

    figure = a_figure(numerals=("102", "104", "106", "108"))
    for i, first in enumerate(figure.leaders):
        for second in figure.leaders[i + 1:]:
            assert not geom.segments_cross(first.tail(), first.tip(),
                                           second.tail(), second.tip())


def test_the_solver_is_deterministic():
    first = a_figure(numerals=("102", "104", "106"))
    second = a_figure(numerals=("102", "104", "106"))
    assert [lab.to_dict() for lab in first.labels] == [lab.to_dict() for lab in second.labels]


def test_a_numeral_moved_by_hand_keeps_its_lead_line_on_the_part():
    from fm import geom

    figure = a_figure()
    leaders_mod.replace_one(figure, "102", (5.0, 60.0))
    label = figure.label_for("102")
    assert label is not None and label.placed_by == "user"
    leader = figure.leader_for("102")
    target = [poly for prim in figure.prims if prim.owner == "102" for poly in prim.polys()]
    assert min(geom.dist_point_polyline(leader.tip(), poly) for poly in target) < 0.05


# ----------------------------------------------------------------------------- the rule table


def test_every_rule_in_the_table_states_its_authority():
    """A finding with no authority behind it is an opinion dressed up as a citation."""
    for code, rule in rules.RULES.items():
        assert rule.basis in ("rule", "practice"), code
        assert rule.title, f"{code} has no statement of what it requires"
        if rule.basis == "rule":
            assert rule.cite.startswith("37 CFR"), f"{code} claims to be a rule but cites nothing"


def test_every_code_the_checkers_emit_is_in_the_rule_table():
    """A finding the UI cannot look up is a finding nobody can act on."""
    import re
    from pathlib import Path

    root = Path(data_checks.__file__).parent
    emitted: set[str] = set()
    for path in (root / "data.py", root / "raster.py"):
        body = path.read_text(encoding="utf-8")
        emitted |= set(re.findall(r'_finding\(\s*"([a-z_]+)"', body))
    missing = sorted(emitted - set(rules.RULES))
    assert not missing, f"no rule recorded for {missing}"


def test_practice_thresholds_are_not_presented_as_rules():
    assert rules.RULES["line_too_thin"].basis == "practice"
    assert rules.RULES["numeral_too_small"].basis == "rule"
    assert rules.RULES["numeral_too_small"].cite == "37 CFR 1.84(p)(3)"


def test_the_minimum_character_height_is_the_one_the_rule_states():
    assert rules.MIN_CHARACTER_MM == pytest.approx(3.2)


# ------------------------------------------------------------------- anchors on real geometry


@pytest.mark.parametrize("shape", ["box", "rounded", "diamond", "ellipse", "parallelogram",
                                   "stadium", "hexagon", "cylinder"])
def test_every_node_shape_puts_its_anchors_on_its_own_outline(shape):
    """A lead line must reach the feature, not the corner of its bounding box.

    A diamond's bounding-box corner is millimetres clear of the diamond, so anchors taken from
    the box leave the lead line stopping in mid-air, which is what 37 CFR 1.84(q) forbids.
    """
    from fm import geom
    from fm.render import graphfig

    outline = graphfig.node_outline(shape, 50.0, 40.0, 34.0, 18.0)
    anchors = graphfig._box_anchors("102", outline)
    assert anchors, shape
    ring = list(outline) + [outline[0]]
    for anchor in anchors:
        assert geom.dist_point_polyline(anchor.point, ring) < 0.05, \
            f"{shape}: anchor {anchor.point} is off the outline"


@pytest.mark.parametrize("shape", ["diamond", "ellipse", "hexagon"])
def test_the_solver_reaches_a_non_rectangular_shape(shape):
    from fm import geom
    from fm.render import graphfig

    figure = Figure(label="FIG. 1", kind="block_diagram")
    outline = graphfig.node_outline(shape, 40.0, 30.0, 36.0, 22.0)
    figure.prims.append(polygon(outline, role="outline", owner="102"))
    figure.anchors["102"] = graphfig._box_anchors("102", outline)
    leaders_mod.solve(figure)
    leader = figure.leader_for("102")
    assert leader is not None
    ring = list(outline) + [outline[0]]
    assert geom.dist_point_polyline(leader.tip(), ring) < rules.LEADER_TOUCH_MM
