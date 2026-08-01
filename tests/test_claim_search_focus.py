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
