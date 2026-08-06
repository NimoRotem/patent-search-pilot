"""The check that runs after every drafting iteration, whether anyone asks for it or not.

An application that reads beautifully and numbers the same part 34 in one paragraph and 36 in the
next is not a draft, it is a rewrite waiting to happen — and the failure mode of a model writing a
long structured document is precisely this: locally fluent, globally inconsistent.  So every
iteration is followed by a review, and the review is deliberately in two halves that are never
merged:

  DETERMINISTIC CHECKS (this module, in code)
      Numerals, claim numbering and dependency, citation resolution, abstract length, figure
      cross-references.  These are FACTS.  Code counts them the same way every time and a failure
      here is a defect, full stop.

  A REVIEWING AGENT (a second Claude Code run, fresh session)
      Whether the logic holds, whether a reference is described as saying something it does not
      say, whether a claim limitation is genuinely supported by the description.  These are
      JUDGEMENTS.  They are recorded as findings with evidence, and they never borrow the
      authority of the mechanical checks.

The reviewer runs in a NEW session on purpose.  Resuming the drafting session would hand the
reviewer the drafter's own reasoning for every decision, and a model shown its own justification
approves it — the entire value of the second pass is that it has not heard the argument.

CALIBRATION.  A check that cries wolf costs as much as one that misses: a draft flagged FAIL for a
heuristic that is wrong three times in ten teaches the user to ignore the panel.  So checks are
graded by how exactly they can be decided.  ``error`` is reserved for things code can prove
(a numeral used and never defined, a claim depending on a claim that does not exist, a citation
that resolves to nothing).  Everything inferential — antecedent basis, claim support by term
overlap — is ``advisory``: it is shown, it is explained, and it can never by itself fail a draft.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import draft_agent
import draft_cite
import draft_workspace

# Sections in which a prior-art citation belongs.  US practice discusses the art in the Background
# and may incorporate a document by reference in the Detailed Description; a citation in a claim,
# the abstract or the title is a drafting error rather than a style preference.
CITABLE_SECTIONS = frozenset({"background", "cross_reference", "detailed_description"})
UNCITABLE_SECTIONS = frozenset({"title", "claims", "abstract", "summary", "field",
                                "drawing_descriptions"})

ABSTRACT_WORD_LIMIT = 150            # 37 CFR 1.72(b)
TITLE_CHAR_LIMIT = 500               # 37 CFR 1.72(a)

_FIG_RE = re.compile(r"\bFIGS?\.?\s*([0-9]+[A-Za-z]?)", re.IGNORECASE)
_FIG_RANGE_RE = re.compile(r"\bFIGS?\.?\s*([0-9]+[A-Za-z]?)\s*(?:-|–|—|to|through|and)\s*"
                           r"([0-9]+[A-Za-z]?)", re.IGNORECASE)
_NUMERAL_IN_TEXT_RE = re.compile(r"(?<![\w.\-/])(\d{1,4}[a-z]?)(?![\w%°]|\s*(?:%|percent))")
_CLAIM_START_RE = re.compile(r"^\s*(\d{1,3})\s*[.)]\s+", re.MULTILINE)
#  The reference-back phrase, then everything that is still a claim number.  The tail has to span
#  "1 to 3", "1 or 4" and "1, 2 and 3" without running on into "wherein …", which is why the
#  connectors are named rather than folded into a character class: a bare `[0-9,\s-]+` stops dead
#  at the "to" in "claims 1 to 3" and silently reads it as depending on claim 1 alone.
_DEPENDENCY_RE = re.compile(
    r"\b(?:of|in|according\s+to|as\s+(?:set\s+forth|recited|claimed|defined)\s+in|as\s+in)\s+"
    r"(?:any\s+(?:one\s+)?of\s+)?claims?\s+"
    r"([0-9][0-9,\s\u2013\u2014-]*(?:(?:or|and|to|through)\s*[0-9][0-9,\s\u2013\u2014-]*)*)",
    re.IGNORECASE)
_RANGE_RE = re.compile(r"(\d{1,3})\s*(?:-|\u2013|\u2014|to|through)\s*(\d{1,3})", re.IGNORECASE)
_DRAFTING_NOTE_RE = re.compile(r"\[DRAFTING NOTE:([^\]]*)\]", re.IGNORECASE)
#  The phrase is captured in a LOOKAHEAD so the scan consumes only the article.  Consuming the
#  noun phrase as well was a real defect: in "a tool comprising a body, a pump", the first match
#  swallowed "tool comprising a body", so "a body" was never seen as an introduction and every
#  later "the body" was reported as lacking antecedent basis. Same for the references: "the body
#  carries the pump" hid "the pump" entirely.
_ARTICLE_INTRO_RE = re.compile(
    r"\b(?:a|an|at\s+least\s+one|one\s+or\s+more|a\s+plurality\s+of|plural)\s+"
    r"(?=([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,4}))")
_ARTICLE_REF_RE = re.compile(
    r"\b(?:the|said)\s+(?=([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,4}))")

# Words that end a noun phrase.  Without this, "the housing is coupled to" reads as a five-word
# term and never matches the "a housing" that introduced it.
_PHRASE_STOP = frozenset("""
is are was were be being been has have had having comprising comprises comprise including
includes include configured adapted arranged disposed positioned mounted coupled connected
attached engaged extending extends defined defining formed forming provided provides wherein
whereby such that which when while so as to of for with from into onto upon between among
and or but if then than at on in by via said the a an each any all least one more least
first second third fourth being does do did can may will shall would should could
""".split())

# Phrases a patent attorney writes without antecedent basis and nobody objects to.
_NO_BASIS_NEEDED = frozenset({
    "invention", "present invention", "art", "prior art", "related art", "same", "like",
    "drawings", "figures", "figure", "specification", "disclosure", "embodiment", "embodiments",
    "following", "foregoing", "art to which", "group consisting", "claim", "claims",
    "accompanying drawings", "detailed description", "scope", "spirit", "extent", "art of",
    "user", "operator", "environment", "atmosphere", "ambient", "ground", "earth", "air",
    "horizontal", "vertical", "longitudinal", "lateral", "axial", "radial", "art disclosed",
})

_STOPWORDS = frozenset("""
a an the and or but of for to in on at by with from into onto is are was were be been being
that this these those it its as such which when where while than then so if not no nor
comprising comprises comprise including includes include having has have had wherein whereby
said one two three first second third plurality least more most other another each any all
about substantially generally approximately configured adapted arranged thereof therein thereto
""".split())


# =============================================================================================
# Deterministic checks
# =============================================================================================
def _check(name: str, status: str, detail: str, *, severity: str = "error",
           items: Sequence[Any] = ()) -> dict[str, Any]:
    return {"name": name, "status": status, "severity": severity, "detail": detail,
            "items": [str(i)[:300] for i in items][:60]}


def run_checks(*, sections: Mapping[str, str], numerals: Sequence[Mapping[str, str]],
               figures: Sequence[Mapping[str, Any]], allowed_references: Sequence[str] = (),
               allow_remote: bool = True) -> list[dict[str, Any]]:
    """Every mechanical check, in one pass. Pure apart from citation resolution."""
    checks: list[dict[str, Any]] = []
    text_sections = {key: str(sections.get(key) or "") for key, _n, _h in
                     draft_workspace.SECTION_FILES}
    spec_text = "\n\n".join(text_sections[key] for key in
                            ("field", "background", "summary", "drawing_descriptions",
                             "detailed_description"))
    claims_text = text_sections["claims"]

    checks.append(_sections_present(text_sections))
    checks.append(_title_form(text_sections["title"]))
    checks.append(_abstract_form(text_sections["abstract"]))
    checks.extend(_numeral_checks(spec_text, claims_text, numerals, figures))
    checks.extend(_figure_checks(text_sections, figures))
    checks.extend(_claim_checks(claims_text, spec_text))
    checks.extend(_citation_checks(text_sections, allowed_references, allow_remote=allow_remote))
    checks.append(_open_notes(text_sections))
    return checks


def _sections_present(sections: Mapping[str, str]) -> dict[str, Any]:
    missing = [heading for key, _name, heading in draft_workspace.SECTION_FILES
               if not str(sections.get(key) or "").strip()]
    if missing:
        return _check("Every section is written", "fail",
                      f"{len(missing)} section(s) are still empty.", items=missing)
    return _check("Every section is written", "pass", "All nine sections carry text.")


def _title_form(title: str) -> dict[str, Any]:
    title = title.strip()
    problems = []
    if len(title) > TITLE_CHAR_LIMIT:
        problems.append(f"{len(title)} characters; 37 CFR 1.72(a) allows {TITLE_CHAR_LIMIT}.")
    if re.search(r"\b(new|improved|improvement in|useful)\b", title, re.IGNORECASE):
        problems.append('MPEP 606 objects to "new", "improved" and similar puffery in a title.')
    if "\n" in title:
        problems.append("The title runs to more than one line.")
    if not problems:
        return _check("Title is in filing form", "pass", f"{len(title)} characters.")
    return _check("Title is in filing form", "warn", " ".join(problems), severity="warn")


def _abstract_form(abstract: str) -> dict[str, Any]:
    abstract = abstract.strip()
    words = len(re.findall(r"\S+", abstract))
    problems = []
    if words > ABSTRACT_WORD_LIMIT:
        problems.append(f"{words} words; 37 CFR 1.72(b) allows {ABSTRACT_WORD_LIMIT}.")
    if len([p for p in re.split(r"\n\s*\n", abstract) if p.strip()]) > 1:
        problems.append("The abstract must be a single paragraph.")
    if re.match(r"^\s*(this|the)\s+(invention|disclosure|application|patent)\b", abstract,
                re.IGNORECASE):
        problems.append('MPEP 608.01(b) discourages opening with "This invention...".')
    if re.search(r"\bmeans\s+for\b", abstract, re.IGNORECASE):
        problems.append('"means for" in the abstract invites a 35 USC 112(f) reading.')
    if not problems:
        return _check("Abstract is in filing form", "pass", f"{words} words, one paragraph.")
    status = "fail" if words > ABSTRACT_WORD_LIMIT else "warn"
    return _check("Abstract is in filing form", status, " ".join(problems),
                  severity="error" if status == "fail" else "warn")


# ---------------------------------------------------------------------------------------------
# Reference numerals — the check this whole review exists for
# ---------------------------------------------------------------------------------------------
def numerals_used(text: str) -> Counter:
    """Numbers in prose that are being used as reference numerals.

    Not every number in a specification is a numeral: dimensions, percentages, years, claim
    numbers and figure numbers are not.  Those are removed first, and what survives is counted.
    Getting this wrong in the generous direction is the expensive mistake — it would report every
    measurement as an undefined part — so the exclusions are aggressive.
    """
    cleaned = re.sub(r"\bFIGS?\.?\s*[0-9]+[A-Za-z]?(\s*(?:-|–|—|to|through|and)\s*[0-9]+[A-Za-z]?)?",
                     " ", text or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"\bclaims?\s+[0-9,\s\-–and or]+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[REF:[^\]]*\]", " ", cleaned)
    # Numbers carrying units, decimals, ranges or percent signs are measurements.
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*(?:%|percent|mm|cm|m\b|in\.|inch(?:es)?|ft|kg|g\b|lb|"
                     r"psi|kpa|mpa|bar|deg|degrees?|°|hz|khz|mhz|v\b|volts?|a\b|amps?|w\b|watts?|"
                     r"s\b|sec(?:onds?)?|min(?:utes?)?|h\b|hours?|rpm|n\b|newtons?)", " ",
                     cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d+\.\d+\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:19|20)\d{2}\b", " ", cleaned)
    cleaned = re.sub(r"\b\d+\s*(?:-|–|—|to)\s*\d+\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:35|37)\s+(?:U\.?S\.?C\.?|C\.?F\.?R\.?)[^.\n]*", " ", cleaned)
    return Counter(match.group(1) for match in _NUMERAL_IN_TEXT_RE.finditer(cleaned))


def _numeral_checks(spec_text: str, claims_text: str,
                    numerals: Sequence[Mapping[str, str]],
                    figures: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    table = {str(item.get("numeral") or "").strip(): str(item.get("part") or "").strip()
             for item in numerals if str(item.get("numeral") or "").strip()}
    used = numerals_used(spec_text)
    out: list[dict[str, Any]] = []

    undefined = sorted((n for n in used if n not in table), key=_numeral_sort)
    if undefined:
        out.append(_check(
            "Every numeral in the text is defined", "fail",
            f"{len(undefined)} reference numeral(s) appear in the specification but are not in "
            "the numeral table, so the part they label is never fixed.",
            items=[f"{n} (used {used[n]}x)" for n in undefined]))
    else:
        out.append(_check("Every numeral in the text is defined", "pass",
                          f"All {len(used)} numerals used in the text are in the table."))

    unused = sorted((n for n in table if n not in used), key=_numeral_sort)
    if unused:
        out.append(_check(
            "Every defined numeral is used", "warn",
            f"{len(unused)} numeral(s) are listed in the table but never appear in the "
            "specification.", severity="warn",
            items=[f"{n} = {table[n]}" for n in unused]))
    else:
        out.append(_check("Every defined numeral is used", "pass",
                          "No orphaned entries in the numeral table."))

    by_part: dict[str, list[str]] = defaultdict(list)
    for numeral, part in table.items():
        by_part[_normal(part)].append(numeral)
    collisions = {part: sorted(nums, key=_numeral_sort)
                  for part, nums in by_part.items() if len(nums) > 1 and part}
    if collisions:
        out.append(_check(
            "One numeral per part", "fail",
            f"{len(collisions)} part(s) carry more than one reference numeral. A reader cannot "
            "tell whether these are the same element or different ones.",
            items=[f"{part}: {', '.join(nums)}" for part, nums in collisions.items()]))
    else:
        out.append(_check("One numeral per part", "pass",
                          "Each part in the table has exactly one numeral."))

    #  Scan the claim BODIES, not the claim set: "1. A handheld vacuum lifting tool…" begins with
    #  the claim's own number, and counting that as a reference numeral reported every claim in
    #  the application as an undefined part. Measured on a real 20-claim draft: 8 false positives,
    #  which is exactly the noise that teaches a user to ignore the panel.
    in_claims = numerals_used("\n".join(claim["text"] for claim in split_claims(claims_text)))
    stray = sorted((n for n in in_claims if n not in table), key=_numeral_sort)
    if stray:
        out.append(_check(
            "Numerals in the claims are defined", "warn",
            "Reference numerals appear in the claims without being in the table. Numerals in "
            "claims are permitted (37 CFR 1.75(d)(1)) but must be in parentheses and must match "
            "the specification.", severity="warn", items=stray))

    out.append(_first_use_introduces(spec_text, table))

    figure_numerals = {re.sub(r"\D", "", str(n).split()[0]) if str(n).strip() else ""
                       for figure in figures for n in (figure.get("numerals") or [])}
    unknown_on_figures = sorted((n for n in figure_numerals if n and n not in table),
                                key=_numeral_sort)
    if unknown_on_figures:
        out.append(_check(
            "Numerals on the drawings are defined", "fail",
            "A drawing lists a reference numeral that the numeral table does not define.",
            items=unknown_on_figures))
    elif figures:
        out.append(_check("Numerals on the drawings are defined", "pass",
                          "Every numeral marked on a drawing is in the table."))
    return out


def _first_use_introduces(spec_text: str, table: Mapping[str, str]) -> dict[str, Any]:
    """Is each numeral's first appearance next to the name of the part it labels?

    Heuristic and marked as such: it compares the head noun of the table entry against the six
    words before the first occurrence.  A numeral introduced as "the cup 10" instead of "a suction
    cup 10" is a real defect, but so is a false positive here, so this can only ever advise.
    """
    problems = []
    for numeral, part in table.items():
        head = _head_noun(part)
        if not head:
            continue
        match = re.search(rf"((?:\S+\s+){{0,6}})\b{re.escape(numeral)}\b", spec_text)
        if not match:
            continue
        window = _normal(match.group(1))
        if head not in window and not any(word in window for word in _normal(part).split()):
            problems.append(f"{numeral} ({part}) — first written as: …{match.group(1).strip()} "
                            f"{numeral}")
    if not problems:
        return _check("Numerals are introduced with their part name", "pass",
                      "Each numeral first appears beside the part it labels.", severity="advisory")
    return _check("Numerals are introduced with their part name", "warn",
                  f"{len(problems)} numeral(s) may first appear without naming the part. This is "
                  "a heuristic reading of the words before the numeral; check each one.",
                  severity="advisory", items=problems)


def _numeral_sort(numeral: str) -> tuple[int, str]:
    return (int(re.sub(r"\D", "", numeral) or 0), numeral)


# ---------------------------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------------------------
def figure_number(label: str) -> str:
    """The number of the figure a label names, or ''.

    A real caption is "FIG. 3 — Enlarged detail III of FIG. 2", and stripping non-digits out of
    that yields "32". The first FIG. in the label is the figure the label is FOR; any later one is
    a cross-reference.
    """
    match = _FIG_RE.search(str(label or ""))
    if match:
        return match.group(1).upper()
    digits = re.sub(r"\D", "", str(label or ""))
    return digits[:3]


def figures_mentioned(text: str) -> set[str]:
    found = set()
    for match in _FIG_RANGE_RE.finditer(text or ""):
        start, end = match.group(1), match.group(2)
        try:
            for number in range(int(re.sub(r"\D", "", start)), int(re.sub(r"\D", "", end)) + 1):
                found.add(str(number))
        except ValueError:
            pass
    for match in _FIG_RE.finditer(text or ""):
        found.add(match.group(1).upper())
    return found


def _figure_checks(sections: Mapping[str, str],
                   figures: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    described = figures_mentioned(sections.get("drawing_descriptions", ""))
    in_detail = figures_mentioned(sections.get("detailed_description", ""))
    out: list[dict[str, Any]] = []

    undescribed = sorted(in_detail - described, key=_numeral_sort)
    if undescribed:
        out.append(_check(
            "Every figure used is described", "fail",
            "The detailed description refers to figures that the Brief Description of the "
            "Drawings does not list. 37 CFR 1.74 requires each figure to be described.",
            items=[f"FIG. {n}" for n in undescribed]))
    else:
        out.append(_check("Every figure used is described", "pass",
                          f"{len(described) or 0} figure(s) described, all referred to figures "
                          "are among them."))

    unused = sorted(described - in_detail, key=_numeral_sort)
    if unused:
        out.append(_check(
            "Every described figure is discussed", "warn",
            "A figure is listed in the Brief Description but never referred to in the detailed "
            "description.", severity="warn", items=[f"FIG. {n}" for n in unused]))

    if described:
        numeric = sorted({int(re.sub(r"\D", "", n)) for n in described if re.sub(r"\D", "", n)})
        gaps = [n for n in range(1, (numeric[-1] if numeric else 0) + 1) if n not in numeric]
        if gaps:
            out.append(_check("Figure numbering is contiguous", "warn",
                              "The figure numbers skip one or more values.", severity="warn",
                              items=[f"FIG. {n} missing" for n in gaps]))

    if figures:
        labels = {figure_number(f.get("label")) for f in figures}
        missing = sorted({n for n in described
                          if n not in labels and re.sub(r"\D", "", n) not in labels},
                         key=_numeral_sort)
        if missing:
            out.append(_check(
                "Each described figure has a drawing sheet", "warn",
                "A figure is described in the specification but no drawing has been prepared for "
                "it yet.", severity="warn", items=[f"FIG. {n}" for n in missing]))
    return out


# ---------------------------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------------------------
def split_claims(claims_text: str) -> list[dict[str, Any]]:
    """Split a numbered claim set into individual claims, preserving the numbers as written."""
    text = (claims_text or "").strip()
    if not text:
        return []
    marks = list(_CLAIM_START_RE.finditer(text))
    if not marks:
        return [{"number": 1, "text": text}]
    out = []
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        out.append({"number": int(match.group(1)),
                    "text": text[match.end():end].strip()})
    return out


def claim_dependencies(claim_text: str) -> list[int]:
    """The claim numbers a claim depends from, as written.

    Ranges are expanded FIRST and removed from the string before the remaining bare numbers are
    read, so "claims 1 to 3, or claim 7" yields 1, 2, 3, 7 rather than 1, 3, 7.
    """
    numbers: set[int] = set()
    for match in _DEPENDENCY_RE.finditer(claim_text or ""):
        body = match.group(1)
        for span in _RANGE_RE.finditer(body):
            low, high = int(span.group(1)), int(span.group(2))
            if 0 < low <= high <= 999:
                numbers.update(range(low, high + 1))
        for value in re.findall(r"\d{1,3}", _RANGE_RE.sub(" ", body)):
            numbers.add(int(value))
    return sorted(numbers)


def _claim_checks(claims_text: str, spec_text: str) -> list[dict[str, Any]]:
    claims = split_claims(claims_text)
    out: list[dict[str, Any]] = []
    if not claims:
        return [_check("Claims are numbered consecutively", "fail", "No claims were found.")]

    numbers = [claim["number"] for claim in claims]
    expected = list(range(1, len(claims) + 1))
    if numbers != expected:
        duplicates = [n for n, count in Counter(numbers).items() if count > 1]
        out.append(_check(
            "Claims are numbered consecutively", "fail",
            "37 CFR 1.126 requires claims to be numbered consecutively from 1. "
            + (f"Repeated numbers: {duplicates}. " if duplicates else "")
            + f"Found {numbers[:20]}."))
    else:
        out.append(_check("Claims are numbered consecutively", "pass",
                          f"{len(claims)} claims, numbered 1 to {len(claims)}."))

    known = set(numbers)
    broken, forward, multiples, multiple_on_multiple = [], [], [], []
    for claim in claims:
        deps = claim_dependencies(claim["text"])
        for dependency in deps:
            if dependency not in known:
                broken.append(f"claim {claim['number']} depends on claim {dependency}, "
                              "which does not exist")
            elif dependency >= claim["number"]:
                forward.append(f"claim {claim['number']} depends on claim {dependency}")
        if len(deps) > 1:
            multiples.append(f"claim {claim['number']} depends on {deps}")
            for dependency in deps:
                parent = next((c for c in claims if c["number"] == dependency), None)
                if parent and len(claim_dependencies(parent["text"])) > 1:
                    multiple_on_multiple.append(
                        f"claim {claim['number']} depends on multiple-dependent claim {dependency}")

    if broken or forward:
        out.append(_check("Claim dependencies are valid", "fail",
                          "A dependent claim must refer back to an earlier, existing claim.",
                          items=broken + forward))
    else:
        out.append(_check("Claim dependencies are valid", "pass",
                          "Every dependent claim refers back to an earlier claim."))
    if multiple_on_multiple:
        out.append(_check(
            "No multiple dependent claim depends on another", "fail",
            "37 CFR 1.75(c): a multiple dependent claim may not serve as a basis for another "
            "multiple dependent claim.", items=multiple_on_multiple))
    elif multiples:
        out.append(_check(
            "Multiple dependent claims", "warn",
            "Multiple dependent claims are allowed but each is counted as several claims for "
            "fees (37 CFR 1.75(c)) and carries a surcharge.", severity="warn", items=multiples))

    independents = [c for c in claims if not claim_dependencies(c["text"])]
    if not independents:
        out.append(_check("At least one independent claim", "fail",
                          "Every claim depends on another; there is no independent claim."))
    else:
        out.append(_check("At least one independent claim", "pass",
                          f"{len(independents)} independent claim(s): "
                          f"{[c['number'] for c in independents]}."))

    out.append(_antecedent_basis(claims))
    out.append(_claim_support(claims, spec_text))
    means = [f"claim {c['number']}" for c in claims
             if re.search(r"\bmeans\s+for\b|\bstep\s+for\b", c["text"], re.IGNORECASE)]
    if means:
        out.append(_check(
            "Means-plus-function language", "warn",
            "35 USC 112(f) construes these limitations as covering only the corresponding "
            "structure in the specification and its equivalents. Confirm the specification "
            "discloses that structure, or rewrite the limitation structurally.",
            severity="warn", items=means))
    return out


def _antecedent_basis(claims: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Definite articles without a prior indefinite introduction, within a claim's own chain."""
    by_number = {claim["number"]: claim for claim in claims}
    problems: list[str] = []
    for claim in claims:
        chain, seen, cursor = [], set(), claim["number"]
        while cursor in by_number and cursor not in seen:
            seen.add(cursor)
            chain.append(by_number[cursor]["text"])
            parents = claim_dependencies(by_number[cursor]["text"])
            cursor = parents[0] if parents else None            # type: ignore[assignment]
        chain_text = " ".join(reversed(chain)).lower()
        introduced: set[str] = set()
        for match in _ARTICLE_INTRO_RE.finditer(chain_text):
            introduced |= _terms(_trim_phrase(match.group(1)))
        own = by_number[claim["number"]]["text"].lower()
        for match in _ARTICLE_REF_RE.finditer(own):
            phrase = _trim_phrase(match.group(1))
            if not phrase or phrase in _NO_BASIS_NEEDED:
                continue
            candidates = _terms(phrase)
            if (candidates & introduced) or (candidates & _NO_BASIS_NEEDED) or \
                    re.match(r"^claim\b", phrase):
                continue
            problems.append(f"claim {claim['number']}: “the {phrase}”")
    if not problems:
        return _check("Antecedent basis in the claims", "pass",
                      "Every definite article has an earlier introduction in its own claim chain.",
                      severity="advisory")
    return _check(
        "Antecedent basis in the claims", "warn",
        f"{len(problems)} definite article(s) may lack antecedent basis. This is a language "
        "heuristic, not a parse of the claim: read each one before changing it.",
        severity="advisory", items=sorted(set(problems))[:40])


