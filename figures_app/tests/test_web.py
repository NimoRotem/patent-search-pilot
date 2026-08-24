"""The web layer: the pages render, the artifacts are owned, and the prefix is honoured.

A template typo does not fail any other test in this suite, and the results page is where all
of the compiler's work is actually seen. These run against a temporary data directory with a
stubbed session; no model is called and no real job is compiled.
"""
from __future__ import annotations

import importlib
import json
import os
import time
import uuid

import pytest

pytest.importorskip("flask")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PFC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-key-not-used-for-anything-real")
    monkeypatch.setenv("PILOT_ROOT", str(tmp_path / "no-pilot-here"))
    import app as web

    web = importlib.reload(web)
    # Stand in for a signed-in account. The gate itself is exercised by the test below.
    web.app.before_request_funcs = {}

    @web.app.before_request
    def _stub_user():
        from flask import request

        request.environ["pfc.user"] = {"id": 1, "email": "tester@example.com",
                                       "is_active": True, "session_version": 1}

    return web, web.app.test_client()


def _fixture_job(web, *, owner: int = 1) -> str:
    job_id = uuid.uuid4().hex
    directory = web.JOBS_DIR / job_id
    (directory / "figures").mkdir(parents=True)
    (directory / "originals").mkdir(parents=True)
    (directory / "figures" / "fig_1.svg").write_text("<svg/>", encoding="utf-8")
    (directory / "originals" / "original_000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (directory / "validation_report.json").write_text(json.dumps({
        "job_id": job_id, "overall_status": "PARTIAL", "blocking_issues": [], "warnings": [],
        "figures": [{
            "figure_id": "FIG_1", "figure_number": "1", "figure_type": "block_diagram",
            "title": "an example system", "status": "VALIDATED", "correction_attempts": 0,
            "checks": {"source_grounding": "PASS", "references": "PASS", "semantics": "PASS",
                       "geometry": "PASS", "vision": "SKIPPED"},
            "issues": [], "corrections_applied": [], "reason": "",
            "source_evidence": ["p0004"], "svg_path": "fig_1.svg", "pdf_path": "",
            "png_path": "", "original_matches": [0]}]}), encoding="utf-8")
    (directory / "figure_index.json").write_text(json.dumps([
        {"figure_id": "FIG_1", "figure": "FIG. 1", "status": "VALIDATED",
         "figure_type": "block_diagram", "title": "an example system", "svg": "fig_1.svg",
         "pdf": "", "png": "", "originals": [0], "notes": ["a note about this figure"]}]),
        encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps({
        "job_id": job_id, "overall_status": "PARTIAL", "generated_at": "2026-01-01T00:00:00",
        "document": {}, "config": {}, "figures": [],
        "provenance": {"renderer_version": "r", "validation_version": "v",
                       "validation_profile": "uspto_utility_v1.0",
                       "source_document_sha256": "0" * 64, "model_config_hash": "abc",
                       "prompt_versions": {}}}), encoding="utf-8")
    (directory / "document.json").write_text(json.dumps({
        "title": "An example patent", "publication_number": "US-1-B2", "origin": "link",
        "google_patents": "https://example.invalid/p", "espacenet": None,
        "original_figures": [{"index": 0, "filename": "original_000.png", "url": "",
                              "figure_labels": ["FIG. 1"], "label_source": "vision"}]}),
        encoding="utf-8")
    (directory / "patent_graph.json").write_text(json.dumps({
        "entities": [{"reference_numeral": "120", "canonical_name": "sensor", "aliases": [],
                      "attributes": {"mention_count": 4}}],
        "conflicts": [], "discarded": []}), encoding="utf-8")
    (directory / "notes.json").write_text(json.dumps(["read 12 paragraphs"]), encoding="utf-8")
    web._write_state(job_id, owner_user_id=owner, state="done", status="PARTIAL",
                     source="US-1-B2", created_at="2026-01-01T00:00:00")
    return job_id


def test_the_upload_page_renders(client):
    _web, http = client
    response = http.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Compile the figures of a patent" in body
    assert "It will not" in body


