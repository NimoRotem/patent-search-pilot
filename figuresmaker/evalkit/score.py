"""Scoring one run against what the granted patent says about its own drawings.

Four numbers, and each one is chosen because it fails for a different reason.

* **numeral-set agreement.** Jaccard between the characters our figures carry and the characters
  the patent's own description ties to figures. It falls when we omit a part, and it falls when
  we put a character in a drawing the description never connects it to. 37 CFR 1.84(p)(4) is
  exactly this requirement in both directions.
* **per-figure agreement.** The same measure, but figure by figure, over the labels the two sets
  share. A drawing set can have a perfect overall numeral set and still put every part in the
  wrong view.
* **claim-element coverage.** The fraction of independent-claim elements whose part reaches at
  least one figure. 37 CFR 1.83(a). This is the one an examiner objects to.
* **validator pass rate.** The fraction of runs with no compliance error left. It measures the
  pipeline, not the reading.

Figure count and kind agreement are reported alongside, because a set that drops half the views
can still score well on the parts it did draw.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence

from fm.drawing import Figure
from fm.schemas import Claim, Plan, ValidationReport

from .corpus import Truth


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    left, right = set(a), set(b)
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 1.0


@dataclass
class Score:
    number: str
    ok: bool = True
    error: str = ""
    seconds: float = 0.0

    figures_expected: int = 0
    figures_produced: int = 0
    figure_label_agreement: float = 0.0
    kind_agreement: float = 0.0

    numeral_agreement: float = 0.0
    numerals_expected: int = 0
    numerals_produced: int = 0
    numerals_missing: list[str] = field(default_factory=list)
    numerals_extra: list[str] = field(default_factory=list)

    per_figure_agreement: float = 0.0
    per_figure: dict[str, float] = field(default_factory=dict)

    claim_elements: int = 0
    claim_elements_covered: int = 0
    claim_coverage: float = 0.0

    validator_passed: bool = False
    errors: int = 0
    warnings: int = 0
    error_codes: dict[str, int] = field(default_factory=dict)

    attempts: dict[str, int] = field(default_factory=dict)
    model_calls: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_run(truth: Truth, figures: Sequence[Figure], plan: Plan, claims: list[Claim],
              report: ValidationReport, *, seconds: float = 0.0,
              attempts: Optional[dict] = None, calls: Optional[dict] = None) -> Score:
    out = Score(number=truth.number, seconds=round(seconds, 1),
                attempts=dict(attempts or {}), model_calls=dict(calls or {}))

    produced_labels = [f.label for f in figures]
    out.figures_expected = len(truth.figures)
    out.figures_produced = len(produced_labels)
    out.figure_label_agreement = jaccard(truth.figures, produced_labels)

    kinds = {f.label: f.kind for f in figures}
    shared_kinds = [label for label in truth.kinds if label in kinds]
    out.kind_agreement = (
        sum(1 for label in shared_kinds if kinds[label] == truth.kinds[label])
        / len(shared_kinds)) if shared_kinds else 0.0

    produced_numerals: set[str] = set()
    per_figure_produced: dict[str, set[str]] = {}
    for figure in figures:
        numerals = set(figure.numerals())
        per_figure_produced[figure.label] = numerals
        produced_numerals |= numerals

    expected_numerals = {n for numerals in truth.per_figure.values() for n in numerals}
    if not expected_numerals:
        expected_numerals = set(truth.numerals)
    out.numerals_expected = len(expected_numerals)
    out.numerals_produced = len(produced_numerals)
    out.numeral_agreement = jaccard(expected_numerals, produced_numerals)
    out.numerals_missing = sorted(expected_numerals - produced_numerals)[:40]
    out.numerals_extra = sorted(produced_numerals - expected_numerals)[:40]

    shared = [label for label in truth.per_figure if label in per_figure_produced]
    for label in shared:
        out.per_figure[label] = round(
            jaccard(truth.per_figure[label], per_figure_produced[label]), 3)
    out.per_figure_agreement = round(
        statistics.fmean(out.per_figure.values()), 4) if out.per_figure else 0.0

    elements = [e for c in claims if c.independent for e in c.elements]
    out.claim_elements = len(elements)
    out.claim_elements_covered = sum(
        1 for e in elements if e.numeral and e.numeral in produced_numerals)
    out.claim_coverage = (out.claim_elements_covered / out.claim_elements) \
        if out.claim_elements else 0.0

    out.validator_passed = report.passed
    out.errors = len(report.errors())
    out.warnings = len(report.warnings())
    for finding in report.errors():
        out.error_codes[finding.code] = out.error_codes.get(finding.code, 0) + 1

    for key in ("numeral_agreement", "per_figure_agreement", "claim_coverage",
                "figure_label_agreement", "kind_agreement"):
        setattr(out, key, round(float(getattr(out, key)), 4))
    return out


def failed(number: str, error: str, seconds: float = 0.0) -> Score:
    return Score(number=number, ok=False, error=error[:400], seconds=round(seconds, 1))


def aggregate(scores: Sequence[Score]) -> dict[str, Any]:
    done = [s for s in scores if s.ok]
    if not done:
        return {"cases": len(scores), "completed": 0,
                "note": "no case completed, so no score is meaningful"}

    def mean(attribute: str) -> float:
        return round(statistics.fmean(getattr(s, attribute) for s in done), 4)

    codes: dict[str, int] = {}
    for score in done:
        for code, count in score.error_codes.items():
            codes[code] = codes.get(code, 0) + count

    return {
        "cases": len(scores),
        "completed": len(done),
        "completion_rate": round(len(done) / len(scores), 3),
        "numeral_agreement": mean("numeral_agreement"),
        "per_figure_agreement": mean("per_figure_agreement"),
        "claim_coverage": mean("claim_coverage"),
        "figure_label_agreement": mean("figure_label_agreement"),
        "kind_agreement": mean("kind_agreement"),
        "validator_pass_rate": round(sum(1 for s in done if s.validator_passed) / len(done), 3),
        "mean_errors": mean("errors"),
        "mean_warnings": mean("warnings"),
        "mean_seconds": mean("seconds"),
        "error_codes": dict(sorted(codes.items(), key=lambda kv: -kv[1])),
        "failures": [{"number": s.number, "error": s.error} for s in scores if not s.ok],
    }


def table(scores: Sequence[Score]) -> str:
    header = (f"{'patent':18s} {'figs':>9s} {'numerals':>9s} {'per-fig':>8s} {'claims':>8s} "
              f"{'kind':>6s} {'err':>4s} {'warn':>5s} {'secs':>6s}")
    lines = [header, "-" * len(header)]
    for score in scores:
        if not score.ok:
            lines.append(f"{score.number:18s} {'FAILED':>9s}   {score.error[:60]}")
            continue
        lines.append(
            f"{score.number:18s} "
            f"{score.figures_produced:4d}/{score.figures_expected:<4d} "
            f"{score.numeral_agreement:9.3f} {score.per_figure_agreement:8.3f} "
            f"{score.claim_coverage:8.3f} {score.kind_agreement:6.2f} "
            f"{score.errors:4d} {score.warnings:5d} {score.seconds:6.0f}")
    return "\n".join(lines)
