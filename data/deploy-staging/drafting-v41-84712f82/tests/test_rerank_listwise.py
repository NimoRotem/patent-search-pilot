"""Tests for the listwise agentic reranker (task C).

Covers the two invariants that matter for correctness: (1) the output is always a PERMUTATION of
the input — reranking reorders, never adds or drops a candidate — and (2) the deterministic
FALLBACK path preserves the incoming order on any LLM failure/garbage. Also checks the call
bound, channel-agnostic ranking, depth cap, and that matched passage text (not the title) drives
the order. No paid APIs: a fake chat_fn is injected everywhere.
"""
import re
import rerank_listwise as R


def _mk(i, kw="misc"):
    return {"family": f"F{i}", "pub": f"US-{i}-A1", "title": f"patent {i} {kw}",
            "abstract": f"abstract {i} about {kw}", "assignees": [f"Co{i}"],
            "publication_date": "2020-01-01", "channels": ["dense" if i % 2 else "pqai"],
            "claims": [{"claim_no": 1, "independent": True, "text": f"a {kw} apparatus {i}"}]}


def _batch_blocks(user):
    """Parse the prompt into {batch_id: full multi-line block text} — a candidate's block spans
    its '[n] ...' line plus following continuation lines (e.g. 'matched text: ...')."""
    blocks, cur, cid = {}, [], None
    for l in user.splitlines():
        m = re.match(r"\[(\d+)\]", l)
        if m:
            if cid is not None:
                blocks[cid] = "\n".join(cur)
            cid, cur = int(m.group(1)), [l]
        elif cid is not None:
            cur.append(l)
    if cid is not None:
        blocks[cid] = "\n".join(cur)
    return blocks


def _batch_ids(user):
    return sorted(_batch_blocks(user).keys())


def _fake_prefer(keyword):
    """A fake LLM that ranks a window putting candidates whose FULL block (title + matched
    passage text) mentions `keyword` first (stable by id) — judging on the passage like the
    real model, not just the title."""
    def chat(system, user, max_tokens=400):
        scored = []
        for idn, block in _batch_blocks(user).items():
            scored.append((0 if keyword.lower() in block.lower() else 1, idn))
        scored.sort()
        return {"order": [i for _, i in scored]}
    return chat


def test_permutation_invariant_basic():
    cands = [_mk(i, "vacuum" if i % 3 == 0 else "misc") for i in range(25)]
    cfg = R.ListwiseConfig(window_size=8, overlap=3, passes=2, depth=None)
    out = R.listwise_rerank({"brief": "vacuum gripper", "elements": ["vacuum"]}, cands,
                            cfg=cfg, chat_fn=_fake_prefer("vacuum"))
    assert sorted(c["family"] for c in out) == sorted(c["family"] for c in cands)
    assert len(out) == len(cands)


def test_reorders_meaningfully_by_matched_text():
    cands = [_mk(i, "vacuum" if i % 3 == 0 else "misc") for i in range(25)]
    in_ids = [c["family"] for c in cands]
    cfg = R.ListwiseConfig(window_size=8, overlap=3, passes=2, depth=None)
    out = R.listwise_rerank({"brief": "vacuum gripper"}, cands, cfg=cfg,
                            chat_fn=_fake_prefer("vacuum"))
    out_ids = [c["family"] for c in out]
    assert out_ids != in_ids                       # it actually moved things
    vac = [c["family"] for c in cands if "vacuum" in c["title"]]
    assert set(out_ids[:len(vac)]) == set(vac)      # relevant art bubbled to the front


def test_fallback_on_empty_llm_preserves_order():
    cands = [_mk(i) for i in range(12)]
    in_ids = [c["family"] for c in cands]
    out = R.listwise_rerank("q", cands, chat_fn=lambda s, u, **k: {})
    assert [c["family"] for c in out] == in_ids


def test_fallback_on_exception_preserves_order():
    cands = [_mk(i) for i in range(12)]
    in_ids = [c["family"] for c in cands]

    def boom(s, u, **k):
        raise RuntimeError("llm down")
    out = R.listwise_rerank("q", cands, chat_fn=boom)
    assert [c["family"] for c in out] == in_ids


