"""The figures maker, served at nimo.iptorch.com/figuresmaker/.

Two pages. One takes a draft and starts a job; the other is the editor, where a figure sits
beside the registry it was built from and every edit re-runs the whole compliance check.

Jobs run on daemon threads and are polled, not held open. A figure set is a few dozen model calls
and takes minutes, and a held request dies at the proxy long before that. Everything a poll needs
is on disk, written atomically, so a browser never reads half a job.
"""
from __future__ import annotations

import os
import threading
import traceback
from typing import Any, Optional

from flask import Flask, Response, jsonify, render_template, request

import authgate
from fm import export, llm, pipeline, store
from fm.drawing import Figure
from fm.render import leaders as leaders_mod, sheet as sheetmod
from fm.schemas import Registry
from fm.validate import rules

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 48 * 1024 * 1024
authgate.install(app)

MAX_CONCURRENT = int(os.environ.get("FM_MAX_CONCURRENT", "2"))
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_running: set[str] = set()
_running_lock = threading.Lock()


# ------------------------------------------------------------------------------------- pages


@app.get("/")
def index():
    return render_template("index.html", prefix=_prefix(),
                           jobs=store.listing(_owner(), limit=25),
                           who=authgate.user().get("email", ""))


@app.get("/job/<job_id>")
def job_page(job_id: str):
    job = store.load(job_id)
    if job is None:
        return render_template("missing.html", prefix=_prefix(), job_id=job_id), 404
    return render_template("job.html", prefix=_prefix(), job=job.as_dict(),
                           who=authgate.user().get("email", ""))


@app.get("/healthz")
def healthz():
    ok, detail = llm.available()
    graphviz = _which("dot")
    return jsonify({
        "ok": True,
        "model": {"ok": ok, "detail": detail},
        "graphviz": graphviz,
        "cairosvg": _module_present("cairosvg"),
        "pypdf": _module_present("pypdf"),
        "pillow": _module_present("PIL"),
        "data_dir": str(store.DATA_DIR),
        "running": len(_running),
    })


def _which(binary: str) -> str:
    import shutil
    return shutil.which(binary) or ""


