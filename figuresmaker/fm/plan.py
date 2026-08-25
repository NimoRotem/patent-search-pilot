"""Deciding what the figures are, and then what is in each one.

Two model calls, deliberately separated. The first says which views exist and which numerals
belong to each: that is a reading of the draft, and it is grounded in the brief description of
the drawings whenever the draft has one, because the brief description is the applicant's own
statement of what the figures are and inventing a different set would be a change to the
specification.

The second builds one scene per figure, and it is the last point at which a model touches a
drawing. It cannot draw. It can only fill in a schema whose every field is either a name from a
fixed library or a number the renderer will clamp, which is what makes the picture repeatable:
the same draft gives the same geometry, and the geometry can be checked.

When there is no brief description, figures are generated so that every element of every
independent claim appears in at least one of them, because 37 CFR 1.83(a) requires the drawing to
show every feature specified in the claims, and a proposed brief description is written back for
the author to paste in.
"""
from __future__ import annotations

import re
from typing import Optional

from . import llm
from .schemas import (Claim, Conventions, FigurePlan, GraphScene, MechScene, PART_NAMES, Plan,
                      PlanElement, Registry, Sections, SeqScene, UIScene, UI_TYPES)
from .sections import figure_sort_key

MAX_FIGURES = int(__import__("os").environ.get("FM_MAX_FIGURES", "20"))
MAX_ELEMENTS_PER_FIGURE = 18

PLAN_SYSTEM = """You plan the drawings for a patent application. You decide which views exist \
and what each one contains. You do not draw.

INPUTS you are given: the reference numeral registry (the only numerals that exist), the brief \
description of the drawings if the draft has one, the independent claims broken into elements, \
and the description.

RULES
1. If a BRIEF DESCRIPTION OF THE DRAWINGS is supplied, it is authoritative. Produce exactly \
those figures, with those labels, and set each one's kind from what the sentence says it is. Do \
not add, drop, merge or renumber a figure.
2. If there is no brief description, choose the smallest set of figures such that every element \
of every independent claim appears in at least one figure. Label them FIG. 1, FIG. 2, ... in \
order. A system or apparatus case normally opens with an overall view; a method claim needs a \
flowchart; a user interface claim needs a screen.
3. elements: use ONLY numerals from the registry. Put a numeral in every figure that shows that \
part. A numeral may appear in several figures; that is normal and correct. Do not put more than \
18 numerals in one figure: split the view instead.
4. relations: how the elements in that figure stand to each other. kind is one of contains, \
connects, flows_to, sends, attached_to, coaxial_with, adjacent_to, supports, part_of. Use \
"contains" for a part inside another part and for a block inside a subsystem. Use "flows_to" \
only in a flowchart, "sends" only in a sequence diagram.
5. kind is one of: block_diagram, flowchart, sequence, perspective, exploded, cross_section, \
ui_screen. Choose by what the figure has to communicate, not by what looks impressive. \
Electronics, software architecture and networks are block diagrams. A method is a flowchart. A \
protocol between parties is a sequence. A physical thing is a perspective; an assembly whose \
parts must be seen separately is exploded; an internal detail is a cross_section, and then set \
conventions.hatching true, parent to the figure it is taken from, and conventions.section_line \
to a name such as "A-A".
6. title: one line saying what the view shows, in the register of a brief description.
7. proposed_brief_description: one sentence per figure, in the form "FIG. 1 is a block diagram \
of a ...". Always fill this in, even when the draft already has a brief description, so the two \
can be compared.
8. notes: anything the author has to decide that you could not. Be specific.

Return JSON only."""


def _registry_block(registry: Registry, limit: int = 240) -> str:
    rows = []
    for entry in registry.entries[:limit]:
        figures = ", ".join(entry.figures) if entry.figures else "-"
        rows.append(f"{entry.numeral}\t{entry.term}\t[{figures}]\t{entry.mentions}x")
    head = "numeral\tterm\tfigures the description ties it to\tmentions\n"
    return head + "\n".join(rows)


def _claims_block(claims: list[Claim]) -> str:
    rows = []
    for claim in claims:
        if not claim.independent:
            continue
        rows.append(f"CLAIM {claim.number}")
        for element in claim.elements:
            numeral = element.numeral or "?"
            rows.append(f"  [{numeral}] {element.term or ''} :: {element.text[:180]}")
    return "\n".join(rows) if rows else "(no independent claims were found)"


def _brief_block(sections: Sections) -> str:
    if not sections.brief_items:
        return "(the draft has no brief description of the drawings)"
    return "\n".join(f"{item.label}\t{item.text}" for item in sections.brief_items)


