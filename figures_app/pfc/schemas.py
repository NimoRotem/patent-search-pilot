"""Typed contracts for every stage of the figure compiler.

Every stage of the pipeline consumes a typed object and returns a typed object. Nothing crosses
a stage boundary as prose, and nothing an LLM returns is used before it has passed validation
here (spec: "Every model response must pass Pydantic validation").

Three objects carry the whole design, and they are deliberately kept apart:

  ``PatentGraph``  semantic truth, extracted from the document and traceable to it
  ``FigureSpec``   which part of that truth one figure shows
  ``LayoutScene``  where it sits on the sheet

A layout change must never alter a ``FigureSpec``, and a ``FigureSpec`` must never contain a
coordinate. That separation is what makes the correction loop safe: it can move a label without
being able to change what the label means.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "pfc-1"


class Strict(BaseModel):
    """Reject anything the schema does not name.

    A model that invents a field is a model that has misunderstood the task, and silently
    dropping the field hides that.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# source document
# ---------------------------------------------------------------------------
class Paragraph(Strict):
    """One addressable unit of the source document.

    ``id`` is stable for the life of a job (``p0001``, ``p0002``, ...) and is the only thing
    evidence ever points at. Character offsets are recorded but are never the sole identifier,
    because a re-parse can shift them while the paragraph is still the same paragraph.
    """

    id: str = Field(pattern=r"^p\d{4,6}$")
    section_id: str
    text: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)

    @model_validator(mode="after")
    def _span_is_forward(self):
        if self.char_end < self.char_start:
            raise ValueError("paragraph ends before it starts")
        return self


SectionId = Literal[
    "title", "abstract", "background", "summary", "brief_drawings",
    "detailed_description", "claims", "other",
]


class Section(Strict):
    id: SectionId
    title: str = ""
    paragraph_ids: list[str] = Field(default_factory=list)


class OriginalFigure(Strict):
    """A drawing sheet lifted out of the source patent, kept ONLY for comparison.

    The compiler never reads geometry from these and never traces them. They exist so a human
    can put the generated figure beside the one the applicant filed. ``figure_labels`` is what a
    vision pass read off the sheet ("FIG. 1", "FIG. 2A"); it is used to pair a sheet with a
    generated figure and for nothing else.
    """

    index: int = Field(ge=0)
    filename: str = ""
    url: str = ""
    figure_labels: list[str] = Field(default_factory=list)
    label_source: Literal["vision", "none"] = "none"


class SourceDocument(Strict):
    document_id: str
    title: str = ""
    publication_number: str = ""
    origin: Literal["upload", "link"]
    origin_label: str = ""
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sections: list[Section] = Field(default_factory=list)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    original_figures: list[OriginalFigure] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    google_patents: Optional[str] = None
    espacenet: Optional[str] = None

    def paragraph(self, paragraph_id: str) -> Optional[Paragraph]:
        return next((p for p in self.paragraphs if p.id == paragraph_id), None)

    def text_of(self, section_id: str) -> str:
        return "\n\n".join(p.text for p in self.paragraphs if p.section_id == section_id)


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------
class Evidence(Strict):
    """Where in the document a semantic fact came from.

    ``quote`` is stored so a validation report can show the sentence without re-reading the
    source, and so a later re-parse that moves the offsets can still be audited.
    """

    section_id: str
    paragraph_id: str = Field(pattern=r"^p\d{4,6}$")
    quote_start: int = Field(ge=0)
    quote_end: int = Field(ge=0)
    quote: str = ""

    @model_validator(mode="after")
    def _span_is_forward(self):
        if self.quote_end < self.quote_start:
            raise ValueError("evidence span ends before it starts")
        return self


# ---------------------------------------------------------------------------
# patent graph
# ---------------------------------------------------------------------------
EntityType = Literal[
    "component", "system", "assembly", "material", "region",
    "signal", "data", "step", "actor", "other",
]

# The symbol library. A visual class chooses the conventional NOTATION for the class of thing
# the applicant named — the coil symbol for a coil, hatching for a cut substrate — and never a
# dimension, a count or a feature the document did not state. "generic_component" is the honest
# default and is what most entities get. See pfc/visualclass.py for where the line is drawn.
VisualClass = Literal[
    "generic_component", "boundary",
    # structure
    "housing", "chamber", "plate", "substrate", "electrode", "shaft", "tube", "opening",
    "connector", "seal", "fastener", "frame", "beam", "arm", "workpiece",
    # motion and power
    "motor", "pump", "valve", "piston", "actuator", "spring", "gear", "bearing", "roller",
    "belt", "conveyor", "wheel", "gripper", "suction_cup", "cutter", "nozzle",
    # electrical, optical and control
    "coil", "magnet", "power", "sensor", "heater", "filter", "adhesive", "lens", "antenna",
    "display", "interface", "processor", "controller", "memory", "storage", "network",
    # flowchart-only
    "process_step", "decision", "terminator", "data_store",
]

