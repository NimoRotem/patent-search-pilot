"""The compiler's state machine.

One pass, one direction, typed data at every boundary. There is no autonomous agent deciding
what to do next: the order is fixed, each stage's output is validated before the next stage is
allowed to see it, and a stage that cannot produce a grounded result stops that figure rather
than passing something plausible along.

    INGEST -> PARSE -> EXTRACT -> RECONCILE -> PLAN -> SPEC
           -> LAYOUT -> RENDER -> VALIDATE -> VISION -> CORRECT -> FINAL -> EXPORT

Figures are independent after the graph is built, so one blocked figure never costs the others,
and the expensive semantic work is done once for the whole document rather than once per
figure.
"""
from __future__ import annotations

import json
import shutil
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import correct as correction
from . import appearance, ingest, neighbours as neighbours_mod, numerals
from . import plan as planning, referencemode, spec as speccing
from . import vision, visualclass
from .extract import extract_graph
from .ground import ParagraphIndex, make_grounder
from .layout import UnsupportedFigure, build_scene
from .numerals import sort_key
from .profiles import load_profile
from .providers import CallLog, ModelUnavailable, config_hash, text_reasoner, vision_verifier
from .prompts import versions as prompt_versions
from .render import RENDERER_VERSION, export_all, render_svg, svg_to_png
from .schemas import (FigureChecks, FigureResult, JobConfig, Manifest, ManifestFigure,
                      OriginalFigure, PatentGraph, Provenance, SourceDocument,
                      ValidationReport)
from .validate import (VALIDATION_VERSION, FigureBundle, ValidationContext, blocking,
                       validate_figure, validate_job, warnings)

STAGES = (
    ("INGEST", "Reading the patent"),
    ("PARSE_DOCUMENT", "Finding the sections and paragraphs"),
    ("EXTRACT_SEMANTICS", "Extracting the components and their relationships"),
    ("RECONCILE_GRAPH", "Reconciling the reference numerals"),
    ("PLAN_FIGURES", "Working out which figures the patent describes"),
    ("BUILD_FIGURE_SPECS", "Deciding what each figure shows"),
    ("LAYOUT", "Laying the figures out"),
    ("RENDER", "Drawing the figures"),
    ("DETERMINISTIC_VALIDATE", "Checking the figures against the patent"),
    ("VISION_VALIDATE", "Having the figures read back independently"),
    ("CORRECT", "Repairing what can be repaired"),
    ("FINAL_VALIDATE", "Final check"),
    ("EXPORT", "Writing the artifacts"),
)
_STAGE_PCT = {key: int(5 + 92 * index / max(1, len(STAGES) - 1))
              for index, (key, _) in enumerate(STAGES)}
_STAGE_LABEL = dict(STAGES)

# Figures compiled at once. Each holds one rendered sheet and makes at most a few model
# calls, so the bound is about being a good neighbour on a host that also serves the
# prior-art search, not about memory.
FIGURE_WORKERS = 4

Progress = Optional[Callable[[str, str, int], None]]


@dataclass
class JobPaths:
    root: Path

    @property
    def figures(self) -> Path:
        return self.root / "figures"

    @property
    def originals(self) -> Path:
        return self.root / "originals"

    @property
    def debug(self) -> Path:
        return self.root / "debug"

    def ensure(self) -> None:
        for path in (self.root, self.figures, self.originals, self.debug):
            path.mkdir(parents=True, exist_ok=True)


@dataclass
class JobOutcome:
    job_id: str
    document: SourceDocument
    graph: PatentGraph
    report: ValidationReport
    manifest: Manifest
    notes: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str),
                    encoding="utf-8")


