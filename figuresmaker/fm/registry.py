"""The reference numeral registry.

Everything downstream is built from this. A figure may only draw numerals the registry holds; the
validator asks the registry whether a numeral in a drawing is real; the redline writes back into
the draft using the registry's terms. So the registry is built twice over: a regex first, because
patent prose puts the numeral immediately after the noun it names and that is a pattern you can
match exactly, and then a model, because deciding that "the housing", "said housing" and "the
outer housing" are one term is a judgement.

Conflicts are found before anything is drawn, and they are reported against whoever can fix
them. A numeral used for two different parts is a defect in the draft, not in the renderer, and
37 CFR 1.84(p)(5) says so in as many words.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Optional

from . import llm
from .schemas import (Conflict, ExtractionResult, RefEntry, Registry, Sections,
                      UnnumberedElement)

# Words that take a number after them without the number being a reference character.
# "step 302" and "stage 410" are deliberately absent: a flowchart's steps are elements, and they
# are the commonest thing to be numbered in a software case.
_NOT_ELEMENTS = {
    "fig", "figs", "figure", "figures", "claim", "claims", "table", "tables", "example",
    "examples", "paragraph", "paragraphs", "page", "pages", "section", "sections", "equation",
    "formula", "appendix", "chapter", "no", "number", "version", "item", "type", "class",
    "level", "application", "publication", "patent", "serial", "docket", "reference", "annex",
    "note", "clause", "aspect", "embodiment", "option", "case",
}

# A number followed by one of these is a measurement, not a numeral.
_UNITS = (r"mm|cm|km|nm|um|µm|m|in|inch|inches|ft|feet|mil|mils|kg|g|mg|lb|lbs|oz|"
          r"ms|us|µs|ns|s|sec|seconds?|min|minutes?|h|hours?|hz|khz|mhz|ghz|"
          r"v|mv|kv|a|ma|w|kw|mw|j|kj|n|kn|pa|kpa|mpa|bar|psi|"
          r"°|degrees?|deg|celsius|fahrenheit|k|%|percent|rpm|bits?|bytes?|kb|mb|gb|tb|px")
_UNIT_AFTER = re.compile(r"^\s*(?:" + _UNITS + r")\b", re.I)

_STOPWORDS = {
    "a", "an", "the", "said", "such", "its", "their", "his", "her", "this", "that", "these",
    "those", "and", "or", "of", "to", "in", "on", "at", "by", "for", "with", "from", "into",
    "onto", "upon", "via", "as", "is", "are", "be", "being", "been", "which", "wherein",
    "whereby", "each", "any", "all", "both", "some", "one", "another", "other", "least",
    "more", "most", "than", "then", "when", "while", "may", "can", "will", "shall", "would",
    "also", "further", "respective", "respectively", "corresponding", "including", "includes",
    "include", "comprising", "comprises", "comprise", "having", "has", "have", "shown", "show",
    "illustrated", "depicted", "described", "referred", "designated", "denoted", "generally",
    "e.g", "i.e", "etc", "example", "embodiment",
}

# Ordinals and positions are part of the term: "first arm 106" and "second arm 108" are two
# different parts, and dropping the ordinal would collapse them into one.
_KEEP_MODIFIERS = {
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
    "tenth", "upper", "lower", "left", "right", "front", "rear", "back", "inner", "outer",
    "top", "bottom", "proximal", "distal", "primary", "secondary", "main", "auxiliary",
    "central", "peripheral", "internal", "external", "input", "output", "male", "female",
}

_PHRASE = (r"(?P<phrase>(?:[A-Za-z][A-Za-z\-']*\s+){0,6}?[A-Za-z][A-Za-z\-']*)"
           r"(?:\s*\([^)]{0,40}\))?\s+")
_NUMERAL_MULTI = r"(?P<num>\d{%d,4}[a-z]?(?:['′]{1,2})?)"

_RUN = re.compile(r"\s*(?:,|;|\s+and\s+|\s+or\s+|\s*[-–]\s*|\s*/\s*)\s*(\d{2,4}[a-z]?)")

_MAX_TERM_WORDS = 5


def _pattern(min_digits: int) -> re.Pattern:
    return re.compile(_PHRASE + (_NUMERAL_MULTI % min_digits) + r"\b")


# A determiner starts a fresh noun phrase. In "a housing carrying a vacuum pump 104" the numeral
# names the vacuum pump, not the carrying, and the "a" before it is what says so.
_DETERMINERS = {"a", "an", "the", "said", "its", "their", "his", "her", "this", "that", "these",
                "those", "each", "any", "every", "one", "another", "other", "such"}


def clean_term(phrase: str) -> str:
    """A noun phrase reduced to the term a registry can compare.

    The determiner rule is doing most of the work. Patent prose stacks clauses, and the numeral
    belongs to the last noun phrase before it; the determiner that opens that phrase is the
    boundary. Without it "carrying a vacuum pump" becomes the name of the part, which then fails
    to match "vacuum pump" in the claims and the coverage check reports a claimed feature as
    missing when it is right there in the figure.

    Ordinals and positions stay, because they are what tell a first arm from a second one.
    """
    words = [w for w in re.split(r"\s+", (phrase or "").strip().lower()) if w]
    last_determiner = max((i for i, w in enumerate(words) if w in _DETERMINERS), default=-1)
    if last_determiner >= 0 and last_determiner + 1 < len(words):
        words = words[last_determiner + 1:]
    while words and words[0] in _STOPWORDS and words[0] not in _KEEP_MODIFIERS:
        words.pop(0)
    while words and words[-1] in _STOPWORDS and words[-1] not in _KEEP_MODIFIERS:
        words.pop()
    if len(words) > _MAX_TERM_WORDS:
        words = words[-_MAX_TERM_WORDS:]
    term = " ".join(words).strip(" -'")
    term = re.sub(r"\s+", " ", term)
    return term


def _plausible(term: str) -> bool:
    if not term or len(term) < 2:
        return False
    head = term.split()[-1]
    if head in _NOT_ELEMENTS or term in _NOT_ELEMENTS:
        return False
    if head in _STOPWORDS:
        return False
    if not re.search(r"[a-z]{2}", term):
        return False
    return True


def _singular(term: str) -> str:
    """Enough of a singulariser to merge "arms 106, 108" with "arm 106"."""
    if not term:
        return term
    head = term.split()[-1]
    if head.endswith("ies") and len(head) > 4:
        new = head[:-3] + "y"
    elif head.endswith("sses") or head.endswith("shes") or head.endswith("ches") \
            or head.endswith("xes"):
        new = head[:-2]
    elif head.endswith("s") and not head.endswith("ss") and not head.endswith("us") \
            and len(head) > 3:
        new = head[:-1]
    else:
        return term
    return " ".join(term.split()[:-1] + [new])


# --------------------------------------------------------------------------- regex extraction


class Candidate:
    __slots__ = ("numeral", "phrases", "offsets", "figures", "sections", "evidence")

    def __init__(self, numeral: str):
        self.numeral = numeral
        self.phrases: Counter = Counter()
        self.offsets: list[int] = []
        self.figures: list[str] = []
        self.sections: list[str] = []
        self.evidence: list[str] = []


def scan(sections: Sections) -> dict[str, Candidate]:
    """Every numeral the prose ties to a noun phrase, with where it was seen."""
    text = sections.raw
    found = _scan_with(text, sections, 2)
    if len(found) < 3:
        # A draft that numbers its parts 1..9 is unusual but legitimate. Only widen when the
        # normal pattern found almost nothing, because widening also catches "at least 2 arms".
        widened = _scan_with(text, sections, 1)
        if len(widened) > len(found):
            found = widened
    return found


def _scan_with(text: str, sections: Sections, min_digits: int) -> dict[str, Candidate]:
    pattern = _pattern(min_digits)
    by_offset = sorted(sections.paragraphs, key=lambda p: p.start)
    out: dict[str, Candidate] = {}

    for match in pattern.finditer(text):
        numeral = match.group("num")
        tail = text[match.end():match.end() + 24]
        if _UNIT_AFTER.match(tail):
            continue
        raw_phrase = match.group("phrase")
        term = clean_term(raw_phrase)
        if not _plausible(term):
            continue
        # A numeral immediately preceded by a blocked word ("claim 1") is not an element even if
        # the words before it look like a noun phrase.
        last_word = raw_phrase.strip().split()[-1].lower().rstrip(".")
        if last_word in _NOT_ELEMENTS:
            continue

        numerals = [numeral]
        # "first and second arms 106, 108" and "arms 106 and 108" name one term twice over.
        cursor = match.end()
        while True:
            run = _RUN.match(text, cursor)
            if not run:
                break
            after = text[run.end():run.end() + 24]
            if _UNIT_AFTER.match(after):
                break
            numerals.append(run.group(1))
            cursor = run.end()
            if len(numerals) > 12:
                break

        singular = _singular(term)
        sentence = _sentence_around(text, match.start())
        para = _paragraph_at(by_offset, match.start())
        for i, value in enumerate(numerals):
            entry = out.setdefault(value, Candidate(value))
            entry.phrases[singular if len(numerals) > 1 else term] += 1
            entry.offsets.append(match.start())
            if len(entry.evidence) < 4 and sentence not in entry.evidence:
                entry.evidence.append(sentence)
            if para is not None:
                for label in para.figures:
                    if label not in entry.figures:
                        entry.figures.append(label)
                if para.section not in entry.sections:
                    entry.sections.append(para.section)

    # A numeral that appears in the text without ever being attached to a phrase is still a
    # numeral: it is what 37 CFR 1.84(p)(4) calls a reference character mentioned in the
    # description, and it has to reach a drawing.
    for match in re.finditer(r"(?<![\w./-])(\d{2,4}[a-z]?)(?![\w%])", text):
        value = match.group(1)
        if value not in out:
            continue
        entry = out[value]
        entry.offsets.append(match.start())
        para = _paragraph_at(by_offset, match.start())
        if para is not None:
            for label in para.figures:
                if label not in entry.figures:
                    entry.figures.append(label)
            if para.section not in entry.sections:
                entry.sections.append(para.section)
    return out


def _sentence_around(text: str, offset: int, span: int = 220) -> str:
    start = max(0, offset - span // 2)
    end = min(len(text), offset + span // 2)
    chunk = text[start:end].replace("\n", " ")
    return re.sub(r"\s+", " ", chunk).strip()


def _paragraph_at(paragraphs, offset: int):
    lo, hi = 0, len(paragraphs) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        para = paragraphs[mid]
        if offset < para.start:
            hi = mid - 1
        elif offset >= para.end:
            lo = mid + 1
        else:
            return para
    return None


# ------------------------------------------------------------------------------ model cleanup

EXTRACT_SYSTEM = """You are a patent draftsperson's assistant preparing a reference numeral \
registry from a specification. You are canonicalising, not inventing.