def _claim_support(claims: Sequence[Mapping[str, Any]], spec_text: str) -> dict[str, Any]:
    """Claim vocabulary that never appears in the specification.

    35 USC 112(a) needs the description to support what is claimed.  Word presence is a weak proxy
    for support, so this is advisory — but a claim term that appears NOWHERE in the description is
    a reliable signal, and it is the exact defect a model introduces when it broadens a claim
    without going back to widen the description.
    """
    spec_words = set(_normal(spec_text).split())
    spec_stems = {_stem(word) for word in spec_words}
    missing: list[str] = []
    for claim in claims:
        for word in set(_normal(claim["text"]).split()):
            if len(word) < 4 or word in _STOPWORDS or word.isdigit():
                continue
            #  A claim that says "energising" where the description says "energises" is the same
            #  word, and reporting it as unsupported buries the one that genuinely IS a different
            #  word ("housing" for what the description calls a body).
            if word in spec_words or _stem(word) in spec_stems:
                continue
            missing.append(f"claim {claim['number']}: “{word}”")
    if not missing:
        return _check("Claim terms appear in the description", "pass",
                      "Every substantive claim word also appears in the specification.",
                      severity="advisory")
    return _check(
        "Claim terms appear in the description", "warn",
        f"{len(set(missing))} claim word(s) do not appear anywhere in the specification. Under "
        "35 USC 112(a) the description must support what is claimed; check whether the "
        "description needs to be widened or the claim reworded.",
        severity="advisory", items=sorted(set(missing))[:40])