def _module_present(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _prefix() -> str:
    return request.headers.get("X-Forwarded-Prefix", "").rstrip("/")


def _owner() -> str:
    return authgate.user().get("email", "")


# -------------------------------------------------------------------------------- starting


@app.post("/api/start")
def start():
    text = (request.form.get("text") or "").strip()
    url = (request.form.get("url") or "").strip()
    paper = (request.form.get("paper") or "a4").lower()
    if paper not in sheetmod.PAPERS:
        paper = "a4"
    upload: Optional[tuple[str, bytes]] = None
    handle = request.files.get("file")
    if handle and handle.filename:
        upload = (handle.filename, handle.read())
    if not (text or url or upload):
        return jsonify({"error": "paste a draft, give a link or a patent number, or attach a "
                                 "file"}), 400

    with _running_lock:
        if len(_running) >= MAX_CONCURRENT:
            return jsonify({"error": f"{MAX_CONCURRENT} figure sets are already being built on "
                                     "this host. Wait for one to finish."}), 429

    label = url or (upload[0] if upload else "pasted draft")
    job = store.new_job(owner=_owner(), title=label[:120], source=label[:200],
                        options={"paper": paper})
    thread = threading.Thread(target=_worker, name=f"fm-{job.id}", daemon=True,
                             args=(job.id, text, url, upload, paper))
    thread.start()
    return jsonify({"id": job.id, "url": f"{_prefix()}/job/{job.id}"})


def _worker(job_id: str, text: str, url: str, upload, paper: str) -> None:
    with _running_lock:
        _running.add(job_id)
    acquired = _slots.acquire(timeout=1800)
    try:
        job = store.load(job_id)
        if job is None:
            return
        if not acquired:
            job.status = "failed"
            job.error = "timed out waiting for a free worker slot"
            store.save(job)
            return
        pipeline.execute(job, text=text, url=url, upload=upload, paper=paper)
    except Exception:
        job = store.load(job_id)
        if job is not None:
            job.status = "failed"
            job.error = "the worker crashed; see traceback.txt"
            store.write_text(job.path / "traceback.txt", traceback.format_exc())
            store.save(job)
    finally:
        if acquired:
            _slots.release()
        with _running_lock:
            _running.discard(job_id)


@app.get("/api/job/<job_id>")
def job_status(job_id: str):
    job = store.load(job_id)
    if job is None:
        return jsonify({"error": "no such job"}), 404
    return jsonify(job.as_dict())


@app.get("/api/jobs")
def jobs():
    return jsonify({"jobs": store.listing(_owner(), limit=60)})


# ------------------------------------------------------------------------------------ data


@app.get("/api/job/<job_id>/data")
def job_data(job_id: str):
    job = store.load(job_id)
    if job is None:
        return jsonify({"error": "no such job"}), 404
    figures = pipeline.load_figures(job)
    return jsonify({
        "job": job.as_dict(),
        "registry": store.read_json(job.path / "registry.json") or {},
        "plan": store.read_json(job.path / "plan.json") or {},
        "report": store.read_json(job.path / "report.json") or {},
        "claims": store.read_json(job.path / "claims.json") or [],
        "sheets": store.read_json(job.path / "sheets.json") or [],
        "sections": _sections_lite(job),
        "figures": [{"label": f.label, "kind": f.kind, "title": f.title,
                     "numerals": f.numerals(),
                     "svg": sheetmod.figure_svg(f)} for f in figures],
        "rules": {code: {"cite": r.cite, "basis": r.basis, "title": r.title}
                  for code, r in rules.RULES.items()},
    })


def _sections_lite(job: store.Job) -> dict[str, Any]:
    raw = store.read_json(job.path / "sections.json") or {}
    return {"title": raw.get("title", ""), "source": raw.get("source", ""),
            "source_ref": raw.get("source_ref", ""),
            "brief_items": raw.get("brief_items") or [],
            "claims": len(raw.get("claims") or [])}


# ------------------------------------------------------------------------------- the editor


def _load_for_edit(job_id: str):
    job = store.load(job_id)
    if job is None:
        return None, None, (jsonify({"error": "no such job"}), 404)
    if job.owner and job.owner != _owner():
        return None, None, (jsonify({"error": "this job belongs to another account"}), 403)
    figures = pipeline.load_figures(job)
    if not figures:
        return None, None, (jsonify({"error": "this job has no figures"}), 400)
    return job, figures, None


def _find(figures, label: str) -> Optional[Figure]:
    for figure in figures:
        if figure.label == label:
            return figure
    return None


def _after_edit(job: store.Job, figures, changed: Figure):
    paper = (job.options or {}).get("paper", "a4")
    pipeline.save_figures(job, figures)
    report = pipeline.revalidate(job, figures, paper=paper)
    job.summary = {**(job.summary or {}),
                   "errors": len(report.errors()), "warnings": len(report.warnings())}
    store.save(job)
    return jsonify({"ok": True, "figure": {"label": changed.label,
                                           "svg": sheetmod.figure_svg(changed),
                                           "numerals": changed.numerals()},
                    "report": report.model_dump()})


@app.post("/api/job/<job_id>/move")
def move_numeral(job_id: str):
    job, figures, error = _load_for_edit(job_id)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    figure = _find(figures, body.get("figure", ""))
    if figure is None:
        return jsonify({"error": "no such figure"}), 404
    numeral = str(body.get("numeral") or "")
    try:
        at = (float(body["x"]), float(body["y"]))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "x and y are required, in millimetres"}), 400
    problems = leaders_mod.replace_one(figure, numeral, at)
    if problems:
        return jsonify({"error": problems[0].message}), 400
    return _after_edit(job, figures, figure)


@app.post("/api/job/<job_id>/retarget")
def retarget_leader(job_id: str):
    job, figures, error = _load_for_edit(job_id)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    figure = _find(figures, body.get("figure", ""))
    if figure is None:
        return jsonify({"error": "no such figure"}), 404
    try:
        tip = (float(body["x"]), float(body["y"]))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "x and y are required, in millimetres"}), 400
    problems = leaders_mod.retarget(figure, str(body.get("numeral") or ""), tip)
    if problems:
        return jsonify({"error": problems[0].message}), 400
    return _after_edit(job, figures, figure)


@app.post("/api/job/<job_id>/resolve")
def resolve_figure(job_id: str):
    """Put every numeral back where the solver would put it."""
    job, figures, error = _load_for_edit(job_id)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    figure = _find(figures, body.get("figure", ""))
    if figure is None:
        return jsonify({"error": "no such figure"}), 404
    if body.get("keep_manual"):
        leaders_mod.solve(figure, effort=2)
    else:
        figure.labels = []
        figure.leaders = []
        leaders_mod.solve(figure, effort=2)
    return _after_edit(job, figures, figure)


