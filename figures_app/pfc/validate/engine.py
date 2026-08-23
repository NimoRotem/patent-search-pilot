"""The validation engine: rules as objects, run over one context, producing typed issues.

Every rule is a small class with an identifier, a severity and a repair action. That last field
is what makes the correction loop possible: a rule does not merely say a figure is wrong, it
says which of the narrow, semantics-preserving repairs owns the problem. A label overlap is
repaired by moving a label; a missing relation is not, and no amount of moving labels will fix
it, so it blocks.

Nothing here mutates what it inspects. A validator that could edit the artifact it is checking
could make any figure pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..profiles import DrawingProfile
from ..schemas import (FigurePlan, FigureSpec, LayoutScene, PatentGraph, Severity,
                       ValidationIssue)

VALIDATION_VERSION = "pfc-validate-1.0.0"


@dataclass
class FigureBundle:
    """One figure as it stands after rendering: what it should show, and what it does."""

    spec: FigureSpec
    scene: LayoutScene
    svg: str = ""


@dataclass
class ValidationContext:
    graph: PatentGraph
    profile: DrawingProfile
    plan: FigurePlan
    figure: Optional[FigureBundle] = None
    figures: Sequence[FigureBundle] = field(default_factory=tuple)
    config: dict = field(default_factory=dict)


class ValidationRule:
    rule_id: str = ""
    severity: Severity = "blocking"
    category: str = "semantic"
    repair_action: str = "none"
    scope: str = "figure"          # "figure" or "job"

    def validate(self, context: ValidationContext) -> list[ValidationIssue]:
        raise NotImplementedError

    def issue(self, message: str, **kwargs) -> ValidationIssue:
        payload = {
            "rule_id": self.rule_id, "severity": self.severity, "category": self.category,
            "message": message, "repair_action": self.repair_action,
        }
        payload.update(kwargs)
        return ValidationIssue(**payload)


def run_rules(rules: Sequence[ValidationRule], context: ValidationContext,
              scope: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for rule in rules:
        if rule.scope != scope:
            continue
        found = rule.validate(context)
        if not found:
            continue
        figure_id = context.figure.spec.figure_id if context.figure else None
        for item in found:
            if scope == "figure" and item.figure_id is None:
                item.figure_id = figure_id
        issues.extend(found)
    return issues


def blocking(issues: Sequence[ValidationIssue]) -> list[ValidationIssue]:
    return [issue for issue in issues if issue.severity == "blocking"]


def warnings(issues: Sequence[ValidationIssue]) -> list[ValidationIssue]:
    return [issue for issue in issues if issue.severity == "warning"]