Rules you must follow:
1. Use ONLY numerals that appear in the candidate list. Never add a numeral of your own.
2. For each numeral give the single canonical term: the noun phrase the specification uses for \
that part, singular, lower case, no article. Keep ordinals and positions ("first arm", "upper \
housing") because they distinguish parts. Drop "said", "the", "a".
3. aliases: the other wordings the specification uses for the same part. Do not put the \
canonical term in aliases.
4. Drop a candidate entirely if the number is not a reference character: a measurement, a claim \
or figure number, a date, a percentage, a model number quoted from prior art.
5. unnumbered: structural elements the description clearly describes but never gives a numeral \
to. Only physical parts, steps or interface elements that a drawing would need to show. Give the \
sentence as evidence. Do not list abstract qualities, advantages or materials.

Return JSON only."""


def canonicalise(candidates: dict[str, Candidate], sections: Sections,
                 reasoner: Optional[llm.Reasoner] = None) -> ExtractionResult:
    """Ask a model to settle the wording. Falls back to the commonest phrasing if it cannot."""
    if not candidates:
        return ExtractionResult()
    listing = []
    for numeral in sorted(candidates, key=_numeral_sort):
        entry = candidates[numeral]
        phrases = ", ".join(f"{p!r}x{n}" for p, n in entry.phrases.most_common(5))
        listing.append(f"{numeral}: {phrases}")
    context = (
        "CANDIDATE NUMERALS (numeral: observed noun phrases with counts)\n"
        + "\n".join(listing)
        + "\n\nSPECIFICATION (may be truncated)\n"
        + _budget(sections))
    if reasoner is None:
        reasoner = llm.fast()
    try:
        return reasoner.structured("registry_canonicalise", ExtractionResult, EXTRACT_SYSTEM,
                                   context, max_tokens=16000)
    except Exception:
        return ExtractionResult()


def _budget(sections: Sections, limit: int = 90000) -> str:
    """The parts of the draft that carry numerals, trimmed to fit a prompt."""
    parts = [sections.brief, sections.detailed, sections.claims_text]
    text = "\n\n".join(p for p in parts if p)
    if not text.strip():
        text = sections.raw
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.3):]
    return head + "\n\n[... middle of the description omitted ...]\n\n" + tail


def _numeral_sort(value: str) -> tuple[int, str]:
    match = re.match(r"(\d+)(.*)", value)
    return (int(match.group(1)), match.group(2)) if match else (10 ** 9, value)


# ------------------------------------------------------------------------------------- build


def build(sections: Sections, reasoner: Optional[llm.Reasoner] = None,
          *, use_model: bool = True) -> Registry:
    candidates = scan(sections)
    cleaned = canonicalise(candidates, sections, reasoner) if use_model else ExtractionResult()
    chosen = {ref.numeral: ref for ref in cleaned.refs}

    entries: list[RefEntry] = []
    dropped: list[str] = []
    for numeral, candidate in sorted(candidates.items(), key=lambda kv: _numeral_sort(kv[0])):
        ref = chosen.get(numeral)
        if cleaned.refs and ref is None:
            dropped.append(numeral)
            continue
        if ref is not None:
            term = clean_term(ref.term) or candidate.phrases.most_common(1)[0][0]
            aliases = [clean_term(a) for a in ref.aliases if clean_term(a)]
        else:
            term = candidate.phrases.most_common(1)[0][0]
            aliases = [p for p, _ in candidate.phrases.most_common(4)[1:]]
        aliases = [a for a in dict.fromkeys(aliases) if a and a != term]
        entries.append(RefEntry(
            numeral=numeral, term=term, aliases=aliases,
            figures=sorted(candidate.figures, key=_figure_key),
            mentions=len(candidate.offsets), sections=candidate.sections,
            first_offset=min(candidate.offsets) if candidate.offsets else 0,
            evidence=candidate.evidence))

    unnumbered = _suggest_numerals(cleaned.unnumbered, entries)
    registry = Registry(entries=entries, unnumbered=unnumbered)
    registry.conflicts = find_conflicts(registry, sections, dropped)
    return registry


def _figure_key(label: str) -> tuple[int, str]:
    from .sections import figure_sort_key
    return figure_sort_key(label)


def next_free_numeral(entries: list[RefEntry], step: int = 2) -> int:
    used = set()
    for entry in entries:
        match = re.match(r"(\d+)", entry.numeral)
        if match:
            used.add(int(match.group(1)))
    if not used:
        return 100
    value = max(used) + step
    while value in used:
        value += step
    return value


def _suggest_numerals(unnumbered: list[UnnumberedElement],
                      entries: list[RefEntry]) -> list[UnnumberedElement]:
    out: list[UnnumberedElement] = []
    known = {e.term for e in entries} | {a for e in entries for a in e.aliases}
    working = list(entries)
    for item in unnumbered:
        term = clean_term(item.term)
        if not term or term in known or _singular(term) in known:
            continue
        known.add(term)
        value = str(next_free_numeral(working))
        working.append(RefEntry(numeral=value, term=term))
        out.append(UnnumberedElement(term=term, evidence=item.evidence,
                                     suggested_numeral=value))
    return out


# ---------------------------------------------------------------------------------- conflicts


def find_conflicts(registry: Registry, sections: Sections,
                   dropped: Optional[list[str]] = None) -> list[Conflict]:
    """Everything wrong with the registry, before a single line is drawn."""
    out: list[Conflict] = []
    by_term: dict[str, list[RefEntry]] = defaultdict(list)

    for entry in registry.entries:
        by_term[_singular(entry.term)].append(entry)

    # One numeral, two terms. The regex sees each mention separately, so a numeral whose observed
    # phrases disagree after singularisation is the draft contradicting itself.
    for entry in registry.entries:
        variants = {_singular(clean_term(a)) for a in entry.aliases if clean_term(a)}
        variants.discard(_singular(entry.term))
        genuine = {v for v in variants if not _related(v, entry.term)}
        if genuine:
            out.append(Conflict(
                code="numeral_two_terms", severity="error", numeral=entry.numeral,
                term=entry.term, stage="draft",
                message=(f"{entry.numeral} is used for \"{entry.term}\" and also for "
                         + ", ".join(f'"{v}"' for v in sorted(genuine))
                         + ". One reference character must not designate different parts."),
                evidence=entry.evidence[:2], cite="37 CFR 1.84(p)(5)"))

    # One term, two numerals.
    for term, group in sorted(by_term.items()):
        if len(group) < 2:
            continue
        numerals = [e.numeral for e in group]
        if _ordinal_family(numerals):
            continue      # 106a and 106b are instances of one part, which is the convention
        out.append(Conflict(
            code="term_two_numerals", severity="warning", term=term, stage="draft",
            numeral=", ".join(numerals),
            message=(f"\"{term}\" is given more than one reference character ("
                     + ", ".join(numerals) + "). The same part must carry the same character in "
                     "every view; if these are genuinely different parts, the terms need to say "
                     "so."),
            evidence=[e.evidence[0] for e in group if e.evidence][:2],
            cite="37 CFR 1.84(p)(5)"))

    # A numeral nobody ever tied to a figure.
    known_figures = {item.label for item in sections.brief_items}
    for entry in registry.entries:
        if entry.figures:
            continue
        out.append(Conflict(
            code="numeral_no_figure", severity="warning", numeral=entry.numeral,
            term=entry.term, stage="draft",
            message=(f"{entry.numeral} (\"{entry.term}\") is never discussed in a paragraph that "
                     "names a figure, so the draft does not say which view should show it. It "
                     "will be placed by the planner."),
            evidence=entry.evidence[:1], cite="37 CFR 1.84(p)(4)"))

    # A figure the brief description promises but the body never discusses.
    discussed = {label for entry in registry.entries for label in entry.figures}
    for label in sorted(known_figures - discussed, key=_figure_key):
        out.append(Conflict(
            code="figure_never_discussed", severity="warning", stage="draft", term=label,
            message=(f"{label} is listed in the brief description of the drawings but no "
                     "paragraph of the description discusses it, so nothing says what it "
                     "contains."), cite="37 CFR 1.84(p)(4)"))

    for item in registry.unnumbered:
        out.append(Conflict(
            code="element_unnumbered", severity="warning", term=item.term, stage="draft",
            numeral=item.suggested_numeral,
            message=(f"\"{item.term}\" is described but carries no reference character. "
                     f"{item.suggested_numeral} is free."),
            evidence=[item.evidence] if item.evidence else [],
            cite="37 CFR 1.84(p)(4)"))

    for numeral in (dropped or []):
        out.append(Conflict(
            code="numeral_discarded", severity="info", numeral=numeral, stage="registry",
            message=(f"{numeral} was read as a number rather than a reference character and is "
                     "not in the registry."), cite=""))
    return out


def _related(a: str, b: str) -> bool:
    """Two wordings of one part: one contains the other, or they share their head noun."""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return a.split()[-1] == b.split()[-1]


def _ordinal_family(numerals: list[str]) -> bool:
    """106a, 106b, 106c: the convention for several instances of one part."""
    stems = {re.match(r"(\d+)", n).group(1) for n in numerals if re.match(r"(\d+)", n)}
    return len(stems) == 1 and all(re.match(r"\d+[a-z]", n) for n in numerals)
