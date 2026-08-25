"""Claim elements, and tying them to reference numerals.

37 CFR 1.83(a): "The drawing in a nonprovisional application must show every feature of the
invention specified in the claims." That sentence is the reason this module exists. To check it
you need the claims broken into the features they specify, and each feature matched to the part
the description numbers, so the validator can ask whether that part reaches a figure.

The split is done twice, the same way the registry is: a regex on the punctuation patent claims
actually use, then a model to fix the cases where the punctuation lies.
"""
from __future__ import annotations

import re
from typing import Optional

from . import llm
from .registry import clean_term, _singular
from .schemas import Claim, ClaimElement, ClaimSplitResult, Registry

_SPLIT = re.compile(r";\s*(?:and\s+)?|:\s*\n|\n\s*(?=[a-z]\)|\([a-z]\)|\d+\))")
_LEAD_MARK = re.compile(r"^\s*(?:\([a-z0-9]{1,3}\)|[a-z0-9]{1,3}[\.\)])\s+", re.I)
_PREAMBLE_END = re.compile(r"\b(?:compris\w+|consist\w+|including|includes|having|that has|"
                           r"wherein)\b[,:]?\s*", re.I)
_NUMERAL_IN_CLAIM = re.compile(r"\((\d{2,4}[a-z]?)\)")

_ELEMENT_STOP = {"wherein", "whereby", "thereby", "such that", "so that", "characterized in",
                 "characterised in", "and wherein", "the method of", "the system of"}


def split_elements(claim: Claim) -> list[ClaimElement]:
    """The features one claim specifies, from its punctuation alone."""
    body = claim.text.strip()
    preamble_end = _PREAMBLE_END.search(body)
    tail = body[preamble_end.end():] if preamble_end else body
    parts = [p.strip() for p in _SPLIT.split(tail) if p and p.strip()]
    if len(parts) <= 1:
        # A claim written as one sentence: fall back to the noun phrases introduced by "a"/"an".
        parts = [p.strip() for p in re.split(r",\s+(?=(?:a|an|at least one|one or more)\s)", tail)
                 if p.strip()]
    out: list[ClaimElement] = []
    for part in parts:
        text = _LEAD_MARK.sub("", part).strip(" ,.;")
        if len(text) < 3:
            continue
        low = text.lower()
        if any(low.startswith(stop) for stop in _ELEMENT_STOP):
            continue
        out.append(ClaimElement(text=text[:400], term=_head_term(text)))
    return out


# Words that end the head noun phrase of a limitation. Everything after one of these describes
# the part rather than naming it.
_HEAD_STOP = {"configured", "adapted", "operable", "arranged", "for", "to", "that", "which",
              "wherein", "whereby", "having", "comprising", "including", "with", "and", "or",
              "in", "on", "at", "of", "by", "from", "within", "between", "adjacent", "against",
              "along", "about", "onto", "into", "through", "responsive", "capable", "so"}
_PREPOSITION = {"by", "to", "in", "on", "with", "from", "within", "between", "against", "at",
                "about", "along", "onto", "into", "through", "adjacent", "relative", "over",
                "under", "across"}


def _head_term(text: str) -> str:
    """The thing a claim limitation is about: the noun phrase it opens with.

    "a vacuum pump carried by the housing" is a vacuum pump. The cut has to come at "carried",
    and it cannot come at every word ending in -ed, because "a curved arm" is an arm. So a
    participle ends the phrase only when a preposition follows it, which is what turns it from an
    adjective into a clause.
    """
    body = re.sub(r"^\s*(?:at least one|one or more|a plurality of|each of|the|a|an)\s+", "",
                  text.strip(), flags=re.I)
    words = [w.strip(",;.:()").lower() for w in re.split(r"\s+", body)]
    words = [w for w in words if w]
    kept: list[str] = []
    for index, token in enumerate(words[:9]):
        if token in _HEAD_STOP:
            break
        if token.endswith(("ed", "ing")) and index + 1 < len(words) \
                and words[index + 1] in _PREPOSITION:
            break
        kept.append(token)
    return clean_term(" ".join(kept))