ShapeHint = Literal[
    "rectangular", "circular", "elliptical", "cylindrical", "annular", "planar",
    "tubular", "conical", "spherical",
]

# Finite and typed, per the spec. Anything the extractor cannot map lands on "other" and carries
# the source phrase in ``attributes.source_phrase`` rather than inventing a predicate string.
Predicate = Literal[
    "contains", "inside", "attached_to", "coupled_to", "connected_to",
    "electrically_connected_to", "fluidly_connected_to", "communicates_with",
    "receives_from", "transmits_to", "upstream_of", "downstream_of", "adjacent_to",
    "above", "below", "between", "surrounds", "supports", "mounted_on",
    "passes_through", "moves_relative_to", "controls", "drives", "detects",
    "generates", "processes", "stores", "outputs", "inputs", "precedes", "follows",
    "optional_with", "other",
]

# Which predicates carry a direction that may be drawn as an arrowhead. A physical relationship
# such as ``attached_to`` is symmetric on the page even though the sentence had a subject and an
# object, and drawing an arrow on it asserts a flow the document never disclosed.
DIRECTED_PREDICATES = frozenset({
    "receives_from", "transmits_to", "upstream_of", "downstream_of", "controls",
    "drives", "detects", "generates", "processes", "stores", "outputs", "inputs",
    "precedes", "follows",
})

CONTAINMENT_PREDICATES = frozenset({"contains", "inside", "surrounds"})

Direction = Literal["subject_to_object", "object_to_subject", "bidirectional", "none"]


Orientation = Literal["horizontal", "vertical"]
RelativeSize = Literal["small", "medium", "large"]

# How much of the available cell each relative size takes. A housing that dwarfs the sensor
# inside it is what a real drawing looks like; everything the same size is what a block diagram
# looks like.
SIZE_SCALE: dict[str, float] = {"small": 0.62, "medium": 1.0, "large": 1.55}


class Appearance(Strict):
    """How one component is drawn, decided ONCE for the whole document.

    This is the object that makes consistency structural rather than hoped for. An entity's
    appearance is settled before any figure is laid out and every figure then draws that entity
    from this record, so the same battery cannot come out as a battery on one sheet and a box on
    the next. A different view may turn it or resize the sheet around it; it may not redraw it.

    ``source`` says who decided, which is what lets a reviewer tell a disclosed shape from a
    conventional one:

      ``disclosed``  the description states the shape ("the housing is cylindrical")
      ``model``      a reasoning pass chose the symbol from the library for this component
      ``keyword``    the component's own name matched the symbol table
      ``default``    nothing settled it, so it is a plain outline
    """

    symbol: str = "generic_component"
    orientation: Orientation = "horizontal"
    size: RelativeSize = "medium"
    source: Literal["disclosed", "model", "keyword", "default"] = "default"
    note: str = Field(default="", max_length=200)

    @property
    def key(self) -> str:
        """What must match across figures. Orientation may legitimately differ by view."""
        return f"{self.symbol}/{self.size}"


class Entity(Strict):
    id: str = Field(min_length=1, max_length=120)
    canonical_name: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list)
    reference_numeral: Optional[str] = Field(default=None, pattern=r"^[A-Za-z]?\d{1,4}[A-Za-z]?$")
    numeral_status: Literal["EXISTING", "PROPOSED", "NONE"] = "NONE"
    entity_type: EntityType = "component"
    visual_class: VisualClass = "generic_component"
    shape_hint: Optional[ShapeHint] = None
    shape_hint_grounded: bool = False
    appearance: Appearance = Field(default_factory=Appearance)
    attributes: dict[str, Any] = Field(default_factory=dict)
    embodiment_scope: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)

    @model_validator(mode="after")
    def _shape_needs_grounding(self):
        # A shape HINT is a claim that the document states a shape, so it still has to be
        # grounded. Choosing a conventional symbol is a separate decision and lives on
        # `appearance`, where it records who chose it.
        if self.shape_hint is not None and not self.shape_hint_grounded:
            raise ValueError("a shape hint must be grounded in the document")
        if self.reference_numeral and self.numeral_status == "NONE":
            raise ValueError("an entity with a numeral must record where the numeral came from")
        return self


