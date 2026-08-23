"""Document text -> canonical sections and stably identified paragraphs.

Two things here matter more than they look.

**Sectioning is not decoration.** Reference numerals are extracted from the detailed
description and nowhere else. A granted US patent opens with a cover page whose citation table
reads "Hoogland 2004 ... Smith 2010 ... Lert 2012" — a page of name-then-number pairs that any
numeral extractor will happily read as fifty components. Slicing the cover page off before
extraction is what keeps it out of the graph.

**Paragraph identifiers are the currency of evidence.** Every entity and every relation in the
graph points at a paragraph id, so those ids must be stable for the life of a job and must
never be character offsets alone: a re-parse moves offsets while the paragraph is still the
same paragraph.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .schemas import Paragraph, Section, SectionId

MAX_PARAGRAPH_CHARS = 2400
MIN_PARAGRAPH_CHARS = 40

# Heading -> canonical section. Ordered: the first pattern that matches a line wins, so the more
# specific "BRIEF DESCRIPTION OF THE DRAWINGS" is tested before the bare "DESCRIPTION".
_HEADINGS: tuple[tuple[SectionId, re.Pattern[str]], ...] = (
    ("abstract", re.compile(r"^\(?57\)?\s*abstract(\s+of\s+the\s+disclosure)?\s*[:.]?$|^abstract\s*[:.]?$", re.I)),
    ("brief_drawings", re.compile(
        r"^(brief\s+)?description\s+of\s+(the\s+)?(several\s+views\s+of\s+the\s+)?drawings?"
        r"(\s+and\s+figures)?\s*[:.]?$"
        r"|^brief\s+description\s+of\s+(the\s+)?(figures?|views?)\s*[:.]?$"
        r"|^(the\s+)?drawings?\s*[:.]?$"
        r"|^kurze\s+beschreibung\s+der\s+(zeichnungen|figuren)\s*[:.]?$"
        r"|^br(e|è)ve\s+description\s+des\s+dessins\s*[:.]?$", re.I)),
    ("detailed_description", re.compile(
        r"^detailed\s+description.*$"
        r"|^description\s+of\s+(the\s+)?(preferred\s+|example\s+|illustrative\s+|exemplary\s+)?"
        r"(embodiment|implementation|example)s?.*$"
        r"|^(detailed\s+)?description\s+of\s+the\s+invention\s*[:.]?$"
        r"|^modes?\s+for\s+carrying\s+out\s+the\s+invention\s*[:.]?$"
        r"|^best\s+mode.*$"
        r"|^ausf(ü|ue)hrungsbeispiele?\s*[:.]?$", re.I)),
    ("summary", re.compile(
        r"^summary(\s+of\s+the\s+(invention|disclosure))?\s*[:.]?$"
        r"|^brief\s+summary.*$"
        r"|^zusammenfassung\s+der\s+erfindung\s*[:.]?$", re.I)),
    ("background", re.compile(
        r"^background(\s+of\s+the\s+(invention|disclosure|art))?\s*[:.]?$"
        r"|^(technical\s+)?field(\s+of\s+the\s+(invention|disclosure))?\s*[:.]?$"
        r"|^prior\s+art\s*[:.]?$"
        r"|^cross[\s-]reference.*$"
        r"|^technisches\s+gebiet\s*[:.]?$|^stand\s+der\s+technik\s*[:.]?$", re.I)),
    ("claims", re.compile(
        r"^(having\s+thus\s+described.{0,120}?,\s*)?what\s+is\s+claimed(\s+as\s+new.{0,80})?(\s+is)?\s*[:.]?$"
        r"|^what\s+we\s+claim(\s+is)?\s*[:.]?$"
        r"|^(we|i)\s+claim\s*[:.]?$"
        r"|^that\s+which\s+is\s+claimed(\s+is)?\s*[:.]?$"
        r"|^the\s+(invention|embodiments?)\s+(is\s+)?claimed(\s+is|\s+as\s+follows)?\s*[:.]?$"
        r"|^claims?\s*[:.]?$"
        r"|^patentanspr(ü|ue)che\s*[:.]?$|^anspr(ü|ue)che\s*[:.]?$"
        r"|^revendications\s*[:.]?$", re.I)),
)

# A heading line is short, is not a sentence, and stands alone. Requiring all three keeps the
# words "detailed description" inside a paragraph of prose from cutting the document in half.
_HEADING_MAX_CHARS = 90

# EP/WO/CN publications number their paragraphs. When those markers are present they are the
# document's own paragraph boundaries and beat any blank-line heuristic.
_PARA_MARKER = re.compile(r"(?m)^\s*\[\s*(\d{3,5})\s*\]\s*")

_FIG_SENTENCE = re.compile(r"\bFIGS?\.?\s*\d|\bFIGURES?\s+\d", re.I)
# A block shorter than the minimum is usually header/footer debris from a PDF column, but not
# when it names a part and its reference sign. "The sensor 120 is disclosed." is thirty
# characters and is the whole basis for entity 120 existing.
_NAMES_A_PART = re.compile(r"[A-Za-z]{3,}\s+\(?\d{1,4}[A-Za-z]?\)?(?!\d)")


def normalize(text: str) -> str:
    """Whitespace repair only. Never changes a word, a number or an order."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", " ")
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _is_heading(line: str) -> Optional[SectionId]:
    stripped = line.strip().strip("*").strip()
    if not stripped or len(stripped) > _HEADING_MAX_CHARS:
        return None
    # Drop a leading numbering ("1. DETAILED DESCRIPTION", "II. Background").
    candidate = re.sub(r"^\(?[0-9IVXivx]{1,4}[.)]\s+", "", stripped).strip()
    if candidate.endswith(("?", ";", ",")):
        return None
    for section_id, pattern in _HEADINGS:
        if pattern.match(candidate):
            return section_id
    return None