def run_job(job_id: str, *, root: Path, config: JobConfig,
            upload: Optional[tuple[bytes, str]] = None, link: str = "",
            progress: Progress = None, use_models: bool = True) -> JobOutcome:
    """Compile one patent's figures. Never raises for a figure-level failure."""
    paths = JobPaths(root)
    paths.ensure()
    call_log = CallLog(path=paths.root / "model_calls.jsonl")

    def stage(key: str) -> None:
        if progress:
            try:
                progress(key, _STAGE_LABEL.get(key, key), _STAGE_PCT.get(key, 50))
            except Exception:
                pass

    def on_ingest_stage(_key: str, message: str) -> None:
        if progress:
            try:
                progress("INGEST", message, _STAGE_PCT["INGEST"])
            except Exception:
                pass

    reasoner = None
    verifier = None      # reads a FINISHED sheet back, to check it
    locator = None       # reads GENERATED artwork, to find the parts and place numerals on them
    notes: list[str] = []
    if use_models:
        try:
            reasoner = text_reasoner(log=call_log)
        except ModelUnavailable as exc:
            notes.append(f"no reasoning model is available ({exc}); only the parts of the "
                         "document that can be read deterministically were used")
        if config.verification_level != "off":
            try:
                verifier = vision_verifier(log=call_log)
            except ModelUnavailable as exc:
                notes.append(f"no vision model is available ({exc}); the figures were not read "
                             "back independently")
        # Locating the parts in generated artwork is part of DRAWING the figure, not of checking
        # it, so it does not answer to `verification_level`. Sharing the switch meant asking for
        # no verification silently turned the reference-guided mode off and fell every figure
        # back to the schematic.
        if config.figure_style == "reference_guided":
            try:
                locator = verifier or vision_verifier(log=call_log)
            except ModelUnavailable as exc:
                notes.append(f"no vision model is available ({exc}), so the reference-guided "
                             "drawings could not be grounded and every figure was drawn from "
                             "its specification instead")

    # -- INGEST / PARSE -----------------------------------------------------
    stage("INGEST")
    if upload is not None:
        result = ingest.ingest_upload(upload[0], upload[1], on_stage=on_ingest_stage)
    else:
        result = ingest.ingest_link(link, on_stage=on_ingest_stage)
    ingest.save_original_figures(result, paths.originals)
    document = result.document
    notes.extend(document.notes)

    stage("PARSE_DOCUMENT")
    description = [p for p in document.paragraphs
                   if p.section_id in {"detailed_description", "summary", "brief_drawings"}]
    registry = numerals.build_registry(description)
    if not registry:
        notes.append("no reference numerals could be read out of the description; without them "
                     "there is nothing a patent figure can be labelled with")

    # -- EXTRACT / RECONCILE ------------------------------------------------
    stage("EXTRACT_SEMANTICS")
    graph = extract_graph(document, registry, description, reasoner,
                          grounder=make_grounder(reasoner))
    stage("RECONCILE_GRAPH")
    # The extractor names a class from the paragraphs where it can. Anything still on the
    # default gets one from the drafter's own words for it, which is what decides the symbol it
    # is drawn with. Deliberately last, so the text always beats the keyword table.
    visualclass.classify_all(graph.entities)
    # How each part is DRAWN is settled here, once, for the whole document. Every figure then
    # reads it off the entity, which is what makes the same part recognisably the same on every
    # sheet; the CON rules check the drawings that actually came out.
    appearance.decide(graph, reasoner, notes)
    if graph.blocking_conflicts:
        notes.append(f"{len(graph.blocking_conflicts)} reference-numeral conflict(s) in the "
                     "draft are reported rather than repaired")
    if graph.discarded:
        notes.append(f"{len(graph.discarded)} proposed relationship(s) were discarded for want "
                     "of support in the text")

    # -- PLAN / SPEC --------------------------------------------------------
    stage("PLAN_FIGURES")
    figure_plan = planning.build_plan(document, registry, reasoner,
                                      max_figures=config.max_figures)
    notes.extend(figure_plan.notes)

    stage("BUILD_FIGURE_SPECS")
    profile = load_profile(config.jurisdiction)
    # The neighbouring patents are found once for the job, not once per figure: the same handful
    # of examiner-cited documents is the right reference for every sheet, and fetching their
    # drawings is the expensive part.
    neighbourhood = neighbours_mod.Neighbourhood()
    if config.figure_style == "reference_guided":
        stage("PLAN_FIGURES")
        neighbourhood = neighbours_mod.find(document.publication_number)
        notes.extend(neighbourhood.notes)
    results: list[FigureResult] = []
    bundles: list[FigureBundle] = []
    specs = {}
    figure_notes: dict[str, list[str]] = {}

    for item in figure_plan.figures:
        figure_id = f"FIG_{item.figure_number.upper()}"
        try:
            spec, spec_notes = speccing.build_spec(document, graph, registry, item, reasoner)
        except Exception:
            traceback.print_exc()
            spec, spec_notes = None, ["the specification for this figure could not be built"]
        figure_notes[figure_id] = spec_notes
        if spec is None:
            results.append(FigureResult(
                figure_id=figure_id, figure_number=item.figure_number,
                figure_type=item.figure_type, title=item.description[:200], status="BLOCKED",
                reason=(spec_notes[0] if spec_notes else
                        "nothing in the description grounds this figure"),
                source_evidence=[e.paragraph_id for e in item.evidence]))
            continue
        specs[figure_id] = spec
        results.append(FigureResult(
            figure_id=figure_id, figure_number=spec.figure_number,
            figure_type=spec.figure_type, title=spec.title, status="SPECIFIED",
            source_evidence=[e.paragraph_id for e in spec.evidence]))
        _write_json(paths.debug / f"{_stem(spec.figure_number)}_spec.json", spec.model_dump())

    total_sheets = max(1, len(specs))

    # -- LAYOUT / RENDER / VALIDATE / VISION / CORRECT ----------------------
    # Figures are independent once the graph exists: each one has its own specification, its own
    # sheet and its own verification. Compiling them one at a time made a five-figure patent wait
    # out five sequential readings by a thinking vision model, which is most of the wall clock
    # for none of the work. Order is restored afterwards, so the sheet numbering and the report
    # are identical whichever finishes first.
    stage("LAYOUT")
    ordered = sorted(specs.items(), key=lambda item: sort_key(item[1].figure_number))
    done = [0]

    earlier_artwork: list = []
    # Set the first time the image model refuses for quota. Every later figure would find the
    # same, so they stop asking: on a real run all four spent three attempts each discovering it
    # and each wrote a raw 429 payload into the report.
    out_of_quota: list = []

    def compile_one(numbered) -> Optional[FigureBundle]:
        sheet_number, (figure_id, spec) = numbered
        record = next(row for row in results if row.figure_id == figure_id)
        try:
            bundle = _compile_figure(spec, graph, profile, figure_plan, config, record,
                                     verifier, call_log, paths,
                                     sheet_number=sheet_number, sheet_total=total_sheets,
                                     neighbourhood=(neighbourhood
                                                    if config.figure_style == "reference_guided"
                                                    else None),
                                     locator=locator, earlier=earlier_artwork,
                                     exhausted=out_of_quota)
        except UnsupportedFigure as exc:
            record.status = "BLOCKED"
            record.reason = str(exc)
            return None
        except Exception:
            traceback.print_exc()
            record.status = "BLOCKED"
            record.reason = "this figure could not be compiled"
            return None
        finally:
            done[0] += 1
            if progress:
                share = done[0] / max(1, len(ordered))
                stage_key = "VISION_VALIDATE" if verifier is not None \
                    else "DETERMINISTIC_VALIDATE"
                try:
                    progress(stage_key,
                             f"Checking figure {done[0]} of {len(ordered)}",
                             int(_STAGE_PCT["RENDER"] +
                                 (_STAGE_PCT["FINAL_VALIDATE"] - _STAGE_PCT["RENDER"]) * share))
                except Exception:
                    pass
        return bundle

    if ordered:
        if config.figure_style == "reference_guided":
            # Sequential on purpose. Each generated figure becomes a reference for the next, and
            # that chaining is the whole reason the housing in FIG. 1 is still the same housing
            # in FIG. 2. Running them concurrently would trade the consistency for the speed.
            bundles = [bundle for bundle in map(compile_one, enumerate(ordered, 1))
                       if bundle is not None]
        else:
            with ThreadPoolExecutor(max_workers=min(FIGURE_WORKERS, len(ordered))) as pool:
                bundles = [bundle for bundle in pool.map(compile_one, enumerate(ordered, 1))
                           if bundle is not None]

    # -- FINAL / EXPORT -----------------------------------------------------
    stage("FINAL_VALIDATE")
    job_context = ValidationContext(graph=graph, profile=profile, plan=figure_plan,
                                    figures=bundles, config=config.model_dump())
    job_issues = validate_job(job_context)
    for issue in blocking(job_issues):
        target = next((row for row in results if row.figure_id == issue.figure_id), None)
        if target is not None:
            target.issues.append(issue)
            target.status = "BLOCKED"
            target.reason = target.reason or issue.message

    stage("EXPORT")
    export_failures: dict[str, set] = {}
    by_id = {bundle.spec.figure_id: bundle for bundle in bundles}
    for record in results:
        bundle = by_id.get(record.figure_id)
        if bundle is None:
            continue
        written = export_all(bundle.svg, profile, paths.figures,
                             _stem(bundle.spec.figure_number))
        record.svg_path = written.get("svg", "")
        record.pdf_path = written.get("pdf", "")
        record.png_path = written.get("png", "")
        for kind in ("pdf", "png"):
            reason = written.get(f"{kind}_error")
            if reason:
                export_failures.setdefault(kind, set()).add(reason)

    if out_of_quota:
        notes.append(
            "the image model ran out of quota partway through this job, so the figures after "
            "that point were drawn from the specification rather than in the idiom of the "
            "neighbouring art. They are complete and checked; they are just plainer. Running the "
            "same patent again once the quota resets produces the reference-guided drawings")

    # Once per job, not once per figure: a missing converter fails all forty identically.
    for kind, reasons in sorted(export_failures.items()):
        notes.append(
            f"the {kind.upper()} of every figure could not be written ({'; '.join(sorted(reasons))[:200]}). "
            f"The SVG is complete and is the canonical drawing; the {kind.upper()} is a "
            f"conversion of it, so this is a converter missing on this box rather than anything "
            f"wrong with the figures")

    _pair_originals(document, results, paths, verifier is not None, notes)

    report = ValidationReport(
        job_id=job_id,
        figures=results,
        blocking_issues=[issue for record in results for issue in blocking(record.issues)]
        + blocking(job_issues),
        warnings=[issue for record in results for issue in warnings(record.issues)]
        + warnings(job_issues))
    report.overall_status = _overall(results)

    manifest = Manifest(
        job_id=job_id,
        document={"title": document.title, "publication_number": document.publication_number,
                  "sha256": document.sha256, "origin": document.origin,
                  "origin_label": document.origin_label,
                  "google_patents": document.google_patents,
                  "espacenet": document.espacenet},
        config=config,
        provenance=Provenance(
            renderer_version=RENDERER_VERSION, validation_version=VALIDATION_VERSION,
            validation_profile=profile.version_tag,
            source_document_sha256=document.sha256, model_config_hash=config_hash(),
            prompt_versions=prompt_versions(
                "patent_graph_v1", "evidence_check_v1", "figure_plan_v1", "figure_spec_v1",
                "flow_steps_v1", "visual_verify_v1")),
        figures=[ManifestFigure(
            figure=f"FIG. {record.figure_number}", figure_id=record.figure_id,
            figure_type=record.figure_type,
            svg=f"figures/{record.svg_path}" if record.svg_path else "",
            pdf=f"figures/{record.pdf_path}" if record.pdf_path else "",
            png=f"figures/{record.png_path}" if record.png_path else "",
            status=record.status,
            original_sheets=[document.original_figures[index].filename
                             for index in record.original_matches
                             if index < len(document.original_figures)])
            for record in results],
        overall_status=report.overall_status, generated_at=_now())

    _write_json(paths.root / "document.json", document.model_dump())
    _write_json(paths.root / "patent_graph.json", graph.model_dump())
    _write_json(paths.root / "appearance.json", appearance.summary(graph.entities))
    _write_json(paths.root / "validation_report.json", report.model_dump())
    _write_json(paths.root / "manifest.json", manifest.model_dump())
    _write_json(paths.root / "figure_index.json", [
        {"figure_id": record.figure_id, "figure": f"FIG. {record.figure_number}",
         "status": record.status, "figure_type": record.figure_type,
         "title": record.title,
         "svg": record.svg_path, "pdf": record.pdf_path, "png": record.png_path,
         "originals": record.original_matches,
         "notes": figure_notes.get(record.figure_id, [])}
        for record in results])
    _write_json(paths.root / "notes.json", notes)
    _make_zip(paths)

    return JobOutcome(job_id=job_id, document=document, graph=graph, report=report,
                      manifest=manifest, notes=notes, usage=call_log.totals)


