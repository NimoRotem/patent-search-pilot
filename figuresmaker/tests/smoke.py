"""Drive every renderer from hand-written scenes and write the sheets out.

No model is called. This is how a change to the geometry is checked: build the same four scenes,
render them, run the validator, and look at the SVG. Run it with

    ./.venv/bin/python -m tests.smoke /tmp/fm-smoke
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from fm import validate
from fm.drawing import Figure
from fm.render import render, sheet as sheetmod
from fm.schemas import (Claim, ClaimElement, Conventions, FigurePlan, GraphEdge, GraphNode,
                        GraphScene, MechScene, Plan, PlanElement, RefEntry, Registry, Sections,
                        SectionSpec, ExplodeSpec, SeqActor, SeqMessage, SeqScene, Solid, UINode,
                        UIScene)

REGISTRY = Registry(entries=[
    RefEntry(numeral="100", term="gripping apparatus", figures=["FIG. 1"]),
    RefEntry(numeral="102", term="housing", figures=["FIG. 1", "FIG. 3"]),
    RefEntry(numeral="104", term="vacuum pump", figures=["FIG. 1", "FIG. 3"]),
    RefEntry(numeral="106", term="controller", figures=["FIG. 1"]),
    RefEntry(numeral="108", term="pressure sensor", figures=["FIG. 1"]),
    RefEntry(numeral="110", term="battery", figures=["FIG. 1"]),
    RefEntry(numeral="112", term="suction cup", figures=["FIG. 2", "FIG. 3"]),
    RefEntry(numeral="114", term="seal", figures=["FIG. 3"]),
    RefEntry(numeral="116", term="handle", figures=["FIG. 2"]),
    RefEntry(numeral="118", term="display", figures=["FIG. 4"]),
    RefEntry(numeral="202", term="drive shaft", figures=["FIG. 2"]),
    RefEntry(numeral="302", term="receiving a start signal", figures=["FIG. 5"]),
    RefEntry(numeral="304", term="evacuating the chamber", figures=["FIG. 5"]),
    RefEntry(numeral="306", term="comparing the pressure to a threshold", figures=["FIG. 5"]),
    RefEntry(numeral="308", term="signalling a fault", figures=["FIG. 5"]),
    RefEntry(numeral="310", term="holding the load", figures=["FIG. 5"]),
    RefEntry(numeral="402", term="pressure readout", figures=["FIG. 4"]),
    RefEntry(numeral="404", term="release control", figures=["FIG. 4"]),
    RefEntry(numeral="406", term="status list", figures=["FIG. 4"]),
    RefEntry(numeral="502", term="remote terminal", figures=["FIG. 6"]),
])


def block_figure() -> tuple[FigurePlan, GraphScene]:
    plan = FigurePlan(label="FIG. 1", kind="block_diagram",
                      title="a gripping apparatus", elements=[
                          PlanElement(numeral=n, term=REGISTRY.term_for(n))
                          for n in ("100", "102", "104", "106", "108", "110")])
    scene = GraphScene(direction="TB", nodes=[
        GraphNode(numeral="100", label="gripping apparatus", shape="box"),
        GraphNode(numeral="102", label="housing", shape="box", parent="100"),
        GraphNode(numeral="104", label="vacuum pump", shape="box", parent="102"),
        GraphNode(numeral="106", label="controller", shape="box", parent="102"),
        GraphNode(numeral="108", label="pressure sensor", shape="box", parent="102"),
        GraphNode(numeral="110", label="battery", shape="cylinder"),
    ], edges=[
        GraphEdge(source="106", target="104", label="drive"),
        GraphEdge(source="108", target="106", label="pressure"),
        GraphEdge(source="110", target="106", arrow=False),
    ])
    return plan, scene


def flow_figure() -> tuple[FigurePlan, GraphScene]:
    plan = FigurePlan(label="FIG. 5", kind="flowchart", title="a method of gripping a load",
                      conventions=Conventions(flow_arrows=True), elements=[
                          PlanElement(numeral=n, term=REGISTRY.term_for(n))
                          for n in ("302", "304", "306", "308", "310")])
    scene = GraphScene(direction="TB", nodes=[
        GraphNode(numeral="302", label="receive start signal", shape="rounded"),
        GraphNode(numeral="304", label="evacuate the chamber", shape="box"),
        GraphNode(numeral="306", label="pressure below threshold?", shape="diamond"),
        GraphNode(numeral="308", label="signal a fault", shape="box"),
        GraphNode(numeral="310", label="hold the load", shape="rounded"),
    ], edges=[
        GraphEdge(source="302", target="304"),
        GraphEdge(source="304", target="306"),
        GraphEdge(source="306", target="310", label="Yes"),
        GraphEdge(source="306", target="308", label="No"),
    ])
    return plan, scene


def perspective_figure() -> tuple[FigurePlan, MechScene]:
    plan = FigurePlan(label="FIG. 2", kind="perspective", view="isometric",
                      title="the gripping apparatus", elements=[
                          PlanElement(numeral=n, term=REGISTRY.term_for(n))
                          for n in ("102", "112", "116", "202")])
    scene = MechScene(camera="isometric", solids=[
        Solid(id="102", numeral="102", part="housing",
              params={"w": 90, "h": 34, "d": 60, "t": 4}, at=[0, 12, 0]),
        Solid(id="112", numeral="112", part="suction_cup",
              params={"r": 34, "h": 18}, at=[0, -14, 0]),
        Solid(id="116", numeral="116", part="handle", params={"r": 5, "len": 70},
              at=[0, 44, 0]),
        Solid(id="202", numeral="202", part="shaft", params={"r": 6, "h": 26}, at=[0, 0, 0]),
    ])
    return plan, scene


def section_figure() -> tuple[FigurePlan, MechScene]:
    plan = FigurePlan(label="FIG. 3", kind="cross_section", parent="FIG. 2",
                      title="the apparatus taken along A-A",
                      conventions=Conventions(hatching=True, section_line="A-A"), elements=[
                          PlanElement(numeral=n, term=REGISTRY.term_for(n))
                          for n in ("102", "104", "112", "114")])
    scene = MechScene(camera="front", solids=[
        Solid(id="102", numeral="102", part="housing",
              params={"w": 90, "h": 34, "d": 60, "t": 4}, at=[0, 12, 0]),
        Solid(id="104", numeral="104", part="motor",
              params={"r": 13, "h": 24, "shaft_r": 3, "shaft_h": 10}, at=[0, 14, 0]),
        Solid(id="112", numeral="112", part="suction_cup", params={"r": 34, "h": 18},
              at=[0, -14, 0]),
        Solid(id="114", numeral="114", part="torus", params={"R": 32, "r": 3}, at=[0, -23, 0]),
    ], section=SectionSpec(axis="z", offset=0.0, keep="negative", name="A-A"))
    return plan, scene


def exploded_figure() -> tuple[FigurePlan, MechScene]:
    plan = FigurePlan(label="FIG. 6", kind="exploded", title="the apparatus, exploded",
                      conventions=Conventions(exploded_axis="y"), elements=[
                          PlanElement(numeral=n, term=REGISTRY.term_for(n))
                          for n in ("102", "104", "112")])
    scene = MechScene(camera="isometric", solids=[
        Solid(id="102", numeral="102", part="housing",
              params={"w": 90, "h": 34, "d": 60, "t": 4}, at=[0, 12, 0]),
        Solid(id="104", numeral="104", part="motor",
              params={"r": 13, "h": 24, "shaft_r": 3, "shaft_h": 10}, at=[0, 14, 0]),
        Solid(id="112", numeral="112", part="suction_cup", params={"r": 34, "h": 18},
              at=[0, -14, 0]),
    ], explode=ExplodeSpec(axis="y", gap=48.0, order=["112", "102", "104"]))
    return plan, scene


def ui_figure() -> tuple[FigurePlan, UIScene]:
    plan = FigurePlan(label="FIG. 4", kind="ui_screen", title="a display screen",
                      elements=[PlanElement(numeral=n, term=REGISTRY.term_for(n))
                                for n in ("118", "402", "404", "406")])
    scene = UIScene(device="window", root=UINode(
        id="root", numeral="118", type="window", direction="column", children=[
            UINode(type="titlebar", label="Grip status", weight=0.5),
            UINode(numeral="402", type="chart", label="Pressure", weight=2.0),
            UINode(numeral="406", type="list", label="Recent lifts", weight=2.2),
            UINode(type="row", direction="row", weight=0.8, children=[
                UINode(numeral="404", type="button", label="Release"),
                UINode(type="button", label="Cancel"),
            ]),
        ]))
    return plan, scene


def sequence_figure() -> tuple[FigurePlan, SeqScene]:
    plan = FigurePlan(label="FIG. 7", kind="sequence", title="the exchange with a terminal",
                      elements=[PlanElement(numeral=n, term=REGISTRY.term_for(n))
                                for n in ("106", "502")])
    scene = SeqScene(actors=[SeqActor(numeral="106", label="controller"),
                             SeqActor(numeral="502", label="remote terminal")],
                     messages=[
                         SeqMessage(source="502", target="106", label="request a lift"),
                         SeqMessage(source="106", target="502", label="acknowledge", dashed=True),
                         SeqMessage(source="106", target="106", label="evacuate"),
                         SeqMessage(source="106", target="502", label="report the pressure",
                                    dashed=True),
                     ])
    return plan, scene


BUILDERS = (block_figure, perspective_figure, section_figure, ui_figure, flow_figure,
            exploded_figure, sequence_figure)


def main(out_dir: str = "/tmp/fm-smoke") -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    figures: list[Figure] = []
    plans: list[FigurePlan] = []
    findings = []
    failures = 0

    for builder in BUILDERS:
        plan, scene = builder()
        started = time.monotonic()
        try:
            figure, stage_findings = render(plan, scene)
        except Exception as exc:
            print(f"  FAIL {plan.label} ({plan.kind}): {type(exc).__name__}: {exc}")
            failures += 1
            continue
        elapsed = time.monotonic() - started
        box = figure.content_bbox()
        print(f"  ok   {plan.label:8s} {plan.kind:14s} {len(figure.prims):5d} prims  "
              f"{len(figure.labels):2d} numerals  "
              f"{box[2] - box[0]:6.1f}x{box[3] - box[1]:6.1f} mm  {elapsed:5.2f}s  "
              f"{len(stage_findings)} finding(s)")
        for item in stage_findings:
            print(f"         - [{item.severity}] {item.message}")
        figures.append(figure)
        plans.append(plan)
        findings.extend(stage_findings)
        (out / f"{plan.label.replace('. ', '').replace(' ', '')}.svg").write_text(
            sheetmod.figure_svg(figure), encoding="utf-8")

    figures.sort(key=lambda f: __import__("fm.sections", fromlist=["x"]).figure_sort_key(f.label))
    sheets = sheetmod.pack(figures, "a4")
    for sheet in sheets:
        (out / f"sheet-{sheet.number}.svg").write_text(
            sheetmod.sheet_svg(sheet, figures), encoding="utf-8")
    print(f"\n  {len(sheets)} sheet(s) written to {out}")

    claims = [Claim(number=1, independent=True, text="A gripping apparatus.", elements=[
        ClaimElement(text="a housing", term="housing", numeral="102"),
        ClaimElement(text="a vacuum pump", term="vacuum pump", numeral="104"),
        ClaimElement(text="a suction cup", term="suction cup", numeral="112"),
        ClaimElement(text="a controller", term="controller", numeral="106"),
    ])]
    plan_all = Plan(figures=plans)
    report = validate.validate(figures, plan_all, REGISTRY, claims, Sections(raw=""),
                              sheets=sheets)
    print(f"\n  validation: {len(report.errors())} error(s), {len(report.warnings())} warning(s), "
          f"{len([f for f in report.findings if f.severity == 'info'])} info")
    for item in report.findings[:40]:
        print(f"    [{item.severity:7s}] {item.code:26s} {item.cite:20s} {item.message[:110]}")
    return failures


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fm-smoke"))