# ---------------------------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------------------------
def _citation_checks(sections: Mapping[str, str], allowed: Sequence[str],
                     *, allow_remote: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    allowed_set = {draft_cite.normalize(a) for a in allowed if draft_cite.normalize(a)}
    cited: list[str] = []
    misplaced: list[str] = []
    malformed: list[str] = []

    for key, _name, heading in draft_workspace.SECTION_FILES:
        body = str(sections.get(key) or "")
        here = draft_cite.citations_in(body)
        cited.extend(here)
        malformed.extend(f"{heading}: [REF:{raw}]"
                         for raw in draft_cite.malformed_citations_in(body))
        if here and key in UNCITABLE_SECTIONS:
            misplaced.extend(f"{heading}: [REF:{c}]" for c in dict.fromkeys(here))

    unique = list(dict.fromkeys(draft_cite.normalize(c) or c for c in cited))
    if malformed:
        out.append(_check("Citation tokens are well formed", "fail",
                          "A citation token does not contain a usable publication number.",
                          items=malformed))
    if misplaced:
        out.append(_check(
            "Citations sit where they belong", "fail",
            "Prior art is discussed in the Background and may be incorporated by reference in the "
            "Detailed Description. A citation in the claims, abstract, title or summary is a "
            "drafting error.", items=misplaced))
    elif unique:
        out.append(_check("Citations sit where they belong", "pass",
                          "Every citation is in a section where prior art belongs."))

    if not unique:
        out.append(_check(
            "Prior art is cited", "warn" if allowed_set else "pass",
            "The draft cites no prior art. Where art was supplied, the Background should say what "
            "it teaches and where this invention departs from it."
            if allowed_set else "No prior art was supplied for this draft.",
            severity="warn" if allowed_set else "advisory"))
        return out

    unselected = [c for c in unique if allowed_set and c not in allowed_set]
    if unselected:
        out.append(_check(
            "Citations are to supplied references", "fail",
            "The draft cites a publication that is not among the prior art supplied to it. Either "
            "add it as a source or remove the citation.", items=unselected))

    resolved = draft_cite.check_all(unique, allow_remote=allow_remote)
    unreachable = [f"{pub} — {record.get('reason') or 'not found'}"
                   for pub, record in resolved.items() if not record.get("found")]
    if unreachable:
        out.append(_check(
            "Every citation resolves to a real publication", "fail",
            f"{len(unreachable)} of {len(unique)} cited publication(s) could not be found in the "
            "corpus or any reachable source. A citation that resolves to nothing is the one "
            "defect a reader cannot catch by reading.", items=unreachable))
    else:
        out.append(_check("Every citation resolves to a real publication", "pass",
                          f"All {len(unique)} cited publication(s) resolve.",
                          items=[f"{pub} — {record.get('title', '')[:120]} "
                                 f"({record.get('source')})" for pub, record in resolved.items()]))

    uncited = sorted(allowed_set - set(unique))
    if uncited:
        out.append(_check(
            "Supplied art is addressed", "warn",
            f"{len(uncited)} supplied reference(s) are never cited. That can be correct — not "
            "every search hit belongs in the Background — but each one is art the examiner may "
            "raise, so the draft should have a reason for passing over it.",
            severity="warn", items=uncited))

    bare = []
    for key, _name, heading in draft_workspace.SECTION_FILES:
        for pub in draft_cite.bare_publication_numbers(str(sections.get(key) or "")):
            if pub not in unique:
                bare.append(f"{heading}: {pub}")
    if bare:
        out.append(_check(
            "Publication numbers use citation tokens", "warn",
            "A publication number is written into the prose without a [REF:...] token, so it is "
            "invisible to the citation list and the IDS export.", severity="warn", items=bare))
    return out


def _open_notes(sections: Mapping[str, str]) -> dict[str, Any]:
    notes = []
    for key, _name, heading in draft_workspace.SECTION_FILES:
        for match in _DRAFTING_NOTE_RE.finditer(str(sections.get(key) or "")):
            notes.append(f"{heading}: {match.group(1).strip()[:180]}")
    if not notes:
        return _check("No unresolved drafting notes", "pass",
                      "The draft contains no open [DRAFTING NOTE] placeholders.")
    return _check("No unresolved drafting notes", "warn",
                  f"{len(notes)} drafting note(s) still need a fact only the inventor has. These "
                  "must be resolved before filing.", severity="warn", items=notes)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())


