"""The web layer: the pages render, the artifacts are owned, and the prefix is honoured.

A template typo does not fail any other test in this suite, and the results page is where all
of the compiler's work is actually seen. These run against a temporary data directory with a
stubbed session; no model is called and no real job is compiled.
"""
from __future__ import annotations

import importlib
import json
import os
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