class Relation(Strict):
    id: str = Field(min_length=1, max_length=120)
    subject: str
    predicate: Predicate
    object: str
    direction: Direction = "none"
    attributes: dict[str, Any] = Field(default_factory=dict)
    embodiment_scope: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)

    @model_validator(mode="after")
    def _direction_is_earned(self):
        if self.direction != "none" and self.predicate not in DIRECTED_PREDICATES:
            raise ValueError(
                f"predicate {self.predicate} carries no drawable direction")
        return self


class Conflict(Strict):
    conflict_id: str
    type: Literal[
        "REFERENCE_NUMERAL_COLLISION", "ENTITY_MULTIPLE_NUMERALS",
        "CONTRADICTORY_RELATION", "CONTRADICTORY_DIRECTION",
    ]
    severity: Literal["blocking", "warning"] = "blocking"
    message: str
    entity_ids: list[str] = Field(default_factory=list)
    relation_ids: list[str] = Field(default_factory=list)
    reference_numeral: Optional[str] = None
    # One entry per competing reading: {name, uses, paragraph_id, quote}. A collision report
    # has to show BOTH readings or a reader cannot check it.
    readings: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class PatentGraph(Strict):
    schema_version: Literal["pfc-1"] = "pfc-1"
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    reference_registry: dict[str, str] = Field(default_factory=dict)
    embodiments: list[str] = Field(default_factory=list)
    discarded: list[dict[str, Any]] = Field(default_factory=list)

    def entity(self, entity_id: str) -> Optional[Entity]:
        return next((e for e in self.entities if e.id == entity_id), None)

    def by_numeral(self, numeral: str) -> Optional[Entity]:
        return next((e for e in self.entities if e.reference_numeral == numeral), None)

    @property
    def blocking_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.severity == "blocking"]


# ---------------------------------------------------------------------------
# figure plan and spec
# ---------------------------------------------------------------------------
FigureType = Literal[
    "block_diagram", "flowchart", "data_flow", "logical_schematic",
    "mechanical_schematic", "exploded_schematic", "cross_section_schematic",
    "timing_diagram", "state_diagram", "sequence_diagram", "network_topology",
    "ui_schematic", "other",
]

ViewType = Literal[
    "schematic", "perspective", "plan", "elevation", "section", "exploded",
    "detail", "flow", "other",
]

# Tier A is fully deterministic and is what the MVP renders. Tier B is the conservative
# mechanical schematic. Tier C needs geometry the text does not carry and is refused rather
# than guessed at.
TIER_A = frozenset({"block_diagram", "flowchart", "data_flow", "logical_schematic",
                    "state_diagram", "network_topology", "sequence_diagram"})
TIER_B = frozenset({"mechanical_schematic", "exploded_schematic", "ui_schematic"})
TIER_C = frozenset({"cross_section_schematic", "timing_diagram"})


class FigurePlanItem(Strict):
    figure_number: str = Field(min_length=1, max_length=8)
    description: str = ""
    explicit: bool = False
    figure_type: FigureType = "block_diagram"
    view_type: ViewType = "schematic"
    evidence: list[Evidence] = Field(default_factory=list)


class FigurePlan(Strict):
    figures: list[FigurePlanItem] = Field(default_factory=list)
    source: Literal["explicit", "planned", "mixed"] = "planned"
    notes: list[str] = Field(default_factory=list)


class SpecEntity(Strict):
    entity_id: str
    reference_numeral: Optional[str] = None
    role: Literal["primary", "context", "boundary"] = "primary"


VisualRepresentation = Literal[
    "leader", "association", "bidirectional_association", "data_flow", "control_flow",
    "physical_connection", "movement", "process_sequence", "containment",
]


class SpecRelation(Strict):
    relation_id: str
    visual_representation: VisualRepresentation


ConstraintType = Literal["left_of", "above", "inside", "same_rank", "adjacent"]


class LayoutConstraint(Strict):
    type: ConstraintType
    a: str
    b: str


class FlowStep(Strict):
    """One box of a flowchart, with its own evidence and its own reference numeral."""

    id: str
    text: str = Field(min_length=1, max_length=400)
    reference_numeral: Optional[str] = None
    kind: Literal["process", "decision", "terminator"] = "process"
    evidence: list[Evidence] = Field(min_length=1)


class FlowEdge(Strict):
    from_step: str
    to_step: str
    label: str = ""