def _trim_phrase(phrase: str) -> str:
    words = []
    for word in _normal(phrase).split():
        if word in _PHRASE_STOP:
            break
        words.append(word)
    return " ".join(words).strip()


def _terms(phrase: str) -> set[str]:
    """Every contiguous run of up to three words in a noun phrase, singular and plural.

    Matching prefixes alone was not enough on a real draft: "an evacuable chamber" was introduced
    and "the evacuable chamber displaces" was reported as lacking basis, because the participle
    that follows the noun is part of neither a prefix nor the head word. A claim term is normally
    one to three words, so comparing every short run inside the phrase finds the noun wherever it
    sits, at the cost of occasionally accepting a term that was not introduced — the right trade
    for a check that can only ever advise.
    """
    words = _trim_phrase(phrase).split()
    out: set[str] = set()
    for size in (1, 2, 3):
        for start in range(0, max(0, len(words) - size + 1)):
            run = words[start:start + size]
            out.add(" ".join(run))
            out.add(" ".join(run[:-1] + [_singular(run[-1])]))
    return {term for term in out if term}


def _head_noun(phrase: str) -> str:
    words = _trim_phrase(phrase).split()
    return words[-1] if words else ""


#  Longest first, and the -ise family before the bare -es/-ed, or "energises" stems to "energis"
#  while "energising" stems to "energ" and the two forms of one word stop matching.
_SUFFIXES = tuple(sorted(
    ("isations", "isation", "isers", "iser", "ising", "ises", "ised", "ise",
     "ations", "ation", "ings", "ing", "ies", "ied", "able", "ible", "edly", "ally",
     "ness", "ment", "es", "ed", "ly", "s"), key=len, reverse=True))


