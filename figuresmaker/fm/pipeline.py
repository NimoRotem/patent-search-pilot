"""Draft in, checked figure set out.

The shape of it: parse, register, plan, render, place, check. What makes it more than a chain is
what happens when the check fails. Every finding names the stage that caused it, so a missing
element goes back to the planner with the missing element quoted, a scene the renderer could not
build goes back to the scene call for that one figure, and a crossed lead line goes back to the
placement solver with more room to work in. Nothing is regenerated wholesale in the hope that the
next roll comes up different.

The retry budget is small and fixed. Two planner attempts and two scene attempts per figure. A
draft that still fails after that has something wrong with it that another model call will not
fix, and the report says what.
"""
from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import appearance as appearance_mod
from . import claims as claims_mod
from . import ingest as ingest_mod
from . import llm, plan as plan_mod, redline, registry as registry_mod, sections as sections_mod
from . import store, validate
from .drawing import Figure
from .render import RenderError, render, sheet as sheetmod
from .schemas import (Claim, Finding, FigurePlan, Plan, Registry, Sections, ValidationReport)

MAX_PLAN_ATTEMPTS = int(os.environ.get("FM_PLAN_ATTEMPTS", "2"))
MAX_SCENE_ATTEMPTS = int(os.environ.get("FM_SCENE_ATTEMPTS", "2"))
MAX_PLACEMENT_ATTEMPTS = int(os.environ.get("FM_PLACEMENT_ATTEMPTS", "2"))


Progress = Callable[[str, str, str], None]      # step, state, detail


def _noop(step: str, state: str, detail: str = "") -> None:
    return None


@dataclass
class Result:
    sections: Sections
    registry: Registry
    claims: list[Claim]
    plan: Plan
    figures: list[Figure]
    sheets: list[sheetmod.Sheet]
    report: ValidationReport
    appearance: appearance_mod.Appearance
    attempts: dict[str, int] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        out = validate.summarise(self.report)
        out.update({
            "figures": len(self.figures),
            "sheets": len(self.sheets),
            "numerals": len(self.registry.entries),
            "registry_conflicts": len([c for c in self.registry.conflicts
                                       if c.severity == "error"]),
            "claims": len([c for c in self.claims if c.independent]),
            "attempts": self.attempts,
            "model_calls": self.calls,
        })
        return out


# ---------------------------------------------------------------------------------- stages


def read_draft(*, text: str = "", url: str = "",
               upload: Optional[tuple[str, bytes]] = None) -> ingest_mod.Ingested:
    return ingest_mod.ingest(text=text, url=url, upload=upload)


def build_registry(sections: Sections, log: Optional[llm.CallLog] = None,
                   use_model: bool = True) -> Registry:
    return registry_mod.build(sections, llm.fast(log) if use_model else None,
                              use_model=use_model)


def build_claims(sections: Sections, registry: Registry, log: Optional[llm.CallLog] = None,
                 use_model: bool = True) -> list[Claim]:
    claims = claims_mod.analyse(sections.claims, llm.fast(log) if use_model else None,
                                use_model=use_model)
    return claims_mod.match_to_registry(claims, registry)


def scene_key(figure_plan: FigurePlan) -> tuple:
    """What a scene depends on. A replan that leaves a figure alone reuses its scene."""
    return (figure_plan.label, figure_plan.kind, figure_plan.view,
            tuple(sorted(e.numeral for e in figure_plan.elements)),
            tuple(sorted((r.kind, r.source, r.target) for r in figure_plan.relations)))


