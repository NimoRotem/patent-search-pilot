"""Cross-figure consistency: the guardrail that pays for the freedom to draw recognisable parts.

Letting the compiler draw a battery as a battery is only safe if it draws THAT battery the same
way every time. A reader who sees one symbol on FIG. 1 and a different one on FIG. 4 has to work
out whether they are looking at the same part, and a patent drawing that makes a reader guess has
failed at the one job it has.

Consistency is arranged structurally first: an entity's appearance is settled once, before any
layout, and every figure reads it off the entity. These rules check the DRAWINGS that came out,
because a renderer, a correction pass or a hand-applied patch can still diverge from the record,
and a guarantee nobody checks is a hope.

Four things are checked, and the severities differ on purpose:

  CON001  one part, one symbol, on every sheet                          blocking
  CON002  one kind of part, one symbol, across the document             warning
  CON003  two parts that appear together twice keep their size order    warning
  CON004  a part is only turned where the figure is a different view    warning

Only the first is blocking. A part drawn as two different things is a defect a reader cannot
resolve. The rest are the kind of thing that makes a drawing set look hand-assembled, which is
worth reporting and not worth refusing a figure over.
"""
from __future__ import annotations

from collections import defaultdict

from ..numerals import sort_key
from ..schemas import ValidationIssue
from .engine import ValidationContext, ValidationRule


def _drawn(context: ValidationContext):
    """(entity_id, node, figure) for everything drawn anywhere in the job."""
    for bundle in context.figures:
        for node in bundle.scene.nodes:
            yield node.entity_id, node, bundle


class EntityDrawnConsistently(ValidationRule):
    """CON001 — the same part is drawn with the same symbol on every sheet."""

    rule_id = "CON001"
    severity = "blocking"
    category = "cross_figure"
    repair_action = "relayout"
    scope = "job"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        seen: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for entity_id, node, bundle in _drawn(context):
            seen[entity_id].append((node.symbol or "", bundle.spec.figure_id))
        issues: list[ValidationIssue] = []
        for entity_id, rows in sorted(seen.items()):
            symbols_used = {symbol for symbol, _figure in rows}
            if len(symbols_used) <= 1:
                continue
            entity = context.graph.entity(entity_id)
            numeral = entity.reference_numeral if entity else None
            where = ", ".join(f"{figure} as "
                              f"{(symbol or 'a plain outline').replace('_', ' ')}"
                              for symbol, figure in sorted(rows, key=lambda r: r[1]))
            issues.append(self.issue(
                f"Reference {numeral or entity_id} is drawn as more than one thing across the "
                f"figures: {where}. A reader cannot tell they are the same part.",
                entity_id=entity_id, reference_numeral=numeral,
                detail={"symbols": sorted(symbols_used)}))
        return issues


class ClassDrawnConsistently(ValidationRule):
    """CON002 — two parts of the same kind are drawn with the same symbol.

    Not blocking. A patent may legitimately show two sensors differently when the description
    distinguishes them, and refusing that would be the compiler overruling the draft. But it is
    usually an oversight, so it is reported.
    """

    rule_id = "CON002"
    severity = "warning"
    category = "cross_figure"
    repair_action = "none"
    scope = "job"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        by_class: dict[str, set[str]] = defaultdict(set)
        members: dict[str, set[str]] = defaultdict(set)
        for entity_id, node, _bundle in _drawn(context):
            entity = context.graph.entity(entity_id)
            if entity is None or entity.visual_class == "generic_component":
                continue
            by_class[entity.visual_class].add(node.symbol or "")
            members[entity.visual_class].add(entity.reference_numeral or entity_id)
        issues = []
        for visual_class, used in sorted(by_class.items()):
            if len(used) <= 1:
                continue
            issues.append(self.issue(
                f"The parts the description calls a {visual_class.replace('_', ' ')} "
                f"({', '.join(sorted(members[visual_class], key=sort_key))}) are not all drawn "
                f"the same way: {', '.join(sorted(s or 'plain outline' for s in used))}.",
                detail={"visual_class": visual_class, "symbols": sorted(used)}))
        return issues


class RelativeSizeConsistently(ValidationRule):
    """CON003 — two parts that appear together on two sheets keep their size order.

    If the housing dwarfs the sensor on FIG. 1 and the sensor dwarfs the housing on FIG. 3, one
    of the two figures is lying about the assembly. Compared on drawn area, and only when the
    difference is big enough to be a decision rather than a rounding.
    """

    rule_id = "CON003"
    severity = "warning"
    category = "cross_figure"
    repair_action = "relayout"
    scope = "job"

    MEANINGFUL = 1.35        # one must be this much bigger before the order counts as stated

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        areas: dict[str, dict[str, float]] = defaultdict(dict)
        for entity_id, node, bundle in _drawn(context):
            # A container is sized by what it holds, not by what it is, so it is not compared.
            if node.is_container:
                continue
            areas[bundle.spec.figure_id][entity_id] = node.box.width * node.box.height

        order: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for figure_id, sizes in areas.items():
            ids = sorted(sizes)
            for index, first in enumerate(ids):
                for second in ids[index + 1:]:
                    a, b = sizes[first], sizes[second]
                    if a >= b * self.MEANINGFUL:
                        order[(first, second)].append((figure_id, "first"))
                    elif b >= a * self.MEANINGFUL:
                        order[(first, second)].append((figure_id, "second"))

        issues = []
        for (first, second), rows in sorted(order.items()):
            verdicts = {verdict for _figure, verdict in rows}
            if len(verdicts) <= 1:
                continue
            left = context.graph.entity(first)
            right = context.graph.entity(second)
            issues.append(self.issue(
                f"{(left.reference_numeral if left else first)} and "
                f"{(right.reference_numeral if right else second)} swap which is the larger "
                f"between {rows[0][0]} and {rows[-1][0]}.",
                entity_id=first,
                detail={"pair": [first, second],
                        "figures": [figure for figure, _v in rows]}))
        return issues


class OrientationIsAView(ValidationRule):
    """CON004 — a part is only turned where the figure genuinely is a different view.

    Turning a shaft from along the page to up it is exactly what a plan and an elevation of the
    same assembly should do. Turning it between two figures that claim the same view type is not
    a view change, it is an inconsistency.
    """

    rule_id = "CON004"
    severity = "warning"
    category = "cross_figure"
    repair_action = "relayout"
    scope = "job"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        seen: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for entity_id, node, bundle in _drawn(context):
            seen[entity_id][bundle.spec.view_type].add(node.orientation)
        issues = []
        for entity_id, by_view in sorted(seen.items()):
            for view_type, orientations in sorted(by_view.items()):
                if len(orientations) <= 1:
                    continue
                entity = context.graph.entity(entity_id)
                issues.append(self.issue(
                    f"{(entity.reference_numeral if entity else entity_id)} is drawn both "
                    f"{' and '.join(sorted(orientations))} in figures that are both "
                    f"{view_type} views.",
                    entity_id=entity_id,
                    reference_numeral=entity.reference_numeral if entity else None,
                    detail={"view_type": view_type,
                            "orientations": sorted(orientations)}))
        return issues


CONSISTENCY_RULES = [
    EntityDrawnConsistently(), ClassDrawnConsistently(),
    RelativeSizeConsistently(), OrientationIsAView(),
]