def _stem(word: str) -> str:
    """A crude stem: enough to see that energise/energises/energising are one word.

    Deliberately not a real stemmer. Everything it is used for is a comparison between two words
    from the same document, so it only has to be CONSISTENT, and a wrong-but-consistent stem
    costs a missed advisory rather than a false one.
    """
    word = word.replace("z", "s")
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            word = word[:-len(suffix)]
            break
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]
    return word


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es") and word[-3] in "sxzh":
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def verdict_for(checks: Sequence[Mapping[str, Any]],
                findings: Sequence[Mapping[str, Any]]) -> str:
    """pass / warn / fail — a triage signal about internal consistency, never about patentability.

    Only a check that code can PROVE, or a finding the reviewer marked critical, can fail a draft.
    Advisory checks and lesser findings can raise a warning and nothing more.
    """
    for check in checks:
        if check.get("status") == "fail" and check.get("severity") == "error":
            return "fail"
    if any(str(f.get("severity")) == "critical" for f in findings):
        return "fail"
    if any(check.get("status") in ("fail", "warn") for check in checks):
        return "warn"
    if findings:
        return "warn"
    return "pass"


def counts_for(checks: Sequence[Mapping[str, Any]],
               findings: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    severities = Counter(str(f.get("severity") or "minor") for f in findings)
    return {
        "checks": len(checks),
        "checks_passed": sum(1 for c in checks if c.get("status") == "pass"),
        "checks_failed": sum(1 for c in checks if c.get("status") == "fail"),
        "checks_warned": sum(1 for c in checks if c.get("status") == "warn"),
        "findings": len(findings),
        "critical": severities.get("critical", 0),
        "major": severities.get("major", 0),
        "minor": severities.get("minor", 0),
    }


def summarize(checks: Sequence[Mapping[str, Any]], findings: Sequence[Mapping[str, Any]],
              verdict: str) -> str:
    counts = counts_for(checks, findings)
    parts = []
    if counts["checks_failed"]:
        parts.append(f"{counts['checks_failed']} mechanical check(s) failed")
    if counts["checks_warned"]:
        parts.append(f"{counts['checks_warned']} raised a warning")
    if counts["critical"]:
        parts.append(f"{counts['critical']} critical review finding(s)")
    if counts["major"]:
        parts.append(f"{counts['major']} major")
    if counts["minor"]:
        parts.append(f"{counts['minor']} minor")
    if not parts:
        return (f"All {counts['checks']} consistency checks passed and the reviewer found nothing "
                "to raise.")
    lead = {"fail": "The draft is not internally consistent yet",
            "warn": "The draft holds together, with points to settle",
            "pass": "The draft is consistent"}.get(verdict, "Reviewed")
    return f"{lead}: {', '.join(parts)}."


# =============================================================================================
# The reviewing agent
# =============================================================================================
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                    "category": {"type": "string", "enum": [
                        "figures_and_numerals", "internal_logic", "terminology",
                        "citations", "claim_support", "claim_scope", "enablement", "formalities"]},
                    "title": {"type": "string"},
                    "where": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "category", "title", "where", "detail", "evidence",
                             "fix"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}