def test_the_results_page_shows_both_drawings(client):
    web, http = client
    job_id = _fixture_job(web)
    response = http.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Compiled" in body and "As filed" in body
    assert f"/jobs/{job_id}/artifact/figures/fig_1.svg" in body
    assert f"/jobs/{job_id}/artifact/originals/original_000.png" in body
    assert "Reference registry" in body
    assert "Validated" in body


def test_the_api_reports_the_figure_counts(client):
    web, http = client
    job_id = _fixture_job(web)
    payload = http.get(f"/v1/jobs/{job_id}").get_json()
    assert payload["figures"] == {"total": 1, "validated": 1, "blocked": 0,
                                  "needs_text_update": 0}
    figures = http.get(f"/v1/jobs/{job_id}/figures").get_json()
    assert figures[0]["preview_url"].endswith("fig_1.svg")


def test_another_account_cannot_see_the_job(client):
    """A patent draft is confidential and two accounts share this host."""
    web, http = client
    job_id = _fixture_job(web, owner=999)
    assert http.get(f"/jobs/{job_id}").status_code == 404
    assert http.get(f"/v1/jobs/{job_id}").status_code == 404
    assert http.get(f"/jobs/{job_id}/artifact/figures/fig_1.svg").status_code == 404


def test_an_artifact_path_cannot_escape_its_job(client):
    web, http = client
    job_id = _fixture_job(web)
    for name in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "fig_1.svg/../../job.json"):
        assert http.get(f"/jobs/{job_id}/artifact/figures/{name}").status_code in (404, 308)
    assert http.get(f"/jobs/{job_id}/artifact/secrets/fig_1.svg").status_code == 404


def test_links_carry_the_prefix_when_proxied(client):
    """Served at /figures, every link the page emits must say so."""
    _web, http = client
    body = http.get("/", headers={"X-Forwarded-Prefix": "/figures"}).get_data(as_text=True)
    assert "/figures/v1/jobs" in body
    plain = http.get("/").get_data(as_text=True)
    assert "/figures/" not in plain


def test_a_forged_prefix_is_ignored(client):
    _web, http = client
    body = http.get("/", headers={"X-Forwarded-Prefix": "https://evil.invalid/x"}
                    ).get_data(as_text=True)
    assert "evil.invalid" not in body


def test_the_health_probe_needs_no_session(client):
    web, http = client
    # Reinstate the real gate to prove the open path is genuinely open.
    web.app.before_request_funcs = {}
    web.authgate.install(web.app)
    response = http.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert http.get("/").status_code in (302, 401)


def test_a_job_can_be_deleted(client):
    web, http = client
    job_id = _fixture_job(web)
    assert http.delete(f"/v1/jobs/{job_id}").status_code == 200
    assert not (web.JOBS_DIR / job_id).exists()


# ---------------------------------------------------------------------------
# Creating a job. Nothing exercised this route, and a TypeError in it reached a
# user as "Unexpected token '<'" because the 500 came back as an HTML page.
# ---------------------------------------------------------------------------
@pytest.fixture
def client_no_pipeline(client, monkeypatch):
    """The web layer with the compiler stubbed: this is about the route, not the pipeline."""
    web, http = client
    started: list[tuple] = []
    monkeypatch.setattr(web, "_start",
                        lambda job_id, config, upload, link: started.append(
                            (job_id, config, upload, link)))
    return web, http, started


def test_creating_a_job_answers_json(client_no_pipeline):
    web, http, started = client_no_pipeline
    response = http.post("/v1/jobs", data={"url": "US-11338449-B2",
                                           "jurisdiction": "uspto_utility"})
    assert response.status_code == 202, response.get_data(as_text=True)
    assert response.mimetype == "application/json"
    payload = response.get_json()
    assert web._JOB_ID.match(payload["job_id"])
    assert payload["url"].endswith(payload["job_id"])
    assert len(started) == 1

    # The state it wrote is readable, and carries the job id every page reads back.
    state = web._read_state(payload["job_id"])
    assert state["job_id"] == payload["job_id"]
    assert state["owner_user_id"] == 1
    assert state["source"] == "US-11338449-B2"
    assert state["config"]["jurisdiction"] == "uspto_utility"
    assert http.get("/").status_code == 200      # the job list renders with it