def render_figure(figure_plan: FigurePlan, sections: Sections, registry: Registry,
                  appearance: appearance_mod.Appearance, log: Optional[llm.CallLog] = None,
                  *, scene: Any = None, cache: Optional[dict] = None,
                  progress: Progress = _noop) -> tuple[Figure, list[Finding], int]:
    """One figure, with its own bounded retry on the scene call."""
    feedback = ""
    last_error = ""
    reasoner = llm.deep(log)
    key = scene_key(figure_plan)
    if scene is None and cache is not None:
        scene = cache.get(key)
    for attempt in range(MAX_SCENE_ATTEMPTS):
        try:
            if scene is None or attempt > 0:
                built = plan_mod.build_scene(figure_plan, sections, registry, reasoner,
                                             feedback + appearance.mech_hint(
                                                 [e.numeral for e in figure_plan.elements]))
            else:
                built = scene
            built = plan_mod.clamp_scene(figure_plan.kind, built)
            _constrain(figure_plan.kind, built, appearance)
            drawn, findings = render(figure_plan, built, appearance)
        except RenderError as exc:
            last_error = str(exc)
            feedback = (f"The previous scene could not be drawn: {exc}. Return a scene that "
                        "fixes exactly this.")
            progress("figures", "running", f"{figure_plan.label}: retrying, {exc}")
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            feedback = (f"The previous scene was rejected: {last_error}. Return a corrected "
                        "scene.")
            progress("figures", "running", f"{figure_plan.label}: retrying, {last_error}")
            continue
        # A scene can be drawable and still wrong: a part the size of a speck, or an assembled
        # view whose parts do not meet. Those are the renderer's own findings, and they are worth
        # one more attempt with the defect quoted, because they are exactly the kind of mistake a
        # model corrects when told what it was.
        fixable = [f for f in findings if f.severity == "error" and f.stage == "renderer"]
        if fixable and attempt + 1 < MAX_SCENE_ATTEMPTS:
            feedback = ("The previous scene was drawn but rejected. Fix exactly these problems "
                        "and return a corrected scene:\n"
                        + "\n".join(f"- {f.message}" for f in fixable[:8]))
            last_error = fixable[0].message
            progress("figures", "running", f"{figure_plan.label}: {last_error[:90]}")
            scene = None
            continue
        _learn(figure_plan.kind, built, appearance)
        if cache is not None:
            cache[key] = built
        return drawn, findings, attempt + 1
    raise RenderError(f"{figure_plan.label}: no drawable scene after {MAX_SCENE_ATTEMPTS} "
                      f"attempts ({last_error})", stage="renderer", figure=figure_plan.label)


def _constrain(kind: str, scene: Any, appearance: appearance_mod.Appearance) -> None:
    if kind in ("perspective", "exploded", "cross_section"):
        appearance.constrain_mech(scene)
    elif kind in ("block_diagram", "flowchart"):
        appearance.constrain_graph(scene)


def _learn(kind: str, scene: Any, appearance: appearance_mod.Appearance) -> None:
    if kind in ("perspective", "exploded", "cross_section"):
        appearance.learn_mech(scene)
    elif kind in ("block_diagram", "flowchart"):
        appearance.learn_graph(scene)


# ------------------------------------------------------------------------------------- run