SPLIT_SYSTEM = """You split patent claims into the features they specify, for a drawing \
completeness check under 37 CFR 1.83(a).

For each claim you are given, return its elements. An element is a structural part, a step, or a \
displayed item that a drawing would have to show. Give:
  text: the limitation, quoted from the claim, trimmed to the essentials.
  term: the bare NOUN PHRASE for the thing, singular, lower case, no article and no "said". \
Never a bare verb: for a method step, name the thing the step acts on or produces, so \
"receiving a request at an interface" gives the term "request", not "receiving".

Do NOT return elements for: purely functional wherein-clauses that add no new part, intended \
use, materials, or ranges. Do NOT invent parts the claim does not recite. Return every \
independent claim you were given, by its number. Return JSON only."""


def refine(claims: list[Claim], reasoner: Optional[llm.Reasoner] = None) -> list[Claim]:
    """Let a model correct the split. A failure here leaves the regex result standing."""
    targets = [c for c in claims if c.independent]
    if not targets:
        return claims
    context = "CLAIMS\n\n" + "\n\n".join(f"{c.number}. {c.text}" for c in targets)[:60000]
    if reasoner is None:
        reasoner = llm.fast()
    try:
        result = reasoner.structured("claim_split", ClaimSplitResult, SPLIT_SYSTEM, context,
                                     max_tokens=12000)
    except Exception:
        return claims
    by_number = {item.number: item for item in result.claims}
    for claim in claims:
        item = by_number.get(claim.number)
        if item and item.elements:
            claim.elements = [
                ClaimElement(text=e.text[:400], term=clean_term(e.term) or _head_term(e.text))
                for e in item.elements if (e.text or e.term)]
    return claims


def analyse(claims: list[Claim], reasoner: Optional[llm.Reasoner] = None,
            *, use_model: bool = True) -> list[Claim]:
    for claim in claims:
        if claim.independent and not claim.elements:
            claim.elements = split_elements(claim)
    if use_model:
        claims = refine(claims, reasoner)
    return claims


# ------------------------------------------------------------------------------------ matching


def match_to_registry(claims: list[Claim], registry: Registry) -> list[Claim]:
    """Give every claim element the numeral of the part it names, where one exists."""
    index: dict[str, str] = {}
    for entry in registry.entries:
        for key in [entry.term, _singular(entry.term)] + entry.aliases:
            key = clean_term(key)
            if key and key not in index:
                index[key] = entry.numeral
            singular = _singular(key)
            if singular and singular not in index:
                index[singular] = entry.numeral

    for claim in claims:
        parenthesised = _NUMERAL_IN_CLAIM.findall(claim.text)
        for i, element in enumerate(claim.elements):
            element.numeral = _match_one(element, index, registry)
            if not element.numeral and i < len(parenthesised):
                # EP-style claims carry the numeral in brackets after the noun.
                inline = _NUMERAL_IN_CLAIM.search(element.text)
                if inline and inline.group(1) in registry.by_numeral():
                    element.numeral = inline.group(1)
    return claims


def _match_one(element: ClaimElement, index: dict[str, str], registry: Registry) -> str:
    term = clean_term(element.term)
    for key in (term, _singular(term)):
        if key and key in index:
            return index[key]
    if not term:
        return ""
    # Fall back to the best head-noun match, requiring the head noun to agree exactly so that
    # "drive shaft" never quietly matches "drive belt".
    head = _singular(term).split()[-1]
    best, best_score = "", 0.0
    words = set(_singular(term).split())
    for entry in registry.entries:
        candidate = _singular(entry.term)
        if not candidate or candidate.split()[-1] != head:
            continue
        overlap = len(words & set(candidate.split())) / max(1, len(words | set(candidate.split())))
        if overlap > best_score:
            best, best_score = entry.numeral, overlap
    return best if best_score >= 0.34 else ""


def covered_numerals(claims: list[Claim]) -> list[str]:
    out: list[str] = []
    for claim in claims:
        if not claim.independent:
            continue
        for element in claim.elements:
            if element.numeral and element.numeral not in out:
                out.append(element.numeral)
    return out


def uncovered_elements(claims: list[Claim]) -> list[tuple[int, ClaimElement]]:
    """Independent-claim features that no registry numeral matched."""
    return [(claim.number, element) for claim in claims if claim.independent
            for element in claim.elements if not element.numeral]