REVIEW_SYSTEM = """You are reviewing a US patent application draft for INTERNAL CONSISTENCY. You
did not write it and you have not heard the reasons for any of its choices; judge only what is on
the page.

You are NOT assessing patentability, novelty, non-obviousness, validity, infringement or freedom
to operate, and you must not state or imply any of them. You are checking whether the document
says the same thing everywhere and whether it is supported by its own sources.

WHAT TO CHECK, in this order of importance:

1. FIGURES, NUMERALS AND DESCRIPTIONS AGREE.
   Every reference numeral labels one part and only that part, everywhere it appears. The part a
   numeral labels in the detailed description is the part it labels in draft/numerals.md and on
   the figure files in figures/. A figure described in the Brief Description of the Drawings shows
   what the detailed description says it shows. Nothing is described as being shown in a figure
   that the figure's own file does not contain.

2. THE LANGUAGE AND THE LOGIC HOLD TOGETHER.
   One name per thing, used consistently — not "gripper" here and "grasping unit" there for the
   same element. No statement that contradicts another. No step that depends on a structure the
   draft never gives it. No embodiment described as preferred in one place and impossible in
   another.

3. THE CITATIONS ARE HONEST.
   Read prior_art/. For every [REF:...] citation in the draft, check that what the draft says
   about that reference is actually in that reference's file. A characterisation the source does
   not support is the most damaging error in this document: report it as critical. Report a
   citation used where the source file says nothing on the point. (Whether the publication EXISTS
   is checked mechanically elsewhere — do not spend turns on it.)

4. THE CLAIMS MATCH WHAT WAS DISCLOSED.
   Every limitation in every claim must have support in the detailed description and, where the
   limitation is structural, be visible in the drawings the draft describes. A claim broader than
   the description supports is a critical finding. So is a claim reciting an element the
   description never mentions. Check terminology drift between claim and description for the SAME
   element.

HOW TO REPORT
   Use the tools to read the workspace. Every finding must name where it is (`where`) and quote
   the text it is about (`evidence`) — a finding without a quote from the document is a guess and
   must not be reported. If you are unsure, say so in the detail and mark it minor.
   Report NOTHING you have not verified by reading. An empty findings list is a valid and useful
   answer; padding it with speculation is not.
   `python3 tools/patent_lookup.py <PUB>` reads a publication out of the corpus if you need the
   real text of a reference that prior_art/ summarises."""

