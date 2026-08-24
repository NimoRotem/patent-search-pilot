"""Which figures exist, what each is for, and which numerals belong to it.

A patent that has been drafted already answers the first two questions itself, in its brief
description of the drawings, and that answer wins. Planning a figure set the applicant did not
describe is a fallback for a draft that has none, not an opportunity to improve on one that
does.

The third question is answered deterministically and is the quiet load-bearing part of the
whole compiler: a figure shows the parts that the paragraphs discussing that figure name. That
binding comes from the document, so a figure cannot fill up with components merely because they
exist somewhere in the patent.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from . import prompts
from .numerals import RegistryEntry, normalize_name, numerals_in, sort_key
from .providers import StructuredOutputError, TextReasoner
from .schemas import (Evidence, FigurePlan, FigurePlanItem, FigureType, Paragraph,
                      SourceDocument, ViewType)

# "FIG. 1", "FIGS. 1A-1E", "FIGURES 3 and 4", "Fig 2b".
#
# The whitespace before the full stop is not cosmetic. A USPTO text layer reads "FIGS . 1A - 11",
# spacing the printed page does not have, and a pattern that requires "FIGS." to be contiguous
# finds no figures at all in a real granted patent. Measured on US-11338449-B2: every one of its
# figures was invisible until this allowed the space.
_FIG_TOKEN = re.compile(r"\bFIGS?\s*\.?|\bFIGURES?\b", re.I)
# The letter suffix must touch its digits. Allowing a space there looks harmless and is not:
# "FIG. 1 illustrates" then reads as figure "1 i", and "FIGURES 3 and 4" loses figure 4 to the
# "a" of "and". A drafter writes "1A", never "1 A".
_FIG_REF = re.compile(
    r"\bFIG(?:URE)?S?\s*\.?\s*(?P<body>\d{1,3}[A-Za-z]?(?:\s*(?:[-–—]|to|through|and|,|&)"
    r"\s*\d{1,3}[A-Za-z]?)*)", re.I)
_ONE_FIG = re.compile(r"(\d{1,3})([A-Za-z]?)")
_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z(\[])")

MAX_FIGURES = 40

# Deterministic classification. Ordered: the first phrase found in the caption wins, so
# "flow diagram" beats the later mention of a controller.
_TYPE_RULES: tuple[tuple[str, FigureType, ViewType], ...] = (
    (r"\b(?:flow\s*(?:chart|diagram)|flowchart|process\s+(?:flow|diagram)|method\s+of|"
     r"steps?\s+of|algorithm)\b", "flowchart", "flow"),
    (r"\bstate\s+(?:diagram|machine|transition)", "state_diagram", "schematic"),
    (r"\bsequence\s+diagram|\bmessage\s+(?:flow|sequence)|\bcall\s+flow", "sequence_diagram",
     "schematic"),
    (r"\b(?:network|topolog)", "network_topology", "schematic"),
    (r"\bdata\s*flow|\bsignal\s+flow|\binformation\s+flow", "data_flow", "schematic"),
    (r"\btiming\s+diagram|\bwaveform", "timing_diagram", "other"),
    (r"\b(?:graphical\s+)?user\s+interface|\bscreen\s*shot|\bdisplay\s+screen", "ui_schematic",
     "plan"),
    (r"\bexploded", "exploded_schematic", "exploded"),
    (r"\b(?:cross[\s-]*section|sectional|section\s+(?:view|through)|cutaway)",
     "cross_section_schematic", "section"),
    (r"\bcircuit\s+(?:diagram|schematic)|\bschematic\s+(?:circuit|electrical)",
     "logical_schematic", "schematic"),
    (r"\bblock\s+diagram|\bschematic\s+(?:block\s+)?(?:diagram|illustration|representation)|"
     r"\barchitectur|\bsystem\s+diagram", "block_diagram", "schematic"),
    (r"\bperspective\s+view|\bisometric", "mechanical_schematic", "perspective"),
    (r"\b(?:top|bottom|plan)\s+view", "mechanical_schematic", "plan"),
    (r"\b(?:side|front|rear|back|end)\s+(?:elevation|view)|\belevation", "mechanical_schematic",
     "elevation"),
    (r"\b(?:detail|enlarged|close[\s-]*up)\s+view", "mechanical_schematic", "detail"),
)


class _PlanReplyFigure(BaseModel):
    figure_number: str = ""
    description: str = ""
    explicit: bool = True
    figure_type: str = "block_diagram"
    view_type: str = "schematic"
    paragraph_id: str = ""


class _PlanReply(BaseModel):
    figures: list[_PlanReplyFigure] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def expand_figure_reference(body: str) -> list[str]:
    """"1A-1E" -> [1A, 1B, 1C, 1D, 1E]; "3 and 4" -> [3, 4]; "2" -> [2].

    A letter range is expanded because a drafter who writes "FIGS. 1A-1E show ..." has
    described five figures, and the office expects five. A numeric range is expanded the same
    way. A range whose ends do not share a number ("FIGS. 4-6") expands numerically.
    """
    tokens = [(m.group(1), m.group(2).upper()) for m in _ONE_FIG.finditer(body or "")]
    if not tokens:
        return []
    ranged = re.search(r"[-–—]|\bto\b|\bthrough\b", body or "", re.I) is not None
    if ranged and len(tokens) >= 2:
        (first_num, first_letter), (last_num, last_letter) = tokens[0], tokens[-1]
        if first_letter and last_letter and first_num == last_num:
            start, stop = ord(first_letter), ord(last_letter)
            if 0 <= stop - start < 26:
                return [f"{first_num}{chr(code)}" for code in range(start, stop + 1)]
        if not first_letter and not last_letter:
            try:
                start, stop = int(first_num), int(last_num)
            except ValueError:
                start = stop = -1
            if 0 < start <= stop and stop - start < 40:
                return [str(value) for value in range(start, stop + 1)]
    return [f"{number}{letter}" for number, letter in tokens]


def _classify(caption: str) -> tuple[FigureType, ViewType]:
    text = " ".join(str(caption or "").split()).lower()
    for pattern, figure_type, view_type in _TYPE_RULES:
        if re.search(pattern, text):
            return figure_type, view_type
    return "mechanical_schematic", "schematic"


def discover_figures(document: SourceDocument) -> list[FigurePlanItem]:
    """The figures the patent describes, in the patent's own words and numbering."""
    sources = [p for p in document.paragraphs if p.section_id == "brief_drawings"]
    if not sources:
        # Some drafts never head the section. Fall back to any sentence that both names a
        # figure and says what it shows.
        sources = [p for p in document.paragraphs
                   if _FIG_TOKEN.search(p.text) and re.search(
                       r"\b(?:shows?|illustrat|depict|is\s+a|are\s+|présente|zeigt)", p.text, re.I)]
    found: dict[str, FigurePlanItem] = {}
    for paragraph in sources:
        # Every figure reference in the paragraph, with its caption running to the NEXT one.
        #
        # Splitting into sentences first and taking one figure from each looks equivalent and is
        # not: a source that has stripped the punctuation gives "...of the presently disclosed
        # subject matter FIG. 2 shows a bottom perspective view...", which is one sentence
        # containing nine figures. Measured on US-2024/0246200-A1, where it found FIG. 1 and
        # missed the other eight.
        text = paragraph.text
        matches = list(_FIG_REF.finditer(text))
        for index, match in enumerate(matches):
            numbers = expand_figure_reference(match.group("body"))
            if not numbers:
                continue
            stop = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            caption = text[match.end():stop].strip(" -–—:;,.").strip()
            if not caption:
                # A bare mention with nothing after it: fall back to the sentence around it, so
                # a cross-reference still carries something a reader can check.
                caption = next((s for s in _SENTENCE.split(text)
                                if match.group(0) in s), text).strip()
            figure_type, view_type = _classify(caption)
            for number in numbers[:MAX_FIGURES]:
                key = number.upper()
                if key in found:
                    continue
                found[key] = FigurePlanItem(
                    figure_number=key, description=caption[:400], explicit=True,
                    figure_type=figure_type, view_type=view_type,
                    evidence=[Evidence(section_id=paragraph.section_id,
                                       paragraph_id=paragraph.id,
                                       quote_start=match.start(), quote_end=stop,
                                       quote=text[match.start():stop].strip()[:400])])
    return [found[key] for key in sorted(found, key=lambda k: sort_key(k))]