def build_plan(sections: Sections, registry: Registry, claims: list[Claim],
               reasoner: Optional[llm.Reasoner] = None,
               feedback: str = "") -> Plan:
    context = (
        f"TITLE\n{sections.title or '(untitled)'}\n\n"
        f"BRIEF DESCRIPTION OF THE DRAWINGS\n{_brief_block(sections)}\n\n"
        f"REFERENCE NUMERAL REGISTRY\n{_registry_block(registry)}\n\n"
        f"INDEPENDENT CLAIM ELEMENTS\n{_claims_block(claims)}\n\n"
        f"DESCRIPTION\n{_description_budget(sections)}")
    if feedback:
        context += ("\n\nThe previous plan was rejected by the compliance checker. Fix exactly "
                    f"these problems:\n{feedback}")
    if reasoner is None:
        reasoner = llm.deep()
    plan = reasoner.structured("figure_plan", Plan, PLAN_SYSTEM, context, max_tokens=16000)
    return tidy_plan(plan, sections, registry)


def _description_budget(sections: Sections, limit: int = 40000) -> str:
    text = sections.detailed or sections.raw
    if len(text) <= limit:
        return text
    return (text[: int(limit * 0.65)] + "\n\n[... middle of the description omitted ...]\n\n"
            + text[-int(limit * 0.35):])


def tidy_plan(plan: Plan, sections: Sections, registry: Registry) -> Plan:
    """Everything about a plan that can be fixed without asking again.

    A model that returns "Figure 1" instead of "FIG. 1", or a numeral that is not in the
    registry, has made a clerical error. Correcting it here keeps the retry budget for the
    mistakes that actually need another opinion.
    """
    known = registry.by_numeral()
    figures: list[FigurePlan] = []
    seen_labels: set[str] = set()
    if len(plan.figures) > MAX_FIGURES:
        plan.truncated_from = len(plan.figures)

    for figure in plan.figures[:MAX_FIGURES]:
        label = _tidy_label(figure.label, len(figures) + 1)
        if label in seen_labels:
            continue
        seen_labels.add(label)
        elements: list[PlanElement] = []
        for element in figure.elements:
            numeral = (element.numeral or "").strip()
            if numeral not in known:
                continue
            if any(e.numeral == numeral for e in elements):
                continue
            elements.append(PlanElement(numeral=numeral, term=known[numeral].term,
                                        role=element.role or "part", note=element.note))
        elements = elements[:MAX_ELEMENTS_PER_FIGURE]
        allowed = {e.numeral for e in elements}
        relations = [r for r in figure.relations
                     if r.source in allowed and r.target in allowed and r.source != r.target]
        figure.label = label
        figure.elements = elements
        figure.relations = _dedupe_relations(relations)
        figure.conventions = figure.conventions or Conventions()
        if figure.kind == "cross_section":
            figure.conventions.hatching = True
            if not figure.conventions.section_line:
                figure.conventions.section_line = _section_name(len(figures))
        if figure.kind in ("flowchart",):
            figure.conventions.flow_arrows = True
        figures.append(figure)

    figures.sort(key=lambda f: figure_sort_key(f.label))
    plan.figures = figures
    if not plan.proposed_brief_description:
        plan.proposed_brief_description = [
            f"{f.label} {_brief_verb(f.kind)} {f.title.rstrip('.')}." for f in figures]
    return plan


_LABEL = re.compile(r"(\d+)\s*([A-Za-z]?)")


def _tidy_label(raw: str, ordinal: int) -> str:
    match = _LABEL.search(raw or "")
    if not match:
        return f"FIG. {ordinal}"
    return f"FIG. {int(match.group(1))}{match.group(2).upper()}"


def _section_name(index: int) -> str:
    letter = chr(ord("A") + (index % 26))
    return f"{letter}-{letter}"


def _brief_verb(kind: str) -> str:
    return {
        "block_diagram": "is a block diagram of",
        "flowchart": "is a flow chart of",
        "sequence": "is a sequence diagram of",
        "perspective": "is a perspective view of",
        "exploded": "is an exploded perspective view of",
        "cross_section": "is a cross-sectional view of",
        "ui_screen": "is a view of a display screen showing",
    }.get(kind, "shows")


def _dedupe_relations(relations):
    seen = set()
    out = []
    for relation in relations:
        key = (relation.kind, relation.source, relation.target)
        if key in seen:
            continue
        seen.add(key)
        out.append(relation)
    return out


# --------------------------------------------------------------------------------------- scenes

_COMMON_SCENE_RULES = """
You are filling in one figure of a patent drawing set. You are given the figure's plan: its kind,
its title, the elements it must show by reference numeral, and how they stand to each other.

HARD RULES
* Use ONLY the numerals listed in the plan. Never introduce a numeral of your own.
* Every listed numeral must appear exactly once as the thing that carries it.
* The drawing is line art in black on white. There is no colour, no shading and no photograph.
* Keep it legible. A patent figure that shows twelve things clearly is worth more than one that
  shows thirty badly.
Return JSON only."""