def run(*, text: str = "", url: str = "", upload: Optional[tuple[str, bytes]] = None,
        paper: str = "a4", progress: Progress = _noop, log: Optional[llm.CallLog] = None,
        use_model: bool = True, raster_checks: bool = True) -> Result:
    progress("ingest", "running")
    draft = read_draft(text=text, url=url, upload=upload)
    progress("ingest", "done", f"{len(draft.text):,} characters from {draft.source}")

    progress("sections", "running")
    sections = sections_mod.analyse(draft.text, title=draft.title, source=draft.source,
                                    source_ref=draft.source_ref)
    progress("sections", "done",
             f"{len(sections.claims)} claims, {len(sections.brief_items)} figures promised")

    progress("registry", "running")
    registry = build_registry(sections, log, use_model)
    if not registry.entries:
        raise ValueError("no reference numerals were found in this draft. A figure set is built "
                         "from the numerals the description gives its parts; add them, or paste "
                         "a draft that has them.")
    errors = [c for c in registry.conflicts if c.severity == "error"]
    progress("registry", "done",
             f"{len(registry.entries)} numerals, {len(errors)} conflict(s)")

    progress("claims", "running")
    claim_list = build_claims(sections, registry, log, use_model)
    independent = [c for c in claim_list if c.independent]
    progress("claims", "done",
             f"{len(independent)} independent claim(s), "
             f"{sum(len(c.elements) for c in independent)} elements")

    attempts = {"plan": 0, "scenes": 0, "placement": 0, "scenes_reused": 0}
    # A second planning pass usually changes one or two figures and leaves the rest alone.
    # Without this the whole set is re-generated, which is minutes of model time to redraw
    # figures that were already right.
    scene_cache: dict[tuple, Any] = {}
    feedback = ""
    best: Optional[tuple[Plan, list[Figure], list[sheetmod.Sheet], ValidationReport,
                         appearance_mod.Appearance]] = None

    for plan_attempt in range(MAX_PLAN_ATTEMPTS):
        attempts["plan"] = plan_attempt + 1
        progress("plan", "running", "" if not feedback else "revising after the compliance check")
        plan = plan_mod.build_plan(sections, registry, claim_list, llm.deep(log), feedback)
        if not plan.figures:
            raise ValueError("the planner produced no figures for this draft.")
        progress("plan", "done", f"{len(plan.figures)} figure(s)")

        progress("figures", "running", f"0/{len(plan.figures)}")
        appearance = appearance_mod.Appearance()
        figures: list[Figure] = []
        stage_findings: list[Finding] = []
        for index, figure_plan in enumerate(plan.figures, start=1):
            reused = scene_key(figure_plan) in scene_cache
            try:
                drawn, findings, used = render_figure(figure_plan, sections, registry,
                                                      appearance, log, cache=scene_cache,
                                                      progress=progress)
            except RenderError as exc:
                stage_findings.append(Finding(
                    code="figure_not_drawn", severity="error", stage=exc.stage,
                    figure=figure_plan.label,
                    message=str(exc), cite="", basis="practice"))
                progress("figures", "running", f"{index}/{len(plan.figures)}: {exc}")
                continue
            attempts["scenes"] += used
            attempts["scenes_reused"] += 1 if reused else 0
            figures.append(drawn)
            stage_findings.extend(findings)
            progress("figures", "running",
                     f"{index}/{len(plan.figures)} {figure_plan.label} ({figure_plan.kind})"
                     + (" [reused]" if reused else ""))
        if not figures:
            raise ValueError("no figure could be drawn from this plan. " +
                             "; ".join(f.message for f in stage_findings[:3]))
        progress("figures", "done", f"{len(figures)} drawn")

        progress("layout", "running")
        sheets = sheetmod.pack(figures, paper)
        progress("layout", "done", f"{len(sheets)} sheet(s)")

        progress("validate", "running")
        report = validate.validate(figures, plan, registry, claim_list, sections,
                                   sheets=sheets, extra=stage_findings,
                                   raster_checks=raster_checks, paper=paper)

        # Placement is cheap to redo and is the commonest remaining failure, so it gets its own
        # retry before the planner is asked to think again.
        placement_errors = [f for f in report.errors() if f.stage == "placement"]
        for _ in range(MAX_PLACEMENT_ATTEMPTS if placement_errors else 0):
            attempts["placement"] += 1
            progress("validate", "running", "re-placing numerals to clear the lead lines")
            for figure in figures:
                if any(f.figure == figure.label for f in placement_errors):
                    figure.labels = [lab for lab in figure.labels if lab.placed_by == "user"]
                    from .render import leaders as leaders_mod
                    leaders_mod.solve(figure, effort=2)
            sheets = sheetmod.pack(figures, paper)
            report = validate.validate(figures, plan, registry, claim_list, sections,
                                       sheets=sheets, extra=stage_findings,
                                       raster_checks=raster_checks, paper=paper)
            placement_errors = [f for f in report.errors() if f.stage == "placement"]
            if not placement_errors:
                break

        result = (plan, figures, sheets, report, appearance)
        if best is None or _score(report) < _score(best[3]):
            best = result
        if report.passed:
            break
        feedback = validate.feedback_for(report, "planner")
        if not feedback:
            break                      # nothing left that the planner can fix
        progress("validate", "running",
                 f"{len(report.errors())} error(s); asking the planner to revise")

    assert best is not None
    plan, figures, sheets, report, appearance = best
    progress("validate", "done",
             f"{len(report.errors())} error(s), {len(report.warnings())} warning(s)")

    return Result(sections=sections, registry=registry, claims=claim_list, plan=plan,
                  figures=figures, sheets=sheets, report=report, appearance=appearance,
                  attempts=attempts, calls=(log.totals if log else {}))


def _score(report: ValidationReport) -> tuple[int, int]:
    return (len(report.errors()), len(report.warnings()))