REVIEW_PROMPT = """Review the draft in this workspace.

  draft/            the application, one file per section, plus numerals.md
  figures/          one file per drawing: what it shows and which numerals appear on it
  prior_art/        the references the draft is allowed to cite, with their actual text
  input/            the inventor's disclosure and the conversation with the drafter

Read draft/ in full — every section, not a sample. Then read numerals.md and figures/, then the
prior_art/ files for every reference the draft cites.

The mechanical checks below have ALREADY been run in code. Do not repeat them; use them as
context for where to look.

%(checks)s

Return your findings in the required structured form."""


def review(workspace: Path, *, checks: Sequence[Mapping[str, Any]],
           transcript: Path | None = None, model: str = "",
           timeout: int = draft_agent.QA_TIMEOUT) -> dict[str, Any]:
    """Run the independent reviewer over a workspace. Never raises."""
    lines = []
    for check in checks:
        if check.get("status") == "pass":
            continue
        lines.append(f"- [{check.get('status')}] {check.get('name')}: {check.get('detail')}")
        for item in (check.get("items") or [])[:8]:
            lines.append(f"    · {item}")
    context = ("\n".join(lines) if lines else
               "- every mechanical check passed; look for what code cannot see")
    try:
        run = draft_agent.run(
            workspace=workspace,
            prompt=REVIEW_PROMPT % {"checks": context},
            system_prompt=REVIEW_SYSTEM,
            schema=REVIEW_SCHEMA,
            session_id=draft_agent.new_session_id(),
            resume=False,                       # a fresh mind on purpose; see the module docstring
            model=model or draft_agent.QA_MODEL,
            tools="Read,Glob,Grep,Bash",
            timeout=timeout,
            transcript=transcript,
        )
    except draft_agent.AgentError as exc:
        return {"ok": False, "error": str(exc), "findings": [], "summary": "", "cost_usd": 0.0,
                "duration_ms": 0, "model": model or draft_agent.QA_MODEL, "steps": []}
    if not run.ok:
        return {"ok": False, "error": run.error, "findings": [], "summary": "",
                "cost_usd": run.cost_usd, "duration_ms": run.duration_ms, "model": run.model,
                "steps": run.steps}
    return {"ok": True, "error": "", "summary": str(run.result.get("summary") or "")[:8000],
            "findings": normalize_findings(run.result.get("findings")),
            "cost_usd": run.cost_usd, "duration_ms": run.duration_ms, "model": run.model,
            "steps": run.steps}


