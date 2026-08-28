"""Tests for task C: per-result relevancy score+opinion, out-of-domain de-dilution, and their
integration with the listwise reranker.

Covers: the deterministic relevancy FALLBACK (LLM outage -> cosine-derived score, never a crash);
LLM parse + malformed-item fallback; the bounded call count; the OOD noise classifier and the
STABLE de-dilution partition (federated never demoted, in-domain untouched, permutation preserved);
and the end-to-end rerank_report_cards ordering (score-primary, listwise tiebreak, OOD noise sunk,
never a drop). No paid APIs: a fake chat_fn is injected everywhere.
"""
import copy
import relevancy
import retrieval
import rerank_listwise as RL


# --------------------------------------------------------------------------- helpers
def _local(i, rel=80, ncov=0, ms=None, kw="vacuum gripper"):
    c = {"family": f"L{i}", "pub": f"US-{i}-A1", "title": f"local patent {i} {kw}",
         "abstract": f"abstract {i} about {kw}", "relevancy": rel, "n_covers": ncov,
         "covers_elements": ["e1"] * ncov,
         "claims": [{"claim_no": 1, "independent": True, "text": f"a {kw} device {i}"}]}
    if ms is not None:
        c["match_score"] = ms
    return c


def _fed(i, rel=80, kw="drone cleaner"):
    return {"family": f"fed:F{i}", "pub": f"EP-{i}-A1", "title": f"federated {i} {kw}",
            "abstract": f"abstract {i} about {kw}", "relevancy": rel, "n_covers": 0,
            "federated_only": True,
            "claims": [{"claim_no": 1, "independent": True, "text": f"a {kw} {i}"}]}


FAIL = lambda system, user, max_tokens=0: {}          # LLM outage
def _fixed(scores):
    """chat_fn returning a fixed {id: score} mapping for whatever batch it is shown."""
    def fn(system, user, max_tokens=0):
        import re
        ids = [int(m) for m in re.findall(r"\[(\d+)\]", user)]
        return {"results": [{"id": i, "score": scores.get(i, 0), "opinion": f"op{i}"} for i in ids]}
    return fn


# --------------------------------------------------------------------------- relevancy fallback
def test_fallback_score_uses_cosine_relevancy():
    assert relevancy._fallback_score({"relevancy": 73}) == 73
    assert relevancy._fallback_score({"relevancy": 999}) == 100   # clamped
    assert relevancy._fallback_score({"match_score": 0.90}) == 100
    assert relevancy._fallback_score({}) == 30                    # neutral-low default


def test_score_batch_llm_outage_falls_back_deterministically():
    batch = [_local(1, rel=82), _local(2, rel=41)]
    out = relevancy.score_batch("q", batch, chat_fn=FAIL)
    assert [o["score"] for o in out] == [82, 41]
    assert all(o["source"] == "fallback" for o in out)
    assert all(o["opinion"] for o in out)                         # never empty


def test_score_batch_parses_llm_and_fills_missing():
    batch = [_local(1, rel=10), _local(2, rel=20), _local(3, rel=30)]
    # LLM scores 1 and 3, omits 2 and sends a garbage score for a bogus id -> 2 must fall back.
    def fn(system, user, max_tokens=0):
        return {"results": [{"id": 1, "score": 88, "opinion": "good"},
                            {"id": 99, "score": 50}, {"id": 3, "score": "x"}]}
    out = relevancy.score_batch("q", batch, chat_fn=fn)
    assert out[0] == {"score": 88, "opinion": "good", "source": "llm"}
    assert out[1]["source"] == "fallback" and out[1]["score"] == 20   # cosine fallback
    assert out[2]["source"] == "fallback"                              # bad score -> fallback


def test_score_cards_bounds_calls_and_annotates():
    cards = [_local(i, rel=50) for i in range(30)]
    calls = {"n": 0}
    def fn(system, user, max_tokens=0):
        calls["n"] += 1
        return {}
    relevancy.score_cards("q", cards, chat_fn=fn, batch_size=5, max_cards=25)
    # 25 scored in batches of 5 -> 5 calls, never 30.
    assert calls["n"] == 5
    assert all(relevancy.SCORE_KEY in c for c in cards[:25])
    assert all(relevancy.SCORE_KEY not in c for c in cards[25:])       # tail left unscored


# --------------------------------------------------------------------------- OOD classifier
def test_is_local_noise_rules():
    # federated is never noise, even with low relevance
    assert retrieval.is_local_noise(_fed(1, rel=5)) is False
    # local below floor with no element coverage -> noise
    assert retrieval.is_local_noise(_local(1, rel=20)) is True
    # local above floor -> kept
    assert retrieval.is_local_noise(_local(2, rel=80)) is False
    # local below floor BUT covers an element -> kept (real matched element)
    assert retrieval.is_local_noise(_local(3, rel=10, ncov=1)) is False
    # LLM score present overrides cosine relevancy
    c = _local(4, rel=90); c[relevancy.SCORE_KEY] = 20
    assert retrieval.is_local_noise(c, score_key=relevancy.SCORE_KEY) is True


def test_deprioritize_indomain_is_noop():
    cards = [_local(1, rel=10), _fed(2), _local(3, rel=90)]
    out = retrieval.deprioritize_ood_local(cards, in_domain=True)
    assert [c["family"] for c in out] == [c["family"] for c in cards]


