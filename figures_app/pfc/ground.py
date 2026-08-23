"""The hallucination guard: a second reader that never saw the first one's reasoning.

An extractor asked to find relationships will find relationships, and the ones it invents are
the plausible ones — a sensor connected to a battery, a controller that drives a motor — which
is exactly what makes them dangerous in a patent drawing. Catching them needs a reader with no
stake in the answer.

So each surviving relation is put to a fresh call whose entire context is one paragraph and one
sentence. It does not know a figure is being drawn, it does not know what else was extracted,
and it does not see the extractor's confidence. It answers one question: does this paragraph
say this?

Unsupported relations are discarded and recorded. Without a reasoner the pass is skipped and
the run says so rather than quietly claiming a grounding that never happened.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from pydantic import BaseModel

from . import prompts
from .providers import StructuredOutputError, TextReasoner
from .schemas import Entity, Paragraph, PatentGraph, Relation

MAX_WORKERS = 6
MIN_SUPPORT_CONFIDENCE = 0.5

_HUMAN = {
    "contains": "contains", "inside": "is inside", "attached_to": "is attached to",
    "coupled_to": "is coupled to", "connected_to": "is connected to",
    "electrically_connected_to": "is electrically connected to",
    "fluidly_connected_to": "is fluidly connected to",
    "communicates_with": "communicates with", "receives_from": "receives from",
    "transmits_to": "transmits to", "upstream_of": "is upstream of",
    "downstream_of": "is downstream of", "adjacent_to": "is adjacent to",
    "above": "is above", "below": "is below", "between": "is between",
    "surrounds": "surrounds", "supports": "supports", "mounted_on": "is mounted on",
    "passes_through": "passes through", "moves_relative_to": "moves relative to",
    "controls": "controls", "drives": "drives", "detects": "detects",
    "generates": "generates", "processes": "processes", "stores": "stores",
    "outputs": "outputs", "inputs": "inputs", "precedes": "precedes",
    "follows": "follows", "optional_with": "is optional with", "other": "is related to",
}


class _SupportReply(BaseModel):
    supported: bool = False
    confidence: float = 0.0
    reason: str = ""


def statement(relation: Relation, by_id: dict[str, Entity]) -> str:
    """The relation as one plain sentence, naming parts the way the patent does."""
    subject = by_id.get(relation.subject)
    obj = by_id.get(relation.object)
    if subject is None or obj is None:
        return ""

    def name(entity: Entity) -> str:
        numeral = entity.reference_numeral
        return f"{entity.canonical_name} {numeral}" if numeral else entity.canonical_name

    verb = _HUMAN.get(relation.predicate, relation.predicate.replace("_", " "))
    return f"The {name(subject)} {verb} the {name(obj)}."


def make_grounder(reasoner: Optional[TextReasoner]):
    """A callable matching ``extract_graph``'s ``grounder`` hook."""
    if reasoner is None:
        return None

    system = prompts.load("evidence_check_v1")
    prompt_version = prompts.version("evidence_check_v1")

    def check(relations: list[Relation], paragraph_index: dict[str, Paragraph],
              by_id: dict[str, Entity], graph: PatentGraph) -> list[Relation]:
        if not relations:
            return relations

        def one(relation: Relation) -> tuple[Relation, Optional[_SupportReply]]:
            evidence = relation.evidence[0]
            paragraph = paragraph_index.get(evidence.paragraph_id)
            claim = statement(relation, by_id)
            if paragraph is None or not claim:
                return relation, None
            context = (f"PARAGRAPH\n{paragraph.text}\n\nSTATEMENT\n{claim}")
            try:
                return relation, reasoner.generate_structured(
                    task="evidence_check", schema=_SupportReply, system=system,
                    context=context, prompt_version=prompt_version, max_tokens=300)
            except StructuredOutputError:
                return relation, None

        kept: list[Relation] = []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(relations))) as pool:
            for relation, reply in pool.map(one, relations):
                if reply is None:
                    # The checker could not answer. Keeping an unchecked relation would make the
                    # grounding claim false, so it goes, and the reason is recorded.
                    graph.discarded.append({
                        "kind": "relation", "reason": "the grounding check could not be run",
                        "detail": {"relation_id": relation.id,
                                   "statement": statement(relation, by_id)}})
                    continue
                if not reply.supported or reply.confidence < MIN_SUPPORT_CONFIDENCE:
                    graph.discarded.append({
                        "kind": "relation",
                        "reason": f"not entailed by its own evidence: {reply.reason[:180]}",
                        "detail": {"relation_id": relation.id,
                                   "statement": statement(relation, by_id),
                                   "paragraph_id": relation.evidence[0].paragraph_id}})
                    continue
                relation.confidence = round(
                    min(1.0, (relation.confidence + reply.confidence) / 2), 3)
                kept.append(relation)
        return kept

    return check


# ---------------------------------------------------------------------------
# evidence retrieval
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r"[a-z0-9]+")


class ParagraphIndex:
    """A small BM25 index over the document's paragraphs.

    Used for reporting and diagnosis, not for extraction: when a figure comes out thinner than
    its caption promised, the report can point at the paragraphs that discuss the missing part.
    A reference numeral is weighted far above any word, because a numeral match is a near
    certainty where a word match is a guess.
    """

    K1 = 1.4
    B = 0.72
    NUMERAL_WEIGHT = 6.0

    def __init__(self, paragraphs: Iterable[Paragraph]):
        self.paragraphs = list(paragraphs)
        self.tokens = [Counter(_TOKEN.findall(p.text.lower())) for p in self.paragraphs]
        self.lengths = [sum(c.values()) or 1 for c in self.tokens]
        self.avg_length = sum(self.lengths) / max(1, len(self.lengths))
        self.doc_freq: Counter[str] = Counter()
        for counter in self.tokens:
            self.doc_freq.update(counter.keys())

    def search(self, query: str, numerals: Iterable[str] = (), limit: int = 5
               ) -> list[tuple[Paragraph, float]]:
        terms = _TOKEN.findall(str(query or "").lower())
        wanted = {str(n).lower() for n in numerals}
        total = max(1, len(self.paragraphs))
        scored: list[tuple[Paragraph, float]] = []
        for index, paragraph in enumerate(self.paragraphs):
            counter = self.tokens[index]
            length = self.lengths[index]
            score = 0.0
            for term in terms:
                freq = counter.get(term, 0)
                if not freq:
                    continue
                idf = math.log(1 + (total - self.doc_freq[term] + 0.5) /
                               (self.doc_freq[term] + 0.5))
                score += idf * (freq * (self.K1 + 1)) / (
                    freq + self.K1 * (1 - self.B + self.B * length / self.avg_length))
            for numeral in wanted:
                if counter.get(numeral):
                    score += self.NUMERAL_WEIGHT
            if score > 0:
                scored.append((paragraph, round(score, 4)))
        scored.sort(key=lambda item: (-item[1], item[0].id))
        return scored[:limit]
