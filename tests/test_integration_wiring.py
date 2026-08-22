"""Integration-layer tests: the wiring that connects the parallel workstreams to the hardened
webapp. These cover the seams BETWEEN modules, which no single agent's suite could test."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import webapp
import webview
import search_modes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TRUST_LOOPBACK", "1")
    webapp.app.config["TESTING"] = True
    auth.reset_limits()
    with webapp.app.test_client() as c:
        yield c


# --------------------------------------------------------------------------- mode allowlist
def test_unavailable_mode_is_refused_not_silently_downgraded(client):
    """Before this wiring, mode=invalidity reached the pipeline and got novelty dates back,
    labelled as an invalidity opinion. It must now be refused at the API boundary."""
    r = client.post("/run", data={"query": "a vacuum gripper", "mode": "invalidity"})
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"] == "mode_not_available"
    assert body["mode"] == "invalidity"
    assert body["detail"]


def test_fto_is_refused_rather_than_500(client):
    r = client.post("/run", data={"query": "a vacuum gripper", "mode": "fto"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "mode_not_available"


def test_unknown_mode_is_rejected(client):
    r = client.post("/run", data={"query": "a vacuum gripper", "mode": "banana"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "unknown_mode"


def test_available_modes_still_pass_validation():
    for m in ("novelty", "inventive_step"):
        assert search_modes.require_available(m).value == m


def test_api_modes_reports_capabilities(client):
    r = client.get("/api/modes")
    assert r.status_code == 200
    modes = {m["mode"]: m for m in r.get_json()["modes"]}
    assert modes["novelty"]["available"] is True
    assert modes["invalidity"]["available"] is False
    assert modes["fto"]["available"] is False


# --------------------------------------------------------------------------- wide slug
def test_wide_and_narrow_get_different_slugs():
    """A wide result must not overwrite the narrow one's cached report."""
    q, mode = "a suction cup lifter", "novelty"
    assert webapp.slugify(q + "|" + mode) != webapp.slugify(q + "|" + mode + "|wide")


def test_wide_is_threaded_through_to_generate(monkeypatch):
    seen = {}

    def fake_generate(slug, query, subject, mode, wide=False):
        seen["wide"] = wide

    monkeypatch.setattr(webapp, "_generate", fake_generate)
    webapp._run_job("s1", "q", None, "novelty", gated=False, wide=True)
    assert seen["wide"] is True


