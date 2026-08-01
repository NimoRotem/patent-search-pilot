"""Coverage-ledger agent tests: ranking (seed backbone + centrality), stop-condition, keys."""
from agent import AgentConfig, CoverageLedger, CoverageAgent


def test_search_worker_default_has_bounded_serial_fallback(monkeypatch):
    monkeypatch.delenv("AGENT_SEARCH_WORKERS", raising=False)
    assert AgentConfig().search_workers == 2
    monkeypatch.setenv("AGENT_SEARCH_WORKERS", "1")
    assert AgentConfig().search_workers == 1
    monkeypatch.setenv("AGENT_SEARCH_WORKERS", "8")
    assert AgentConfig().search_workers == 2
    monkeypatch.setenv("AGENT_SEARCH_WORKERS", "invalid")
    assert AgentConfig().search_workers == 2


def test_seed_backbone_ranks_above_element_only_finds():
    """final_score keeps the whole-query 'seed' hits at the top (agentic can't score below vector),
    while promoting central / citation-reached unique finds."""
    led = CoverageLedger(["e1", "e2"])
    led.register_families([("A", "pA", 0.9), ("B", "pB", 0.5)], bucket="seed")
    led.register_families([("C", "pC", 0.8), ("A", "pA", 0.7)], bucket="element")
    led.channel_families.setdefault("citation", set()).add("C")
    led.register_families([("C", "pC", 0.75)], channel="citation", bucket="element")
    ranked = led.ranked_families()
    assert ranked[0] == "A", "seed's strongest hit stays #1"
    assert ranked.index("B") < ranked.index("C"), "seed hit B ranks above element-only C"
    assert "C" in ranked, "unique citation find still surfaces"


def test_stop_condition_triggers_on_low_marginal_yield():
    led = CoverageLedger(["e"])
    led.round_new = [50, 40]
    assert led.should_stop(budget_calls_left=10, max_rounds_reached=False) is False
    led.round_new = [1, 0]                         # two low-yield rounds
    assert led.should_stop(budget_calls_left=10, max_rounds_reached=False) is True
    led.round_new = [50, 50]
    assert led.should_stop(budget_calls_left=0, max_rounds_reached=False) is True   # budget cap
    assert led.should_stop(budget_calls_left=10, max_rounds_reached=True) is True   # round cap


def test_decompose_returns_elements(monkeypatch):
    A = CoverageAgent.__new__(CoverageAgent)       # no Retriever needed
    els = A.decompose("A vacuum gripper with a seal and a pump", subject=None)
    assert isinstance(els, list) and len(els) >= 1
    assert all(isinstance(e, str) and e for e in els)


def test_report_has_expected_keys():
    """A minimal ledger renders a report dict with the keys the web UI + export rely on."""
    import types
    A = CoverageAgent.__new__(CoverageAgent)
    # stub the retriever bits the report touches
    A.r = types.SimpleNamespace(
        rerank_families=lambda q, fam, top=25, **kw: fam,
        family_key=lambda pid: str(pid),
    )
    A._ground = False
    led = CoverageLedger(["seal element"])
    led.register_families([("famX", "pidX", 0.7)], bucket="seed")
    led.add_evidence("seal element", {"family": "famX", "pub": "US-1", "coord": None,
                                      "kind": "abstract", "score": 0.7, "basis": "public_prior_art",
                                      "channels": ["dense"]})
    from search_modes import Mode
    rep = A.report("a seal element", subject=None, mode=Mode.NOVELTY, ledger=led, rounds=1)
    for k in ["query", "mode", "elements", "element_coverage", "element_evidence",
              "combination_view", "ranked_families", "channel_families", "round_new_families",
              "n_families", "llm_usage", "cross_encoder_rerank"]:
        assert k in rep, f"report missing key: {k}"
    assert rep["ranked_families"] == ["famX"]
    assert rep["cross_encoder_rerank"]["attempted"] is True
    assert rep["cross_encoder_rerank"]["applied"] is None


