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
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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
# How many scene calls are in flight at once. Each is an independent model call; the ceiling is
# the provider's patience and this host's memory, not anything about the drawings.
SCENE_WORKERS = int(os.environ.get("FM_SCENE_WORKERS", "8"))


Progress = Callable[[str, str, str], None]           # step, state, detail
FigureProgress = Callable[[str, str, str], None]     # figure label, state, detail


def _noop(step: str, state: str, detail: str = "") -> None:
    return None


def _noop_figure(label: str, state: str, detail: str = "") -> None:
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


def generate_scene(figure_plan: FigurePlan, sections: Sections, registry: Registry,
                   log: Optional[llm.CallLog] = None, *, feedback: str = "") -> Any:
    """One model call: the scene for one figure. The slow part, and the only slow part."""
    built = plan_mod.build_scene(figure_plan, sections, registry, llm.scene(log), feedback)
    return plan_mod.clamp_scene(figure_plan.kind, built)


def generate_scenes(plans: Sequence[FigurePlan], sections: Sections, registry: Registry,
                    log: Optional[llm.CallLog] = None, *, cache: Optional[dict] = None,
                    feedback: Optional[dict[str, str]] = None,
                    on_figure: FigureProgress = _noop_figure) -> dict[str, Any]:
    """Every figure's scene, at once.

    This is where the ten minutes went. Each scene is an independent model call over a passage
    that has already been selected for it, and they were being made one after another because the
    appearance store made figure N depend on figure N-1. It does not have to: consistency between
    views is imposed afterwards, deterministically, by letting the first figure in label order
    decide what a part looks like and constraining the rest to it. So the calls go out together
    and the wall clock becomes the slowest single call rather than the sum of all of them.
    """
    feedback = feedback or {}
    out: dict[str, Any] = {}
    todo: list[FigurePlan] = []
    for figure_plan in plans:
        cached = cache.get(scene_key(figure_plan)) if cache is not None else None
        if cached is not None and not feedback.get(figure_plan.label):
            out[figure_plan.label] = cached
            on_figure(figure_plan.label, "done", "reused")
        else:
            todo.append(figure_plan)
            on_figure(figure_plan.label, "pending", "")

    if not todo:
        return out

    workers = max(1, min(SCENE_WORKERS, len(todo)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fm-scene") as pool:
        futures = {
            pool.submit(generate_scene, figure_plan, sections, registry, log,
                        feedback=feedback.get(figure_plan.label, "")): figure_plan
            for figure_plan in todo}
        for figure_plan in todo:
            on_figure(figure_plan.label, "generating", figure_plan.kind.replace("_", " "))
        for future in as_completed(futures):
            figure_plan = futures[future]
            try:
                out[figure_plan.label] = future.result()
                on_figure(figure_plan.label, "drawing", "")
            except Exception as exc:
                out[figure_plan.label] = exc
                on_figure(figure_plan.label, "failed", f"{type(exc).__name__}: {exc}"[:160])
    return out


def assemble(plan: Plan, scenes: dict[str, Any],
             on_drawn: Callable[[Figure], None] = lambda _figure: None
             ) -> tuple[list[Figure], list[Finding], dict[str, str],
                        appearance_mod.Appearance]:
    """Draw every figure from its scene, in label order, and say which need another go.

    Order matters and cost does not: drawing is a tenth of a second and it is what makes a part
    look like itself in every view, because the first figure to use a numeral decides its shape
    and the rest are constrained to that. Rebuilding the whole set from scratch after a retry is
    therefore cheap and keeps the result identical whichever figures were regenerated.
    """
    appearance = appearance_mod.Appearance()
    figures: list[Figure] = []
    findings: list[Finding] = []
    retry: dict[str, str] = {}

    for figure_plan in plan.figures:
        scene = scenes.get(figure_plan.label)
        if scene is None or isinstance(scene, Exception):
            reason = str(scene) if scene is not None else "no scene was produced"
            findings.append(Finding(
                code="figure_not_drawn", severity="error", stage="renderer",
                figure=figure_plan.label, basis="practice",
                message=f"{figure_plan.label} has no drawable scene: {reason}"))
            retry[figure_plan.label] = (
                f"The previous attempt could not be used: {reason}. Return a scene that fixes "
                "exactly this.")
            continue
        try:
            _constrain(figure_plan.kind, scene, appearance)
            drawn, figure_findings = render(figure_plan, scene, appearance)
        except RenderError as exc:
            findings.append(Finding(
                code="figure_not_drawn", severity="error", stage=exc.stage,
                figure=figure_plan.label, message=str(exc), basis="practice"))
            retry[figure_plan.label] = (
                f"The previous scene could not be drawn: {exc}. Return a scene that fixes "
                "exactly this.")
            continue
        except Exception as exc:
            findings.append(Finding(
                code="figure_not_drawn", severity="error", stage="renderer",
                figure=figure_plan.label, basis="practice",
                message=f"{figure_plan.label}: {type(exc).__name__}: {exc}"))
            retry[figure_plan.label] = (
                f"The previous scene was rejected: {type(exc).__name__}: {exc}. Return a "
                "corrected scene.")
            continue

        _learn(figure_plan.kind, scene, appearance)
        figures.append(drawn)
        findings.extend(figure_findings)
        on_drawn(drawn)

        # A scene can be drawable and still wrong: a part the size of a speck, or an assembled
        # view whose parts do not meet. Those are worth one more call with the defect quoted,
        # because they are exactly the kind of mistake a model corrects when told what it was.
        fixable = [f for f in figure_findings
                   if f.severity == "error" and f.stage == "renderer"]
        if fixable:
            retry[figure_plan.label] = (
                "The previous scene was drawn but rejected. Fix exactly these problems and "
                "return a corrected scene:\n"
                + "\n".join(f"- {f.message}" for f in fixable[:8]))
    return figures, findings, retry, appearance


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
        use_model: bool = True, raster_checks: bool = True,
        on_figure: FigureProgress = _noop_figure,
        on_plan: Callable[[Plan], None] = lambda _plan: None,
        on_drawn: Callable[[Figure], None] = lambda _figure: None) -> Result:
    progress("ingest", "running")
    draft = read_draft(text=text, url=url, upload=upload)
    progress("ingest", "done", f"{len(draft.text):,} characters from {draft.source}")

    progress("sections", "running")
    sections = sections_mod.analyse(draft.text, title=draft.title, source=draft.source,
                                    source_ref=draft.source_ref)
    progress("sections", "done",
             f"{len(sections.claims)} claims, {len(sections.brief_items)} figures promised")

    # The registry and the claim split are both fast-model calls over the same sections and
    # neither needs the other, so they go out together.
    progress("registry", "running")
    progress("claims", "running")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fm-read") as pool:
        registry_future = pool.submit(build_registry, sections, log, use_model)
        claims_future = pool.submit(claims_mod.analyse, sections.claims,
                                    llm.fast(log) if use_model else None, use_model=use_model)
        registry = registry_future.result()
        raw_claims = claims_future.result()
    if not registry.entries:
        raise ValueError("no reference numerals were found in this draft. A figure set is built "
                         "from the numerals the description gives its parts; add them, or paste "
                         "a draft that has them.")
    errors = [c for c in registry.conflicts if c.severity == "error"]
    progress("registry", "done",
             f"{len(registry.entries)} numerals, {len(errors)} conflict(s)")

    claim_list = claims_mod.match_to_registry(raw_claims, registry)
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

        on_plan(plan)
        progress("figures", "running",
                 f"{len(plan.figures)} scene(s), up to {SCENE_WORKERS} at a time")
        scenes: dict[str, Any] = {}
        figures: list[Figure] = []
        stage_findings: list[Finding] = []
        appearance = appearance_mod.Appearance()
        retry: dict[str, str] = {}

        for scene_attempt in range(MAX_SCENE_ATTEMPTS):
            targets = plan.figures if scene_attempt == 0 else \
                [f for f in plan.figures if f.label in retry]
            if not targets:
                break
            attempts["scenes"] += len(targets)
            if scene_attempt == 0:
                attempts["scenes_reused"] += sum(
                    1 for f in targets if scene_key(f) in scene_cache)
            else:
                progress("figures", "running",
                         f"revising {len(targets)} figure(s) the renderer rejected")
            scenes.update(generate_scenes(
                targets, sections, registry, log, cache=scene_cache,
                feedback=retry if scene_attempt else None, on_figure=on_figure))
            for figure_plan in targets:
                value = scenes.get(figure_plan.label)
                if value is not None and not isinstance(value, Exception):
                    scene_cache[scene_key(figure_plan)] = value

            figures, stage_findings, retry, appearance = assemble(plan, scenes, on_drawn)
            for figure in figures:
                on_figure(figure.label, "done", f"{len(figure.prims)} lines")
            last_round = scene_attempt + 1 >= MAX_SCENE_ATTEMPTS
            for label in retry:
                on_figure(label, "failed" if last_round else "generating", "")
            if not retry:
                break

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

    # Scene calls finish on their own threads, so the job file is written from several of them.
    # A lock around the state, and a floor on how often it is written, keeps a page that polls
    # twice a second from costing more than the work it is watching.
    lock = threading.Lock()
    last_written = [0.0]

    def flush(force: bool = False) -> None:
        now = time.time()
        if force or now - last_written[0] > 0.35:
            last_written[0] = now
            store.save(job)

    def progress(step: str, state: str, detail: str = "") -> None:
        with lock:
            entry = job.step(step)
            if state == "running" and not entry.started:
                entry.started = time.time()
            entry.state = state
            if detail:
                entry.detail = detail
            if state in ("done", "failed"):
                entry.finished = time.time()
            flush(force=True)

    def on_plan(plan: Plan) -> None:
        with lock:
            job.figures = [store.FigureState(label=f.label, kind=f.kind, title=f.title)
                           for f in plan.figures]
            flush(force=True)

    def on_figure(label: str, state: str, detail: str = "") -> None:
        with lock:
            entry = job.figure(label)
            if state in ("generating", "drawing") and not entry.started:
                entry.started = time.time()
            entry.state = state
            if detail:
                entry.detail = detail
            if state in ("done", "failed"):
                entry.finished = time.time()
            flush(force=state in ("done", "failed"))

    def on_drawn(figure: Figure) -> None:
        # Written the moment it exists, so the page can show the set filling in rather than
        # nothing at all until the last figure lands.
        store.write_text(job.path / f"figure-{_slug(figure.label)}.svg",
                         sheetmod.figure_svg(figure))
        with lock:
            job.figure(figure.label).ready = True
            flush(force=True)

    try:
        result = run(text=text, url=url, upload=upload, paper=paper, progress=progress,
                     log=log, raster_checks=raster_checks, on_figure=on_figure,
                     on_plan=on_plan, on_drawn=on_drawn)
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
