"""One figure's semantic specification: what it shows, and nothing about where anything goes.

Selection starts from the document rather than from a model. The paragraphs that name FIG. 3
name the parts FIG. 3 shows, so that set is the candidate list before anything is asked of a
language model. The model's job is narrower and better suited to it: decide which of those
parts the caption's stated purpose actually needs, which one is the enclosing boundary, and
which disclosed arrangements are worth expressing as layout constraints.

Two safeguards sit around that call:

* whatever it returns is intersected back onto the document-derived set, so it can subtract but
  never add;
* alternatives are separated before anything is drawn, because "in one embodiment the sensor is
  inside the housing, in another it is mounted outside" must not become one drawing of a sensor
  that is both.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from pydantic import BaseModel, Field

from . import prompts
from .numerals import RegistryEntry, sort_key
from .plan import figure_numerals
from .providers import StructuredOutputError, TextReasoner
from .schemas import (CONTAINMENT_PREDICATES, DIRECTED_PREDICATES, Entity, Evidence,
                      FigurePlanItem, FigureSpec, FlowEdge, FlowStep, LayoutConstraint,
                      PatentGraph, Relation, SourceDocument, SpecEntity, SpecRelation,
                      VisualRepresentation, stable_id)

MAX_ENTITIES_PER_FIGURE = 12
MAX_STEPS_PER_FIGURE = 16

# How each predicate is drawn. Only the predicates that carry a disclosed direction become
# arrows; a physical relationship is a plain line, because an arrowhead on it would assert a
# flow the patent never described.
_REPRESENTATION: dict[str, VisualRepresentation] = {
    "contains": "containment", "inside": "containment", "surrounds": "containment",
    "attached_to": "physical_connection", "coupled_to": "physical_connection",
    "connected_to": "physical_connection", "mounted_on": "physical_connection",
    "supports": "physical_connection", "passes_through": "physical_connection",
    "adjacent_to": "association", "above": "association", "below": "association",
    "between": "association", "optional_with": "association", "other": "association",
    "electrically_connected_to": "physical_connection",
    "fluidly_connected_to": "physical_connection",
    "communicates_with": "bidirectional_association",
    "moves_relative_to": "movement",
    "receives_from": "data_flow", "transmits_to": "data_flow", "outputs": "data_flow",
    "inputs": "data_flow", "generates": "data_flow", "processes": "data_flow",
    "stores": "data_flow", "detects": "data_flow",
    "upstream_of": "process_sequence", "downstream_of": "process_sequence",
    "precedes": "process_sequence", "follows": "process_sequence",
    "controls": "control_flow", "drives": "control_flow",
}


class _SpecEntityReply(BaseModel):
    entity_id: str = ""
    role: str = "primary"


class _ConstraintReply(BaseModel):
    type: str = ""
    a: str = ""
    b: str = ""


class _SpecReply(BaseModel):
    entities: list[_SpecEntityReply] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    layout_constraints: list[_ConstraintReply] = Field(default_factory=list)
    title: str = ""
    missing: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class _StepReply(BaseModel):
    id: str = ""
    text: str = ""
    reference_numeral: str = ""
    kind: str = "process"
    paragraph_id: str = ""
    quote: str = ""


class _StepEdgeReply(BaseModel):
    from_step: str = ""
    to_step: str = ""
    label: str = ""


class _StepsReply(BaseModel):
    steps: list[_StepReply] = Field(default_factory=list)
    edges: list[_StepEdgeReply] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def representation(relation: Relation) -> VisualRepresentation:
    return _REPRESENTATION.get(relation.predicate, "association")


def _dominant_embodiment(relations: list[Relation]) -> Optional[str]:
    """The embodiment a figure is drawn in, when the candidates disagree.

    A relation with no embodiment phrase holds in all of them and never forces a choice. A
    choice is only forced when two scoped statements about the same pair of parts contradict
    each other, and then the better-supported scope wins and the other is dropped with a note.
    """
    scoped = [r for r in relations if r.embodiment_scope]
    if not scoped:
        return None
    pairs: dict[tuple[str, str], set[str]] = {}
    for relation in scoped:
        key = tuple(sorted((relation.subject, relation.object)))
        pairs.setdefault(key, set()).add(relation.embodiment_scope[0])  # type: ignore[arg-type]
    if not any(len(scopes) > 1 for scopes in pairs.values()):
        return None
    counts = Counter(r.embodiment_scope[0] for r in scoped)
    return counts.most_common(1)[0][0]


def _select_relations(graph: PatentGraph, entity_ids: set[str]) -> tuple[list[Relation], list[str]]:
    inside = [r for r in graph.relations
              if r.subject in entity_ids and r.object in entity_ids]
    notes: list[str] = []
    chosen = _dominant_embodiment(inside)
    if chosen is not None:
        dropped = [r for r in inside
                   if r.embodiment_scope and r.embodiment_scope[0] != chosen]
        inside = [r for r in inside if not r.embodiment_scope or r.embodiment_scope[0] == chosen]
        if dropped:
            notes.append(
                f"the description gives alternatives for these parts; this figure shows "
                f"{chosen!r} and leaves {len(dropped)} statement(s) about the other "
                "arrangement(s) out")
    return inside, notes


# Words a figure caption uses to describe the DRAWING rather than the invention. A caption that
# says "a block diagram of an example computing system" names one component, not three.
_DRAWING_WORDS = frozenset({
    "view", "views", "diagram", "diagrams", "chart", "flowchart", "flow", "illustration",
    "representation", "perspective", "schematic", "section", "cross", "elevation", "plan",
    "detail", "embodiment", "embodiments", "example", "aspect", "figure", "drawing",
    "drawings", "method", "process", "steps", "step", "operation", "sequence", "block",
    "invention", "disclosure", "portion", "part", "arrangement",
    "principle", "overview", "summary", "top", "bottom", "side", "front", "rear", "isometric",
    "exploded", "enlarged", "close", "up",
})

_CAPTION_PHRASE = re.compile(
    r"\b(?:a|an|the)\s+((?:[a-z][a-z-]{2,20}\s+){0,3}[a-z][a-z-]{2,20})\b", re.I)


def unnumbered_components(caption: str, registry: dict[str, RegistryEntry]) -> list[str]:
    """Things a figure's own caption says it shows that the description never numbered.

    A patent figure labels its parts with reference numerals. When the caption promises a part
    and the description gives that part no numeral, the honest answer is that the TEXT is
    incomplete, not that the compiler should invent a numeral or quietly leave the part out.
    """
    from .numerals import find_numeral, normalize_name

    out: list[str] = []
    for match in _CAPTION_PHRASE.finditer(str(caption or "")):
        phrase = match.group(1).strip()
        words = [word.lower() for word in phrase.split()]
        if any(word in _DRAWING_WORDS for word in words):
            continue
        key = normalize_name(phrase)
        if not key or len(key) < 4:
            continue
        if find_numeral(phrase, registry):
            continue
        # A caption phrase that merely contains a numbered part's name is that part.
        if any(normalize_name(entry.canonical_name) in key or
               key in normalize_name(entry.canonical_name)
               for entry in registry.values()):
            continue
        if phrase not in out:
            out.append(phrase)
    return out[:4]


MIN_SEEDS_BEFORE_CLOSURE = 3


def _close_over_parts(graph: PatentGraph, seeds: list[Entity],
                      notes: list[str]) -> list[Entity]:
    """Add the parts the description says are inside the parts this figure is bound to.

    A figure captioned "illustrates the system 100" is bound, by the paragraphs that name it, to
    the system alone. Drawing one empty rectangle would be technically grounded and useless.
    What the patent also says is that the system contains the sensor and the controller, and a
    figure of the system shows what the system contains, so one hop of DISCLOSED containment is
    followed.

    When even that leaves fewer than three parts, one hop of any disclosed relationship is
    followed as well: a figure of two parts that are stated to be connected is a figure of both.
    Every addition is a statement the document makes; nothing is added by association or by what
    such a device usually has.
    """
    if not seeds:
        return seeds
    known = {entity.id: entity for entity in graph.entities}
    chosen = {entity.id: entity for entity in seeds}
    added: list[str] = []

    def hop(predicates) -> None:
        for relation in graph.relations:
            if predicates is not None and relation.predicate not in predicates:
                continue
            for near, far in ((relation.subject, relation.object),
                              (relation.object, relation.subject)):
                if near in chosen and far not in chosen and far in known:
                    chosen[far] = known[far]
                    added.append(known[far].reference_numeral or far)

    hop(CONTAINMENT_PREDICATES)
    if len(chosen) < MIN_SEEDS_BEFORE_CLOSURE:
        hop(None)
    if added:
        notes.append(
            "the description binds this figure to "
            f"{', '.join(sorted({e.reference_numeral or e.id for e in seeds}, key=sort_key))}, "
            f"and states that {', '.join(sorted(set(added), key=sort_key))} "
            f"{'is' if len(set(added)) == 1 else 'are'} part of that, so "
            f"{'it is' if len(set(added)) == 1 else 'they are'} shown too")
    return list(chosen.values())


def _boundary_candidate(entities: list[Entity], relations: list[Relation]) -> Optional[str]:
    """The entity everything else is disclosed as sitting inside, if there is one."""
    contained: Counter[str] = Counter()
    for relation in relations:
        if relation.predicate in {"contains", "surrounds"}:
            contained[relation.subject] += 1
        elif relation.predicate == "inside":
            contained[relation.object] += 1
    if not contained:
        return None
    best, count = contained.most_common(1)[0]
    return best if count >= 2 else None


def build_spec(document: SourceDocument, graph: PatentGraph, registry: dict[str, RegistryEntry],
               item: FigurePlanItem, reasoner: Optional[TextReasoner]
               ) -> tuple[Optional[FigureSpec], list[str]]:
    """A plan item -> its FigureSpec, plus notes about anything left out."""
    from .extract import entity_id_for

    notes: list[str] = []
    numerals, paragraph_ids = figure_numerals(document, item.figure_number, registry,
                                              item.description)
    candidate_ids = [entity_id_for(numeral) for numeral in numerals]
    candidates = [graph.entity(eid) for eid in candidate_ids]
    candidates = [entity for entity in candidates if entity is not None]
    candidates = _close_over_parts(graph, candidates, notes)

    figure_id = f"FIG_{item.figure_number.upper()}"
    evidence = list(item.evidence)
    for paragraph_id in paragraph_ids[:8]:
        paragraph = document.paragraph(paragraph_id)
        if paragraph is None:
            continue
        evidence.append(Evidence(section_id=paragraph.section_id, paragraph_id=paragraph.id,
                                 quote_start=0, quote_end=min(200, len(paragraph.text)),
                                 quote=paragraph.text[:200]))

    if item.figure_type == "flowchart":
        spec = _flowchart_spec(document, graph, item, figure_id, evidence, candidates,
                               reasoner, notes)
        return spec, notes

    if not candidates:
        notes.append("no paragraph of the description names both this figure and a reference "
                     "numeral, so there is nothing grounded to draw")
        return None, notes

    relations, relation_notes = _select_relations(graph, {e.id for e in candidates})
    notes.extend(relation_notes)

    chosen = candidates
    constraints: list[LayoutConstraint] = []
    boundary = _boundary_candidate(candidates, relations)
    title = item.description[:200]

    if reasoner is not None:
        reply = _refine(item, candidates, relations, reasoner, notes)
        if reply is not None:
            allowed = {entity.id: entity for entity in candidates}
            picked = [row for row in reply.entities if row.entity_id in allowed]
            if picked:
                chosen = [allowed[row.entity_id] for row in picked]
                roles = {row.entity_id: row.role for row in picked}
                boundary = next((eid for eid, role in roles.items() if role == "boundary"),
                                boundary)
            valid_relations = {relation.id for relation in relations}
            if reply.relations:
                keep = {rid for rid in reply.relations if rid in valid_relations}
                if keep:
                    relations = [r for r in relations if r.id in keep]
            constraints = _constraints(reply.layout_constraints, {e.id for e in chosen})
            if reply.title.strip():
                title = reply.title.strip()[:200]
            notes.extend(f"not in the model: {text[:160]}" for text in reply.missing[:4])
            notes.extend(str(text)[:160] for text in reply.notes[:3])

    if len(chosen) > MAX_ENTITIES_PER_FIGURE:
        # Keep the parts the description talks about most, and say what went.
        ranked = sorted(chosen, key=lambda e: (-int(e.attributes.get("mention_count") or 0),
                                               sort_key(e.reference_numeral or "")))
        dropped = ranked[MAX_ENTITIES_PER_FIGURE:]
        chosen = ranked[:MAX_ENTITIES_PER_FIGURE]
        notes.append(
            "this figure is described as showing "
            f"{len(chosen) + len(dropped)} numbered parts; the {len(dropped)} least discussed "
            f"({', '.join(e.reference_numeral or e.id for e in dropped)}) were left out to keep "
            "it readable")

    chosen_ids = {entity.id for entity in chosen}
    relations = [r for r in relations if r.subject in chosen_ids and r.object in chosen_ids]
    spec_entities = [
        SpecEntity(entity_id=entity.id, reference_numeral=entity.reference_numeral,
                   role="boundary" if entity.id == boundary else "primary")
        for entity in sorted(chosen, key=lambda e: sort_key(e.reference_numeral or ""))]
    spec_relations = [SpecRelation(relation_id=relation.id,
                                   visual_representation=representation(relation))
                      for relation in relations]
    constraints = [c for c in constraints if c.a in chosen_ids and c.b in chosen_ids]
    embodiment = sorted({scope for relation in relations for scope in relation.embodiment_scope})

    unnumbered = unnumbered_components(item.description, registry)
    annotations = [f"needs-numeral:{name}" for name in unnumbered]
    for name in unnumbered:
        notes.append(f"the caption of this figure names {name!r}, and the description gives it "
                     "no reference numeral")

    spec = FigureSpec(
        figure_id=figure_id, figure_number=item.figure_number, title=title,
        figure_type=item.figure_type, view_type=item.view_type,
        source_description=item.description[:400], entities=spec_entities,
        relations=spec_relations, layout_constraints=constraints, annotations=annotations,
        prohibited_entities=sorted({e.id for e in graph.entities} - chosen_ids),
        embodiment_scope=embodiment, evidence=evidence[:12])
    return spec, notes


def _constraints(rows: list[_ConstraintReply], allowed: set[str]) -> list[LayoutConstraint]:
    valid = {"left_of", "above", "inside", "same_rank", "adjacent"}
    out: list[LayoutConstraint] = []
    for row in rows:
        kind = row.type.strip().lower()
        if kind in valid and row.a in allowed and row.b in allowed and row.a != row.b:
            out.append(LayoutConstraint(type=kind, a=row.a, b=row.b))  # type: ignore[arg-type]
    return out[:20]


def _refine(item: FigurePlanItem, candidates: list[Entity], relations: list[Relation],
            reasoner: TextReasoner, notes: list[str]) -> Optional[_SpecReply]:
    entity_block = "\n".join(
        f"{entity.id}\t{entity.reference_numeral or '-'}\t{entity.canonical_name}"
        for entity in candidates)
    relation_block = "\n".join(
        f"{relation.id}\t{relation.subject} {relation.predicate} {relation.object}"
        f"\t[{relation.evidence[0].paragraph_id}] {relation.evidence[0].quote[:160]}"
        for relation in relations) or "(none extracted)"
    context = (f"FIGURE\nFIG. {item.figure_number}: {item.description}\n"
               f"Drawing type: {item.figure_type} ({item.view_type})\n\n"
               f"ENTITIES AVAILABLE (entity_id, numeral, name)\n{entity_block}\n\n"
               f"RELATIONS AVAILABLE (relation_id, statement, evidence)\n{relation_block}")
    try:
        return reasoner.generate_structured(
            task="figure_spec", schema=_SpecReply, system=prompts.load("figure_spec_v1"),
            context=context, prompt_version=prompts.version("figure_spec_v1"), max_tokens=3000)
    except StructuredOutputError:
        notes.append("figure selection fell back to every part the description binds to this "
                     "figure")
        return None


def _flowchart_spec(document: SourceDocument, graph: PatentGraph, item: FigurePlanItem,
                    figure_id: str, evidence: list[Evidence], candidates: list[Entity],
                    reasoner: Optional[TextReasoner], notes: list[str]) -> Optional[FigureSpec]:
    """A method figure is a sequence of steps, not a set of components."""
    steps: list[FlowStep] = []
    edges: list[FlowEdge] = []
    if reasoner is not None:
        material = _method_material(document, item)
        if material:
            try:
                reply = reasoner.generate_structured(
                    task="flow_steps", schema=_StepsReply,
                    system=prompts.load("flow_steps_v1"), context=material,
                    prompt_version=prompts.version("flow_steps_v1"), max_tokens=4000)
            except StructuredOutputError:
                reply = None
            if reply is not None:
                steps, edges = _accept_steps(document, reply, notes)
    if not steps:
        notes.append("no step of this method could be read out of the description with its own "
                     "supporting text, so the flowchart is not drawn")
        return None
    return FigureSpec(
        figure_id=figure_id, figure_number=item.figure_number,
        title=item.description[:200], figure_type="flowchart", view_type="flow",
        source_description=item.description[:400], steps=steps, step_edges=edges,
        evidence=evidence[:12])


def _method_material(document: SourceDocument, item: FigurePlanItem) -> str:
    """The claims and description paragraphs a method figure may be built from."""
    claims = [p for p in document.paragraphs if p.section_id == "claims"
              and re.search(r"\bmethod\b|\bprocess\b|\bsteps?\b", p.text, re.I)]
    body: list = []
    label = item.figure_number.upper()
    for paragraph in document.paragraphs:
        if paragraph.section_id != "detailed_description":
            continue
        if re.search(rf"\bFIGS?\s*\.?\s*{re.escape(label)}\b", paragraph.text, re.I):
            body.append(paragraph)
    if not body:
        body = [p for p in document.paragraphs if p.section_id == "detailed_description"
                and re.search(r"\bstep\b|\bmethod\b|\bthen\b|\bnext\b", p.text, re.I)][:12]
    pieces = [f"FIGURE\nFIG. {item.figure_number}: {item.description}"]
    if claims:
        pieces.append("METHOD CLAIMS\n" + "\n\n".join(
            f"[{p.id}] {p.text}" for p in claims[:4]))
    if body:
        pieces.append("DESCRIPTION\n" + "\n\n".join(
            f"[{p.id}] {p.text}" for p in body[:12]))
    return "\n\n".join(pieces) if (claims or body) else ""


def _accept_steps(document: SourceDocument, reply: _StepsReply, notes: list[str]
                  ) -> tuple[list[FlowStep], list[FlowEdge]]:
    from .extract import _quote_span

    steps: list[FlowStep] = []
    seen: set[str] = set()
    for index, row in enumerate(reply.steps[:MAX_STEPS_PER_FIGURE], 1):
        text = " ".join(str(row.text or "").split())[:400]
        paragraph = document.paragraph(row.paragraph_id.strip())
        if not text or paragraph is None:
            continue
        span = _quote_span(paragraph, row.quote)
        if span is None:
            notes.append(f"a proposed step was dropped: its quotation is not in "
                         f"{row.paragraph_id or 'any paragraph'}")
            continue
        numeral = str(row.reference_numeral or "").strip().upper()
        if numeral and not re.fullmatch(r"[A-Z]?\d{1,4}[A-Z]?", numeral):
            numeral = ""
        kind = row.kind if row.kind in {"process", "decision", "terminator"} else "process"
        step_id = row.id.strip() or f"step_{index}"
        if step_id in seen:
            step_id = f"{step_id}_{index}"
        seen.add(step_id)
        steps.append(FlowStep(
            id=step_id, text=text, reference_numeral=numeral or None, kind=kind,
            evidence=[Evidence(section_id=paragraph.section_id, paragraph_id=paragraph.id,
                               quote_start=span[0], quote_end=span[1],
                               quote=paragraph.text[span[0]:span[1]][:400])]))
    ids = {step.id for step in steps}
    edges = [FlowEdge(from_step=row.from_step, to_step=row.to_step,
                      label=str(row.label or "")[:24])
             for row in reply.edges
             if row.from_step in ids and row.to_step in ids and row.from_step != row.to_step]
    if not edges and len(steps) > 1:
        # A method the document states in order, with no branch, is a chain. Saying so is not an
        # inference about the invention; it is the order the drafter wrote.
        edges = [FlowEdge(from_step=steps[i].id, to_step=steps[i + 1].id)
                 for i in range(len(steps) - 1)]
    return steps, edges


def spec_reference_numerals(spec: FigureSpec) -> list[str]:
    """Every numeral this figure is expected to print, in reading order."""
    numerals = [entity.reference_numeral for entity in spec.entities if entity.reference_numeral]
    numerals += [step.reference_numeral for step in spec.steps if step.reference_numeral]
    return sorted({n for n in numerals if n}, key=sort_key)
