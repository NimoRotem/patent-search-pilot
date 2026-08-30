"""The glue between a draft in Postgres and a filing package on disk.

Three jobs, all of them slow enough that a request thread must not wait for them:

  INSPECT   one vision pass per uploaded sheet, cached on the image's own bytes, so the
            reconciliation between the drawings and the text has something to work from.
  BUILD     every paper in the package, plus the deterministic audit over the built files.
  REVIEW    the clean-room QA agent reading the built package as a stranger.

The build is an explicit action rather than something the Filing tab does on load. A sheet
inspection takes the better part of a minute and the audit reads every file it just wrote; doing
that on a page load would make the tab feel broken, and doing it on a poll would do it over and
over. So: the page shows the last build, says whether the draft has moved since, and offers the
button.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Mapping, Sequence

import figure_facts
import filing_pack
import filing_profile
import filing_qa
import filing_rules

_BUILDING: dict[int, float] = {}
_LOCK = threading.Lock()


def store(project_id: int) -> Path:
    import draft_workspace
    path = draft_workspace.root() / "filing" / f"p{int(project_id)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_path(project_id: int) -> Path:
    return store(project_id) / "build.json"


def _zip_path(project_id: int) -> Path:
    return store(project_id) / "package.zip"


def building(project_id: int) -> bool:
    with _LOCK:
        return int(project_id) in _BUILDING


def last_build(project_id: int) -> dict[str, Any] | None:
    try:
        return json.loads(_build_path(project_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def package_bytes(project_id: int) -> bytes:
    try:
        return _zip_path(project_id).read_bytes()
    except OSError:
        return b""


# =============================================================================================
# Reading the sheets
# =============================================================================================
def inspect_sheets(figures: Sequence[Mapping[str, Any]],
                   numerals: Sequence[Mapping[str, str]] = ()) -> dict[str, dict[str, Any]]:
    """One inventory per distinct uploaded image, keyed by its sha256."""
    out: dict[str, dict[str, Any]] = {}
    for figure in figures:
        png = bytes(figure.get("png") or b"")
        if not png:
            continue
        key = figure_facts.sheet_key(png)
        if key in out:
            continue
        try:
            out[key] = figure_facts.inspect_sheet(
                png, label=str(figure.get("label") or ""), numerals=numerals)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            out[key] = {"error": f"{type(exc).__name__}: {exc}"[:300],
                        "label": str(figure.get("label") or ""), "views": [],
                        "unlabelled_views": [], "numerals": [], "text_labels": [],
                        "numeral_key_table": False, "divider_rules": 0,
                        "sheet_number_text": "",
                        "smallest_reference_character_height_fraction": 0.0}
    return out


def reconcile(*, sheets: Mapping[str, Mapping[str, Any]], sections: Mapping[str, str],
              numerals: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    facts = list(sheets.values())
    findings = list(figure_facts.reconcile(
        sheets=facts, sections=sections, numerals=numerals,
        claim_terms=figure_facts.claim_terms_from(sections.get("claims") or "")))
    for sheet in facts:
        if sheet.get("error"):
            findings.append(filing_rules.finding(
                "37 CFR 1.83", "blocker", str(sheet.get("label") or "a sheet"),
                "This sheet could not be inspected",
                str(sheet["error"]) + ". The drawings cannot be reconciled with the text until "
                "it can be read, and a package built now would be checked on the text alone."))
    return [dict(item) for item in findings]


# =============================================================================================
# Building
# =============================================================================================
def build(*, project: Mapping[str, Any], version: Mapping[str, Any],
          profile: Mapping[str, Any], numerals: Sequence[Mapping[str, str]],
          figures: Sequence[Mapping[str, Any]],
          citations: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Inspect, reconcile, build, audit. Returns the summary; writes the package to disk."""
    project_id = int(project["id"])
    sections = dict(version.get("sections") or {})
    sheets = inspect_sheets(figures, numerals)
    reconciliation = reconcile(sheets=sheets, sections=sections, numerals=numerals)
    built = filing_pack.build(project=project, version=version, profile=profile,
                              figures=figures, sheet_facts=sheets, citations=citations)
    built["findings"] = list(built["findings"]) + reconciliation
    built["files"][filing_pack.AUDIT_NAME] = filing_pack.audit_text(
        built["findings"]).encode("utf-8")
    built["files"][filing_pack.README_NAME] = filing_pack.read_me(
        project, filing_profile.resolve(profile, project), fees=built["fees"],
        gaps=built["gaps"], audit=built["findings"],
        sheet_count=len(built["sheets"])).encode("utf-8")
    built["verdict"] = filing_rules.verdict(built["findings"])
    built["ready"] = not filing_rules.blockers(built["findings"])

    _zip_path(project_id).write_bytes(filing_pack.zip_bytes(built["files"]))
    summary = {
        "built_at": time.time(),
        "version_no": int(version.get("version_no") or 0),
        "verdict": built["verdict"],
        "ready": built["ready"],
        "findings": built["findings"],
        "fees": built["fees"],
        "gaps": built["gaps"],
        "sheets": built["sheets"],
        "measurements": built["measurements"],
        "files": [{"name": name, "bytes": len(blob)}
                  for name, blob in sorted(built["files"].items())],
        "reconciliation": reconciliation,
    }
    _build_path(project_id).write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    return {"summary": summary, "built": built, "sheets": sheets,
            "reconciliation": reconciliation}