def figure_numerals(document: SourceDocument, figure_number: str,
                    registry: dict[str, RegistryEntry], caption: str = ""
                    ) -> tuple[list[str], list[str]]:
    """The numerals a figure shows, and the paragraph ids that say so.

    A paragraph counts when it names this figure. Everything it then names is material this
    figure is described as showing. A paragraph that names several figures contributes to all
    of them, which is correct: the drafter wrote one sentence about several views.

    The figure's own caption counts too, by name rather than by numeral. "FIG. 1 illustrates an
    example computing system" names the system without printing its numeral, and a figure of a
    system that omits the system itself is not the figure the patent described.
    """
    known = set(registry)
    wanted = str(figure_number).upper()
    numerals: dict[str, None] = {}
    paragraph_ids: list[str] = []
    caption_key = normalize_name(caption)
    if caption_key:
        for numeral, entry in registry.items():
            name = normalize_name(entry.canonical_name)
            if name and len(name) > 2 and name in caption_key:
                numerals[numeral] = None
    for paragraph in document.paragraphs:
        if paragraph.section_id in {"other", "claims"}:
            continue
        labels: set[str] = set()
        for match in _FIG_REF.finditer(paragraph.text):
            labels.update(expand_figure_reference(match.group("body")))
        if wanted not in {label.upper() for label in labels}:
            continue
        paragraph_ids.append(paragraph.id)
        for numeral in numerals_in(paragraph.text):
            if numeral in known:
                numerals[numeral] = None
    return sorted(numerals, key=sort_key), paragraph_ids