def _compile_figure(spec, graph, profile, figure_plan, config: JobConfig,
                    record: FigureResult, verifier, call_log, paths: JobPaths, *,
                    sheet_number: int, sheet_total: int,
                    neighbourhood=None, locator=None,
                    earlier: Optional[list] = None,
                    exhausted: Optional[list] = None) -> FigureBundle:
    """One figure, from its specification to a checked sheet.

    Everything after the graph happens here, so the whole of a figure's fate — laid out,
    rendered, measured, repaired, read back, measured again — is one function that can be run
    for one figure without reference to any other.
    """
    bundle: Optional[FigureBundle] = None
    if (config.figure_style == "reference_guided" and neighbourhood is not None
            and not exhausted):
        drawn = referencemode.draw_figure(
            spec, graph, profile, neighbourhood, locator,
            earlier=list(earlier or ()), sheet_number=sheet_number, sheet_total=sheet_total)
        record.corrections_applied.extend(drawn.notes)
        if drawn.exhausted and exhausted is not None:
            exhausted.append(drawn.failed)
        if drawn.ok:
            bundle = FigureBundle(spec=spec, scene=drawn.scene, svg=drawn.svg,
                                  artwork=drawn.artwork)
            record.artwork_references = list(drawn.references)
            if earlier is not None:
                earlier.append(drawn.artwork)
            _write_json(paths.debug / f"{_stem(spec.figure_number)}_artwork.json",
                        {"references": drawn.references, "notes": drawn.notes})
            (paths.figures / f"{_stem(spec.figure_number)}_art.png").write_bytes(drawn.artwork)
        elif drawn.exhausted:
            # Not a fact about the drawing. "The reference-guided drawing was not usable
            # (ClientError: 429 RESOURCE_EXHAUSTED {...})" is what this used to say, on all four
            # figures, with the raw payload in it.
            record.corrections_applied.append(
                "the image model is out of quota on this box, so this figure was drawn from the "
                "specification instead. Nothing is wrong with it; run it again when the quota "
                "resets to get the reference-guided drawing")
        else:
            # A figure that could not be generated or grounded is drawn the deterministic way
            # rather than left out. Something correct and plain beats nothing.
            record.corrections_applied.append(
                f"the reference-guided drawing was not usable ({drawn.failed}), so this figure "
                "was drawn from the specification instead")
    elif config.figure_style == "reference_guided" and exhausted:
        record.corrections_applied.append(
            "the image model had already run out of quota earlier in this job, so this figure "
            "was drawn from the specification without asking again")

    if bundle is None:
        scene = build_scene(spec, graph, profile, sheet_number=sheet_number,
                            sheet_total=sheet_total)
        bundle = FigureBundle(spec=spec, scene=scene, svg=render_svg(scene, profile))
    scene = bundle.scene
    record.status = "RENDERED"

    context = ValidationContext(graph=graph, profile=profile, plan=figure_plan,
                                figure=bundle, config=config.model_dump())
    issues = validate_figure(context)

    attempt = 0
    seen_signatures = {_defect_signature(issues)}
    while blocking(issues) and attempt < correction.MAX_ATTEMPTS:
        outcome = correction.correct(spec, graph, bundle.scene, profile, issues, attempt)
        attempt += 1
        if not outcome.changed:
            break
        bundle.scene = outcome.scene
        bundle.svg = render_svg(bundle.scene, profile, bundle.artwork)
        record.corrections_applied.extend(outcome.applied)
        context.figure = bundle
        issues = validate_figure(context)
        signature = _defect_signature(issues)
        if signature in seen_signatures:
            record.corrections_applied.append(_unsatisfiable_note(issues, attempt))
            break
        seen_signatures.add(signature)

    observed = None
    if verifier is not None and not blocking(issues):
        observed, vision_issues = _verify(bundle, profile, verifier, call_log, config,
                                          paths, record)
        issues = issues + vision_issues
        extra = 0
        seen_vision = {_defect_signature(vision_issues)}
        while blocking(vision_issues) and attempt + extra < correction.MAX_ATTEMPTS:
            outcome = correction.correct(spec, graph, bundle.scene, profile, vision_issues,
                                         attempt + extra)
            extra += 1
            if not outcome.changed:
                break
            bundle.scene = outcome.scene
            bundle.svg = render_svg(bundle.scene, profile, bundle.artwork)
            record.corrections_applied.extend(outcome.applied)
            context.figure = bundle
            issues = validate_figure(context)
            if blocking(issues):
                break
            observed, vision_issues = _verify(bundle, profile, verifier, call_log, config,
                                              paths, record)
            issues = issues + vision_issues
            signature = _defect_signature(vision_issues)
            if signature in seen_vision:
                record.corrections_applied.append(
                    _unsatisfiable_note(vision_issues, attempt + extra))
                break
            seen_vision.add(signature)
        attempt += extra

    record.correction_attempts = attempt
    record.issues = issues
    record.checks = _checks(issues, verified=observed is not None)
    record.status = _status(issues)
    if record.status == "BLOCKED":
        record.reason = correction.summarise(issues)
    _write_json(paths.debug / f"{_stem(spec.figure_number)}_scene.json",
                bundle.scene.model_dump())
    return bundle