class FigureSpec(Strict):
    figure_id: str
    figure_number: str
    title: str = ""
    figure_type: FigureType
    view_type: ViewType
    source_description: str = ""
    entities: list[SpecEntity] = Field(default_factory=list)
    relations: list[SpecRelation] = Field(default_factory=list)
    steps: list[FlowStep] = Field(default_factory=list)
    step_edges: list[FlowEdge] = Field(default_factory=list)
    layout_constraints: list[LayoutConstraint] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list)
    prohibited_entities: list[str] = Field(default_factory=list)
    embodiment_scope: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_something_to_draw(self):
        if not self.entities and not self.steps:
            raise ValueError("a figure spec must name at least one entity or step")
        return self


# ---------------------------------------------------------------------------
# layout scene
# ---------------------------------------------------------------------------
class Box(Strict):
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def cx(self) -> float:
        return self.x + self.width / 2

    @property
    def cy(self) -> float:
        return self.y + self.height / 2

    def inflated(self, pad: float) -> "Box":
        return Box(x=self.x - pad, y=self.y - pad,
                   width=self.width + 2 * pad, height=self.height + 2 * pad)

    def overlaps(self, other: "Box", gap: float = 0.0) -> bool:
        return not (self.right + gap <= other.x or other.right + gap <= self.x or
                    self.bottom + gap <= other.y or other.bottom + gap <= self.y)


class Point(Strict):
    x: float
    y: float


NodeShape = Literal[
    "box", "rounded_box", "ellipse", "circle", "cylinder", "diamond", "stadium",
    "container", "plate", "shaft", "tube", "chamber", "opening", "parallelogram",
]


class LayoutNode(Strict):
    entity_id: str
    reference_numeral: Optional[str] = None
    caption: str = ""
    shape: NodeShape = "box"
    # The symbol this part is drawn with, taken from the entity's settled appearance. Empty
    # means a plain outline. Carried onto the node so a validator can compare what was drawn on
    # one sheet with what was drawn on another without re-deriving anything.
    symbol: str = ""
    orientation: Orientation = "horizontal"
    box: Box
    depth: int = 0
    is_container: bool = False
    role: Literal["primary", "context", "boundary"] = "primary"


class LayoutEdge(Strict):
    relation_id: str
    from_entity: str
    to_entity: str
    edge_type: VisualRepresentation
    points: list[Point] = Field(min_length=2)
    arrow_at_end: bool = False
    arrow_at_start: bool = False
    label: str = ""


class LayoutLabel(Strict):
    """A reference numeral and the leader that binds it to exactly one object."""

    reference_numeral: str
    entity_id: str
    position: Point
    leader_points: list[Point] = Field(min_length=2)
    text_width: float = 0.0
    text_height: float = 0.0

    @property
    def box(self) -> Box:
        return Box(x=self.position.x, y=self.position.y - self.text_height,
                   width=max(self.text_width, 1.0), height=max(self.text_height, 1.0))


class LayoutScene(Strict):
    figure_id: str
    figure_number: str
    figure_type: FigureType
    profile_id: str
    sheet_width: float
    sheet_height: float
    drawing_area: Box
    nodes: list[LayoutNode] = Field(default_factory=list)
    edges: list[LayoutEdge] = Field(default_factory=list)
    labels: list[LayoutLabel] = Field(default_factory=list)
    caption: str = ""
    sheet_number: int = 1
    sheet_total: int = 1

    def node(self, entity_id: str) -> Optional[LayoutNode]:
        return next((n for n in self.nodes if n.entity_id == entity_id), None)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
Severity = Literal["blocking", "warning", "info"]

RepairAction = Literal[
    "move_label", "reroute_leader", "rebind_leader", "relayout", "respec",
    "reevaluate_evidence", "revise_text", "none",
]


class ValidationIssue(Strict):
    rule_id: str
    severity: Severity
    category: Literal["grounding", "reference", "semantic", "geometry", "vision",
                      "jurisdiction", "cross_figure"]
    message: str
    figure_id: Optional[str] = None
    entity_id: Optional[str] = None
    relation_id: Optional[str] = None
    reference_numeral: Optional[str] = None
    repair_action: RepairAction = "none"
    evidence: list[Evidence] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


FigureStatus = Literal[
    "PLANNED", "SPECIFIED", "RENDERED", "VALIDATING", "CORRECTING", "VALIDATED",
    "NEEDS_TEXT_UPDATE", "BLOCKED",
]


class FigureChecks(Strict):
    source_grounding: Literal["PASS", "FAIL", "SKIPPED"] = "SKIPPED"
    references: Literal["PASS", "FAIL", "SKIPPED"] = "SKIPPED"
    semantics: Literal["PASS", "FAIL", "SKIPPED"] = "SKIPPED"
    geometry: Literal["PASS", "FAIL", "SKIPPED"] = "SKIPPED"
    vision: Literal["PASS", "FAIL", "SKIPPED"] = "SKIPPED"


