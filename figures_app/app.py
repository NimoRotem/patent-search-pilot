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
from werkzeug.exceptions import HTTPException

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

class PrefixMiddleware:
    """Serve correctly from behind ``location ^~ /figures/``.

    nginx strips the prefix before proxying and passes it in ``X-Forwarded-Prefix``. Without
    this the app is reachable but every link it generates points at the root, so the results
    page loads and none of its artifacts do. Setting ``SCRIPT_NAME`` is what makes ``url_for``
    produce ``/figures/...``.

    Only a prefix matching the expected shape is honoured, so a forged header cannot make the
    app emit links to somewhere else.
    """

    _SAFE = re.compile(r"^/[A-Za-z0-9_-]{1,40}$")

    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        prefix = (environ.get("HTTP_X_FORWARDED_PREFIX") or "").rstrip("/")
        if prefix and self._SAFE.match(prefix):
            environ["SCRIPT_NAME"] = prefix
            path = environ.get("PATH_INFO", "")
            if path.startswith(prefix):
                environ["PATH_INFO"] = path[len(prefix):] or "/"
        forwarded_proto = environ.get("HTTP_X_FORWARDED_PROTO")
        if forwarded_proto in {"http", "https"}:
            environ["wsgi.url_scheme"] = forwarded_proto
        return self.application(environ, start_response)


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024
app.wsgi_app = PrefixMiddleware(app.wsgi_app)
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
        # Recorded here rather than passed in by every caller: doing the latter is what produced
        # `_write_state(job_id, job_id=job_id, ...)`, a TypeError that reached a user because
        # nothing exercised the route that made the call.
        state["job_id"] = job_id
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


def _reconcile_interrupted() -> int:
    """Close out jobs whose worker died with the process that owned it.

    A compilation runs on a daemon thread. Restart the service, or lose it to an OOM, and that
    thread is gone while ``job.json`` still says ``running`` at 40%: the page polls forever and
    the account has no way to tell a job that is working from one that stopped existing during a
    deploy. Nothing else notices, because the thread that would have written the error is the
    thread that died.

    Called once at import, which is once per worker process. The test is whether the process
    that took the job is still alive, not how long ago it last said something: a stage can take
    minutes, so any timeout long enough not to kill a working job is too long to be useful, and
    a second worker starting up must not close a job the first one began a moment ago.
    """
    if not JOBS_DIR.is_dir():
        return 0
    closed = 0
    for path in sorted(JOBS_DIR.iterdir()):
        if not path.is_dir():
            continue
        state = _read_state(path.name)
        if not state or state.get("state") != "running":
            continue
        if _process_alive(state.get("worker_pid")):
            continue
        _write_state(path.name, state="error", status="BLOCKED", pct=100,
                     message="this compilation was interrupted, most likely by the service "
                             "restarting. Nothing was lost except the run itself; submit it "
                             "again.")
        closed += 1
    return closed


def _process_alive(pid) -> bool:
    """Is that process still there? Absent or unreadable counts as gone.

    A job record written before this field existed has no pid, and predates the running process
    by definition, so it is closed.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # alive and owned by somebody else
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# running a compilation
# ---------------------------------------------------------------------------
def _start(job_id: str, config: JobConfig, upload, link: str) -> None:
    def progress(stage: str, message: str, pct: int) -> None:
        _write_state(job_id, stage=stage, message=message, pct=pct)

    def work() -> None:
        # Written from inside the worker, so the record names the process that is actually doing
        # the compiling. See _reconcile_interrupted.
        _write_state(job_id, worker_pid=os.getpid())
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
    """The submitted settings, or a ValueError naming the field that is wrong.

    It used to answer any invalid field by returning ``JobConfig()``: the whole set of defaults.
    So a request that asked for the reference-guided style and got one other field wrong was
    quietly compiled as a schematic, in the generic jurisdiction, and nothing anywhere said so.
    Substituting a different job for the one that was asked for is worse than refusing it.
    """
    def flag(name: str) -> bool:
        return str(form.get(name, "")).lower() in {"1", "true", "yes", "on"}

    payload = {
        "jurisdiction": form.get("jurisdiction") or "generic",
        "verification_level": form.get("verification_level") or "standard",
        "figure_style": form.get("figure_style") or "patent_line_art",
        "allow_new_reference_numbers": flag("allow_new_reference_numbers"),
    }
    if form.get("max_figures"):
        try:
            payload["max_figures"] = int(form["max_figures"])
        except (TypeError, ValueError):
            raise ValueError(f"max_figures is not a number: {form['max_figures']!r}") from None
    try:
        return JobConfig(**payload)
    except Exception as exc:
        fields = ", ".join(sorted(
            str(error.get("loc", ("?",))[0]) for error in getattr(exc, "errors", lambda: [])()
        )) or "one of the settings"
        raise ValueError(f"{fields} is not a value this compiler accepts") from exc


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

    # Read before the slot is taken, so a rejected setting cannot hold one.
    try:
        config = _config_from_form(request.form)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not _slots.acquire(blocking=False):
        return jsonify({"error": "the compiler is already working on as many patents as this "
                                 "box will take, please try again shortly"}), 429

    # The slot is released by the worker thread. Anything that fails BEFORE that thread exists
    # has to hand it back here, or two failed submissions leave the app permanently "busy" with
    # no job running.
    try:
        _sweep()
        job_id = uuid.uuid4().hex
        _write_state(job_id, owner_user_id=int(authgate.user().get("id") or 0),
                     state="running", status="QUEUED", stage="INGEST", pct=2,
                     message="Reading the patent",
                     source=(link or (upload[1] if upload else "")),
                     config=config.model_dump(),
                     created_at=datetime.now(timezone.utc).isoformat())
        _start(job_id, config, upload, link)
    except Exception:
        _slots.release()
        raise
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


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
def _is_api_request() -> bool:
    return request.path.startswith("/v1/") or request.path.startswith("/api/")


@app.errorhandler(HTTPException)
def _http_error(error: HTTPException):
    """An API path answers JSON however it fails.

    Flask's default handlers render HTML, so a 413 or a 500 on /v1/jobs reached the browser as
    a login-shaped page and `response.json()` failed with "Unexpected token '<'" — a message
    that says nothing about what went wrong. Every API failure now carries its reason.
    """
    if _is_api_request():
        return jsonify({"error": error.description or error.name,
                        "status": error.code}), (error.code or 500)
    return error


@app.errorhandler(Exception)
def _unhandled(error: Exception):
    traceback.print_exc()
    if _is_api_request():
        return jsonify({"error": "the server failed on that request",
                        "detail": f"{type(error).__name__}: {error}"[:300],
                        "status": 500}), 500
    raise error


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "patent-figure-compiler",
                    "jobs": len(list(JOBS_DIR.glob("*"))) if JOBS_DIR.is_dir() else 0,
                    "slots_in_use": MAX_CONCURRENT - getattr(_slots, "_value",
                                                             MAX_CONCURRENT)})


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


def _on_start() -> None:
    """Run once per worker process, and never a reason the service will not come up.

    Both of these are housekeeping. A read-only data directory or a job record another process
    is mid-write on is worth a line in the log and nothing more: refusing to start would turn a
    tidy-up into an outage, and this runs at import, where an exception takes the worker with it.
    """
    try:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        traceback.print_exc()
        print(f"[pfc] {JOBS_DIR} could not be created: {exc}", flush=True)
        return
    try:
        closed = _reconcile_interrupted()
    except Exception:
        traceback.print_exc()
        return
    if closed:
        print(f"[pfc] closed {closed} job(s) interrupted by a restart", flush=True)


_on_start()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8637")), debug=False)