def _split_blocks(body: str) -> list[str]:
    """Paragraph blocks of one section, preferring the document's own [0001] markers."""
    body = body.strip()
    if not body:
        return []
    marks = list(_PARA_MARKER.finditer(body))
    if len(marks) >= 3:
        blocks = []
        for index, mark in enumerate(marks):
            stop = marks[index + 1].start() if index + 1 < len(marks) else len(body)
            blocks.append(body[mark.end():stop].strip())
        head = body[:marks[0].start()].strip()
        if head:
            blocks.insert(0, head)
        return [b for b in blocks if b]
    return [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]


def _reflow(block: str) -> str:
    """Join the hard line breaks a PDF column carries, without gluing hyphenated words."""
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", block)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _chunk(text: str, limit: int = MAX_PARAGRAPH_CHARS) -> list[str]:
    """Cap a runaway paragraph at sentence boundaries so evidence stays quotable."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.;])\s+(?=[A-Z(\[])", text):
        if current and len(current) + len(sentence) + 1 > limit:
            out.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current.strip():
        out.append(current.strip())
    return out or [text[:limit]]


def _cover_page_end(text: str) -> int:
    """Where the front-page bibliographic matter stops.

    A granted patent's cover carries the citation table, the classification codes and the
    inventor list, all of which are dense in "<name> <number>" pairs. Extraction must not see
    them. The cover ends at the first canonical heading; when a document has no heading at all
    there is no cover to cut.
    """
    for match in re.finditer(r"(?m)^.{0,%d}$" % _HEADING_MAX_CHARS, text):
        if _is_heading(match.group(0)):
            return match.start()
    return 0


def parse_sections(text: str) -> tuple[list[Section], list[Paragraph]]:
    """Normalized document text -> ordered sections and their paragraphs."""
    text = normalize(text)
    if not text:
        return [], []

    cuts: list[tuple[int, int, SectionId]] = []
    for match in re.finditer(r"(?m)^.*$", text):
        line = match.group(0)
        section_id = _is_heading(line)
        if section_id:
            cuts.append((match.start(), match.end(), section_id))

    spans: list[tuple[SectionId, str, int, int]] = []
    lead_end = cuts[0][0] if cuts else len(text)
    if lead_end > 0:
        spans.append(("other", "Front matter", 0, lead_end))
    for index, (start, end, section_id) in enumerate(cuts):
        stop = cuts[index + 1][0] if index + 1 < len(cuts) else len(text)
        spans.append((section_id, text[start:end].strip(), end, stop))

    sections: dict[SectionId, Section] = {}
    order: list[SectionId] = []
    paragraphs: list[Paragraph] = []
    counter = 0
    for section_id, title, start, stop in spans:
        if section_id not in sections:
            sections[section_id] = Section(id=section_id, title=title)
            order.append(section_id)
        body = text[start:stop]
        for block in _split_blocks(body):
            flowed = _reflow(block)
            if (len(flowed) < MIN_PARAGRAPH_CHARS and not _FIG_SENTENCE.search(flowed)
                    and not _NAMES_A_PART.search(flowed)):
                continue
            offset = text.find(block, start, stop)
            if offset < 0:
                offset = start
            for piece in _chunk(flowed):
                counter += 1
                paragraph = Paragraph(
                    id=f"p{counter:04d}", section_id=section_id, text=piece,
                    char_start=offset, char_end=offset + len(block))
                paragraphs.append(paragraph)
                sections[section_id].paragraph_ids.append(paragraph.id)
    return [sections[key] for key in order], paragraphs


def find_title(text: str, fallback: str = "") -> str:
    """The invention title: the first substantial non-heading line of the front matter."""
    if fallback.strip():
        return fallback.strip()[:300]
    for line in normalize(text).splitlines():
        candidate = line.strip()
        if 12 <= len(candidate) <= 200 and not _is_heading(candidate) \
                and not re.match(r"^\(?\d", candidate) and " " in candidate:
            return candidate[:300]
    return ""


def section_paragraphs(paragraphs: Iterable[Paragraph], *section_ids: str) -> list[Paragraph]:
    wanted = set(section_ids)
    return [p for p in paragraphs if p.section_id in wanted]


def description_paragraphs(paragraphs: Iterable[Paragraph]) -> list[Paragraph]:
    """The paragraphs a reference numeral may legitimately be read out of.

    Detailed description first. Summary and brief-description-of-drawings are included because
    a drafter routinely introduces a numeral in the summary ("a housing 110") and the figure
    captions name the numerals a figure must show. The front matter, the background's discussion
    of other people's patents, and the claims are excluded: numerals in the claims are already
    in the description, and numerals on the cover page belong to cited documents.
    """
    return [p for p in paragraphs
            if p.section_id in {"detailed_description", "summary", "brief_drawings"}]