def test_federation_is_never_implicit(monkeypatch):
    """wide=False must not trigger a paid federated call."""
    called = []
    monkeypatch.setattr(webapp.federation, "search",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr(webapp.domain_detect, "detect",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no detect")))

    class FakeAgent:
        def __init__(self, r): pass
        def run(self, *a, **k): return {"elements": [], "ranked_families": []}

    monkeypatch.setattr(webapp, "CoverageAgent", FakeAgent)
    monkeypatch.setattr(webapp, "retriever", lambda: None)
    monkeypatch.setattr(webapp, "_write_report", lambda slug, rep: None)
    webapp._generate("s-narrow", "q", None, "novelty", wide=False)
    assert called == []


def test_detector_failure_does_not_break_generation(monkeypatch):
    """A domain-detector exception must cost the user nothing."""
    monkeypatch.setattr(webapp.domain_detect, "detect",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    written = {}

    class FakeAgent:
        def __init__(self, r): pass
        def run(self, *a, **k): return {"elements": ["e"], "ranked_families": []}

    monkeypatch.setattr(webapp, "CoverageAgent", FakeAgent)
    monkeypatch.setattr(webapp, "retriever", lambda: None)
    monkeypatch.setattr(webapp, "_write_report", lambda slug, rep: written.update(rep))
    webapp._generate("s-detfail", "q", None, "novelty")
    assert written["partial"] is False
    assert written["domain"] is None


def test_generation_records_candidates_against_the_run_slug(monkeypatch, tmp_path):
    recorded = []
    report_file = tmp_path / "candidate-ledger.json"

    class BoundRun:
        run_id = "run-candidate-ledger"

    class FakeAgent:
        def __init__(self, retriever):
            pass

        def run(self, *args, **kwargs):
            return {"elements": ["e"], "ranked_families": [
                {"pub": "US-123-A1", "score": 0.75},
            ]}

    class FakeTrace:
        def write(self, path):
            return path

        def unknown(self):
            return []

        def rows(self):
            return []

        def counts(self):
            return {}

    def write_report(slug, report):
        report_file.write_text(json.dumps(report), encoding="utf-8")

    monkeypatch.setattr(webapp, "CoverageAgent", FakeAgent)
    monkeypatch.setattr(webapp, "retriever", lambda: None)
    monkeypatch.setattr(webapp.accounts, "mark_search_running", lambda slug: None)
    monkeypatch.setattr(webapp.domain_detect, "detect", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp.deep_rank, "run", lambda *args, **kwargs: {})
    monkeypatch.setattr(webapp, "_attach_prosecution", lambda report, slug: None)
    monkeypatch.setattr(webapp, "_attach_disclosures", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp, "_drop_self_family", lambda report: report)
    monkeypatch.setattr(webapp, "_build_view_cached", lambda slug, report: {"cards": []})
    monkeypatch.setattr(webapp, "_write_report", write_report)
    monkeypatch.setattr(webapp, "_write_json_atomic", lambda path, payload: None)
    monkeypatch.setattr(webapp, "_write_detail_preview", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp, "_schedule_background_report_analysis",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp.prefetch, "prefetch_top", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp.query_claim_grid, "ensure", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp.trace, "from_report", lambda *args, **kwargs: FakeTrace())
    monkeypatch.setattr(webapp, "report_path", lambda slug: report_file)
    monkeypatch.setattr(webapp.manifest, "start", lambda *args, **kwargs: {})
    monkeypatch.setattr(webapp.manifest, "finish", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp.run_stats, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp.runctx, "current", lambda slug: BoundRun())
    monkeypatch.setattr(webapp.runctx, "stage_payload", lambda slug, stage: None)
    monkeypatch.setattr(webapp.runctx, "checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp.runctx, "check_lease", lambda slug: None)
    monkeypatch.setattr(webapp.runctx, "event", lambda slug, payload: None)
    monkeypatch.setattr(
        webapp.runctx, "note_candidates",
        lambda slug, candidates: recorded.append((slug, list(candidates))),
    )

    webapp._generate("candidate-ledger", "q", None, "novelty")

    assert [slug for slug, candidates in recorded] == ["candidate-ledger", "candidate-ledger"]
    assert [candidates[0]["pub"] for slug, candidates in recorded] == [
        "US-123-A1", "US-123-A1",
    ]
    assert [candidates[0]["stage"] for slug, candidates in recorded] == ["fused", "final"]


def test_federate_block_survives_federation_outage(monkeypatch):
    monkeypatch.setattr(webapp.federation, "search",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    block = webapp._federate_block("q", "novelty")
    assert block["ok"] is False
    assert block["hits"] == []


# --------------------------------------------------------------------------- view passthrough
def test_build_view_passes_new_keys_through():
    """build_view uses an explicit key allowlist; domain/federation must be in it or the
    template silently renders nothing."""
    import inspect
    src = inspect.getsource(webview.build_view)
    for key in ('"domain"', '"federation"', '"federation_offered"'):
        assert key in src, key


# --------------------------------------------------------------------------- auth + limits
def test_new_expensive_routes_are_auth_gated():
    """Anything not explicitly open must sit behind the auth gate."""
    for ep in ("api_chart", "api_translate", "api_modes", "api_federation_health"):
        assert ep not in auth._OPEN_ENDPOINTS


def test_new_spending_routes_are_rate_limited():
    """The gate keys limits by endpoint NAME — a missing entry means unthrottled Vertex spend."""
    for ep in ("api_chart", "api_translate"):
        assert auth.limiter_for(ep) is not None, ep


def test_cheap_routes_stay_unlimited():
    assert auth.limiter_for("api_modes") is None
    assert auth.limiter_for("status") is None


def test_new_routes_are_registered():
    rules = {r.endpoint for r in webapp.app.url_map.iter_rules()}
    for ep in ("api_chart", "api_translate", "api_modes", "api_federation_health"):
        assert ep in rules, ep


def test_chart_rejects_unsafe_pub(client):
    assert client.get("/api/chart/..%2f..%2fetc%2fpasswd").status_code in (404, 400)


def test_chart_without_a_report_is_a_clean_400(client):
    r = client.get("/api/chart/US-1234567-B2?slug=does-not-exist")
    assert r.status_code == 400
    assert "elements" in r.get_json()["error"]