GRAPH_SYSTEM = """You lay out a patent block diagram or flow chart as a graph.""" + \
    _COMMON_SCENE_RULES + """

nodes: one per numeral in the plan. label is the short name of the part, two to five words, as
the description calls it; it is printed inside the box. shape: "box" for a component or a step,
"diamond" for a decision, "rounded" for a start or end terminal of a flow chart, "cylinder" for a
store or database, "parallelogram" for input or output, "stadium" for an external actor,
"hexagon" for a preparation step. parent: the numeral of the block that encloses this one, when
the plan says one contains the other; otherwise empty.

edges: one per connection. In a flow chart every edge is a step-to-step transition and arrow is
true; label an edge leaving a diamond "Yes" or "No". In a block diagram, an edge is a signal or
coupling path; set arrow false when the coupling has no direction. dashed for an optional or
wireless path.

direction: "TB" for a flow chart and for a hierarchy, "LR" for a signal chain."""

SEQ_SYSTEM = """You lay out a patent sequence diagram.""" + _COMMON_SCENE_RULES + """

actors: one per numeral, in the left-to-right order the exchange reads best in. label is the
short name.
messages: in time order, top to bottom. label is the message, three to six words. dashed true for
a return or a response."""

MECH_SYSTEM = """You describe a physical assembly as placed solid primitives, from which the \
renderer computes a real orthographic projection with hidden lines removed.""" + \
    _COMMON_SCENE_RULES + """

COORDINATES: right handed, millimetres. X to the right, Y up, Z towards the viewer. Every part is
centred on its own origin; "at" moves that origin; "rotate" is degrees about X then Y then Z,
applied before the move. Keep the whole assembly inside roughly 200 mm in every direction.

PARTS you may use, with their parameters. Anything not on this list does not exist.
  box            w, h, d
  plate          w, d, t                     (thin, t along Y)
  housing        w, h, d, t                  (a box hollowed out, open at +Y)
  cylinder       r, h                        (axis along Y)
  tube           r, ri, h
  cone           r, r2, h                    (r at -Y, r2 at +Y; r2 may be 0)
  sphere         r
  dome           r                           (upper half of a sphere)
  prism          r, h, n                     (regular n-sided, axis along Y)
  wedge          w, h, d
  torus          R, r
  rod            r, h
  disc           r, t
  washer         r, ri, t
  screw          r, h, head_r, head_h
  nut            r, h
  bearing        r, ri, h
  spring         r, h, turns, wire
  gear           r, h, teeth
  pulley         r, h
  shaft          r, h
  flange         r, ri, t, bolt_r, bolts
  bracket        w, h, d, t                  (an L, rising in +Y and running in +Z)
  pcb            w, d, t
  connector      w, h, d, pins
  button         r, h
  knob           r, h
  handle         r, len                      (a U-shaped grip, opening -Y)
  lever          len, w, t
  nozzle         r, r2, h
  hose           r, len, bend
  bellows        r, h, folds
  suction_cup    r, h
  motor          r, h, shaft_r, shaft_h
  piston         r, h, rod_r, rod_h
  valve          r, h
  hinge          len, r
  wheel          r, t

RULES FOR THE ASSEMBLY
* This is a picture of the thing ASSEMBLED. Every part must touch or overlap at least one other
  part in the coordinates you give. Parts floating apart in space is only ever correct for an
  exploded figure, and then only with the explode field filled in.
* Parts must touch or fit where the plan says they are attached, contained or coaxial. Two parts
  that are coaxial share an axis: give them the same X and Z and set them apart in Y.
* A part that contains another must be big enough to hold it. A housing's cavity is its w, h, d
  less twice its wall t.
* KEEP THE PARTS IN PROPORTION. The largest and the smallest part in one figure should be within
  a factor of about fifty of each other. A part whose dimensions come out a hundredth of the size
  of its neighbours is drawn as a dot, and a reference character pointing at a dot shows nothing.
  If a real part genuinely is that small, give it its own detail figure instead.
* You may add solids with an empty numeral for features the drawing needs but no numeral names,
  such as a boss, a lug or a mounting foot. Use this sparingly.
* id must be unique. Use the numeral where there is one.
* camera: "isometric" unless the plan's view says otherwise.
* section: fill this in ONLY for a cross_section figure. axis and offset say where the cutting
  plane is; keep says which half survives. The renderer hatches the cut faces.
* explode: fill this in ONLY for an exploded figure. axis is the direction the parts separate
  along, and order lists the solid ids from one end of that axis to the other."""

