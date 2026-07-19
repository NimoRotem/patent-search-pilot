"""Smoke test for the EPO OPS parser + schema mapping — runs WITHOUT credentials.
Invoked by regression.sh; the thorough per-bug regressions live in tests/test_ops_parser.py.

NOTE ON THE EXPECTED VALUES: these were rewritten when the hand-written fixtures were replaced
with verbatim live OPS captures (src/ops_samples/real_*.xml). The previous numbers (6 paragraphs,
5 claims, 3 legal events) described a synthetic mock that did NOT match the real wire format, so
they asserted that three genuine parser bugs were absent when they were in fact present. The
numbers below are what the real EP-2496850-A1 response actually contains.
"""
import ops, patent_text as pt


def test_parsers():
    d = ops.ops_fetch("EP-2496850-A1", mock=True)
    assert d["source"] == "mock", d["source"]

    # description -> paragraphs with coordinates. The real EP description is German (this family
    # publishes DE/FR/EN), which is itself the point: the parser must not mislabel language.
    assert len(d["paragraphs"]) == 47, f"paras={len(d['paragraphs'])}"
    assert d["paragraphs"][0]["para_no"] == "0001"
    assert "saugnapf" in d["paragraphs"][0]["text"].lower()

    # claims -> parsed. Regression for bug #2: a claims response carries one <claims lang=XX>
    # block PER LANGUAGE, and every claim of a language sits under a SINGLE <claim> element as
    # many <claim-text> children. Parsing by <claim> yielded 3 mega-blobs with all three
    # languages mixed and mislabelled "en"; correct parsing yields 9 numbered English claims.
    assert len(d["claims"]) == 9, f"claims={len(d['claims'])}"
    assert d["claims_lang"] == "en"
    assert all(c["text"].strip() for c in d["claims"]), "no empty claim text"
    resolved = pt.resolve_claims(pt.split_claims("\n".join(c["text"] for c in d["claims"])))
    indep = [c for c in resolved if c["is_independent"]]
    assert len(indep) >= 1, f"expected at least one independent claim, got {len(indep)}"
    deps = [c for c in resolved if not c["is_independent"] and c["parents"]]
    assert deps, "expected at least one dependent claim with a resolved parent"
    assert len(deps[0]["resolved_text"]) > len(deps[0]["text"]), "resolved text should inherit"

    # images -> drawing instances
    assert len(d["images"]) >= 1, "expected drawing instances"
    assert any("DRAWINGS" in (im.get("sections") or []) for im in d["images"])
    assert d["images"][0]["link"].startswith("published-data/images/")

    # legal -> events. Regression for bug #3: INPADOC carries code/desc as ATTRIBUTES of <legal>,
    # not child elements, so the old parser returned 0 events for every real response.
    assert len(d["legal_events"]) == 82, f"legal={len(d['legal_events'])}"
    ev = d["legal_events"][0]
    assert ev["code"] == "17P", ev["code"]
    assert ev["date"] == "2012-06-06", ev["date"]
    assert ev["desc"], "legal event must carry a description"

    print("ops parser test: PASS")
    print(f"  paragraphs={len(d['paragraphs'])}  claims={len(d['claims'])} "
          f"(indep={len(indep)})  drawings={len(d['images'])}  legal_events={len(d['legal_events'])}")
    return True


if __name__ == "__main__":
    test_parsers()