def _defect_signature(issues) -> tuple:
    """What is wrong with this figure, as a comparable value."""
    return tuple(sorted((issue.rule_id, issue.entity_id or "", issue.reference_numeral or "")
                        for issue in blocking(issues)))


def _unsatisfiable_note(issues, attempt: int) -> str:
    """Said out loud when repairing changed the drawing and changed nothing about the defect.

    This is the guard for a class of bug that cost three separate sessions here. GEO009 fired on
    a part nested inside another, then on a container's own outline, then on two parts a reader
    had given one box between them: in each case no placement existed that satisfied the rule,
    the repair dutifully moved the numeral three times, and the report said "3 correction passes"
    as though the compiler had tried its best rather than chased its own tail. A rule with no
    satisfying assignment is a defect in the rule, and it now says so on the sheet where it
    happened instead of waiting to be noticed.
    """
    names = sorted({issue.rule_id for issue in blocking(issues)})
    return (f"repairing changed the drawing and left {', '.join(names)} reporting exactly the "
            f"same thing, so the run stopped after {attempt} pass"
            f"{'' if attempt == 1 else 'es'} rather than chase it. Either the rule cannot be "
            f"satisfied for this figure or the repair it asks for does not address it; both are "
            f"worth a look before this figure is redrawn")


def _stem(figure_number: str) -> str:
    return "fig_" + "".join(ch for ch in str(figure_number).lower() if ch.isalnum())


