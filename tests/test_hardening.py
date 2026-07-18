"""M8 adversarial regression tests — lock in the edge-case fixes found by breaking the product."""
import threading, time
import pytest


# ---- retrieval: degenerate queries (empty query used to crash on the embedding call) --------
def test_empty_query_returns_no_results_not_crash():
    from retrieval import Retriever
    R = Retriever()
    for q in ["", "   \n\t ", None]:
        res = R.search(q or "", subject=None, mode="novelty", config="hybrid", topk=50)
        assert res.family_ranked == []


def test_embed_query_handles_empty(monkeypatch):
    import embed
    # unmock: exercise the real guard (returns a full vector, no API error)
    monkeypatch.undo()
    v = embed.embed_query("")
    assert isinstance(v, list) and len(v) == 768


@pytest.mark.parametrize("q", ["gripper", "!@#$%^&*()", "真空吸盘", "'; DROP TABLE x; --",
                               "vacuum " * 700, "US-11999030-B2"])
def test_degenerate_queries_dont_crash(q):
    # vector config exercises the crash-prone embed + dense path without BM25's per-query cost
    from retrieval import Retriever
    res = Retriever().search(q, subject=None, mode="novelty", config="vector", topk=20)
    assert isinstance(res.family_ranked, list)


# ---- agent: malformed LLM responses must not crash the decompose/plan ------------------------
@pytest.mark.parametrize("resp", [{}, None, {"elements": None}, {"elements": []},
                                  {"elements": [1, None, ""]}, {"garbage": 1}])
def test_decompose_survives_malformed_llm(resp, monkeypatch):
    import llm, agent
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: resp)
    A = agent.CoverageAgent.__new__(agent.CoverageAgent)
    els = A.decompose("an invention", None)
    assert isinstance(els, list) and len(els) >= 1 and all(isinstance(e, str) and e for e in els)


@pytest.mark.parametrize("resp", [{}, None, {"synonyms": None, "queries": None}])
def test_plan_survives_malformed_llm(resp, monkeypatch):
    import llm, agent
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: resp)
    A = agent.CoverageAgent.__new__(agent.CoverageAgent)
    plan = A.plan("an element", agent.CoverageLedger(["e"]))
    assert isinstance(plan, dict)


def test_stop_condition_always_terminates():
    from agent import CoverageLedger
    led = CoverageLedger(["e"]); led.round_new = [999] * 9
    assert led.should_stop(999, max_rounds_reached=True) is True     # round cap
    assert led.should_stop(0, max_rounds_reached=False) is True       # budget cap


# ---- reranker: best-effort, never fatal ----------------------------------------------------
def test_rerank_empty_and_failure_are_safe(monkeypatch):
    import rerank
    assert rerank.rerank("q", []) == []
    class Boom:
        def compute_score(self, *a, **k): raise RuntimeError("Already borrowed")
    monkeypatch.setattr(rerank, "_model", Boom())
    monkeypatch.setattr(rerank, "_failed", False)
    out = rerank.rerank("q", ["a", "b", "c"])       # must not raise
    assert len(out) == 3


def test_rerank_families_empty_is_safe():
    from retrieval import Retriever
    assert Retriever().rerank_families("q", [], top=25) == []


# ---- generation lock: concurrent same-query must not double-run -----------------------------
def test_generation_lock_prevents_double_run(monkeypatch):
    import webapp
    starts = []
    def fake_gen(slug, query, subject, mode):
        starts.append(slug); time.sleep(0.25)
        with webapp._JOB_LOCK:
            webapp._JOBS[slug] = {"status": "done", "msg": "done"}
    monkeypatch.setattr(webapp, "_generate", fake_gen)
    slug = "unit-race-test"
    webapp.report_path(slug).unlink(missing_ok=True)
    webapp._JOBS.pop(slug, None)
    ts = [threading.Thread(target=lambda: webapp.ensure_report(slug, query="x", mode="novelty"))
          for _ in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    time.sleep(0.4)
    assert len(starts) == 1, f"double-run: started {len(starts)} times"
