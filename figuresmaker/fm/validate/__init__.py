"""The compliance checker.

No model is called here, at all. Every finding is arithmetic, and every finding names the stage
that has to fix it, which is what lets the pipeline retry the planner for a missing element and
the placement solver for a crossed lead line instead of regenerating everything and hoping.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional, Sequence

from ..drawing import Figure
from ..render import sheet as sheetmod
from ..schemas import Claim, Finding, Plan, Registry, Sections, ValidationReport
from . import data, raster, rules

SHEET_WORKERS = int(os.environ.get("FM_SHEET_WORKERS", "4"))

CHECKS = (
    "numerals in the drawings against the registry",
    "registry numerals against the drawings",
    "one character per part, one part per character",
    "consecutive figure numbering",
    "every independent claim element depicted",
    "hatching on sectional views",
    "brief description against the drawing set",
    "margins and the sight of each sheet",
    "character height",
    "line weight and uniformity",
    "permitted text only",
    "lead lines: touching, not crossing",
    "views not crowded, each within its sheet",
    "black on white, no colour, no grey",
)


def validate(figures: Sequence[Figure], plan: Plan, registry: Registry, claims: list[Claim],
             sections: Sections, *, sheets: Optional[Sequence[sheetmod.Sheet]] = None,
             extra: Iterable[Finding] = (), raster_checks: bool = True,
             paper: str = "a4") -> ValidationReport:
    findings: list[Finding] = list(extra)
    findings += data.check(figures, plan, registry, claims, sections)

    if sheets is None:
        sheets = sheetmod.pack(list(figures), paper)
    allowed = {entry.numeral for entry in registry.entries}
    paper_mm = sheetmod.PAPERS.get(paper, sheetmod.PAPERS["a4"])

    def one_sheet(sheet) -> list[Finding]:
        geometry = sheetmod.sheet_geometry(sheet, figures)
        out = raster.check_geometry(sheet.number, geometry, allowed)
        if raster_checks:
            svg = sheetmod.sheet_svg(sheet, figures)
            out += raster.check_raster(sheet.number, svg, paper_mm, geometry["sight"])
        return out

    # Sheets are independent, and rasterising one is mostly time spent inside cairo and Pillow
    # with the interpreter lock released. A thirteen-sheet set is worth spreading out.
    if len(sheets) > 1 and raster_checks:
        with ThreadPoolExecutor(max_workers=min(SHEET_WORKERS, len(sheets))) as pool:
            for batch in pool.map(one_sheet, sheets):
                findings += batch
    else:
        for sheet in sheets:
            findings += one_sheet(sheet)

    findings = _dedupe(findings)
    report = ValidationReport(findings=findings, checked=list(CHECKS))
    report.passed = not report.errors()
    return report


def _dedupe(findings: Iterable[Finding]) -> list[Finding]:
    seen: set[tuple] = set()
    out: list[Finding] = []
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    for finding in findings:
        key = (finding.code, finding.figure, finding.numeral, finding.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    out.sort(key=lambda f: (severity_rank.get(f.severity, 3), f.code, f.figure, f.numeral))
    return out


def by_stage(report: ValidationReport, stage: str) -> list[Finding]:
    return [f for f in report.findings if f.stage == stage and f.severity == "error"]


def feedback_for(report: ValidationReport, stage: str, figure: str = "") -> str:
    """The errors one stage caused, written so that stage can act on them."""
    items = [f for f in report.findings
             if f.stage == stage and f.severity == "error"
             and (not figure or f.figure == figure or not f.figure)]
    if not items:
        return ""
    lines = []
    for finding in items[:25]:
        cite = f" [{finding.cite}]" if finding.cite else ""
        lines.append(f"- {finding.message}{cite}")
    return "\n".join(lines)


def summarise(report: ValidationReport) -> dict[str, Any]:
    return {
        "passed": report.passed,
        "errors": len(report.errors()),
        "warnings": len(report.warnings()),
        "info": len([f for f in report.findings if f.severity == "info"]),
        "checks": len(CHECKS),
    }


__all__ = ["validate", "by_stage", "feedback_for", "summarise", "CHECKS", "data", "raster",
           "rules"]
