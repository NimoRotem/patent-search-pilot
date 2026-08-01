"""The claim-search control changes retrieval, not just the label shown in the UI."""

import agent
import retrieval


def test_claim_dense_and_lexical_channels_restrict_chunk_kind(monkeypatch):
    r = object.__new__(retrieval.Retriever)
    seen = []

    def capture(sql, params, cap=retrieval.PUB_CAP):
        seen.append(sql)
        return []

    monkeypatch.setattr(r, "_pubs_from_chunks", capture)
    r.channel_claim_dense([0.1, 0.2], None, None)
    r.channel_claim_bm25("swappable RFID base plate", None, None)
    assert len(seen) == 2
    assert all("claim_own" in sql and "claim_resolved" in sql for sql in seen)
    assert "c.kind IN" in seen[0] and "c.kind IN" in seen[1]
    assert "LIMIT 4" in seen[1] and "' & '" in seen[1]
    assert "max(ts_rank_cd" in seen[1]


def test_agent_passes_claim_config_to_every_retrieval(monkeypatch):
    called = []

    class FakeRetriever:
        def search(self, query, **kwargs):
            called.append(kwargs["config"])
            return retrieval.Result([], [], {}, query)

    a = agent.CoverageAgent(FakeRetriever())
    a._search_config = "claim_agentic"
    a._ground = False
    a._fetch_search("RFID identified plate", None, "novelty")
    assert called == ["claim_agentic"]


def test_claim_rrf_uses_claim_dense_floor():
    out = retrieval.Retriever.rrf({"claim_dense": [(1, 0.9)], "claim_bm25": [(2, 4)]})
    assert out[0][0] == 1
    assert "claim_dense" in out[0][2]


def test_retriever_accepts_explicit_channel_sequence(monkeypatch):
    r = object.__new__(retrieval.Retriever)
    r._fam = {7: "F7"}
    monkeypatch.setattr(retrieval.embed, "embed_query", lambda *_args, **_kwargs: [0.1])
    monkeypatch.setattr(r, "channel_claim_dense", lambda *_args: [(7, 0.9)])
    out = r.search("RFID plate", config=["claim_dense"], do_rerank=False)
    assert out.channel_hits == {"claim_dense": [7]}
    assert out.family_ranked[0][0] == "F7"


def test_claim_search_emits_dense_partial_before_lexical_expansion(monkeypatch):
    a = agent.CoverageAgent.__new__(agent.CoverageAgent)
    monkeypatch.setattr(a, "decompose", lambda text, subject: ["RFID plate"])
    calls = []

    def run_search(query, subject, mode, ledger, element=None, cfg="agentic", **kwargs):
        calls.append(cfg)
        n = len(ledger.families_seen) + 1
        ledger.register_families([(f"F{n}", n, 0.8)],
                                 bucket="seed" if kwargs.get("is_seed") else "element")
        return retrieval.Result([], [], {}, query), 1

    monkeypatch.setattr(a, "_run_search", run_search)
    monkeypatch.setattr(a, "report", lambda q, s, m, ledger, rounds, **kw: {
        "ranked_families": ledger.ranked_families(), "elements": ledger.elements})
    events = []
    a.run("RFID identified base plate", mode="novelty",
          cfg=agent.AgentConfig(mode="novelty", max_rounds=0, ground=False,
                                search_config="claim_agentic"),
          on_event=lambda stage, data: events.append(stage))
    assert calls[0] == ["claim_dense"]
    assert calls[1] == ["claim_bm25", "cpc", "citation", "qbe"]
    assert events.index("partial") < events.index("search_progress", 2)