class FigureResult(Strict):
    figure_id: str
    figure_number: str
    figure_type: FigureType
    title: str = ""
    status: FigureStatus = "PLANNED"
    checks: FigureChecks = Field(default_factory=FigureChecks)
    issues: list[ValidationIssue] = Field(default_factory=list)
    correction_attempts: int = 0
    corrections_applied: list[str] = Field(default_factory=list)
    reason: str = ""
    source_evidence: list[str] = Field(default_factory=list)
    svg_path: str = ""
    pdf_path: str = ""
    png_path: str = ""
    original_matches: list[int] = Field(default_factory=list)


class ValidationReport(Strict):
    job_id: str
    overall_status: Literal["VALIDATED", "PARTIAL", "BLOCKED"] = "BLOCKED"
    figures: list[FigureResult] = Field(default_factory=list)
    blocking_issues: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# vision verification
# ---------------------------------------------------------------------------
class ObservedReference(Strict):
    reference: str
    target_description: str = ""
    bbox: list[float] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


class ObservedComponent(Strict):
    observed_id: str
    description: str = ""
    bbox: list[float] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


class ObservedConnection(Strict):
    from_reference: str = ""
    to_reference: str = ""
    direction: Literal["forward", "backward", "bidirectional", "none"] = "none"
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


class ObservedFigure(Strict):
    """What an independent reader says is on the sheet.

    This is a reconstruction, not an opinion. The verifier is never shown the expected answer's
    reasoning and never asked whether the figure "looks correct"; it is asked to read the
    drawing, and the software does the comparing.
    """

    visible_references: list[ObservedReference] = Field(default_factory=list)
    visible_components: list[ObservedComponent] = Field(default_factory=list)
    connections: list[ObservedConnection] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    overlapping_labels: list[str] = Field(default_factory=list)
    ambiguous_leaders: list[str] = Field(default_factory=list)
    possible_errors: list[str] = Field(default_factory=list)


class SemanticDiff(Strict):
    missing_references: list[str] = Field(default_factory=list)
    unexpected_references: list[str] = Field(default_factory=list)
    reference_target_mismatches: list[dict[str, Any]] = Field(default_factory=list)
    missing_connections: list[dict[str, Any]] = Field(default_factory=list)
    unexpected_connections: list[dict[str, Any]] = Field(default_factory=list)
    direction_mismatches: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_visible_text: list[str] = Field(default_factory=list)
    possible_unexpected_objects: list[str] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not any([
            self.missing_references, self.unexpected_references,
            self.reference_target_mismatches, self.missing_connections,
            self.unexpected_connections, self.direction_mismatches,
            self.unsupported_visible_text, self.possible_unexpected_objects,
        ])


# ---------------------------------------------------------------------------
# job configuration and manifest
# ---------------------------------------------------------------------------
Jurisdiction = Literal["generic", "uspto_utility", "pct", "epo"]


class JobConfig(Strict):
    jurisdiction: Jurisdiction = "generic"
    figure_style: Literal["patent_line_art"] = "patent_line_art"
    allow_new_reference_numbers: bool = False
    verification_level: Literal["off", "standard", "strict"] = "standard"
    max_figures: int = Field(default=12, ge=1, le=40)


class Provenance(Strict):
    schema_version: str = SCHEMA_VERSION
    renderer_version: str
    validation_version: str
    validation_profile: str
    source_document_sha256: str
    model_config_hash: str
    prompt_versions: dict[str, str] = Field(default_factory=dict)


class ManifestFigure(Strict):
    figure: str
    figure_id: str
    figure_type: FigureType
    svg: str = ""
    pdf: str = ""
    png: str = ""
    status: FigureStatus
    original_sheets: list[str] = Field(default_factory=list)


class Manifest(Strict):
    job_id: str
    document: dict[str, Any] = Field(default_factory=dict)
    config: JobConfig
    provenance: Provenance
    figures: list[ManifestFigure] = Field(default_factory=list)
    overall_status: Literal["VALIDATED", "PARTIAL", "BLOCKED"] = "BLOCKED"
    generated_at: str = ""


# ---------------------------------------------------------------------------
# helpers shared by the stages
# ---------------------------------------------------------------------------
_SLUG = re.compile(r"[^a-z0-9]+")


def slug(value: str, limit: int = 60) -> str:
    return _SLUG.sub("_", str(value or "").lower()).strip("_")[:limit] or "x"


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "\x1f".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:10]}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