def classify_with_model(items: list[FigurePlanItem], reasoner: Optional[TextReasoner],
                        plan: FigurePlan) -> None:
    """Let the model correct the caption classification. Numbering is never up for discussion.

    The keyword rules read a caption's stock phrases well and read an unusual one badly. The
    model is asked only which drawing type a caption describes, and its answer is applied only
    to figures it names by the number they already have.
    """
    if reasoner is None or not items:
        return
    captions = "\n".join(f"FIG. {item.figure_number}: {item.description}" for item in items)
    try:
        reply = reasoner.generate_structured(
            task="figure_plan", schema=_PlanReply, system=prompts.load("figure_plan_v1"),
            context=f"The patent describes these figures.\n\n{captions}",
            prompt_version=prompts.version("figure_plan_v1"), max_tokens=8000)
    except StructuredOutputError:
        plan.notes.append("figure classification fell back to the caption rules")
        return
    by_number = {item.figure_number.upper(): item for item in items}
    valid_types = set(FigureType.__args__)  # type: ignore[attr-defined]
    valid_views = set(ViewType.__args__)  # type: ignore[attr-defined]
    for row in reply.figures:
        item = by_number.get(row.figure_number.strip().upper())
        if item is None:
            continue
        if row.figure_type in valid_types:
            item.figure_type = row.figure_type  # type: ignore[assignment]
        if row.view_type in valid_views:
            item.view_type = row.view_type  # type: ignore[assignment]
    plan.notes.extend(str(note)[:200] for note in reply.notes[:5])


def propose_figures(document: SourceDocument, registry: dict[str, RegistryEntry],
                    reasoner: Optional[TextReasoner], plan: FigurePlan) -> list[FigurePlanItem]:
    """A minimal figure set for a draft that describes none.

    Deliberately small and deliberately dull: an overall arrangement of the most-discussed
    parts, and a flowchart if the document discloses a method. Anything more ambitious would be
    the compiler deciding what the patent ought to have shown.
    """
    if not registry:
        return []
    items: list[FigurePlanItem] = []
    top = sorted(registry.values(), key=lambda entry: (-entry.count, sort_key(entry.numeral)))
    lead = top[0] if top else None
    evidence = lead.evidence(2) if lead else []
    items.append(FigurePlanItem(
        figure_number="1", explicit=False, figure_type="block_diagram", view_type="schematic",
        description="Overall arrangement of the disclosed parts and their stated relationships.",
        evidence=evidence))
    method = [p for p in document.paragraphs
              if p.section_id in {"claims", "detailed_description"} and
              re.search(r"\bmethod\b|\bsteps?\s+of\b|\bprocess\s+(?:for|of)\b", p.text, re.I)]
    if method:
        items.append(FigurePlanItem(
            figure_number="2", explicit=False, figure_type="flowchart", view_type="flow",
            description="The method the description discloses.",
            evidence=[Evidence(section_id=method[0].section_id, paragraph_id=method[0].id,
                               quote_start=0, quote_end=min(200, len(method[0].text)),
                               quote=method[0].text[:200])]))
    plan.notes.append("the draft describes no figures, so a minimal set was proposed")
    return items


def build_plan(document: SourceDocument, registry: dict[str, RegistryEntry],
               reasoner: Optional[TextReasoner], max_figures: int = 12) -> FigurePlan:
    plan = FigurePlan()
    items = discover_figures(document)
    if items:
        plan.source = "explicit"
        classify_with_model(items, reasoner, plan)
    else:
        plan.source = "planned"
        items = propose_figures(document, registry, reasoner, plan)
    if len(items) > max_figures:
        plan.notes.append(
            f"the patent describes {len(items)} figures; the first {max_figures} were compiled "
            "and the rest were left out of this run")
        items = items[:max_figures]
    plan.figures = items
    return plan
