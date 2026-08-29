"""The checks that need no picture.

Every one of these is arithmetic over the registry, the plan and the figures. No model is
consulted, and none of them is a matter of taste: a numeral is either in the registry or it is
not, a claim element either reaches a figure or it does not, and the figure labels either run
consecutively or they have a gap in them.

That is deliberate. A compliance report whose findings depend on a model's opinion has to be
re-read by a human before it can be relied on, which makes it a suggestion. These findings can be
relied on, so they are the ones the retry loop is allowed to act on.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional, Sequence

from ..drawing import Figure
from ..schemas import Claim, Finding, Plan, Registry, Sections
from ..sections import figure_sort_key
from . import rules


def _finding(code: str, message: str, *, severity: str = "error", stage: str = "planner",
             figure: str = "", numeral: str = "", detail: Optional[dict] = None) -> Finding:
    cite, basis = rules.decorate(code)
    return Finding(code=code, severity=severity, message=message, stage=stage, figure=figure,
                   numeral=numeral, cite=cite, basis=basis, detail=detail or {})


def check(figures: Sequence[Figure], plan: Plan, registry: Registry, claims: list[Claim],
          sections: Sections) -> list[Finding]:
    out: list[Finding] = []
    out += check_cap(plan)
    out += check_numerals(figures, registry)
    out += check_reuse(plan, registry)
    out += check_labels(figures)
    out += check_claims(figures, claims, registry)
    out += check_conventions(figures, plan)
    out += check_brief(figures, sections)
    return out


# ------------------------------------------------------------------------ numerals and terms


def _drawn(figures: Sequence[Figure]) -> dict[str, list[str]]:
    """Numeral to the figures that carry it."""
    out: dict[str, list[str]] = defaultdict(list)
    for figure in figures:
        for numeral in figure.numerals():
            out[numeral].append(figure.label)
    return out


def check_numerals(figures: Sequence[Figure], registry: Registry) -> list[Finding]:
    known = registry.by_numeral()
    drawn = _drawn(figures)
    out: list[Finding] = []

    for numeral, labels in sorted(drawn.items()):
        if numeral in known:
            continue
        out.append(_finding(
            "numeral_not_in_registry",
            f"{numeral} is drawn in {', '.join(labels)} but the description never mentions it.",
            stage="renderer", figure=labels[0], numeral=numeral))

    for entry in registry.entries:
        if entry.numeral in drawn:
            continue
        out.append(_finding(
            "registry_numeral_undrawn",
            f"{entry.numeral} (\"{entry.term}\") is mentioned in the description but appears in "
            "no figure.",
            stage="planner", numeral=entry.numeral,
            detail={"term": entry.term, "mentions": entry.mentions}))
    return out


def check_reuse(plan: Plan, registry: Registry) -> list[Finding]:
    """A numeral that means two different things, seen across the whole drawing set."""
    terms: dict[str, set[str]] = defaultdict(set)
    where: dict[str, list[str]] = defaultdict(list)
    for figure in plan.figures:
        for element in figure.elements:
            if element.term:
                terms[element.numeral].add(element.term.strip().lower())
            where[element.numeral].append(figure.label)

    out: list[Finding] = []
    for numeral, values in sorted(terms.items()):
        if len(values) <= 1:
            continue
        out.append(_finding(
            "numeral_reused",
            f"{numeral} is used for " + " and ".join(sorted(f'"{v}"' for v in values))
            + f" across {', '.join(sorted(set(where[numeral])))}.",
            stage="planner", numeral=numeral, detail={"terms": sorted(values)}))

    by_term: dict[str, set[str]] = defaultdict(set)
    for entry in registry.entries:
        by_term[entry.term.strip().lower()].add(entry.numeral)
    for term, numerals in sorted(by_term.items()):
        if len(numerals) <= 1 or not term:
            continue
        stems = {re.match(r"\d+", n).group(0) for n in numerals if re.match(r"\d+", n)}
        if len(stems) == 1:
            continue                       # 106a and 106b: instances of one part
        out.append(_finding(
            "part_two_numerals",
            f"\"{term}\" carries more than one reference character: "
            + ", ".join(sorted(numerals)) + ".",
            severity="warning", stage="draft", numeral=", ".join(sorted(numerals))))
    return out


# ----------------------------------------------------------------------------------- labels

_LABEL = re.compile(r"^FIG\.\s(\d+)([A-Z]*)$")


def check_cap(plan: Plan) -> list[Finding]:
    """Say when the run drew fewer views than the draft asked for.

    Without this the only sign is dozens of "this numeral is in no figure" errors, which look
    like a planning failure rather than a limit that was hit. A cap that binds silently is a
    truncation reported as success.
    """
    if not plan.truncated_from:
        return []
    from ..plan import MAX_FIGURES

    return [_finding(
        "figure_set_truncated",
        f"the draft calls for {plan.truncated_from} views and this run drew the first "
        f"{MAX_FIGURES}. Everything reported below about the views that were not drawn follows "
        f"from that. Raise FM_MAX_FIGURES to draw them all, and expect about a minute per view.",
        stage="planner", detail={"asked": plan.truncated_from, "drawn": MAX_FIGURES})]


def check_labels(figures: Sequence[Figure]) -> list[Finding]:
    out: list[Finding] = []
    if not figures:
        return [_finding("no_figures", "The plan produced no figures.", stage="planner")]

    parsed: list[tuple[int, str, str]] = []
    for figure in figures:
        match = _LABEL.match(figure.label)
        if not match:
            out.append(_finding(
                "figure_label_malformed",
                f"{figure.label!r} is not of the form \"FIG. 1\" or \"FIG. 2A\".",
                figure=figure.label))
            continue
        parsed.append((int(match.group(1)), match.group(2), figure.label))

    if not parsed:
        return out
    parsed.sort()
    numbers = sorted({p[0] for p in parsed})
    if numbers[0] != 1:
        out.append(_finding(
            "figures_not_sequential",
            f"The views start at FIG. {numbers[0]}; they must start at FIG. 1.",
            figure=parsed[0][2]))
    missing = [n for n in range(numbers[0], numbers[-1] + 1) if n not in numbers]
    if missing:
        out.append(_finding(
            "figures_not_sequential",
            "The views skip " + ", ".join(f"FIG. {n}" for n in missing) + ".",
            detail={"missing": missing}))

    by_number: dict[int, list[str]] = defaultdict(list)
    for number, letter, _label in parsed:
        by_number[number].append(letter)
    for number, letters in sorted(by_number.items()):
        real = sorted(letter for letter in letters if letter)
        if not real:
            continue
        if "" in letters:
            out.append(_finding(
                "figures_not_sequential",
                f"FIG. {number} exists alongside lettered views "
                + ", ".join(f"FIG. {number}{letter}" for letter in real) + ".",
                severity="warning", figure=f"FIG. {number}"))
        expected = [chr(ord("A") + i) for i in range(len(real))]
        if real != expected:
            out.append(_finding(
                "figures_not_sequential",
                f"The lettered views of FIG. {number} are "
                + ", ".join(real) + f"; they should run {', '.join(expected)}.",
                figure=f"FIG. {number}{real[0]}"))
    return out


# ----------------------------------------------------------------------------------- claims


def check_claims(figures: Sequence[Figure], claims: list[Claim],
                 registry: Registry) -> list[Finding]:
    """37 CFR 1.83(a): the drawing must show every feature the claims specify."""
    drawn = _drawn(figures)
    out: list[Finding] = []
    for claim in claims:
        if not claim.independent:
            continue
        for element in claim.elements:
            if not element.numeral:
                # Quote the limitation, not the extracted term. A method step's term is often a
                # bare verb, and "claim 1 recites 'receiving'" reads like a parser bug rather
                # than the real point, which is that nothing in the description numbers it.
                quoted = element.text.strip()
                if len(quoted) > 90:
                    quoted = quoted[:87].rstrip() + "..."
                out.append(_finding(
                    "claim_element_unmatched",
                    f"claim {claim.number} recites \"{quoted}\" and no reference character in the "
                    "description names it, so it cannot be checked against the drawings.",
                    severity="warning", stage="draft",
                    detail={"claim": claim.number, "term": element.term,
                            "text": element.text[:200]}))
                continue
            if element.numeral in drawn:
                continue
            term = registry.term_for(element.numeral) or element.term
            out.append(_finding(
                "claim_element_not_depicted",
                f"claim {claim.number} recites \"{term}\" ({element.numeral}) and no figure "
                "shows it.",
                stage="planner", numeral=element.numeral,
                detail={"claim": claim.number, "text": element.text[:200]}))
    return out


# ------------------------------------------------------------------------------ conventions


def check_conventions(figures: Sequence[Figure], plan: Plan) -> list[Finding]:
    out: list[Finding] = []
    plans = {p.label: p for p in plan.figures}
    labels = {f.label for f in figures}

    for figure in figures:
        spec = plans.get(figure.label)
        if figure.kind != "cross_section":
            continue
        hatched = any(prim.role == "hatch" for prim in figure.prims)
        if not hatched:
            out.append(_finding(
                "section_without_hatching",
                f"{figure.label} is a sectional view and has no hatching on its cut surfaces.",
                stage="renderer", figure=figure.label))
        if spec is None:
            continue
        if spec.parent and spec.parent not in labels:
            out.append(_finding(
                "section_line_missing",
                f"{figure.label} is taken from {spec.parent}, which is not in the drawing set.",
                stage="planner", figure=figure.label))
        elif not spec.parent:
            out.append(_finding(
                "section_line_missing",
                f"{figure.label} is a sectional view but the plan does not say which view the "
                "cutting plane is drawn on.",
                severity="warning", stage="planner", figure=figure.label))
    return out


# ------------------------------------------------------------- the brief description of the
#                                                                drawings


def check_brief(figures: Sequence[Figure], sections: Sections) -> list[Finding]:
    out: list[Finding] = []
    drawn = [f.label for f in figures]
    if not sections.brief_items:
        out.append(_finding(
            "brief_description_missing",
            "The draft has no brief description of the drawings. One has been proposed; it needs "
            "to go into the specification.",
            severity="warning", stage="draft"))
        return out

    promised = [item.label for item in sections.brief_items]
    for label in promised:
        if label not in drawn:
            out.append(_finding(
                "brief_description_mismatch",
                f"the brief description promises {label} and the drawing set does not have it.",
                stage="planner", figure=label))
    for label in drawn:
        if label not in promised:
            out.append(_finding(
                "brief_description_mismatch",
                f"{label} was drawn but the brief description does not mention it.",
                severity="warning", stage="planner", figure=label))
    if promised != sorted(promised, key=figure_sort_key):
        out.append(_finding(
            "figures_not_sequential",
            "the brief description lists the views out of order.",
            severity="warning", stage="draft"))
    return out


def uncovered_registry(figures: Sequence[Figure], registry: Registry) -> list[str]:
    drawn = set(_drawn(figures))
    return [e.numeral for e in registry.entries if e.numeral not in drawn]