def _verify(bundle: FigureBundle, profile, verifier, call_log, config: JobConfig,
            paths: JobPaths, record: FigureResult):
    """Rasterise, have it read back, compare. Never raises.

    A verifier that cannot answer must leave the figure exactly as the deterministic checks left
    it, and the report then says the independent reading was not run. An exception here failing
    the whole job would make the compiler less useful than one that had no verifier at all.
    """
    try:
        return _verify_inner(bundle, profile, verifier, call_log, config, paths, record)
    except Exception:
        traceback.print_exc()
        return None, []


def _verify_inner(bundle: FigureBundle, profile, verifier, call_log, config: JobConfig,
                  paths: JobPaths, record: FigureResult):
    from .providers import second_verifier

    try:
        png = svg_to_png(bundle.svg, profile)
    except Exception:
        return None, []
    expected = [label.reference_numeral for label in bundle.scene.labels]
    observed = vision.observe(png, bundle.spec, verifier, expected_numerals=expected)
    if observed is None:
        return None, []
    from PIL import Image
    import io

    try:
        with Image.open(io.BytesIO(png)) as image:
            size = image.size
    except Exception:
        size = (0, 0)
    first = vision.diff(bundle.spec, bundle.scene, observed, profile, image_size=size)
    _write_json(paths.debug / f"{_stem(bundle.spec.figure_number)}_observed.json",
                observed.model_dump())

    second = None
    if not first.clean or config.verification_level == "strict":
        try:
            other = second_verifier(log=call_log)
            observed_b = vision.observe(png, bundle.spec, other, expected_numerals=expected)
            if observed_b is not None:
                second = vision.diff(bundle.spec, bundle.scene, observed_b, profile,
                                     image_size=size)
                _write_json(
                    paths.debug / f"{_stem(bundle.spec.figure_number)}_observed_b.json",
                    observed_b.model_dump())
        except Exception:
            second = None

    agreed, disagreed = vision.reconcile(first, second)
    issues = vision.issues_from_diff(agreed, observed, bundle.spec.figure_id)
    if disagreed:
        record.corrections_applied.append(
            "two independent readers disagreed about this sheet; the drawing passed every "
            "measurement, so it is flagged rather than failed")
    return observed, issues


