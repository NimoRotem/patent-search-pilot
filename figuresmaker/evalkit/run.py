"""Run the pipeline over a corpus and score it.

    ./.venv/bin/python -m evalkit.run --build            # fetch the corpus only
    ./.venv/bin/python -m evalkit.run --limit 5          # fetch, run, score
    ./.venv/bin/python -m evalkit.run --regressions      # the drawing-objection cases

Each case is run from the cached specification text, not from the network, so a score is
reproducible. The per-case artefacts are kept: a run that scored badly can be looked at.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from fm import llm, pipeline
from fm.render import sheet as sheetmod

from . import corpus, score as score_mod

DEFAULT_DIR = Path("~/figuresmaker-eval").expanduser()


def run_case(text: str, truth: corpus.Truth, out_dir: Path, *,
             raster: bool = True) -> score_mod.Score:
    started = time.monotonic()
    log = llm.CallLog(path=out_dir / "calls.jsonl")
    try:
        result = pipeline.run(text=text, paper="a4", log=log, raster_checks=raster)
    except Exception as exc:
        return score_mod.failed(truth.number, f"{type(exc).__name__}: {exc}",
                                time.monotonic() - started)
    elapsed = time.monotonic() - started

    out_dir.mkdir(parents=True, exist_ok=True)
    for sheet in result.sheets:
        (out_dir / f"sheet-{sheet.number}.svg").write_text(
            sheetmod.sheet_svg(sheet, result.figures), encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(result.report.model_dump(), indent=1), encoding="utf-8")
    (out_dir / "plan.json").write_text(
        json.dumps(result.plan.model_dump(), indent=1), encoding="utf-8")

    return score_mod.score_run(truth, result.figures, result.plan, result.claims,
                               result.report, seconds=elapsed, attempts=result.attempts,
                               calls=result.calls)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="score the figures maker against granted patents")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="where the corpus is cached")
    parser.add_argument("--numbers", nargs="*", default=None,
                        help="patent numbers, otherwise the built-in set")
    parser.add_argument("--build", action="store_true", help="fetch the corpus and stop")
    parser.add_argument("--limit", type=int, default=0, help="run at most this many cases")
    parser.add_argument("--no-raster", action="store_true")
    parser.add_argument("--tag", default="run", help="a name for this run's output directory")
    args = parser.parse_args(argv)

    root = Path(args.dir).expanduser()
    cache = root / "corpus"
    numbers = args.numbers if args.numbers else list(corpus.DEFAULT_NUMBERS)

    print(f"corpus: {len(numbers)} patent(s) -> {cache}")
    corpus.build(numbers, cache)
    cases = corpus.load(cache)
    print(f"  {len(cases)} case(s) cached")
    if args.build:
        return 0
    if args.limit:
        cases = cases[: args.limit]

    runs = root / args.tag
    runs.mkdir(parents=True, exist_ok=True)
    scores: list[score_mod.Score] = []
    for index, (truth, text) in enumerate(cases, start=1):
        print(f"\n[{index}/{len(cases)}] {truth.number}  "
              f"{len(truth.figures)} figure(s), {len(truth.numerals)} numeral(s)", flush=True)
        result = run_case(text, truth, runs / corpus._slug(truth.number),
                          raster=not args.no_raster)
        scores.append(result)
        if result.ok:
            print(f"        numerals {result.numeral_agreement:.3f}  "
                  f"per-figure {result.per_figure_agreement:.3f}  "
                  f"claims {result.claim_coverage:.3f}  "
                  f"{result.errors} error(s)  {result.seconds:.0f}s", flush=True)
        else:
            print(f"        FAILED: {result.error}", flush=True)

    summary = score_mod.aggregate(scores)
    (runs / "scores.json").write_text(
        json.dumps({"summary": summary, "cases": [s.as_dict() for s in scores]}, indent=1),
        encoding="utf-8")

    print("\n" + score_mod.table(scores))
    print("\nsummary")
    for key, value in summary.items():
        if key in ("error_codes", "failures"):
            continue
        print(f"  {key:24s} {value}")
    if summary.get("error_codes"):
        print("  remaining error codes:")
        for code, count in summary["error_codes"].items():
            print(f"    {count:3d}  {code}")
    for failure in summary.get("failures", []):
        print(f"  FAILED {failure['number']}: {failure['error'][:150]}")
    print(f"\nwritten to {runs / 'scores.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
