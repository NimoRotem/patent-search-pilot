"""Coverage-ledger agent tests: ranking (seed backbone + centrality), stop-condition, keys."""
from agent import CoverageLedger, CoverageAgent


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