def start_build(*, project: Mapping[str, Any], version: Mapping[str, Any],
                profile: Mapping[str, Any], numerals: Sequence[Mapping[str, str]],
                figures: Sequence[Mapping[str, Any]],
                citations: Sequence[Mapping[str, Any]] = (),
                then_review: bool = False, model: str = "") -> dict[str, Any]:
    project_id = int(project["id"])
    with _LOCK:
        if project_id in _BUILDING:
            raise RuntimeError("A filing package for this draft is already being built.")
        _BUILDING[project_id] = time.time()

    def _run() -> None:
        try:
            outcome = build(project=project, version=version, profile=profile,
                            numerals=numerals, figures=figures, citations=citations)
            if then_review:
                try:
                    filing_qa.start(
                        project_id=project_id, built=outcome["built"],
                        sections=dict(version.get("sections") or {}), numerals=numerals,
                        figures=figures, reconciliation=outcome["reconciliation"],
                        model=model)
                except RuntimeError:
                    pass                                    # already running; leave it be
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            try:
                _build_path(project_id).write_text(json.dumps({
                    "built_at": time.time(), "error": f"{type(exc).__name__}: {exc}"[:400],
                    "version_no": int(version.get("version_no") or 0),
                    "verdict": "not ready", "ready": False, "findings": [], "gaps": [],
                    "sheets": [], "measurements": [], "files": []}), encoding="utf-8")
            except OSError:
                traceback.print_exc()
        finally:
            with _LOCK:
                _BUILDING.pop(project_id, None)

    threading.Thread(target=_run, name=f"filing-build-{project_id}", daemon=True).start()
    return {"queued": True}


# =============================================================================================
# What the drafting agent asks for
# =============================================================================================
def agent_report(*, sections: Mapping[str, str], numerals: Sequence[Mapping[str, str]],
                 figures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The sheet inventory and the reconciliation, written for the agent that has to fix it.

    The drafting agent owns the drawing TEXT and does not own the drawings. Handing it a list of
    what is actually printed on each sheet is the difference between writing a Brief Description
    from the brief it wrote earlier and writing one from the sheet the user supplied.
    """
    sheets = inspect_sheets(figures, numerals)
    findings = reconcile(sheets=sheets, sections=dict(sections), numerals=list(numerals))
    inventory = []
    for sheet in sheets.values():
        inventory.append({
            "label": sheet.get("label") or "",
            "views": [{"legend": view.get("legend"),
                       "kind": view.get("kind"),
                       "numerals": [item.get("value") for item in view.get("numerals") or []]}
                      for view in sheet.get("views") or []],
            "unnumbered_views": [{"looks_like": view.get("description"),
                                  "numerals": [item.get("value")
                                               for item in view.get("numerals") or []]}
                                 for view in sheet.get("unlabelled_views") or []],
            "numerals": [{"value": item.get("value"), "points_at": item.get("points_at"),
                          "lead_lines": item.get("lead_lines"),
                          "agrees_with_the_table": item.get("matches_declared_part")}
                         for item in sheet.get("numerals") or []],
            "words_printed_on_the_sheet": [item.get("text")
                                           for item in sheet.get("text_labels") or []],
            "reference_numeral_key_printed": bool(sheet.get("numeral_key_table")),
            "divider_rules": int(sheet.get("divider_rules") or 0),
        })
    return {"sheets": inventory, "findings": findings,
            "verdict": filing_rules.verdict(findings)}


# =============================================================================================
# What the page reads
# =============================================================================================
def state(*, project: Mapping[str, Any], version_no: int,
          profile: Mapping[str, Any]) -> dict[str, Any]:
    project_id = int(project["id"])
    resolved = filing_profile.resolve(profile, project)
    build_summary = last_build(project_id)
    qa = filing_qa.latest(project_id)
    stale = bool(build_summary and int(build_summary.get("version_no") or 0) != int(version_no))
    return {
        "profile": filing_profile.public(resolved),
        "build": build_summary,
        "building": building(project_id),
        "stale": stale,
        "qa": qa,
        "qa_running": filing_qa.running(project_id),
        "qa_available": filing_qa.available(),
        "version_no": int(version_no),
        "package_available": bool(build_summary and not build_summary.get("error") and
                                  _zip_path(project_id).exists()),
        "fee_schedule_url": filing_rules.FEE_SCHEDULE_URL,
        "patent_center_url": filing_rules.PATENT_CENTER_URL,
    }
