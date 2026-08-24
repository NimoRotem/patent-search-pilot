"""How each component is drawn — decided once, for the whole document.

**The rule, as it now stands.** Draw a simple, recognisable element for each part. A battery may
look like a battery and a coil like a coil; that is what makes a drawing worth looking at, and
refusing it produced pages of identical rectangles. What is still forbidden is designing the
part: choosing a symbol to show a feature, a count, a dimension or a mechanism the description
does not state. The drawing may say "this is a pump". It may not decide the pump is centrifugal.

**Consistency is the guardrail that makes the freedom safe.** There are only so many ways to
draw a battery, but whichever one is chosen has to be the same on every sheet. So the decision
is made HERE, once per entity, before any figure is laid out, and every figure then reads it off
the entity. Two figures cannot disagree about what a component looks like because there is only
one record and they both use it. The consistency validators then check the drawings that came
out, which catches the case where a renderer or a correction pass diverges from the record.

The decision has four possible authors, and which one decided is recorded on the appearance so a
reviewer can tell a disclosed shape from a conventional one:

    disclosed > model > keyword > default

A shape the description states always wins. Below that a reasoning pass may choose from the
symbol library, because it reads the sentences and a keyword table does not. Below that the
component's own name is matched against the table. If nothing settles it, the part is a plain
outline, and a great many parts should be.
"""
from __future__ import annotations

from typing import Iterable, Optional

from pydantic import BaseModel, Field

from . import prompts, visualclass
from .numerals import sort_key
from .providers import StructuredOutputError, TextReasoner
from .render import symbols
from .schemas import Appearance, Entity, Orientation, PatentGraph, RelativeSize

# Shapes the description can state, and the symbol each implies. A stated shape outranks every
# other author: "the housing is cylindrical" is a fact about this housing.
_DISCLOSED_SYMBOL = {
    "cylindrical": "roller", "tubular": "tube", "annular": "seal", "circular": "wheel",
    "spherical": "wheel", "planar": "plate", "rectangular": "generic_component",
    "elliptical": "seal", "conical": "nozzle",
}

MAX_COMPONENTS_PER_CALL = 60


class _ComponentReply(BaseModel):
    entity_id: str = ""
    symbol: str = ""
    orientation: str = "horizontal"
    size: str = "medium"
    note: str = ""


class _AppearanceReply(BaseModel):
    components: list[_ComponentReply] = Field(default_factory=list)


def _container_ids(graph: PatentGraph) -> set[str]:
    """Entities the description places something else inside."""
    out: set[str] = set()
    for relation in graph.relations:
        if relation.predicate in {"contains", "surrounds"}:
            out.add(relation.subject)
        elif relation.predicate == "inside":
            out.add(relation.object)
    return out


def _contained_ids(graph: PatentGraph) -> set[str]:
    out: set[str] = set()
    for relation in graph.relations:
        if relation.predicate in {"contains", "surrounds"}:
            out.add(relation.object)
        elif relation.predicate == "inside":
            out.add(relation.subject)
    return out


def _default_size(entity: Entity, containers: set[str], contained: set[str]) -> RelativeSize:
    """Relative size from the disclosed assembly, not from the world.

    A part that holds other parts is drawn large and a part that sits inside one is drawn small,
    because that is what the description says about how they relate. Nothing here encodes a
    millimetre.
    """
    if entity.id in containers:
        return "large"
    if entity.id in contained:
        return "small"
    return "medium"


def deterministic(graph: PatentGraph) -> None:
    """Settle every appearance without a model. Always run; the model refines afterwards."""
    containers = _container_ids(graph)
    contained = _contained_ids(graph)
    for entity in graph.entities:
        size = _default_size(entity, containers, contained)
        if entity.shape_hint_grounded and entity.shape_hint in _DISCLOSED_SYMBOL:
            entity.appearance = Appearance(
                symbol=_DISCLOSED_SYMBOL[entity.shape_hint], orientation="horizontal",
                size=size, source="disclosed",
                note=f"the description calls it {entity.shape_hint}")
            continue
        symbol = entity.visual_class
        if symbol == "generic_component":
            symbol = visualclass.classify(entity.canonical_name, tuple(entity.aliases or ()))
        if symbols.has_symbol(symbol) and symbol != "generic_component":
            entity.appearance = Appearance(
                symbol=symbol, orientation="horizontal", size=size, source="keyword",
                note=f"drawn as the conventional {symbol.replace('_', ' ')}")
            continue
        entity.appearance = Appearance(symbol="generic_component", orientation="horizontal",
                                       size=size, source="default",
                                       note="the description does not settle what kind of part "
                                            "this is")


