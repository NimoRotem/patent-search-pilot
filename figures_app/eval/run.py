"""Evaluation harness: measure the compiler against annotated patents.

    python -m eval.run --dataset test_patents/golden

Semantic output is what is scored, never pixels. Two correct drawings of the same figure can
differ in every coordinate, so an image comparison would measure the layout solver's mood; what
matters is whether the right parts, with the right numerals and the right relationships between
them, ended up on the sheet.

This makes live model calls and costs money, so it is not part of the unit suite. Run it before
a release, and after any change to a prompt, the extraction filters or the figure planner.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pfc.numerals import sort_key  # noqa: E402
from pfc.pipeline import run_job  # noqa: E402
from pfc.schemas import JobConfig  # noqa: E402

# The specification's acceptance thresholds. A release that misses one of these is a release
# that should not ship, not a number to be talked around.
THRESHOLDS = {
    "reference_accuracy": 0.995,
    "entity_recall": 0.98,
    "relation_precision": 0.98,
    "relation_recall": 0.95,
    "direction_accuracy": 1.0,
    "duplicate_visible_numerals": 0,
    "unsupported_visible_numerals": 0,
    "wrong_direction_on_validated": 0,
}


@dataclass
class Score:
    hits: int = 0
    expected: int = 0
    produced: int = 0

    @property
    def precision(self) -> float:
        return self.hits / self.produced if self.produced else 1.0

    @property
    def recall(self) -> float:
        return self.hits / self.expected if self.expected else 1.0

    @property
    def f1(self) -> float:
        if not (self.precision + self.recall):
            return 0.0
        return 2 * self.precision * self.recall / (self.precision + self.recall)


@dataclass
class CaseResult:
    case_id: str
    family: str
    seconds: float = 0.0
    calls: int = 0
    tokens: int = 0
    entity: Score = field(default_factory=Score)
    relation: Score = field(default_factory=Score)
    reference_correct: int = 0
    reference_total: int = 0
    direction_correct: int = 0
    direction_total: int = 0
    figures_expected: int = 0
    figures_produced: int = 0
    figures_validated: int = 0
    required_numerals_missing: list[str] = field(default_factory=list)
    prohibited_numerals_present: list[str] = field(default_factory=list)
    duplicate_numerals: list[str] = field(default_factory=list)
    unsupported_numerals: list[str] = field(default_factory=list)
    forbidden_words_drawn: list[str] = field(default_factory=list)
    shape_correct: int = 0
    shape_total: int = 0
    failures: list[str] = field(default_factory=list)


def _normalize(value: str) -> str:
    from pfc.numerals import normalize_name

    return normalize_name(value)


def score_case(case: dict[str, Any], outcome, report_dir: Path) -> CaseResult:
    expected = case.get("expected") or {}
    result = CaseResult(case_id=case["id"], family=case.get("family", "unknown"))

    # -- reference registry -------------------------------------------------
    produced_registry = outcome.graph.reference_registry
    expected_registry = expected.get("reference_registry") or {}
    result.entity.expected = len(expected_registry)
    result.entity.produced = len(produced_registry)
    for numeral, name in expected_registry.items():
        result.reference_total += 1
        produced_name = produced_registry.get(numeral)
        if produced_name is None:
            result.failures.append(f"numeral {numeral} ({name}) was not found")
            continue
        result.entity.hits += 1
        if _normalize(name) in _normalize(produced_name) or \
                _normalize(produced_name) in _normalize(name):
            result.reference_correct += 1
        else:
            result.failures.append(
                f"numeral {numeral} came out as {produced_name!r}, expected {name!r}")

    # -- relations ----------------------------------------------------------
    produced_relations = {
        (relation.subject, relation.predicate, relation.object): relation
        for relation in outcome.graph.relations}
    expected_relations = [row for row in (expected.get("relations") or [])]
    required = [row for row in expected_relations if not row.get("optional")]
    result.relation.expected = len(required)
    result.relation.produced = len(produced_relations)
    for row in expected_relations:
        key = (f"e{row['subject'].lower()}", row["predicate"], f"e{row['object'].lower()}")
        relation = produced_relations.get(key)
        if relation is None:
            if not row.get("optional"):
                result.failures.append(
                    f"relation {row['subject']} {row['predicate']} {row['object']} was not found")
            continue
        if not row.get("optional"):
            result.relation.hits += 1
        result.direction_total += 1
        directed = relation.direction == "subject_to_object"
        if directed == bool(row.get("directed")):
            result.direction_correct += 1
        else:
            result.failures.append(
                f"relation {row['subject']} {row['predicate']} {row['object']} came out "
                f"{'directed' if directed else 'undirected'}")

    # -- shapes -------------------------------------------------------------
    for numeral, shape in (expected.get("shapes") or {}).items():
        result.shape_total += 1
        entity = outcome.graph.by_numeral(numeral)
        if entity is not None and entity.shape_hint == shape and entity.shape_hint_grounded:
            result.shape_correct += 1
        else:
            result.failures.append(
                f"shape of {numeral} came out {getattr(entity, 'shape_hint', None)!r}, "
                f"expected {shape!r}")

    # -- figures ------------------------------------------------------------
    expected_figures = {str(row["figure_number"]): row for row in expected.get("figures") or []}
    result.figures_expected = len(expected_figures)
    result.figures_produced = len(outcome.report.figures)
    known = set(produced_registry)
    for figure in outcome.report.figures:
        if figure.status == "VALIDATED":
            result.figures_validated += 1
        wanted = expected_figures.get(figure.figure_number)
        scene = _scene_for(report_dir, figure.figure_number)
        drawn = {label["reference_numeral"] for label in (scene or {}).get("labels", [])}
        seen: dict[str, int] = {}
        for label in (scene or {}).get("labels", []):
            seen[label["reference_numeral"]] = seen.get(label["reference_numeral"], 0) + 1
        result.duplicate_numerals.extend(
            f"FIG.{figure.figure_number}:{numeral}" for numeral, count in seen.items()
            if count > 1)
        result.unsupported_numerals.extend(
            f"FIG.{figure.figure_number}:{numeral}" for numeral in sorted(drawn - known,
                                                                          key=sort_key))
        captions = " ".join(str(node.get("caption") or "")
                            for node in (scene or {}).get("nodes", [])).lower()
        for word in expected.get("must_not_contain") or []:
            if word.lower() in captions:
                result.forbidden_words_drawn.append(f"FIG.{figure.figure_number}:{word}")
        if wanted is None:
            continue
        if figure.figure_type != wanted.get("figure_type"):
            result.failures.append(
                f"FIG. {figure.figure_number} came out as {figure.figure_type}, expected "
                f"{wanted.get('figure_type')}")
        for numeral in wanted.get("required_numerals") or []:
            if numeral not in drawn:
                result.required_numerals_missing.append(f"FIG.{figure.figure_number}:{numeral}")
        for numeral in wanted.get("prohibited_numerals") or []:
            if numeral in drawn:
                result.prohibited_numerals_present.append(f"FIG.{figure.figure_number}:{numeral}")
    for number in expected_figures:
        if not any(row.figure_number == number for row in outcome.report.figures):
            result.failures.append(f"FIG. {number} was described but not produced")

    return result


def _scene_for(report_dir: Path, figure_number: str) -> dict | None:
    stem = "fig_" + "".join(ch for ch in figure_number.lower() if ch.isalnum())
    path = report_dir / "debug" / f"{stem}_scene.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_case(case: dict[str, Any], jurisdiction: str, verification: str) -> CaseResult:
    workdir = Path(tempfile.mkdtemp(prefix=f"pfc-eval-{case['id']}-"))
    started = time.monotonic()
    try:
        outcome = run_job(
            "eval" + case["id"].replace("-", "")[:28].ljust(28, "0"),
            root=workdir,
            config=JobConfig(jurisdiction=jurisdiction, verification_level=verification,
                             max_figures=12),
            upload=(case["text"].encode("utf-8"), f"{case['id']}.txt"))
        result = score_case(case, outcome, workdir)
        result.seconds = round(time.monotonic() - started, 1)
        result.calls = int(outcome.usage.get("calls") or 0)
        result.tokens = int(outcome.usage.get("prompt_tokens") or 0) + \
            int(outcome.usage.get("completion_tokens") or 0)
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 1.0

    entity_hits = sum(row.entity.hits for row in results)
    entity_expected = sum(row.entity.expected for row in results)
    entity_produced = sum(row.entity.produced for row in results)
    relation_hits = sum(row.relation.hits for row in results)
    relation_expected = sum(row.relation.expected for row in results)
    relation_produced = sum(row.relation.produced for row in results)
    figures_produced = sum(row.figures_produced for row in results)
    return {
        "cases": len(results),
        "entity_precision": ratio(entity_hits, entity_produced),
        "entity_recall": ratio(entity_hits, entity_expected),
        "entity_f1": round(2 * ratio(entity_hits, entity_produced) *
                           ratio(entity_hits, entity_expected) /
                           max(1e-9, ratio(entity_hits, entity_produced) +
                               ratio(entity_hits, entity_expected)), 4),
        "reference_accuracy": ratio(sum(row.reference_correct for row in results),
                                    sum(row.reference_total for row in results)),
        "relation_precision": ratio(relation_hits, relation_produced),
        "relation_recall": ratio(relation_hits, relation_expected),
        "direction_accuracy": ratio(sum(row.direction_correct for row in results),
                                    sum(row.direction_total for row in results)),
        "shape_accuracy": ratio(sum(row.shape_correct for row in results),
                                sum(row.shape_total for row in results)),
        "figure_validation_rate": ratio(sum(row.figures_validated for row in results),
                                        figures_produced),
        "required_numeral_recall": ratio(
            figures_produced - len([x for row in results
                                    for x in row.required_numerals_missing]),
            max(1, figures_produced)),
        "duplicate_visible_numerals": sum(len(row.duplicate_numerals) for row in results),
        "unsupported_visible_numerals": sum(len(row.unsupported_numerals) for row in results),
        "unsupported_entity_rate": ratio(
            sum(len(row.forbidden_words_drawn) for row in results), max(1, figures_produced)),
        "wrong_direction_on_validated": sum(
            row.direction_total - row.direction_correct for row in results),
        "seconds_per_patent": round(sum(row.seconds for row in results) /
                                    max(1, len(results)), 1),
        "calls_per_patent": round(sum(row.calls for row in results) / max(1, len(results)), 1),
        "tokens_per_patent": round(sum(row.tokens for row in results) / max(1, len(results))),
    }


def check_thresholds(summary: dict[str, Any]) -> list[str]:
    problems = []
    for metric, threshold in THRESHOLDS.items():
        value = summary.get(metric)
        if value is None:
            continue
        if metric.startswith(("duplicate_", "unsupported_visible", "wrong_")):
            if value > threshold:
                problems.append(f"{metric} = {value}, must be {threshold}")
        elif value < threshold:
            problems.append(f"{metric} = {value}, must be at least {threshold}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="test_patents/golden")
    parser.add_argument("--jurisdiction", default="uspto_utility")
    parser.add_argument("--verification", default="off",
                        choices=["off", "standard", "strict"],
                        help="vision verification costs a call per figure; off by default here")
    parser.add_argument("--case", action="append", default=[],
                        help="run only these case ids")
    parser.add_argument("--json", default="", help="write the full result to this path")
    args = parser.parse_args()

    dataset = (ROOT / args.dataset) if not Path(args.dataset).is_absolute() \
        else Path(args.dataset)
    files = sorted(dataset.glob("*.json"))
    if args.case:
        files = [path for path in files if path.stem in set(args.case)]
    if not files:
        print(f"no cases in {dataset}", file=sys.stderr)
        return 2

    results: list[CaseResult] = []
    for path in files:
        case = json.loads(path.read_text(encoding="utf-8"))
        print(f"· {case['id']} ({case.get('family', '?')})", flush=True)
        result = run_case(case, args.jurisdiction, args.verification)
        results.append(result)
        print(f"    entities {result.entity.hits}/{result.entity.expected}"
              f"  relations {result.relation.hits}/{result.relation.expected}"
              f"  figures {result.figures_validated}/{result.figures_produced} validated"
              f"  {result.seconds}s  {result.calls} calls")
        for failure in result.failures[:6]:
            print(f"      - {failure}")

    summary = aggregate(results)
    print("\n" + "=" * 62)
    width = max(len(key) for key in summary)
    for key, value in summary.items():
        print(f"{key.ljust(width)}  {value}")

    problems = check_thresholds(summary)
    print("=" * 62)
    if problems:
        print("BELOW THE ACCEPTANCE THRESHOLDS:")
        for problem in problems:
            print(f"  {problem}")
    else:
        print("every acceptance threshold met")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "summary": summary,
            "cases": [{**vars(row), "entity": vars(row.entity),
                       "relation": vars(row.relation)} for row in results]},
            indent=2, default=str), encoding="utf-8")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