def _checks(issues, *, verified: bool) -> FigureChecks:
    def verdict(category: str) -> str:
        relevant = [issue for issue in issues if issue.category == category]
        if any(issue.severity == "blocking" for issue in relevant):
            return "FAIL"
        return "PASS"

    return FigureChecks(
        source_grounding=verdict("grounding"),
        references=verdict("reference"),
        semantics=verdict("semantic"),
        geometry="FAIL" if any(issue.severity == "blocking" and
                               issue.category in {"geometry", "jurisdiction"}
                               for issue in issues) else "PASS",
        vision=(verdict("vision") if verified else "SKIPPED"))


def _status(issues) -> str:
    hard = blocking(issues)
    if not hard:
        return "VALIDATED"
    if all(issue.repair_action == "revise_text" for issue in hard):
        return "NEEDS_TEXT_UPDATE"
    return "BLOCKED"


def _overall(results: list[FigureResult]) -> str:
    if not results:
        return "BLOCKED"
    statuses = {record.status for record in results}
    if statuses == {"VALIDATED"}:
        return "VALIDATED"
    if "VALIDATED" in statuses:
        return "PARTIAL"
    return "BLOCKED"


MAX_SHEETS_READ = 16


def _pair_originals(document: SourceDocument, results: list[FigureResult],
                    paths: JobPaths, read_labels: bool, notes: list[str]) -> None:
    """Put each generated figure beside the sheet the applicant filed for the same number.

    Pairing is done by reading the ``FIG. n`` caption printed on each filed sheet, because a
    patent's third sheet is very often not FIG. 3: one sheet routinely carries FIGS. 1A to 1E,
    and a later sheet carries none at all. This is the one place a model looks at the
    applicant's drawings, it reads a printed caption and nothing else, and nothing it returns
    reaches the semantic model or any generated figure.

    When the captions cannot be read the sheets are paired by position instead, and the
    comparison view says which of the two happened.
    """
    figures = document.original_figures
    if not figures:
        return
    labels: list[list[str]] = [[] for _ in figures]
    if read_labels:
        blobs: list[bytes] = []
        for figure in figures[:MAX_SHEETS_READ]:
            path = paths.originals / figure.filename if figure.filename else None
            try:
                blobs.append(path.read_bytes() if path and path.is_file() else b"")
            except OSError:
                blobs.append(b"")
        if len(figures) > MAX_SHEETS_READ:
            notes.append(
                f"the filed document has {len(figures)} drawing sheets; the captions on the "
                f"first {MAX_SHEETS_READ} were read to pair them with the generated figures, "
                "and the rest are shown unpaired")
        try:
            from .providers.gemini import read_figure_labels

            for index, found in enumerate(read_figure_labels(blobs)):
                labels[index] = found
        except Exception:
            labels = [[] for _ in figures]

    for index, figure in enumerate(figures):
        figure.figure_labels = labels[index]
        figure.label_source = "vision" if labels[index] else "none"

    by_number: dict[str, list[int]] = {}
    for index, figure in enumerate(figures):
        for label in figure.figure_labels:
            key = "".join(ch for ch in label.upper() if ch.isalnum())
            key = key[3:] if key.startswith("FIG") else key
            key = key[3:] if key.startswith("URE") else key
            if key:
                by_number.setdefault(key, []).append(index)
    for position, record in enumerate(results):
        key = record.figure_number.upper()
        if key in by_number:
            record.original_matches = by_number[key]
        elif not by_number and position < len(figures):
            record.original_matches = [position]


def _make_zip(paths: JobPaths) -> None:
    archive = paths.root / "figures.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(paths.root.rglob("*")):
            if path.is_dir() or path.name == archive.name:
                continue
            bundle.write(path, str(path.relative_to(paths.root)))