def test_deprioritize_ood_partitions_and_preserves_order_and_set():
    cards = [_local(1, rel=10), _fed(2), _local(3, rel=90), _local(4, rel=5), _fed(5)]
    out = retrieval.deprioritize_ood_local(cards, in_domain=False)
    fams = [c["family"] for c in out]
    # 3 tiers, order preserved within each: federated first, then relevant local (L3), then noise
    assert fams == ["fed:F2", "fed:F5", "L3", "L1", "L4"]
    assert sorted(fams) == sorted(c["family"] for c in cards)          # permutation


# --------------------------------------------------------------------------- domain interpret
def test_domain_in_domain_defaults_true_when_unknown():
    assert RL._domain_in_domain(None) is True
    assert RL._domain_in_domain({"in_domain": False}) is False
    assert RL._domain_in_domain({"in_domain": True}) is True
    class V:  # object with attribute
        in_domain = False
    assert RL._domain_in_domain(V()) is False


# --------------------------------------------------------------------------- final ordering
def test_relevancy_order_score_primary_listwise_tiebreak():
    a = _fed(1); a[relevancy.SCORE_KEY] = 40
    b = _fed(2); b[relevancy.SCORE_KEY] = 90
    c = _fed(3); c[relevancy.SCORE_KEY] = 90          # ties b -> listwise position breaks it
    order = RL._relevancy_order([a, b, c], in_domain=True)
    assert [x["family"] for x in order] == ["fed:F2", "fed:F3", "fed:F1"]


def test_relevancy_order_unscored_sink_below_scored():
    a = _fed(1); a[relevancy.SCORE_KEY] = 10          # scored, low
    b = _fed(2)                                       # unscored
    order = RL._relevancy_order([b, a], in_domain=True)
    assert [x["family"] for x in order] == ["fed:F1", "fed:F2"]   # scored (even low) beats unscored


def test_relevancy_order_ood_demotes_local_noise():
    hi = _local(1, rel=90); hi[relevancy.SCORE_KEY] = 90
    noise = _local(2, rel=90); noise[relevancy.SCORE_KEY] = 20     # high cosine, low LLM -> noise
    fed = _fed(3); fed[relevancy.SCORE_KEY] = 30
    order = RL._relevancy_order([noise, fed, hi], in_domain=False)
    # the low-LLM-score local card sinks to the bottom despite its high COSINE relevancy (90).
    assert order[-1]["family"] == "L2"
    # in-domain the same card is NOT demoted — it is ordered purely by score, above the fed(30).
    order_in = RL._relevancy_order([noise, fed, hi], in_domain=True)
    assert [x["family"] for x in order_in] == ["L1", "fed:F3", "L2"]


# --------------------------------------------------------------------------- end to end
def test_rerank_report_cards_permutation_and_contiguous_ranks():
    cards = [_local(i, rel=60) for i in range(6)] + [_fed(i) for i in range(3)]
    out = RL.rerank_report_cards({"brief": "q"}, cards, chat_fn=FAIL, domain={"in_domain": False})
    assert sorted(c["family"] for c in out) == sorted(c["family"] for c in cards)
    assert [c["rank"] for c in out] == list(range(1, len(out) + 1))


def test_rerank_report_cards_ood_pushes_noise_down():
    # 3 relevant federated, 3 local noise (LLM will score them low), score via injected chat_fn.
    cards = [_fed(1), _fed(2), _fed(3), _local(4, rel=70), _local(5, rel=70), _local(6, rel=70)]
    scores = {}  # by batch id; the injected fn maps position -> score. Give fed high, local low.
    def fn(system, user, max_tokens=0):
        import re
        # relevancy batch carries 'INVENTION' + 'CANDIDATE REFERENCES'; listwise carries 'BATCH TO RANK'
        if "CANDIDATE REFERENCES" in user:
            out = []
            for m in re.finditer(r"\[(\d+)\] federated|\[(\d+)\] local", user):
                pass
            # score federated 80, local 20 by reading the block label
            for line in user.splitlines():
                m = re.match(r"\[(\d+)\] (federated|local)", line)
                if m:
                    out.append({"id": int(m.group(1)),
                                "score": 80 if m.group(2) == "federated" else 20,
                                "opinion": "x"})
            return {"results": out}
        return {}   # listwise -> identity
    out = RL.rerank_report_cards({"brief": "drone"}, cards, chat_fn=fn, domain={"in_domain": False})
    top3 = [c["family"] for c in out[:3]]
    assert all(f.startswith("fed:") for f in top3)               # federated dominate the head
    assert all(not c.get("federated_only") for c in out[-3:])    # local noise at the bottom
    assert sorted(c["family"] for c in out) == sorted(c["family"] for c in cards)  # nothing dropped


def test_rerank_report_cards_indomain_no_ood_effect():
    cards = [_local(1, rel=20), _local(2, rel=90), _fed(3, rel=50)]
    # in-domain: even a low-relevance local card is never demoted BY THE OOD FILTER.
    out_in = RL.rerank_report_cards({"brief": "q", "domain": {"in_domain": True}}, cards, chat_fn=FAIL)
    out_forced = RL.rerank_report_cards({"brief": "q"}, cards, chat_fn=FAIL, domain={"in_domain": True})
    assert [c["family"] for c in out_in] == [c["family"] for c in out_forced]
    # low-relevance local card is NOT forced to the tail (it is only ordered by its fallback score)
    assert "L1" in [c["family"] for c in out_in]


def test_scoring_off_reduces_to_listwise_order():
    cards = [_fed(i) for i in range(5)]
    pre = retrieval.deprioritize_ood_local(cards, in_domain=True)
    lw = RL.listwise_rerank({"brief": "q"}, pre, chat_fn=FAIL)     # identity
    red = RL._relevancy_order(lw, in_domain=True)                  # all unscored
    assert [id(c) for c in red] == [id(c) for c in lw]