UI_SYSTEM = """You describe a user interface figure as a tree of boxes.""" + \
    _COMMON_SCENE_RULES + """

The renderer lays the tree out itself: you give the structure and the relative weights, not
coordinates. A container has direction "row" or "column" and children; a leaf has a type and a
label. weight is that child's share of its parent along the parent's direction.

types you may use: """ + ", ".join(UI_TYPES) + """.

root must be a "screen" or a "window". Give numeral on the node that carries it, and leave
numeral empty on structural boxes that no numeral names. label is the text printed in the
control, kept short."""


_SCENE_FOR = {
    "block_diagram": (GraphScene, GRAPH_SYSTEM, "graph"),
    "flowchart": (GraphScene, GRAPH_SYSTEM, "graph"),
    "sequence": (SeqScene, SEQ_SYSTEM, "sequence"),
    "perspective": (MechScene, MECH_SYSTEM, "mech"),
    "exploded": (MechScene, MECH_SYSTEM, "mech"),
    "cross_section": (MechScene, MECH_SYSTEM, "mech"),
    "ui_screen": (UIScene, UI_SYSTEM, "ui"),
}


def scene_schema(kind: str):
    return _SCENE_FOR.get(kind, _SCENE_FOR["block_diagram"])


def build_scene(figure: FigurePlan, sections: Sections, registry: Registry,
                reasoner: Optional[llm.Reasoner] = None, feedback: str = ""):
    """The renderer-specific payload for one figure."""
    schema, system, _ = scene_schema(figure.kind)
    elements = "\n".join(
        f"{e.numeral}\t{e.term}\t{e.role}\t{e.note}".rstrip() for e in figure.elements)
    relations = "\n".join(f"{r.kind}\t{r.source} -> {r.target}\t{r.label}".rstrip()
                          for r in figure.relations) or "(none stated)"
    context = (
        f"FIGURE {figure.label}\nkind: {figure.kind}\ntitle: {figure.title}\n"
        f"view: {figure.view or '(unspecified)'}\n"
        f"conventions: hatching={figure.conventions.hatching} "
        f"section_line={figure.conventions.section_line or '-'} "
        f"exploded_axis={figure.conventions.exploded_axis}\n\n"
        f"ELEMENTS (numeral, term, role, note)\n{elements}\n\n"
        f"RELATIONS\n{relations}\n\n"
        f"WHAT THE DESCRIPTION SAYS ABOUT THESE PARTS\n{_evidence(figure, registry)}")
    if feedback:
        context += ("\n\nThe previous attempt at this figure was rejected. Fix exactly these "
                    f"problems:\n{feedback}")
    if reasoner is None:
        reasoner = llm.deep()
    return reasoner.structured(f"scene_{figure.kind}", schema, system, context,
                               max_tokens=12000)


def _evidence(figure: FigurePlan, registry: Registry, limit: int = 9000) -> str:
    by_numeral = registry.by_numeral()
    chunks: list[str] = []
    used = 0
    for element in figure.elements:
        entry = by_numeral.get(element.numeral)
        if not entry or not entry.evidence:
            continue
        for line in entry.evidence[:2]:
            piece = f"[{entry.numeral} {entry.term}] {line}"
            if used + len(piece) > limit:
                return "\n".join(chunks)
            chunks.append(piece)
            used += len(piece)
    return "\n".join(chunks) or "(the description says little about these parts)"


def clamp_scene(kind: str, scene):
    """Keep a model's numbers inside what the renderer can draw.

    A model that asks for a 4 000 mm shaft or a gear with 900 teeth is not wrong about the
    invention, it has just lost track of scale. Clamping is silent and safe; the alternative is a
    figure that is technically valid and unreadable.
    """
    if kind in ("perspective", "exploded", "cross_section") and isinstance(scene, MechScene):
        for solid in scene.solids:
            if solid.part not in PART_NAMES:
                solid.part = "box"
            solid.params = {k: _clamp(float(v), 0.05, 600.0)
                            for k, v in (solid.params or {}).items()
                            if isinstance(v, (int, float))}
            solid.at = [_clamp(float(v), -900.0, 900.0) for v in (solid.at or [0, 0, 0])][:3]
            while len(solid.at) < 3:
                solid.at.append(0.0)
            solid.rotate = [float(v) % 360.0 for v in (solid.rotate or [0, 0, 0])][:3]
            while len(solid.rotate) < 3:
                solid.rotate.append(0.0)
        if scene.explode:
            scene.explode.gap = _clamp(scene.explode.gap, 5.0, 200.0)
        seen: set[str] = set()
        for i, solid in enumerate(scene.solids):
            if not solid.id or solid.id in seen:
                solid.id = f"s{i}"
            seen.add(solid.id)
    return scene


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
