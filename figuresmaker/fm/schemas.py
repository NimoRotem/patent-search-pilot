"""The contracts between stages.

Two families live here. The registry and the section split are what the draft is turned into; the
plan and the scenes are what a model is allowed to say. Every model-facing schema is deliberately
narrow: a planner that can only choose from seven figure types and a renderer that can only build
from a fixed primitive library are a planner and a renderer whose output you can check.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

FigureKind = Literal["block_diagram", "flowchart", "sequence", "perspective", "exploded",
                     "cross_section", "ui_screen"]

FIGURE_KINDS: tuple[str, ...] = ("block_diagram", "flowchart", "sequence", "perspective",
                                 "exploded", "cross_section", "ui_screen")

RELATION_KINDS: tuple[str, ...] = ("contains", "connects", "flows_to", "sends", "attached_to",
                                   "coaxial_with", "adjacent_to", "supports", "part_of")


# ------------------------------------------------------------------------------------ sections


class Paragraph(BaseModel):
    index: int
    section: str                      # field | background | summary | brief | detailed | claims
    text: str
    start: int
    end: int
    figures: list[str] = Field(default_factory=list)   # figure labels this paragraph discusses


class ClaimElement(BaseModel):
    text: str
    term: str = ""
    numeral: str = ""


class Claim(BaseModel):
    number: int
    independent: bool = True
    depends_on: Optional[int] = None
    text: str = ""
    elements: list[ClaimElement] = Field(default_factory=list)


class BriefItem(BaseModel):
    label: str                        # "FIG. 1"
    text: str
    kind_hint: str = ""               # what the sentence itself calls the view


class Sections(BaseModel):
    title: str = ""
    abstract: str = ""
    field: str = ""
    background: str = ""
    summary: str = ""
    brief: str = ""
    brief_items: list[BriefItem] = Field(default_factory=list)
    detailed: str = ""
    claims_text: str = ""
    claims: list[Claim] = Field(default_factory=list)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    raw: str = ""
    source: str = "text"              # text | url | patent_number | pdf
    source_ref: str = ""


# ------------------------------------------------------------------------------------ registry


class RefEntry(BaseModel):
    numeral: str
    term: str
    aliases: list[str] = Field(default_factory=list)
    figures: list[str] = Field(default_factory=list)
    mentions: int = 0
    sections: list[str] = Field(default_factory=list)
    first_offset: int = 0
    evidence: list[str] = Field(default_factory=list)


class Conflict(BaseModel):
    """Something the draft says twice and differently, or does not say at all.

    ``stage`` is who can fix it. A conflict the author has to resolve is not a bug in the
    renderer, and saying so is the difference between a useful report and a wall of red.
    """
    code: str
    severity: Literal["error", "warning", "info"] = "error"
    message: str
    numeral: str = ""
    term: str = ""
    stage: Literal["draft", "registry", "planner", "renderer", "placement"] = "draft"
    evidence: list[str] = Field(default_factory=list)
    cite: str = ""


class UnnumberedElement(BaseModel):
    term: str
    evidence: str = ""
    suggested_numeral: str = ""


class Registry(BaseModel):
    entries: list[RefEntry] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    unnumbered: list[UnnumberedElement] = Field(default_factory=list)

    def by_numeral(self) -> dict[str, RefEntry]:
        return {e.numeral: e for e in self.entries}

    def term_for(self, numeral: str) -> str:
        entry = self.by_numeral().get(numeral)
        return entry.term if entry else ""


# --------------------------------------------------------------- what the extractor may return


class ExtractedRef(BaseModel):
    numeral: str
    term: str
    aliases: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    refs: list[ExtractedRef] = Field(default_factory=list)
    unnumbered: list[UnnumberedElement] = Field(default_factory=list)


class ClaimSplit(BaseModel):
    number: int
    elements: list[ClaimElement] = Field(default_factory=list)


class ClaimSplitResult(BaseModel):
    claims: list[ClaimSplit] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------- plan


class PlanElement(BaseModel):
    numeral: str
    term: str = ""
    role: str = "part"                # part | container | step | actor | screen | region | signal
    note: str = ""                    # a shape or placement hint for the renderer


class PlanRelation(BaseModel):
    kind: str
    source: str
    target: str
    label: str = ""


class Conventions(BaseModel):
    hatching: bool = False
    flow_arrows: bool = False
    hidden_lines: bool = False
    section_line: str = ""            # "A-A", drawn on the parent figure
    exploded_axis: str = "y"


class FigurePlan(BaseModel):
    label: str
    kind: FigureKind
    title: str = ""
    view: str = ""
    parent: str = ""                  # the figure a section or detail is taken from
    elements: list[PlanElement] = Field(default_factory=list)
    relations: list[PlanRelation] = Field(default_factory=list)
    conventions: Conventions = Field(default_factory=Conventions)


class Plan(BaseModel):
    figures: list[FigurePlan] = Field(default_factory=list)
    proposed_brief_description: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # How many views the draft actually asked for, when that was more than this run would draw.
    # A cap that binds and says nothing reads as "we drew everything" to whoever gets the report.
    truncated_from: int = 0


# -------------------------------------------------------------------------------------- scenes
#
# One schema per renderer. These are what a model fills in once the plan has said what a figure
# is for; they are the last point at which a model touches the drawing, and every field is either
# a name from a fixed library or a number the renderer will clamp.


class GraphNode(BaseModel):
    numeral: str
    label: str = ""
    shape: Literal["box", "rounded", "diamond", "ellipse", "parallelogram", "cylinder",
                   "stadium", "hexagon"] = "box"
    parent: str = ""                  # the numeral of the block that encloses this one


class GraphEdge(BaseModel):
    source: str
    target: str
    label: str = ""
    arrow: bool = True
    dashed: bool = False


class GraphScene(BaseModel):
    direction: Literal["TB", "LR"] = "TB"
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class SeqActor(BaseModel):
    numeral: str
    label: str = ""


class SeqMessage(BaseModel):
    source: str
    target: str
    label: str = ""
    dashed: bool = False              # a return, drawn broken by convention


class SeqScene(BaseModel):
    actors: list[SeqActor] = Field(default_factory=list)
    messages: list[SeqMessage] = Field(default_factory=list)


PART_NAMES: tuple[str, ...] = (
    "box", "plate", "cylinder", "tube", "cone", "sphere", "dome", "prism", "wedge", "torus",
    "rod", "disc", "washer", "screw", "nut", "bearing", "spring", "gear", "pulley", "shaft",
    "flange", "bracket", "pcb", "connector", "button", "knob", "handle", "lever", "nozzle",
    "hose", "bellows", "suction_cup", "motor", "piston", "valve", "hinge", "wheel", "housing",
)


class Solid(BaseModel):
    """One placed primitive.

    ``numeral`` may be empty: a fillet, a boss or a mounting lug is part of the picture without
    being a claimed element, and forcing every solid to carry a numeral would either invent
    numerals or leave features out.
    """
    id: str
    numeral: str = ""
    part: str = "box"
    params: dict[str, float] = Field(default_factory=dict)
    at: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotate: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])


class SectionSpec(BaseModel):
    axis: Literal["x", "y", "z"] = "z"
    offset: float = 0.0
    keep: Literal["negative", "positive"] = "negative"
    name: str = "A-A"


class ExplodeSpec(BaseModel):
    axis: Literal["x", "y", "z"] = "y"
    gap: float = 25.0
    order: list[str] = Field(default_factory=list)     # solid ids, bottom of the axis first


class MechScene(BaseModel):
    camera: Literal["isometric", "front", "top", "right", "dimetric", "trimetric"] = "isometric"
    solids: list[Solid] = Field(default_factory=list)
    section: Optional[SectionSpec] = None
    explode: Optional[ExplodeSpec] = None
    hidden_lines: bool = False


UI_TYPES: tuple[str, ...] = (
    "screen", "window", "titlebar", "panel", "row", "column", "label", "heading", "textfield",
    "textarea", "button", "list", "listitem", "table", "checkbox", "radio", "dropdown", "toggle",
    "slider", "progress", "tab_bar", "tab", "nav_bar", "icon", "image", "chart", "map", "card",
    "badge", "search", "avatar", "divider",
)


class UINode(BaseModel):
    id: str = ""
    numeral: str = ""
    type: str = "panel"
    label: str = ""
    weight: float = 1.0
    direction: Literal["row", "column"] = "column"
    children: list["UINode"] = Field(default_factory=list)


UINode.model_rebuild()


class UIScene(BaseModel):
    device: Literal["screen", "window", "phone", "tablet"] = "window"
    root: UINode = Field(default_factory=UINode)


# --------------------------------------------------------------------------------- validation


class Finding(BaseModel):
    """One thing wrong with the output, and who has to fix it."""
    code: str
    severity: Literal["error", "warning", "info"] = "error"
    message: str
    stage: Literal["draft", "registry", "planner", "renderer", "placement", "layout"] = "renderer"
    figure: str = ""
    numeral: str = ""
    cite: str = ""
    basis: Literal["rule", "practice"] = "rule"
    detail: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    passed: bool = True
    checked: list[str] = Field(default_factory=list)

    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]
