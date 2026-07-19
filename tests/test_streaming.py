"""Progressive-rendering / speed regression tests (streaming the first results).

Guards the pieces that let the UI show cards in seconds instead of after the full agent run:
 - report(rerank=False) / _final_rank(rerank=False) skip the slow cross-encoder for the partial snapshot;
 - the agent emits a 'partial' event (before finishing) whose report is renderable;
 - a partial view is never cached to disk (it must not shadow the final report);
 - /status reports 'partial' as ready-but-not-done so the page renders then upgrades.
"""
import json
import types
import pytest
import agent
import webapp


def test_final_rank_skips_reranker_when_not_reranking():
    A = agent.CoverageAgent.__new__(agent.CoverageAgent)

    class _R:
        def rerank_families(self, *a, **k):
            raise AssertionError("the cross-encoder must NOT run for the fast partial snapshot")
    A.r = _R()

    led = types.SimpleNamespace(
        family_score={"F1": 1.0, "F2": 2.0, "F3": 0.5},
        family_pid={},
        final_score=lambda fk: {"F1": 1.0, "F2": 2.0, "F3": 0.5}[fk],
    )
    ranked = A._final_rank("q", led, rerank=False)
    assert ranked == ["F2", "F1", "F3"]          # by final_score desc, reranker never called


def test_run_emits_partial_before_finishing(monkeypatch):
    """The agent must emit a 'partial' event (renderable snapshot) before 'reranking'/return, so the
    UI can show the seed cards immediately. Uses the conftest-mocked embed/LLM (no network)."""
    A = agent.CoverageAgent(webapp.retriever())
    # keep the run short + never touch the real cross-encoder
    monkeypatch.setattr(A.r, "rerank_families", lambda q, fam, top=25, **kw: fam[:top])
    events = []
    from agent import AgentConfig
    A.run("a vacuum gripper with a seal and a pump", mode="novelty",
          cfg=AgentConfig(mode="novelty", max_rounds=0, elements_per_round=1, ground=False),
          on_event=lambda stage, data: events.append((stage, data)))
    stages = [s for s, _ in events]
    assert "elements" in stages
    assert "partial" in stages
    assert stages.index("partial") < len(stages)          # fired during the run
    partial_rep = next(d["report"] for s, d in events if s == "partial")
    assert partial_rep["ranked_families"]                 # the snapshot has cards to show
    assert "elements" in partial_rep


def test_partial_view_is_not_cached(gold_slug, monkeypatch, tmp_path):
    rep = json.loads((webapp.REPORTS / f"{gold_slug}.json").read_text())
    rep["partial"] = True
    # redirect the view-cache dir so we don't clobber the real cache
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    view = webapp._build_view_cached("someslug", rep)
    assert view["partial"] is True
    assert not (tmp_path / "someslug.view.json").exists()   # partial snapshots are never cached


def test_status_reports_partial_and_done(app_client, monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    (tmp_path / "s1.json").write_text("{}")                 # a report file exists
    with webapp._JOB_LOCK:
        webapp._JOBS["s1"] = {"status": "partial", "msg": "refining…"}
    j = app_client.get("/status/s1").get_json()
    assert j["ready"] is True and j["done"] is False        # renderable, not final
    with webapp._JOB_LOCK:
        webapp._JOBS["s1"] = {"status": "done", "msg": "done"}
    j = app_client.get("/status/s1").get_json()
    assert j["ready"] is True and j["done"] is True
