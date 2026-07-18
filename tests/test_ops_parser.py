"""EPO OPS parser test — proves the OPS response parses into the schema shape without credentials
(wraps the standalone src/test_ops.py so it runs under pytest too)."""
import ops
import patent_text as pt


def test_ops_mock_fetch_parses_all_sections():
    d = ops.ops_fetch("EP-2496850-A1", mock=True)
    assert d["source"] == "mock"
    assert len(d["paragraphs"]) == 6 and d["paragraphs"][0]["para_no"] == "0001"
    assert len(d["claims"]) == 5 and d["claims_lang"] == "en"
    assert len(d["images"]) >= 1 and d["images"][0]["link"].startswith("published-data/images/")
    assert len(d["legal_events"]) == 3 and d["legal_events"][0]["code"] == "PG25"


def test_ops_claims_resolve_dependencies():
    d = ops.ops_fetch("EP-2496850-A1", mock=True)
    resolved = pt.resolve_claims(pt.split_claims("\n".join(c["text"] for c in d["claims"])))
    indep = [c for c in resolved if c["is_independent"]]
    assert len(indep) == 1, "one independent claim (claim 1)"
    c5 = next(c for c in resolved if c["claim_no"] == 5)          # "any of the preceding claims"
    assert not c5["is_independent"] and len(c5["resolved_text"]) > len(c5["text"])