def refine(graph: PatentGraph, reasoner: Optional[TextReasoner],
           notes: list[str]) -> int:
    """Let a reasoning pass choose the symbol, reading the sentences a keyword table cannot.

    It may only choose from the library, and it may not overturn a shape the description states.
    Returns how many appearances it settled.
    """
    if reasoner is None or not graph.entities:
        return 0
    candidates = [entity for entity in graph.entities
                  if entity.appearance.source != "disclosed"]
    if not candidates:
        return 0
    candidates = sorted(candidates,
                        key=lambda e: (-int(e.attributes.get("mention_count") or 0),
                                       sort_key(e.reference_numeral or "")))
    candidates = candidates[:MAX_COMPONENTS_PER_CALL]

    rows = []
    for entity in candidates:
        quote = entity.evidence[0].quote if entity.evidence else ""
        rows.append(f"{entity.id}\t{entity.reference_numeral or '-'}\t{entity.canonical_name}"
                    f"\t{quote[:160]}")
    context = ("COMPONENTS (entity_id, numeral, the drafter's name, a sentence about it)\n"
               + "\n".join(rows))

    try:
        reply = reasoner.generate_structured(
            task="component_appearance", schema=_AppearanceReply,
            system=prompts.load("component_appearance_v1"), context=context,
            prompt_version=prompts.version("component_appearance_v1"), max_tokens=8000)
    except StructuredOutputError:
        notes.append("the appearance pass could not be run; parts are drawn from their names")
        return 0

    by_id = {entity.id: entity for entity in candidates}
    settled = 0
    for row in reply.components:
        entity = by_id.get(row.entity_id.strip())
        if entity is None:
            continue
        symbol = row.symbol.strip().lower()
        if not symbols.has_symbol(symbol):
            continue
        orientation: Orientation = (
            "vertical" if row.orientation.strip().lower() == "vertical" else "horizontal")
        size = row.size.strip().lower()
        if size not in {"small", "medium", "large"}:
            size = entity.appearance.size
        entity.appearance = Appearance(
            symbol=symbol, orientation=orientation, size=size,  # type: ignore[arg-type]
            source="model", note=" ".join(str(row.note or "").split())[:200])
        settled += 1
    return settled


def decide(graph: PatentGraph, reasoner: Optional[TextReasoner],
           notes: list[str]) -> None:
    """The one place an appearance is settled. Called once per document, before any layout."""
    deterministic(graph)
    settled = refine(graph, reasoner, notes)
    _sync_visual_class(graph)
    # Counted by what was DRAWN, not by who decided. Counting by author said "19 recognisable,
    # 0 plain outlines" for a document where the model had deliberately left eight parts as
    # outlines, which is the opposite of the honesty the line exists for.
    plain = sum(1 for entity in graph.entities
                if entity.appearance.symbol == "generic_component")
    drawn = len(graph.entities) - plain
    notes.append(
        f"{drawn} part(s) are drawn as a recognisable element and {plain} as a plain outline "
        f"because the description does not settle what kind of thing they are; the choice is "
        f"made once per part and every figure uses it ({settled} of them chosen by reading the "
        f"description rather than the part's name)")


def _sync_visual_class(graph: PatentGraph) -> None:
    """Keep the entity's class in step with what it is drawn as.

    The symbol vocabulary and the visual-class vocabulary are the same words on purpose, so once
    the appearance is settled the class follows from it. Leaving the class on its default while
    the appearance said "sensor" meant the rule that checks two sensors are drawn alike had
    nothing to group by and silently checked nothing.
    """
    from .schemas import VisualClass

    declared = set(VisualClass.__args__)  # type: ignore[attr-defined]
    for entity in graph.entities:
        symbol = entity.appearance.symbol
        if symbol in declared and symbol != "generic_component":
            entity.visual_class = symbol  # type: ignore[assignment]


def geometry(entity: Entity) -> tuple[str, float, float]:
    """``(symbol, width/height, size multiplier)`` for one entity's settled appearance."""
    from .schemas import SIZE_SCALE

    appearance = entity.appearance
    ratio = symbols.aspect(appearance.symbol)
    if appearance.orientation == "vertical":
        ratio = 1.0 / max(ratio, 1e-3)
    return appearance.symbol, ratio, SIZE_SCALE.get(appearance.size, 1.0)


def summary(entities: Iterable[Entity]) -> list[dict]:
    """A reviewable table: what each part is drawn as, and who decided."""
    return [{
        "reference": entity.reference_numeral or "",
        "name": entity.canonical_name,
        "symbol": entity.appearance.symbol,
        "orientation": entity.appearance.orientation,
        "size": entity.appearance.size,
        "decided_by": entity.appearance.source,
        "note": entity.appearance.note,
    } for entity in sorted(entities, key=lambda e: sort_key(e.reference_numeral or ""))]
