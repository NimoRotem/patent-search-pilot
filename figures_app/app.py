"""The patent figure compiler, served at /figures.

Deliberately a separate process from the search application it borrows its document code from.
A deep prior-art search on that box runs for hours and dies if the service restarts, so a
compiler that is still being worked on has no business sharing its process. It shares the
session cookie, the account table and the document cache, and nothing else.

Jobs run on background threads because a compilation makes a dozen model calls and takes
minutes; the browser polls for the stage it is actually in rather than watching a spinner.
Concurrency is bounded, because this host also serves the search.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from flask import (Flask, abort, jsonify, render_template, request, send_file,
                   send_from_directory, url_for)

import authgate
from pfc.ingest import IngestError
from pfc.pipeline import STAGES, run_job
from pfc.profiles import available_profiles
from pfc.schemas import JobConfig

DATA_DIR = Path(os.environ.get("PFC_DATA_DIR", Path.home() / "patent-figures-data"))
JOBS_DIR = DATA_DIR / "jobs"
MAX_CONCURRENT = int(os.environ.get("PFC_MAX_CONCURRENT", "2"))
RETENTION_DAYS = float(os.environ.get("PFC_RETENTION_DAYS", "30"))
MAX_UPLOAD_BYTES = 40 * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024
authgate.install(app)

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_lock = threading.Lock()
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_ARTIFACT = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


# ---------------------------------------------------------------------------
# job records, on disk so a restart does not lose a finished compilation
# ---------------------------------------------------------------------------
def _job_dir(job_id: str) -> Path:
    if not _JOB_ID.match(str(job_id or "")):
        abort(404)
    return JOBS_DIR / job_id


def _state_path(job_id: str) -> Path:
    return _job_dir(job_id) / "job.json"


def _read_state(job_id: str) -> Optional[dict]:
    try:
        return json.loads(_state_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(job_id: str, **changes) -> dict:
    with _lock:
        state = _read_state(job_id) or {}
        state.update(changes)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        path = _state_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        return state


def _owned_state(job_id: str) -> dict:
    """The job, if it belongs to the signed-in account. Per-user isolation is not optional:
    a patent draft is confidential and two accounts share this host."""
    state = _read_state(job_id)
    if not state:
        abort(404)
    if int(state.get("owner_user_id") or 0) != int(authgate.user().get("id") or -1):
        abort(404)
    return state


def _sweep() -> None:
    """Delete jobs past the retention window. Called when a new one starts."""
    if RETENTION_DAYS <= 0 or not JOBS_DIR.is_dir():
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    for path in JOBS_DIR.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


# ---------------------------------------------------------------------------
# running a compilation
# ---------------------------------------------------------------------------
def _start(job_id: str, config: JobConfig, upload, link: str) -> None:
    def progress(stage: str, message: str, pct: int) -> None:
        _write_state(job_id, stage=stage, message=message, pct=pct)

    def work() -> None:
        try:
            outcome = run_job(job_id, root=_job_dir(job_id), config=config,
                              upload=upload, link=link, progress=progress)
            _write_state(job_id, status=outcome.report.overall_status, state="done", pct=100,
                         stage="EXPORT", message="Ready",
                         title=outcome.document.title,
                         publication_number=outcome.document.publication_number,
                         figures=len(outcome.report.figures),
                         validated=sum(1 for row in outcome.report.figures
                                       if row.status == "VALIDATED"),
                         usage=outcome.usage)
        except IngestError as exc:
            _write_state(job_id, state="error", pct=100, status="BLOCKED",
                         message=str(exc)[:400])
        except Exception as exc:
            traceback.print_exc()
            _write_state(job_id, state="error", pct=100, status="BLOCKED",
                         message=f"the compiler failed on this document: {str(exc)[:300]}")
        finally:
            _slots.release()

    threading.Thread(target=work, name=f"pfc-{job_id[:8]}", daemon=True).start()


def _config_from_form(form) -> JobConfig:
    def flag(name: str) -> bool:
        return str(form.get(name, "")).lower() in {"1", "true", "yes", "on"}

    payload = {
        "jurisdiction": form.get("jurisdiction") or "generic",
        "verification_level": form.get("verification_level") or "standard",
        "allow_new_reference_numbers": flag("allow_new_reference_numbers"),
    }
    if form.get("max_figures"):
        try:
            payload["max_figures"] = int(form["max_figures"])
        except (TypeError, ValueError):
            pass
    try:
        return JobConfig(**payload)
    except Exception:
        return JobConfig()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.post("/v1/jobs")
@authgate.require_user
def create_job():
    upload = None
    link = (request.form.get("url") or request.form.get("link") or "").strip()
    uploaded = request.files.get("file")
    if uploaded is not None and (uploaded.filename or "").strip():
        data = uploaded.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return jsonify({"error": f"file larger than "
                                     f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB"}), 413
        upload = (data, uploaded.filename)
    elif not link:
        return jsonify({"error": "attach a patent PDF or paste a patent link"}), 400

    if not _slots.acquire(blocking=False):
        return jsonify({"error": "the compiler is already working on as many patents as this "
                                 "box will take, please try again shortly"}), 429

    _sweep()
    job_id = uuid.uuid4().hex
    config = _config_from_form(request.form)
    _write_state(job_id, job_id=job_id, owner_user_id=int(authgate.user().get("id") or 0),
                 state="running", status="QUEUED", stage="INGEST", pct=2,
                 message="Reading the patent", source=(link or (upload[1] if upload else "")),
                 config=config.model_dump(),
                 created_at=datetime.now(timezone.utc).isoformat())
    _start(job_id, config, upload, link)
    return jsonify({"job_id": job_id, "status": "QUEUED",
                    "url": url_for("results", job_id=job_id)}), 202


@app.get("/v1/jobs/<job_id>")
@authgate.require_user
def job_status(job_id: str):
    state = _owned_state(job_id)
    report = _load(job_id, "validation_report.json") or {}
    figures = report.get("figures") or []
    return jsonify({
        "job_id": job_id, "state": state.get("state"), "status": state.get("status"),
        "stage": state.get("stage"), "pct": state.get("pct"),
        "message": state.get("message"),
        "figures": {
            "total": len(figures),
            "validated": sum(1 for row in figures if row.get("status") == "VALIDATED"),
            "blocked": sum(1 for row in figures if row.get("status") == "BLOCKED"),
            "needs_text_update": sum(1 for row in figures
                                     if row.get("status") == "NEEDS_TEXT_UPDATE"),
        }})


@app.get("/v1/jobs/<job_id>/figures")
@authgate.require_user
def job_figures(job_id: str):
    _owned_state(job_id)
    index = _load(job_id, "figure_index.json") or []
    for row in index:
        if row.get("svg"):
            row["preview_url"] = url_for("artifact", job_id=job_id, kind="figures",
                                         name=row["svg"])
    return jsonify(index)


@app.get("/v1/jobs/<job_id>/validation")
@authgate.require_user
def job_validation(job_id: str):
    _owned_state(job_id)
    return jsonify(_load(job_id, "validation_report.json") or {})


@app.get("/v1/jobs/<job_id>/graph")
@authgate.require_user
def job_graph(job_id: str):
    _owned_state(job_id)
    return jsonify(_load(job_id, "patent_graph.json") or {})


@app.get("/v1/jobs/<job_id>/manifest")
@authgate.require_user
def job_manifest(job_id: str):
    _owned_state(job_id)
    return jsonify(_load(job_id, "manifest.json") or {})


@app.get("/v1/jobs/<job_id>/download")
@authgate.require_user
def job_download(job_id: str):
    _owned_state(job_id)
    archive = _job_dir(job_id) / "figures.zip"
    if not archive.is_file():
        abort(404)
    return send_file(archive, as_attachment=True,
                     download_name=f"patent-figures-{job_id[:8]}.zip")


@app.delete("/v1/jobs/<job_id>")
@authgate.require_user
def job_delete(job_id: str):
    _owned_state(job_id)
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return jsonify({"deleted": job_id})


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "patent-figure-compiler",
                    "jobs": len(list(JOBS_DIR.glob("*"))) if JOBS_DIR.is_dir() else 0})


def _load(job_id: str, name: str):
    try:
        return json.loads((_job_dir(job_id) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------
@app.get("/jobs/<job_id>/artifact/<kind>/<name>")
@authgate.require_user
def artifact(job_id: str, kind: str, name: str):
    _owned_state(job_id)
    if kind not in {"figures", "originals", "debug"} or not _ARTIFACT.match(name):
        abort(404)
    directory = _job_dir(job_id) / kind
    path = directory / name
    if not path.is_file():
        abort(404)
    mimetypes = {".svg": "image/svg+xml", ".png": "image/png", ".pdf": "application/pdf",
                 ".json": "application/json"}
    return send_from_directory(directory, name,
                               mimetype=mimetypes.get(path.suffix.lower()))


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", profiles=available_profiles(),
                           stages=STAGES, jobs=_recent_jobs(), user=authgate.user())


@app.get("/jobs/<job_id>")
@authgate.require_user
def results(job_id: str):
    state = _owned_state(job_id)
    return render_template("results.html", job_id=job_id, state=state, stages=STAGES,
                           report=_load(job_id, "validation_report.json"),
                           index=_load(job_id, "figure_index.json") or [],
                           manifest=_load(job_id, "manifest.json"),
                           document=_load(job_id, "document.json"),
                           graph=_load(job_id, "patent_graph.json"),
                           notes=_load(job_id, "notes.json") or [],
                           user=authgate.user())


def _recent_jobs(limit: int = 12) -> list[dict]:
    if not JOBS_DIR.is_dir():
        return []
    owner = int(authgate.user().get("id") or -1)
    rows = []
    for path in JOBS_DIR.iterdir():
        if not path.is_dir():
            continue
        state = _read_state(path.name)
        if not state or int(state.get("owner_user_id") or 0) != owner:
            continue
        rows.append(state)
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows[:limit]


@app.template_filter("pretty_status")
def pretty_status(value: str) -> str:
    return str(value or "").replace("_", " ").title()


if __name__ == "__main__":
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8637")), debug=False)
