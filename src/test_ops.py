"""Unit test for the EPO OPS parser + schema mapping — runs WITHOUT credentials (Milestone 6 §1).
Proves ops_fetch parses a representative OPS response into the schema shape."""
import ops, patent_text as pt


def test_parsers():
    d = ops.ops_fetch("EP-2496850-A1", mock=True)
    assert d["source"] == "mock", d["source"]

    # description -> paragraphs with coordinates
    assert len(d["paragraphs"]) == 6, f"paras={len(d['paragraphs'])}"
    assert d["paragraphs"][0]["para_no"] == "0001"
    assert "suction cup" in d["paragraphs"][0]["text"].lower()

    # claims -> parsed, with correct dependency resolution
    assert len(d["claims"]) == 5, f"claims={len(d['claims'])}"
    assert d["claims_lang"] == "en"
    blob = "\n".join(f'{c["claim_no"]}. {c["text"].split(".",1)[-1].strip()}' for c in d["claims"])
    resolved = pt.resolve_claims(pt.split_claims("\n".join(c["text"] for c in d["claims"])))
    indep = [c for c in resolved if c["is_independent"]]
    assert len(indep) == 1, f"expected 1 independent claim, got {len(indep)}"     # claim 1
    dep5 = next(c for c in resolved if c["claim_no"] == 5)                          # "any preceding"
    assert not dep5["is_independent"] and dep5["parents"], "claim 5 should depend on preceding"
    assert len(dep5["resolved_text"]) > len(dep5["text"]), "resolved text should inherit"

    # images -> drawing instances
    assert len(d["images"]) >= 1, "expected drawing instances"
    assert any("DRAWINGS" in (im.get("sections") or []) for im in d["images"])
    assert d["images"][0]["link"].startswith("published-data/images/")

    # legal -> events
    assert len(d["legal_events"]) == 3, f"legal={len(d['legal_events'])}"
    assert d["legal_events"][0]["code"] == "PG25"
    assert d["legal_events"][0]["date"] == "2016-07-27"

    print("ops parser test: PASS")
    print(f"  paragraphs={len(d['paragraphs'])}  claims={len(d['claims'])} "
          f"(indep={len(indep)})  drawings={len(d['images'])}  legal_events={len(d['legal_events'])}")
    return True


if __name__ == "__main__":
    test_parsers()