def test_creating_a_job_from_a_file_answers_json(client_no_pipeline):
    import io

    _web, http, started = client_no_pipeline
    response = http.post("/v1/jobs", data={
        "file": (io.BytesIO(b"A PATENT\n\nDETAILED DESCRIPTION\nThe sensor 120 is disclosed.\n"),
                 "draft.txt")})
    assert response.status_code == 202
    assert started[0][2][1] == "draft.txt"


def test_a_submission_with_nothing_attached_is_refused_in_json(client_no_pipeline):
    _web, http, _started = client_no_pipeline
    response = http.post("/v1/jobs", data={})
    assert response.status_code == 400
    assert response.mimetype == "application/json"
    assert "patent" in response.get_json()["error"]


def test_a_failed_submission_does_not_leak_a_concurrency_slot(client, monkeypatch):
    """The worker thread releases the slot. Anything that fails before it exists must too, or
    two bad submissions leave the app permanently 'busy' with nothing running."""
    web, http = client

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "_start", explode)
    for _ in range(web.MAX_CONCURRENT + 2):
        response = http.post("/v1/jobs", data={"url": "US-1-B2"})
        assert response.status_code == 500
        assert response.mimetype == "application/json"
        assert "boom" in response.get_json()["detail"]

    monkeypatch.setattr(web, "_start", lambda *a, **k: None)
    assert http.post("/v1/jobs", data={"url": "US-1-B2"}).status_code == 202


def test_an_api_failure_is_json_not_an_html_page(client, monkeypatch):
    """The defect the user actually saw: a 500 rendered as HTML, and the browser reported
    'Unexpected token <' instead of anything about what went wrong."""
    web, http = client
    monkeypatch.setattr(web, "_start", lambda *a, **k: (_ for _ in ()).throw(ValueError("no")))
    response = http.post("/v1/jobs", data={"url": "US-1-B2"})
    assert response.mimetype == "application/json"
    body = response.get_data(as_text=True)
    assert not body.lstrip().startswith("<")
    assert response.get_json()["status"] == 500


def test_an_upload_over_the_cap_is_json_not_an_html_page(client):
    import io

    web, http = client
    oversize = b"x" * (web.MAX_UPLOAD_BYTES + 2048)
    response = http.post("/v1/jobs", data={"file": (io.BytesIO(oversize), "big.pdf")})
    assert response.status_code == 413
    assert response.mimetype == "application/json"
    assert not response.get_data(as_text=True).lstrip().startswith("<")


def test_a_signed_out_api_call_is_401_json_not_a_login_page(client):
    """A fetch() follows a redirect and gets HTML. An API caller gets a 401 and a login URL."""
    web, http = client
    web.app.before_request_funcs = {}
    web.authgate.install(web.app)
    response = http.post("/v1/jobs", data={"url": "US-1-B2"})
    assert response.status_code == 401
    assert response.mimetype == "application/json"
    assert response.get_json()["login"]


def test_a_job_interrupted_by_a_restart_does_not_poll_forever(tmp_path, monkeypatch):
    """A compilation runs on a daemon thread. Restart the service and that thread is gone while
    ``job.json`` still says running at 40%, so the page spins and the account cannot tell a job
    that is working from one that stopped existing during a deploy."""
    import importlib

    monkeypatch.setenv("PFC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-key-not-used-for-anything-real")
    monkeypatch.setenv("PILOT_ROOT", str(tmp_path / "no-pilot-here"))
    import app as web

    web = importlib.reload(web)
    orphan = uuid.uuid4().hex
    finished = uuid.uuid4().hex
    web._write_state(orphan, owner_user_id=1, state="running", status="QUEUED",
                     stage="EXTRACT", pct=40, message="Reading the description",
                     worker_pid=999_999)          # a process that is not there
    web._write_state(finished, owner_user_id=1, state="done", status="PARTIAL", pct=100)

    # What a fresh worker process does at import.
    assert web._reconcile_interrupted() == 1

    after = web._read_state(orphan)
    assert after["state"] == "error"
    assert after["pct"] == 100
    assert "interrupted" in after["message"]
    assert "submit it again" in after["message"]
    assert web._read_state(finished)["state"] == "done", "a finished job is left alone"

    # And it is idempotent, because every worker process runs it.
    assert web._reconcile_interrupted() == 0