def normalize_findings(value: Any, *, limit: int = 60) -> list[dict[str, Any]]:
    """Bound and clean the reviewer's findings; drop any that carry no evidence.

    The no-evidence rule is enforced here rather than trusted to the prompt.  A finding without a
    quotation cannot be checked by the person reading it, and an unfalsifiable warning in a review
    panel is worse than no warning: it is the one users learn to scroll past.
    """
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        if not isinstance(item, Mapping):
            continue
        evidence = str(item.get("evidence") or "").strip()
        title = str(item.get("title") or "").strip()
        if not title or not evidence:
            continue
        severity = str(item.get("severity") or "minor").lower()
        out.append({
            "severity": severity if severity in ("critical", "major", "minor") else "minor",
            "category": str(item.get("category") or "internal_logic")[:40],
            "title": title[:300],
            "where": str(item.get("where") or "")[:200],
            "detail": str(item.get("detail") or "")[:4000],
            "evidence": evidence[:2000],
            "fix": str(item.get("fix") or "")[:2000],
        })
    order = {"critical": 0, "major": 1, "minor": 2}
    out.sort(key=lambda f: order.get(f["severity"], 3))
    return out


def as_markdown(report: Mapping[str, Any]) -> str:
    """The report as the drafting agent will read it on its next turn."""
    return json.dumps({"verdict": report.get("verdict"), "summary": report.get("summary"),
                       "checks": report.get("checks"), "findings": report.get("findings")},
                      ensure_ascii=False, indent=2)


def failed_check_names(checks: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(c.get("name")) for c in checks if c.get("status") == "fail"]