def test_garbage_and_partial_output_still_permutation():
    cands = [_mk(i) for i in range(15)]

    def messy(system, user, **k):
        ids = _batch_ids(user)
        return {"order": [ids[0], ids[0], 999, "x", ids[1]]}  # dup, out-of-range, non-int, partial
    out = R.listwise_rerank("q", cands, chat_fn=messy)
    assert sorted(c["family"] for c in out) == sorted(c["family"] for c in cands)


def test_never_drops_candidates_across_windows():
    cands = [_mk(i) for i in range(40)]
    cfg = R.ListwiseConfig(window_size=6, overlap=2, passes=3, depth=None)
    out = R.listwise_rerank("q", cands, cfg=cfg, chat_fn=_fake_prefer("misc"))
    assert len(out) == 40
    assert sorted(c["family"] for c in out) == sorted(c["family"] for c in cands)


def test_depth_cap_preserves_tail_and_permutation():
    cands = [_mk(i, "vacuum" if i % 2 else "misc") for i in range(25)]
    in_ids = [c["family"] for c in cands]
    cfg = R.ListwiseConfig(window_size=5, overlap=2, passes=1, depth=10)
    out = R.listwise_rerank({"brief": "vacuum"}, cands, cfg=cfg, chat_fn=_fake_prefer("vacuum"))
    out_ids = [c["family"] for c in out]
    assert out_ids[10:] == in_ids[10:]             # tail beyond depth untouched
    assert sorted(out_ids) == sorted(in_ids)


def test_call_bound_respected():
    cands = [_mk(i) for i in range(30)]
    n = [0]

    def counting(s, u, **k):
        n[0] += 1
        return {}
    cfg = R.ListwiseConfig(window_size=6, overlap=2, passes=3, depth=None, max_llm_calls=4)
    R.listwise_rerank("q", cands, cfg=cfg, chat_fn=counting)
    assert n[0] <= 4
    assert R.plan_calls(len(cands), cfg) == 4


def test_plan_calls_matches_actual():
    cands = [_mk(i) for i in range(25)]
    n = [0]

    def counting(s, u, **k):
        n[0] += 1
        return {}
    cfg = R.ListwiseConfig(window_size=8, overlap=3, passes=2, depth=None)
    R.listwise_rerank("q", cands, cfg=cfg, chat_fn=counting)
    assert n[0] == R.plan_calls(len(cands), cfg)


def test_channel_agnostic_ranks_image_and_api_hits_uniformly():
    # candidates from different channels; channel must not be a ranking signal
    api = {"family": "A", "title": "clamp", "channels": ["pqai"],
           "abstract": "a vacuum suction gripper for stone slabs"}
    img = {"family": "B", "title": "drawing match", "channels": ["image"],
           "abstract": "a vacuum suction gripper diagram"}
    sem = {"family": "C", "title": "irrelevant", "channels": ["dense"],
           "abstract": "a bicycle chain lubricant"}
    out = R.listwise_rerank({"brief": "vacuum suction gripper"}, [sem, api, img],
                            chat_fn=_fake_prefer("vacuum"))
    ids = [c["family"] for c in out]
    assert set(ids) == {"A", "B", "C"}
    assert ids[-1] == "C"                            # the off-topic one sinks regardless of channel


def test_singleton_and_empty():
    assert R.listwise_rerank("q", [], chat_fn=lambda s, u, **k: {}) == []
    one = [_mk(0)]
    assert R.listwise_rerank("q", one, chat_fn=lambda s, u, **k: {}) == one


def test_matched_text_pulled_not_title():
    # the matched passage must reach the prompt (title-only reasoning is the defect we avoid)
    c = _mk(7, "vacuum")
    txt = R._brief(c, R.ListwiseConfig())
    assert "matched text:" in txt
    assert "apparatus 7" in txt                     # claim text present, not just the title


def test_rerank_report_cards_rewrites_rank():
    cards = [dict(_mk(i, "vacuum" if i % 4 == 0 else "misc"), rank=i + 1) for i in range(12)]
    out = R.rerank_report_cards({"brief": "vacuum"}, cards, chat_fn=_fake_prefer("vacuum"))
    assert [c["rank"] for c in out] == list(range(1, 13))   # ranks renumbered 1..n
    assert sorted(c["family"] for c in out) == sorted(c["family"] for c in cards)