def test_health_reports_how_many_slots_are_in_use(client):
    web, http = client
    body = http.get("/healthz").get_json()
    assert body["ok"] is True
    assert body["slots_in_use"] == 0
    web._slots.acquire()
    try:
        assert http.get("/healthz").get_json()["slots_in_use"] == 1
    finally:
        web._slots.release()


def test_a_setting_the_compiler_does_not_accept_is_refused_not_silently_swapped(
        client_no_pipeline):
    """It answered any bad field by compiling with the WHOLE default set.

    So a request asking for the reference-guided style with one other field wrong was quietly
    compiled as a schematic, in the generic jurisdiction, and nothing said so.
    """
    _web, http, _started = client_no_pipeline
    response = http.post("/v1/jobs", data={"url": "US-1-A1",
                                           "figure_style": "reference_guided",
                                           "jurisdiction": "atlantis"})
    assert response.status_code == 400
    assert "jurisdiction" in response.get_json()["error"]

    response = http.post("/v1/jobs", data={"url": "US-1-A1", "max_figures": "many"})
    assert response.status_code == 400
    assert "max_figures" in response.get_json()["error"]


def test_a_refused_setting_does_not_hold_a_concurrency_slot(client_no_pipeline):
    """Two bad submissions used to be enough to make the box look permanently busy."""
    web, http, _started = client_no_pipeline
    before = web._slots._value
    for _ in range(3):
        assert http.post("/v1/jobs", data={"url": "US-1-A1",
                                           "jurisdiction": "atlantis"}).status_code == 400
    assert web._slots._value == before


def test_the_style_that_was_asked_for_is_the_style_that_is_recorded(client_no_pipeline):
    """The job's own record has to agree with the request that made it."""
    web, http, started = client_no_pipeline
    response = http.post("/v1/jobs", data={"url": "US-1-A1",
                                           "figure_style": "reference_guided",
                                           "jurisdiction": "epo",
                                           "verification_level": "strict"})
    assert response.status_code == 202
    state = web._read_state(response.get_json()["job_id"])
    assert state["config"]["figure_style"] == "reference_guided"
    assert state["config"]["jurisdiction"] == "epo"
    assert state["config"]["verification_level"] == "strict"
    # and the config the worker was actually handed is that same one, not a re-read of the form.
    assert started[-1][1].figure_style == "reference_guided"
    assert started[-1][1].verification_level == "strict"


def test_startup_housekeeping_never_stops_the_service_coming_up(tmp_path, monkeypatch):
    """It runs at import. An exception there takes the worker process with it.

    A read-only data directory is a reason to log a line, not a reason for /figures to 502.
    """
    import importlib

    unwritable = tmp_path / "read-only"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    monkeypatch.setenv("PFC_DATA_DIR", str(unwritable / "nested"))
    monkeypatch.setenv("SECRET_KEY", "test-key-not-used-for-anything-real")
    monkeypatch.setenv("PILOT_ROOT", str(tmp_path / "no-pilot-here"))
    try:
        import app as web

        web = importlib.reload(web)          # must not raise
        assert web.app is not None
    finally:
        unwritable.chmod(0o700)

    # And a job record that cannot be parsed does not stop the sweep either.
    monkeypatch.setenv("PFC_DATA_DIR", str(tmp_path / "usable"))
    import app as web

    web = importlib.reload(web)
    broken = web.JOBS_DIR / uuid.uuid4().hex
    broken.mkdir(parents=True)
    (broken / "job.json").write_text("{not json at all")
    good = uuid.uuid4().hex
    web._write_state(good, owner_user_id=1, state="running", pct=10)
    assert web._reconcile_interrupted() == 1
    assert web._read_state(good)["state"] == "error"