@app.post("/api/job/<job_id>/term")
def edit_term(job_id: str):
    """Change what a numeral is called. The registry is the source of truth, so this edits it."""
    job = store.load(job_id)
    if job is None:
        return jsonify({"error": "no such job"}), 404
    if job.owner and job.owner != _owner():
        return jsonify({"error": "this job belongs to another account"}), 403
    body = request.get_json(silent=True) or {}
    numeral = str(body.get("numeral") or "")
    term = " ".join(str(body.get("term") or "").split())[:120]
    if not numeral or not term:
        return jsonify({"error": "numeral and term are required"}), 400

    raw = store.read_json(job.path / "registry.json") or {}
    registry = Registry.model_validate(raw)
    entry = registry.by_numeral().get(numeral)
    if entry is None:
        return jsonify({"error": f"{numeral} is not in the registry"}), 404
    if entry.term != term and entry.term not in entry.aliases:
        entry.aliases.append(entry.term)
    entry.term = term
    store.write_json(job.path / "registry.json", registry.model_dump())

    plan_raw = store.read_json(job.path / "plan.json") or {}
    for figure in plan_raw.get("figures") or []:
        for element in figure.get("elements") or []:
            if element.get("numeral") == numeral:
                element["term"] = term
    store.write_json(job.path / "plan.json", plan_raw)

    figures = pipeline.load_figures(job)
    report = pipeline.revalidate(job, figures, paper=(job.options or {}).get("paper", "a4"))
    return jsonify({"ok": True, "report": report.model_dump(),
                    "registry": registry.model_dump()})


@app.post("/api/job/<job_id>/revalidate")
def revalidate(job_id: str):
    job, figures, error = _load_for_edit(job_id)
    if error:
        return error
    report = pipeline.revalidate(job, figures, paper=(job.options or {}).get("paper", "a4"))
    return jsonify({"ok": True, "report": report.model_dump()})


# ------------------------------------------------------------------------------------ files


@app.get("/api/job/<job_id>/sheet/<int:number>.svg")
def sheet_svg(job_id: str, number: int):
    job = store.load(job_id)
    if job is None:
        return jsonify({"error": "no such job"}), 404
    body = store.read_text(job.path / f"sheet-{number}.svg")
    if not body:
        return jsonify({"error": "no such sheet"}), 404
    return Response(body, mimetype="image/svg+xml")


@app.get("/api/job/<job_id>/drawings.pdf")
def drawings_pdf(job_id: str):
    job = store.load(job_id)
    if job is None:
        return jsonify({"error": "no such job"}), 404
    svgs = _sheet_svgs(job)
    if not svgs:
        return jsonify({"error": "this job has no sheets"}), 404
    try:
        blob = export.sheets_pdf([svg for _name, svg in svgs])
    except export.ExportUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    return Response(blob, mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="drawings-{job_id}.pdf"'})


@app.get("/api/job/<job_id>/bundle.zip")
def bundle(job_id: str):
    job = store.load(job_id)
    if job is None:
        return jsonify({"error": "no such job"}), 404
    sheets = _sheet_svgs(job)
    figures = [(path.name, store.read_text(path))
               for path in sorted(job.path.glob("figure-*.svg"))]
    extras: list[tuple[str, bytes]] = []
    for name in ("registry.json", "plan.json", "report.json", "sheets.json"):
        body = store.read_text(job.path / name)
        if body:
            extras.append((f"data/{name}", body.encode("utf-8")))
    blob = export.bundle(sheet_svgs=sheets, figure_svgs=figures,
                         redline_html=store.read_text(job.path / "redline.html"),
                         extras=extras)
    return Response(blob, mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="figures-{job_id}.zip"'})


@app.get("/api/job/<job_id>/redline.html")
def redline(job_id: str):
    job = store.load(job_id)
    if job is None:
        return jsonify({"error": "no such job"}), 404
    body = store.read_text(job.path / "redline.html")
    if not body:
        return jsonify({"error": "this job has no redline"}), 404
    return Response(body, mimetype="text/html")


@app.get("/api/job/<job_id>/traceback")
def job_traceback(job_id: str):
    job = store.load(job_id)
    if job is None:
        return jsonify({"error": "no such job"}), 404
    return Response(store.read_text(job.path / "traceback.txt") or "(none)",
                    mimetype="text/plain")


def _sheet_svgs(job: store.Job) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in sorted(job.path.glob("sheet-*.svg"),
                       key=lambda p: int(p.stem.split("-")[1])):
        body = store.read_text(path)
        if body:
            out.append((path.name, body))
    return out


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "that file is larger than 48 MB"}), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8639")), debug=False)
