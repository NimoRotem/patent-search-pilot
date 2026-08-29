"""Cutting a specification into the parts that mean different things.

Three of them do real work downstream. The claims say what the drawings must show, because
37 CFR 1.83(a) requires the drawing to show every feature specified in the claims. The brief
description says what figures exist, and is the planner's ground truth whenever it is present.
The detailed description is where reference numerals are introduced, and where each one is tied
to a figure.

That last tie is the subtle one. A numeral belongs to a figure because it appears in a paragraph
that is talking about that figure, and a paragraph is talking about the last figure anybody
named. Carrying that context forward is what turns "housing 102" into "housing 102, shown in
FIG. 1 and FIG. 3".
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .schemas import BriefItem, Claim, Paragraph, Sections

# ------------------------------------------------------------------------------ figure labels

_FIG_TOKEN = re.compile(
    r"\b(?:FIG(?:S?)\.?|FIGURES?)\s*"
    r"(?P<body>\d+[A-Za-z]?(?:\s*(?:[-–—]|to|through|and|,|&)\s*\d*[A-Za-z]?)*)",
    re.I)
_FIG_PIECE = re.compile(r"(\d+)([A-Za-z]?)")


def normalise_figure_label(number: str, letter: str = "") -> str:
    return f"FIG. {int(number)}{letter.upper()}"


def figure_labels_in(text: str) -> list[str]:
    """Every figure a piece of text names, expanded over ranges and lists.

    "FIGS. 2A-2C" is three figures, and a numeral that only ever appears in a paragraph headed
    that way belongs to all three.
    """
    found: list[str] = []
    for match in _FIG_TOKEN.finditer(text or ""):
        body = match.group("body")
        pieces = _FIG_PIECE.findall(body)
        if not pieces:
            continue
        separators = re.findall(r"[-–—]|to|through", body, re.I)
        expanded = _expand(pieces, bool(separators))
        for label in expanded:
            if label not in found:
                found.append(label)
    return found


def _expand(pieces: list[tuple[str, str]], ranged: bool) -> list[str]:
    labels = [normalise_figure_label(num, letter) for num, letter in pieces]
    if not ranged or len(pieces) < 2:
        return labels
    out: list[str] = []
    for i, (num, letter) in enumerate(pieces):
        out.append(normalise_figure_label(num, letter))
        if i + 1 >= len(pieces):
            break
        nxt_num, nxt_letter = pieces[i + 1]
        if num == nxt_num and letter and nxt_letter:
            # FIGS. 2A-2D: walk the letters
            for code in range(ord(letter.upper()) + 1, ord(nxt_letter.upper())):
                out.append(normalise_figure_label(num, chr(code)))
        elif not letter and not nxt_letter and int(nxt_num) - int(num) > 1:
            for value in range(int(num) + 1, int(nxt_num)):
                out.append(normalise_figure_label(str(value)))
    seen: list[str] = []
    for label in out:
        if label not in seen:
            seen.append(label)
    return seen


def figure_sort_key(label: str) -> tuple[int, str]:
    match = re.match(r"FIG\.\s*(\d+)([A-Z]*)", label or "", re.I)
    if not match:
        return (10 ** 6, label or "")
    return (int(match.group(1)), (match.group(2) or "").upper())


# ---------------------------------------------------------------------------------- headings

_HEADINGS: tuple[tuple[str, str], ...] = (
    ("claims", r"(?:what\s+is\s+claimed\s+is|we\s+claim|i\s+claim|the\s+invention\s+claimed\s+is"
               r"|claims?\s+what\s+is\s+claimed|^\s*claims?\s*:?)"),
    ("brief", r"brief\s+description\s+of\s+(?:the\s+)?(?:several\s+views?\s+of\s+(?:the\s+)?)?"
              r"drawings?|description\s+of\s+(?:the\s+)?drawings?|brief\s+description\s+of\s+"
              r"(?:the\s+)?figures?"),
    ("detailed", r"detailed\s+description|description\s+of\s+(?:the\s+)?(?:preferred|example|"
                 r"illustrative|exemplary)\s+embodiments?|detailed\s+description\s+of\s+"
                 r"(?:the\s+)?invention"),
    ("summary", r"summary(?:\s+of\s+(?:the\s+)?(?:invention|disclosure))?"),
    ("background", r"background(?:\s+of\s+(?:the\s+)?invention)?|prior\s+art"),
    ("field", r"(?:technical\s+)?field(?:\s+of\s+(?:the\s+)?(?:invention|disclosure))?"),
    ("abstract", r"abstract(?:\s+of\s+(?:the\s+)?disclosure)?"),
    ("crossref", r"cross[- ]reference\s+to\s+related\s+applications?|related\s+applications?"),
)

_HEADING_LINE = re.compile(
    r"^[ \t]*(?:[IVXLC]+\.?[ \t]+|[0-9]+\.?[ \t]+|\[[0-9]+\][ \t]*)?"
    r"(?P<body>[A-Za-z][A-Za-z ,'\-/()]{2,80}?)[ \t]*:?[ \t]*$", re.M)


def _classify_heading(body: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", body).strip().lower().rstrip(":.")
    if len(text) > 80:
        return None
    for name, pattern in _HEADINGS:
        if re.fullmatch(pattern, text, re.I):
            return name
    return None


def split_sections(text: str) -> dict[str, tuple[int, int]]:
    """Heading name to (start, end) offsets over ``text``.

    A heading only counts when it sits alone on its line. "as described in the summary above" is
    not a heading, and matching it as one silently truncates the detailed description.
    """
    marks: list[tuple[int, int, str]] = []
    for match in _HEADING_LINE.finditer(text):
        name = _classify_heading(match.group("body"))
        if name:
            marks.append((match.start(), match.end(), name))
    # An inline claim opener is common and legitimate: "... embodiment. What is claimed is: 1. A".
    if not any(m[2] == "claims" for m in marks):
        inline = re.search(r"(what\s+is\s+claimed\s+is|we\s+claim|i\s+claim|"
                           r"the\s+invention\s+claimed\s+is)\s*:?", text, re.I)
        if inline:
            marks.append((inline.start(), inline.end(), "claims"))
    marks.sort()
    out: dict[str, tuple[int, int]] = {}
    for i, (start, body_end, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        if name in out:
            # A repeated heading extends the first one rather than replacing it: some drafts
            # carry "DETAILED DESCRIPTION" once per embodiment.
            out[name] = (out[name][0], max(out[name][1], end))
        else:
            out[name] = (body_end, end)
    return out


# ------------------------------------------------------------------------------------- claims

_CLAIM_START = re.compile(r"(?:^|\n)\s*(?P<n>\d{1,3})\s*[\.\)]\s+(?=[A-Za-z“\"(])")
_DEPENDS = re.compile(r"\b(?:of|in|to|according\s+to|as\s+(?:set\s+forth|recited|claimed)\s+in|"
                      r"defined\s+in|as\s+in)\s+(?:any\s+(?:one\s+)?of\s+)?claims?\s+(\d{1,3})",
                      re.I)


def parse_claims(text: str) -> list[Claim]:
    if not text or not text.strip():
        return []
    starts = list(_CLAIM_START.finditer(text))
    claims: list[Claim] = []
    for i, match in enumerate(starts):
        number = int(match.group("n"))
        body_start = match.end()
        body_end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        body = text[body_start:body_end].strip()
        if not body:
            continue
        depends = _DEPENDS.search(body)
        parent = int(depends.group(1)) if depends else None
        if parent is not None and parent >= number:
            parent = None          # a forward reference is a typo, not a dependency
        claims.append(Claim(number=number, independent=parent is None, depends_on=parent,
                            text=body))
    # Claim numbers must run 1..n. Anything else is a numbered list that is not the claim set.
    if claims and [c.number for c in claims] != list(range(claims[0].number,
                                                           claims[0].number + len(claims))):
        claims = [c for c in claims if c.number <= len(claims) + 1]
    return claims


def independent_claims(claims: Iterable[Claim]) -> list[Claim]:
    return [c for c in claims if c.independent]


# --------------------------------------------------------------------- brief description items

_BRIEF_SENTENCE = re.compile(
    r"(?P<lead>\b(?:FIG(?:S?)\.?|FIGURES?)\s*\d+[A-Za-z]?[^.;\n]{0,80}?)"
    r"\s+(?P<verb>is|are|shows?|illustrates?|depicts?|provides?|presents?|sets?\s+forth)\b"
    r"(?P<body>[^\n]*?)(?:[.;](?=\s|$)|$)", re.I)

_KIND_HINTS: tuple[tuple[str, str], ...] = (
    ("cross_section", r"cross[- ]section|sectional\s+view|section\s+taken|section\s+along"),
    ("exploded", r"exploded"),
    ("perspective", r"perspective|isometric|elevation|plan\s+view|top\s+view|side\s+view|"
                    r"front\s+view|rear\s+view|bottom\s+view|assembled"),
    ("flowchart", r"flow\s*chart|flow\s*diagram|flow\s+of|method|process\s+for|steps"),
    ("sequence", r"sequence\s+diagram|message\s+flow|call\s+flow|signal(?:l)?ing\s+diagram|"
                 r"timing\s+diagram|ladder\s+diagram"),
    ("ui_screen", r"user\s+interface|screen\s*shot|screenshot|graphical\s+user|display\s+screen|"
                  r"interface\s+screen"),
    ("block_diagram", r"block\s+diagram|schematic|architecture|system\s+diagram|"
                      r"functional\s+diagram|network\s+diagram"),
)


def _kind_hint(text: str) -> str:
    for name, pattern in _KIND_HINTS:
        if re.search(pattern, text, re.I):
            return name
    return ""


def parse_brief_items(brief_text: str) -> list[BriefItem]:
    items: list[BriefItem] = []
    seen: set[str] = set()
    for match in _BRIEF_SENTENCE.finditer(brief_text or ""):
        sentence = match.group(0).strip()
        labels = figure_labels_in(match.group("lead"))
        if not labels:
            continue
        hint = _kind_hint(sentence)
        for label in labels:
            if label in seen:
                continue
            seen.add(label)
            items.append(BriefItem(label=label, text=sentence, kind_hint=hint))
    items.sort(key=lambda item: figure_sort_key(item.label))
    return items


# --------------------------------------------------------------------------------- paragraphs


def split_paragraphs(text: str, spans: dict[str, tuple[int, int]]) -> list[Paragraph]:
    """Paragraphs with their section and their figure context.

    The carry-forward is the point: "FIG. 2 shows a valve." then "The valve 210 has a seat 212."
    The second paragraph names no figure and is entirely about FIG. 2.
    """
    out: list[Paragraph] = []
    context: list[str] = []
    index = 0
    for match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", text):
        body = match.group(0).strip()
        if not body:
            continue
        section = _section_at(match.start(), spans)
        named = figure_labels_in(body)
        if named:
            context = named
        elif section != "detailed":
            context = []
        out.append(Paragraph(index=index, section=section, text=body,
                             start=match.start(), end=match.end(),
                             figures=list(named or context)))
        index += 1
    return out


def _section_at(offset: int, spans: dict[str, tuple[int, int]]) -> str:
    for name, (start, end) in spans.items():
        if start <= offset < end:
            return name
    return "other"


# ------------------------------------------------------------------------------- entry point


def analyse(text: str, *, title: str = "", source: str = "text", source_ref: str = "") -> Sections:
    spans = split_sections(text)

    def body(name: str) -> str:
        span = spans.get(name)
        return text[span[0]:span[1]].strip() if span else ""

    claims_text = body("claims")
    claims = parse_claims(claims_text)
    brief = body("brief")
    detailed = body("detailed")
    if not detailed:
        # No heading. Everything that is not the claims is the description, which is the right
        # reading of a working draft that has not been formatted yet.
        claim_start = spans.get("claims", (len(text), len(text)))[0]
        detailed = text[:claim_start].strip()

    if not title:
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        title = first[:160] if len(first) < 200 else ""

    return Sections(
        title=title, abstract=body("abstract"), field=body("field"),
        background=body("background"), summary=body("summary"),
        brief=brief, brief_items=parse_brief_items(brief),
        detailed=detailed, claims_text=claims_text, claims=claims,
        paragraphs=split_paragraphs(text, spans), raw=text,
        source=source, source_ref=source_ref)
