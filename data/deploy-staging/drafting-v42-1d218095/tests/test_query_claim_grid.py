"""Uploaded Claim x Reference background-grid contract (hermetic)."""
from __future__ import annotations

import json

import query_claim_grid as qcg


def _report(n=14, source="upload"):
    claims = []
    for i in range(1, n + 1):
        claims.append({
            "claim_no": i,
            "text": f"Claim {i} requires a vacuum plate and control feature {i}.",
            "independent": i in (1, 14),
        })
    return {"query_document": {"source": source, "label": "uploaded.pdf", "claims": claims}}


def _view(n=10):
    return {"cards": [{"pub": f"US-{i}-A1", "title": f"Reference {i}", "rank": i}
                      for i in range(1, n + 1)]}


def test_metadata_is_upload_only_and_bounded():
    meta = qcg.metadata(_report())
    assert meta["available"] is True
    assert meta["n_claims"] == 14 and meta["n_selected"] == qcg.MAX_CLAIMS
    assert qcg.metadata(_report(source="link"))["available"] is False


def test_build_grid_keeps_independent_claims_and_transposes(monkeypatch):
    calls = []

    def fake_chart(elements, pub):
        calls.append((pub, list(elements)))
        return {"pub": pub, "method": "llm", "rows": [
            {"element": text, "verdict": "disclosed" if pub == "US-1-A1" else "partial",
             "quote": "a vacuum plate and control feature", "location": "claim 1",
             "coord": {"claim_no": 1}, "confidence": 0.8,
             "grounding": "verified", "method": "llm"}
            for text in elements
        ]}

    monkeypatch.setattr(qcg.claim_chart, "build_chart", fake_chart)
    grid = qcg.build_grid(_report(), _view())

    assert grid["status"] == "done" and grid["available"] is True
    assert grid["n_claims_shown"] == qcg.MAX_CLAIMS
    assert grid["n_refs_shown"] == qcg.MAX_REFS
    assert len(calls) == qcg.MAX_REFS
    # Claim 14 is independent and must survive the 12-row bound; selected rows stay document-order.
    numbers = [r["claim_no"] for r in grid["rows"]]
    assert 14 in numbers and numbers == sorted(numbers)
    assert all(len(r["cells"]) == qcg.MAX_REFS for r in grid["rows"])
    assert grid["rows"][0]["cells"][0]["verdict"] == "disclosed"
    assert grid["rows"][0]["cells"][1]["verdict"] == "partial"
    assert grid["truncated_claims"] == 2 and grid["truncated_refs"] == 2


def test_cache_is_versioned_and_invalidation_removes_it(tmp_path):
    done = {"version": qcg.VERSION, "status": "done", "available": True,
            "rows": [], "columns": []}
    path = qcg._path(tmp_path, "safe-slug")
    qcg._write_atomic(path, done)
    assert qcg.status("safe-slug", tmp_path)["status"] == "done"
    qcg.invalidate("safe-slug", tmp_path)
    assert not path.exists()


def test_api_post_schedules_from_cached_final_report(app_client, monkeypatch, tmp_path):
    import webapp

    slug = "uploaded-claims-grid"
    (tmp_path / f"{slug}.json").write_text(json.dumps(_report(2)))
    (tmp_path / f"{slug}.view.json").write_text(json.dumps(_view(2)))
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    seen = {}

    def fake_ensure(got_slug, report, view, reports):
        seen.update(slug=got_slug, report=report, view=view, reports=reports)
        return {"status": "queued", "available": True, "n_claims": 2, "n_refs": 2}

    monkeypatch.setattr(webapp.query_claim_grid, "ensure", fake_ensure)
    response = app_client.post(f"/api/query-claim-grid/{slug}")
    assert response.status_code == 200 and response.get_json()["status"] == "queued"
    assert seen["slug"] == slug and seen["reports"] == tmp_path
    assert len(seen["report"]["query_document"]["claims"]) == 2


def test_api_rejects_unsafe_claim_grid_slug(app_client):
    assert app_client.get("/api/query-claim-grid/..%2Fescape").status_code in (400, 404)
