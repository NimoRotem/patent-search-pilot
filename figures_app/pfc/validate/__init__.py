"""Deterministic validation: all rules, run in one place."""
from __future__ import annotations

from typing import Sequence

from ..schemas import ValidationIssue
from .engine import (VALIDATION_VERSION, FigureBundle, ValidationContext, ValidationRule,
                     blocking, run_rules, warnings)
from .geometric import GEOMETRY_RULES
from .semantic import SEMANTIC_RULES

ALL_RULES: list[ValidationRule] = [*SEMANTIC_RULES, *GEOMETRY_RULES]


def validate_figure(context: ValidationContext) -> list[ValidationIssue]:
    return run_rules(ALL_RULES, context, scope="figure")


def validate_job(context: ValidationContext) -> list[ValidationIssue]:
    return run_rules(ALL_RULES, context, scope="job")


def rule_index() -> dict[str, ValidationRule]:
    return {rule.rule_id: rule for rule in ALL_RULES}


__all__ = ["ALL_RULES", "FigureBundle", "VALIDATION_VERSION", "ValidationContext",
           "ValidationRule", "blocking", "rule_index", "validate_figure", "validate_job",
           "warnings"]
