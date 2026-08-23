"""Document plus registry -> the canonical patent graph.

The division of labour is the whole point. The deterministic passes own everything that can be
got wrong catastrophically: which numerals exist, what each names, and which paragraph proves
it. The model owns one thing only, and it is the thing no regular expression does well:
reading a sentence and saying which of a fixed list of relationships it states.

Between the model and the graph sit three filters, and a relation must pass all of them:

1. **Referential** — both endpoints are registry entities and the cited paragraph exists.
2. **Textual** — the quoted words really are in that paragraph, and both endpoints really are
   mentioned in it. A quote that is not in the paragraph is a fabrication, whatever it says.
3. **Entailment** — a second model call, in a fresh context that has never seen the extractor's
   reasoning, is shown the paragraph and the single statement and asked whether the paragraph
   supports it.

Anything rejected is kept in ``graph.discarded`` with its reason, so a thin figure can be
explained rather than merely observed.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field

from . import prompts
from .numerals import RegistryEntry, normalize_name, sort_key
from .providers import StructuredOutputError, TextReasoner
from .schemas import (Conflict, Entity, EntityType, Evidence, Paragraph, PatentGraph,
                      Predicate, Relation, SourceDocument, VisualClass, stable_id)

# One extraction call covers this many characters of description. Sized so the registry, the
# instruction and the window sit well inside the context with room for a long reply, and so a
# 200,000-character grant costs a bounded, predictable number of calls.
WINDOW_CHARS = 12_000
MAX_WINDOWS = 24
# Concurrent extraction windows. Bounded because this host also serves the prior-art search,
# and because Vertex throttles a burst harder than it throttles a stream.
EXTRACT_WORKERS = 6
MIN_QUOTE_CHARS = 12

_PREDICATES = set(Predicate.__args__)  # type: ignore[attr-defined]
_ENTITY_TYPES = set(EntityType.__args__)  # type: ignore[attr-defined]
_VISUAL_CLASSES = set(VisualClass.__args__)  # type: ignore[attr-defined]
_SHAPES = {"rectangular", "circular", "elliptical", "cylindrical", "annular", "planar",
           "tubular", "conical", "spherical"}

# Predicates whose meaning is symmetric on a drawing. Recording both orders of the same pair
# under one of these is a duplicate, not two facts.
_SYMMETRIC = frozenset({"adjacent_to", "attached_to", "coupled_to", "connected_to",
                        "electrically_connected_to", "fluidly_connected_to",
                        "communicates_with", "moves_relative_to", "optional_with"})

# Pairs that cannot both be true of one ordered pair of entities.
_OPPOSITES = {
    ("contains", "inside"), ("above", "below"), ("upstream_of", "downstream_of"),
    ("receives_from", "transmits_to"), ("precedes", "follows"),
}


class _RelationReply(BaseModel):
    subject: str = ""
    predicate: str = ""
    object: str = ""
    direction: str = "none"
    paragraph_id: str = ""
    quote: str = ""
    embodiment: str = ""
    source_phrase: str = ""
    confidence: float = 0.0


class _ShapeReply(BaseModel):
    entity_id: str = ""
    shape: str = ""
    paragraph_id: str = ""
    quote: str = ""


class _TypeReply(BaseModel):
    entity_id: str = ""
    entity_type: str = "component"
    visual_class: str = "generic_component"


class _GraphReply(BaseModel):
    relations: list[_RelationReply] = Field(default_factory=list)
    shape_hints: list[_ShapeReply] = Field(default_factory=list)
    entity_types: list[_TypeReply] = Field(default_factory=list)


def entity_id_for(numeral: str) -> str:
    return f"e{str(numeral).lower()}"


def build_entities(registry: dict[str, RegistryEntry]) -> list[Entity]:
    """The registry, turned into graph entities. Deterministic and complete."""
    out: list[Entity] = []
    for numeral in sorted(registry, key=sort_key):
        entry = registry[numeral]
        out.append(Entity(
            id=entity_id_for(numeral),
            canonical_name=entry.canonical_name,
            aliases=entry.aliases[:12],
            reference_numeral=numeral,
            numeral_status="EXISTING",
            entity_type="component",
            visual_class="generic_component",
            attributes={"mention_count": entry.count},
            evidence=entry.evidence(),
            confidence=min(1.0, 0.6 + 0.1 * entry.count)))
    return out


def _windows(paragraphs: list[Paragraph]) -> list[list[Paragraph]]:
    out: list[list[Paragraph]] = []
    current: list[Paragraph] = []
    size = 0
    for paragraph in paragraphs:
        if current and size + len(paragraph.text) > WINDOW_CHARS:
            out.append(current)
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph.text)
    if current:
        out.append(current)
    if len(out) <= MAX_WINDOWS:
        return out
    # Sample evenly rather than truncating: the distinguishing matter of a patent is spread
    # through the detailed description, and taking the first N windows reads the field.
    step = len(out) / float(MAX_WINDOWS)
    return [out[int(index * step)] for index in range(MAX_WINDOWS)]


def _registry_block(entities: Iterable[Entity], window: list[Paragraph]) -> str:
    """The registry rows worth sending with this window.

    Only entities the window actually mentions. This is the difference between sending a
    fifty-row registry twenty-four times and sending the rows that matter, and it also stops
    one window's components leaking into another window's relationships.
    """
    body = " ".join(p.text for p in window)
    rows = []
    for entity in entities:
        numeral = entity.reference_numeral or ""
        mentions_numeral = bool(numeral) and re.search(rf"(?<![\d-]){re.escape(numeral)}(?![\d-])",
                                                       body) is not None
        mentions_name = normalize_name(entity.canonical_name) in normalize_name(body)
        if mentions_numeral or mentions_name:
            alias = f" (also: {', '.join(entity.aliases[:3])})" if entity.aliases else ""
            rows.append(f"{entity.id}\t{numeral}\t{entity.canonical_name}{alias}")
    return "\n".join(rows)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _quote_span(paragraph: Paragraph, quote: str) -> Optional[tuple[int, int]]:
    """Where a quoted phrase sits in its paragraph, or None if it is not there.

    Compared on collapsed whitespace because a PDF column reconstruction and a model's copy of
    the same sentence differ in spacing far more often than in words. Nothing else is relaxed:
    the words themselves must match.
    """
    needle = _normalize_ws(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return None
    haystack = _normalize_ws(paragraph.text)
    position = haystack.find(needle)
    if position < 0:
        return None
    # Map the collapsed offset back onto the real text so the evidence span is usable.
    real, collapsed = 0, 0
    start = end = None
    previous_space = False
    for real, char in enumerate(paragraph.text):
        is_space = char.isspace()
        if is_space and previous_space:
            continue
        if collapsed == position and start is None:
            start = real
        if collapsed == position + len(needle):
            end = real
            break
        collapsed += 1
        previous_space = is_space
    if start is None:
        return None
    return start, (end if end is not None else min(len(paragraph.text), start + len(quote) + 8))


def _mentions(paragraph: Paragraph, entity: Entity) -> bool:
    text = paragraph.text
    numeral = entity.reference_numeral or ""
    if numeral and re.search(rf"(?<![\d-]){re.escape(numeral)}(?![\d-])", text):
        return True
    key = normalize_name(entity.canonical_name)
    if key and key in normalize_name(text):
        return True
    return any(normalize_name(alias) and normalize_name(alias) in normalize_name(text)
               for alias in entity.aliases)


def extract_graph(document: SourceDocument, registry: dict[str, RegistryEntry],
                  paragraphs: list[Paragraph], reasoner: Optional[TextReasoner],
                  *, grounder=None) -> PatentGraph:
    """Build the graph. Without a reasoner the graph is entities only, and says so."""
    entities = build_entities(registry)
    by_id = {entity.id: entity for entity in entities}
    graph = PatentGraph(
        document_sha256=document.sha256,
        entities=entities,
        reference_registry={numeral: registry[numeral].canonical_name for numeral in registry})

    if reasoner is None or not entities:
        graph.conflicts.extend(_numeral_conflicts(registry))
        return graph

    system = prompts.load("patent_graph_v1")
    prompt_version = prompts.version("patent_graph_v1")
    paragraph_index = {p.id: p for p in paragraphs}
    candidates: list[tuple[_RelationReply, Paragraph]] = []
    shape_claims: list[_ShapeReply] = []
    type_claims: list[_TypeReply] = []

    def read(window: list[Paragraph]) -> Optional[tuple[_GraphReply, set[str]]]:
        block = _registry_block(entities, window)
        if not block:
            return None
        body = "\n\n".join(f"[{p.id}] {p.text}" for p in window)
        context = (f"REFERENCE REGISTRY (entity_id, numeral, name)\n{block}\n\n"
                   f"PARAGRAPHS\n{body}")
        try:
            reply = reasoner.generate_structured(
                task="patent_graph", schema=_GraphReply, system=system, context=context,
                prompt_version=prompt_version, max_tokens=16000)
        except StructuredOutputError:
            return None
        return reply, {p.id for p in window}

    # The windows are independent by construction: each one is shown only its own paragraphs and
    # may only cite them. Reading a 64-page grant one window at a time measured at twenty
    # seconds a window, which is ten minutes of a user watching a progress bar for work that has
    # no ordering constraint at all. The results are collected in window order regardless, so the
    # graph is the same whichever finishes first.
    windows = _windows(paragraphs)
    with ThreadPoolExecutor(max_workers=min(EXTRACT_WORKERS, max(1, len(windows)))) as pool:
        replies = list(pool.map(read, windows))

    for outcome in replies:
        if outcome is None:
            continue
        reply, window_ids = outcome
        for relation in reply.relations:
            paragraph = paragraph_index.get(relation.paragraph_id)
            if paragraph is None or paragraph.id not in window_ids:
                graph.discarded.append({
                    "kind": "relation", "reason": "cited a paragraph outside the window it read",
                    "detail": relation.model_dump()})
                continue
            candidates.append((relation, paragraph))
        shape_claims.extend(reply.shape_hints)
        type_claims.extend(reply.entity_types)

    _apply_types(by_id, type_claims)
    _apply_shapes(by_id, shape_claims, paragraph_index, graph)

    accepted: list[Relation] = []
    for relation, paragraph in candidates:
        built = _accept(relation, paragraph, by_id, graph)
        if built is not None:
            accepted.append(built)

    accepted = _dedupe(accepted)
    if grounder is not None:
        accepted = grounder(accepted, paragraph_index, by_id, graph)

    graph.relations = accepted
    graph.embodiments = sorted({scope for relation in accepted
                                for scope in relation.embodiment_scope})
    graph.conflicts.extend(_numeral_conflicts(registry))
    graph.conflicts.extend(_contradictions(accepted, by_id))
    return graph


def _accept(reply: _RelationReply, paragraph: Paragraph, by_id: dict[str, Entity],
            graph: PatentGraph) -> Optional[Relation]:
    def reject(reason: str) -> None:
        graph.discarded.append({"kind": "relation", "reason": reason,
                                "detail": reply.model_dump()})

    subject = by_id.get(reply.subject.strip())
    obj = by_id.get(reply.object.strip())
    if subject is None or obj is None:
        reject("named an entity that is not in the reference registry")
        return None
    if subject.id == obj.id:
        reject("related an entity to itself")
        return None
    predicate = reply.predicate.strip().lower()
    if predicate not in _PREDICATES:
        reject(f"used a predicate outside the enumeration ({predicate!r})")
        return None
    span = _quote_span(paragraph, reply.quote)
    if span is None:
        reject("quoted words that are not in the cited paragraph")
        return None
    if not _mentions(paragraph, subject) or not _mentions(paragraph, obj):
        reject("cited a paragraph that does not mention both entities")
        return None

    direction = reply.direction.strip().lower()
    if direction not in {"subject_to_object", "object_to_subject", "bidirectional", "none"}:
        direction = "none"
    # The schema refuses a direction on a predicate that does not carry one; strip it here so a
    # model's over-eager arrow becomes a plain line rather than a dropped relation.
    from .schemas import DIRECTED_PREDICATES
    if predicate not in DIRECTED_PREDICATES:
        direction = "none"
    if direction == "object_to_subject":
        subject, obj = obj, subject
        direction = "subject_to_object"

    attributes: dict[str, Any] = {}
    if predicate == "other" and reply.source_phrase.strip():
        attributes["source_phrase"] = reply.source_phrase.strip()[:200]
    embodiment = _embodiment(reply.embodiment, paragraph.text)
    evidence = Evidence(section_id=paragraph.section_id, paragraph_id=paragraph.id,
                        quote_start=span[0], quote_end=span[1],
                        quote=paragraph.text[span[0]:span[1]][:400])
    return Relation(
        id=stable_id("rel", subject.id, predicate, obj.id, paragraph.id, span[0]),
        subject=subject.id, predicate=predicate, object=obj.id, direction=direction,
        attributes=attributes,
        embodiment_scope=[embodiment] if embodiment else [],
        evidence=[evidence],
        confidence=max(0.0, min(1.0, float(reply.confidence or 0.7))))


_EMBODIMENT_RE = re.compile(
    r"\b(?:in\s+)?(?:a|an|one|another|a\s+further|a\s+second|the\s+first|the\s+second|some|"
    r"other|certain|various|alternative|yet\s+another)\s+(?:example\s+)?embodiments?\b", re.I)


def _embodiment(declared: str, paragraph_text: str) -> str:
    """A stable label for the embodiment a statement belongs to.

    Two alternatives of one component must never be drawn as though they held at once. The
    label is the drafter's own phrase, normalised, so "in another embodiment" and "In another
    embodiment," land in the same scope and a different phrase lands in a different one.
    """
    phrase = (declared or "").strip()
    if not phrase:
        match = _EMBODIMENT_RE.search(paragraph_text or "")
        phrase = match.group(0) if match else ""
    phrase = re.sub(r"\s+", " ", phrase).strip().lower().strip(",. ")
    return phrase[:60]


def _apply_types(by_id: dict[str, Entity], claims: list[_TypeReply]) -> None:
    """A type or visual class is a display decision, so a wrong one is cheap and a made-up one
    is not. Unknown values fall back to the conservative default rather than failing the run."""
    for claim in claims:
        entity = by_id.get(claim.entity_id.strip())
        if entity is None:
            continue
        entity_type = claim.entity_type.strip().lower()
        visual_class = claim.visual_class.strip().lower()
        if entity_type in _ENTITY_TYPES:
            entity.entity_type = entity_type  # type: ignore[assignment]
        if visual_class in _VISUAL_CLASSES:
            entity.visual_class = visual_class  # type: ignore[assignment]


def _apply_shapes(by_id: dict[str, Entity], claims: list[_ShapeReply],
                  paragraph_index: dict[str, Paragraph], graph: PatentGraph) -> None:
    """A shape hint is only accepted when the cited paragraph really states it."""
    for claim in claims:
        entity = by_id.get(claim.entity_id.strip())
        shape = claim.shape.strip().lower()
        paragraph = paragraph_index.get(claim.paragraph_id.strip())
        if entity is None or shape not in _SHAPES or paragraph is None:
            continue
        span = _quote_span(paragraph, claim.quote)
        if span is None or shape[:5] not in _normalize_ws(paragraph.text):
            graph.discarded.append({
                "kind": "shape_hint", "reason": "the cited paragraph does not state the shape",
                "detail": claim.model_dump()})
            continue
        entity.shape_hint = shape  # type: ignore[assignment]
        entity.shape_hint_grounded = True
        entity.evidence.append(Evidence(
            section_id=paragraph.section_id, paragraph_id=paragraph.id,
            quote_start=span[0], quote_end=span[1],
            quote=paragraph.text[span[0]:span[1]][:400]))


def _dedupe(relations: list[Relation]) -> list[Relation]:
    """One fact per pair-and-predicate, keeping the best-evidenced statement of it."""
    best: dict[tuple[str, str, str, str], Relation] = {}
    for relation in relations:
        pair = (relation.subject, relation.object)
        if relation.predicate in _SYMMETRIC:
            pair = tuple(sorted(pair))  # type: ignore[assignment]
        key = (pair[0], pair[1], relation.predicate, ";".join(relation.embodiment_scope))
        current = best.get(key)
        if current is None or relation.confidence > current.confidence:
            if current is not None:
                relation.evidence = list({e.paragraph_id: e for e in
                                          current.evidence + relation.evidence}.values())[:6]
            best[key] = relation
        else:
            current.evidence = list({e.paragraph_id: e for e in
                                     current.evidence + relation.evidence}.values())[:6]
    return sorted(best.values(), key=lambda r: (r.subject, r.predicate, r.object))


def _numeral_conflicts(registry: dict[str, RegistryEntry]) -> list[Conflict]:
    from .numerals import collisions, duplicate_numerals

    out: list[Conflict] = []
    for item in collisions(registry):
        numeral = item["numeral"]
        out.append(Conflict(
            conflict_id=stable_id("conf", "collision", numeral),
            type="REFERENCE_NUMERAL_COLLISION", severity="blocking",
            reference_numeral=numeral,
            message=(f"Reference numeral {numeral} is used for two different things: "
                     f"{item['names'][0]!r} ({item['counts'][0]} time"
                     f"{'' if item['counts'][0] == 1 else 's'}) and "
                     f"{item['names'][1]!r} ({item['counts'][1]} time"
                     f"{'' if item['counts'][1] == 1 else 's'}). The draft has to say which "
                     "before a figure can print that numeral."),
            entity_ids=[entity_id_for(numeral)],
            readings=item.get("readings") or [],
            evidence=item["evidence"][:4]))
    for item in duplicate_numerals(registry):
        out.append(Conflict(
            conflict_id=stable_id("conf", "duplicate", item["name"]),
            type="ENTITY_MULTIPLE_NUMERALS", severity="warning",
            message=(f"{item['name']!r} carries more than one reference numeral "
                     f"({', '.join(item['numerals'])}). That is normal when the patent shows "
                     "several of the same kind of part, and a defect when it does not."),
            entity_ids=[entity_id_for(n) for n in item["numerals"]]))
    return out


def _contradictions(relations: list[Relation], by_id: dict[str, Entity]) -> list[Conflict]:
    """Two statements about one pair that cannot both be drawn.

    Only statements in the same embodiment scope conflict. "Inside the housing in one
    embodiment, outside it in another" is the patent doing its job; drawing both at once is the
    compiler failing at its own.
    """
    out: list[Conflict] = []
    seen: dict[tuple[str, str, str], Relation] = {}
    for relation in relations:
        scope = ";".join(relation.embodiment_scope)
        seen[(relation.subject, relation.object, relation.predicate + "|" + scope)] = relation
    for (subject, obj, key), relation in list(seen.items()):
        predicate, _, scope = key.partition("|")
        for left, right in _OPPOSITES:
            other = None
            if predicate == left:
                other = seen.get((obj, subject, left + "|" + scope)) or \
                    seen.get((subject, obj, right + "|" + scope))
            if other is None or other.id == relation.id:
                continue
            names = (by_id[subject].canonical_name, by_id[obj].canonical_name)
            out.append(Conflict(
                conflict_id=stable_id("conf", "contradiction", relation.id, other.id),
                type="CONTRADICTORY_RELATION", severity="blocking",
                message=(f"The draft states two incompatible arrangements of {names[0]!r} and "
                         f"{names[1]!r} in the same embodiment."),
                entity_ids=[subject, obj], relation_ids=[relation.id, other.id],
                evidence=(relation.evidence + other.evidence)[:4]))
    # One conflict per pair, however many ways it was stated.
    unique: dict[tuple[str, ...], Conflict] = {}
    for conflict in out:
        unique[tuple(sorted(conflict.entity_ids))] = conflict
    return list(unique.values())