def test_independent_element_searches_use_bounded_workers(monkeypatch):
    """Every pass still runs and merges in order, but independent elements overlap in time."""
    import threading
    import time
    import types

    state = {"active": 0, "max_active": 0, "calls": [], "closed": 0}
    lock = threading.Lock()

    class FakeRetriever:
        def fork(self):
            return FakeRetriever()

        def close(self):
            with lock:
                state["closed"] += 1

        def search(self, query, **kwargs):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["calls"].append(query)
            time.sleep(0.03)
            with lock:
                state["active"] -= 1
            pid = f"pid-{query}"
            return types.SimpleNamespace(
                family_ranked=[(f"fam-{query}", pid, 0.8, {"dense": 1})],
                channel_hits={"dense": [pid]},
            )

        @staticmethod
        def family_key(pid):
            return f"family-for-{pid}"

        @staticmethod
        def rerank_families(query, fam, return_meta=False, **kwargs):
            meta = {"attempted": True, "applied": True, "scored": len(fam),
                    "requested": len(fam), "model": "fake"}
            return (fam, meta) if return_meta else fam

    agent = CoverageAgent(FakeRetriever())
    monkeypatch.setattr(agent, "decompose", lambda *a, **k: ["e1", "e2", "e3", "e4"])
    events = []
    rep = agent.run(
        "whole invention", cfg=AgentConfig(max_rounds=0, ground=False, search_workers=2),
        on_event=lambda stage, data: events.append((stage, data)),
    )

    assert state["max_active"] == 2
    assert state["closed"] == 4
    assert state["calls"][0] == "whole invention"
    assert set(state["calls"][1:]) == {"e1", "e2", "e3", "e4"}
    assert rep["n_families"] == 5
    progress = [data["search_done"] for stage, data in events if stage == "seed_progress"]
    assert progress == [2, 3, 4, 5]


def test_parallel_and_serial_searches_produce_the_same_report(monkeypatch):
    """Concurrency changes wall time only; ranking/evidence/stopping outputs stay identical."""
    import types
    import llm

    class DeterministicRetriever:
        def fork(self):
            return DeterministicRetriever()

        def close(self):
            pass

        def search(self, query, **kwargs):
            slug = query.replace(" ", "-")
            rows = [
                (f"fam-{slug}", f"pid-{slug}", 0.8, {"dense": 1}),
                ("fam-common", "pid-common", 0.5, {"citation": 1}),
            ]
            return types.SimpleNamespace(
                family_ranked=rows,
                channel_hits={"dense": [rows[0][1]], "citation": [rows[1][1]]},
            )

        @staticmethod
        def family_key(pid):
            return pid.replace("pid-", "fam-")

        @staticmethod
        def rerank_families(query, fam, return_meta=False, **kwargs):
            meta = {"attempted": True, "applied": True, "scored": len(fam),
                    "requested": len(fam), "model": "fake"}
            return (fam, meta) if return_meta else fam

    def run(workers):
        agent = CoverageAgent(DeterministicRetriever())
        monkeypatch.setattr(agent, "decompose", lambda *a, **k: ["e1", "e2", "e3"])
        monkeypatch.setattr(agent, "plan", lambda el, ledger: {
            "queries": [f"{el} q1", f"{el} q2"], "synonyms": [], "de": "",
        })
        return agent.run(
            "whole", cfg=AgentConfig(max_rounds=1, elements_per_round=2,
                                     ground=False, search_workers=workers))

    # Simulate a long-lived worker which already served several searches. The process total must
    # not make either new job start with an exhausted per-search budget.
    monkeypatch.setattr(
        llm, "_usage", {"calls": 100, "prompt_tokens": 1000, "completion_tokens": 100})
    serial = run(1)
    parallel = run(2)
    assert serial["rounds"] == parallel["rounds"] == 1
    keys = (
        "rounds", "n_families", "element_coverage", "element_evidence",
        "ranked_families", "channel_families", "round_new_families",
        "cross_encoder_rerank",
    )
    assert {key: serial[key] for key in keys} == {key: parallel[key] for key in keys}