# ------------------------------------------------------------------------------- persistence


def persist(job: store.Job, result: Result) -> None:
    """Every artefact, written where the browser and the exporter can find it."""
    path = job.path
    store.write_json(path / "sections.json", result.sections.model_dump())
    store.write_json(path / "registry.json", result.registry.model_dump())
    store.write_json(path / "claims.json", [c.model_dump() for c in result.claims])
    store.write_json(path / "plan.json", result.plan.model_dump())
    store.write_json(path / "report.json", result.report.model_dump())
    store.write_json(path / "figures.json", [f.to_dict() for f in result.figures])
    store.write_json(path / "sheets.json", [s.to_dict() for s in result.sheets])
    store.write_json(path / "appearance.json", result.appearance.to_dict())
    for sheet in result.sheets:
        store.write_text(path / f"sheet-{sheet.number}.svg",
                         sheetmod.sheet_svg(sheet, result.figures))
    for figure in result.figures:
        store.write_text(path / f"figure-{_slug(figure.label)}.svg",
                         sheetmod.figure_svg(figure))
    store.write_text(path / "redline.html", redline.build(result))


def load_figures(job: store.Job) -> list[Figure]:
    raw = store.read_json(job.path / "figures.json") or []
    if isinstance(raw, dict):
        raw = raw.get("figures") or []
    return [Figure.from_dict(item) for item in raw]


def save_figures(job: store.Job, figures: Sequence[Figure]) -> None:
    store.write_json(job.path / "figures.json", [f.to_dict() for f in figures])


def _slug(label: str) -> str:
    return label.replace(".", "").replace(" ", "").lower()


def revalidate(job: store.Job, figures: Sequence[Figure], *, paper: str = "a4",
               raster_checks: bool = True) -> ValidationReport:
    """Re-run every check after an edit, from what is on disk."""
    plan = Plan.model_validate(store.read_json(job.path / "plan.json") or {})
    registry = Registry.model_validate(store.read_json(job.path / "registry.json") or {})
    sections = Sections.model_validate(store.read_json(job.path / "sections.json") or {})
    raw_claims = store.read_json(job.path / "claims.json") or []
    claim_list = [Claim.model_validate(c) for c in raw_claims]
    sheets = sheetmod.pack(list(figures), paper)
    report = validate.validate(figures, plan, registry, claim_list, sections, sheets=sheets,
                               raster_checks=raster_checks, paper=paper)
    store.write_json(job.path / "report.json", report.model_dump())
    store.write_json(job.path / "sheets.json", [s.to_dict() for s in sheets])
    for sheet in sheets:
        store.write_text(job.path / f"sheet-{sheet.number}.svg",
                         sheetmod.sheet_svg(sheet, figures))
    for figure in figures:
        store.write_text(job.path / f"figure-{_slug(figure.label)}.svg",
                         sheetmod.figure_svg(figure))
    return report


# ------------------------------------------------------------------------------ job runner


def execute(job: store.Job, *, text: str = "", url: str = "",
            upload: Optional[tuple[str, bytes]] = None, paper: str = "a4",
            raster_checks: bool = True) -> None:
    """Run a job to completion, recording every step. Never raises; failures land on the job."""
    log = llm.CallLog(path=job.path / "calls.jsonl")
    job.status = "running"
    job.pid = os.getpid()
    store.save(job)

    def progress(step: str, state: str, detail: str = "") -> None:
        entry = job.step(step)
        if state == "running" and not entry.started:
            entry.started = time.time()
        entry.state = state
        if detail:
            entry.detail = detail
        if state in ("done", "failed"):
            entry.finished = time.time()
        store.save(job)

    try:
        result = run(text=text, url=url, upload=upload, paper=paper, progress=progress,
                     log=log, raster_checks=raster_checks)
        progress("export", "running")
        persist(job, result)
        progress("export", "done", f"{len(result.sheets)} sheet(s)")
        job.title = result.sections.title or job.title
        job.summary = result.summary()
        job.status = "done"
    except Exception as exc:
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        store.write_text(job.path / "traceback.txt", traceback.format_exc())
        for step in job.steps:
            if step.state == "running":
                step.state = "failed"
                step.finished = time.time()
    finally:
        store.save(job)
