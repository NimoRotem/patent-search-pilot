"""Run the pipeline from a terminal, for testing and for the evaluation harness.

    ./.venv/bin/python fmrun.py US11000000B2 --out /tmp/run1
    ./.venv/bin/python fmrun.py --file draft.txt --out /tmp/run2 --no-raster
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from fm import llm, pipeline, redline
from fm.render import sheet as sheetmod


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="build a patent figure set from a draft")
    parser.add_argument("source", nargs="?", default="",
                        help="a patent number, a link, or - to read the draft from stdin")
    parser.add_argument("--file", default="", help="read the draft from a file")
    parser.add_argument("--out", default="/tmp/fm-run", help="where to write the artefacts")
    parser.add_argument("--paper", default="a4", choices=sorted(sheetmod.PAPERS))
    parser.add_argument("--no-raster", action="store_true",
                        help="skip the pixel checks, which need cairosvg")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    text = url = ""
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.source == "-":
        text = sys.stdin.read()
    elif args.source:
        url = args.source
    else:
        parser.error("give a patent number, a link, --file, or - for stdin")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = llm.CallLog(path=out / "calls.jsonl")
    started = time.monotonic()

    def progress(step: str, state: str, detail: str = "") -> None:
        if args.quiet:
            return
        mark = {"running": "..", "done": "ok", "failed": "!!"}.get(state, "  ")
        print(f"  [{time.monotonic() - started:6.1f}s] {mark} {step:9s} {detail}", flush=True)

    try:
        result = pipeline.run(text=text, url=url, paper=args.paper, progress=progress, log=log,
                              raster_checks=not args.no_raster)
    except Exception as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    for sheet in result.sheets:
        (out / f"sheet-{sheet.number}.svg").write_text(
            sheetmod.sheet_svg(sheet, result.figures), encoding="utf-8")
    for figure in result.figures:
        name = figure.label.replace(".", "").replace(" ", "")
        (out / f"{name}.svg").write_text(sheetmod.figure_svg(figure), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(result.report.model_dump(), indent=1),
                                     encoding="utf-8")
    (out / "registry.json").write_text(json.dumps(result.registry.model_dump(), indent=1),
                                       encoding="utf-8")
    (out / "plan.json").write_text(json.dumps(result.plan.model_dump(), indent=1),
                                   encoding="utf-8")
    (out / "redline.html").write_text(redline.build(result), encoding="utf-8")

    summary = result.summary()
    print(f"\n  {summary['figures']} figure(s) on {summary['sheets']} sheet(s), "
          f"{summary['numerals']} numerals, {summary['errors']} error(s), "
          f"{summary['warnings']} warning(s)")
    print(f"  attempts {summary['attempts']}  model {summary['model_calls']}")
    for figure in result.figures:
        print(f"    {figure.label:9s} {figure.kind:14s} {len(figure.prims):5d} prims  "
              f"{len(figure.labels):2d} numerals")
    for finding in result.report.findings:
        if finding.severity == "info":
            continue
        print(f"    [{finding.severity:7s}] {finding.code:26s} {finding.cite:20s} "
              f"{finding.message[:120]}")
    print(f"\n  written to {out}")
    return 0 if summary["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