def test_a_job_a_live_worker_still_owns_is_left_alone(tmp_path, monkeypatch):
    """With more than one worker, the second starting up must not close the first one's job.

    Any timeout long enough not to kill a working job is too long to be useful, because a single
    stage can run for minutes. So the test is whether the process that took the job is alive.
    """
    import importlib
    import os

    monkeypatch.setenv("PFC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-key-not-used-for-anything-real")
    monkeypatch.setenv("PILOT_ROOT", str(tmp_path / "no-pilot-here"))
    import app as web

    web = importlib.reload(web)
    mine = uuid.uuid4().hex
    theirs = uuid.uuid4().hex
    ancient = uuid.uuid4().hex
    web._write_state(mine, owner_user_id=1, state="running", pct=30, worker_pid=os.getpid())
    web._write_state(theirs, owner_user_id=1, state="running", pct=30, worker_pid=999_999)
    web._write_state(ancient, owner_user_id=1, state="running", pct=30)   # predates the field

    assert web._reconcile_interrupted() == 2
    assert web._read_state(mine)["state"] == "running", "a live worker still owns this one"
    assert web._read_state(theirs)["state"] == "error"
    assert web._read_state(ancient)["state"] == "error"


def test_the_worker_records_which_process_took_the_job(client, monkeypatch):
    """The record has to name the process doing the compiling, or nothing can check it."""
    import os
    import threading

    web, _http = client
    finished = threading.Event()

    def fake_run_job(job_id, **kwargs):
        raise RuntimeError("nothing is compiled in a test")

    monkeypatch.setattr(web, "run_job", fake_run_job)
    monkeypatch.setattr(web._slots, "release", lambda: finished.set())

    job_id = uuid.uuid4().hex
    web._write_state(job_id, owner_user_id=1, state="running", pct=2)
    web._start(job_id, web.JobConfig(), None, "US-1-A1")
    assert finished.wait(timeout=10), "the worker thread never finished"

    state = web._read_state(job_id)
    assert state["worker_pid"] == os.getpid()
    assert state["state"] == "error", "and the failure was recorded rather than swallowed"


def test_no_text_this_app_shows_a_human_carries_an_em_dash():
    """A house rule, and one that is only ever broken by accident.

    It reached a user once already: the full-text ladder printed "only front matter and
    background, the document is truncated" into the notes on the results page, with an em dash
    where that comma is. Grepping is the stated way to verify the rule, so the grep lives here
    rather than in somebody's memory.

    Exempt: docstrings and comments, which reach a reader of the source rather than a reader of
    the page; this test directory, which ships to nobody; and character classes and strip sets,
    which MATCH an em dash in a patent's own text rather than printing one. Prose is told apart
    from a pattern by having words in it.
    """
    import ast
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    offences = []

    def is_prose(value: str) -> bool:
        # A regex is not prose however many letters it contains: FIG, URE, body and A-Za-z all
        # look like words. Backslash escapes and a bracketed dash class give it away.
        if "\\b" in value or "\\s" in value or "\\d" in value or "[-" in value:
            return False
        return len(re.findall(r"[A-Za-z]{3,}", value)) >= 4

    for path in sorted(root.rglob("*.py")):
        if ".venv" in path.parts or "__pycache__" in path.parts or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if "\u2014" not in node.value or id(node) in docstrings:
                continue
            if not is_prose(node.value):
                continue
            offences.append(f"{path.relative_to(root)}:{node.lineno} {node.value[:70]!r}")

    for name in ("templates", "static"):
        folder = root / name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and "\u2014" in path.read_text(encoding="utf-8", errors="ignore"):
                offences.append(str(path.relative_to(root)))

    assert not offences, "em dashes reaching a human: " + "; ".join(offences)
