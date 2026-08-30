"""Two pipelines, not two depths of one.

Every assertion here is a thing the single-pipeline version did wrong: a prior-art search that
paid for the claim ledger, a claim attack that silently became a find, settings asked after the
run they govern had started, and a results page offering a submission built from nothing read.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import search_mode as sm                                                  # noqa: E402
import search_profile                                                     # noqa: E402
from agent import AgentConfig                                             # noqa: E402

CLAIMS = [{"claim_no": 1, "independent": True,
           "text": "A vacuum gripper comprising a base element and a seal disposed on it."}]


# ------------------------------------------------------------------ the modes are distinct
def test_the_default_is_the_fast_search():
    for v in ("", None, "nonsense", "fast"):
        assert sm.normalise(v) == sm.FAST


def test_the_attack_is_selected_explicitly():
    for v in ("attack", "claim_attack", "packet", "submission", "ATTACK"):
        assert sm.normalise(v) == sm.ATTACK


def test_they_run_at_different_depths():
    assert sm.depth_for(sm.FAST) == "fast"
    assert sm.depth_for(sm.ATTACK) == "submission"


# ------------------------------------------------------------------ the attack refuses, not degrades
def test_the_attack_refuses_without_claims():
    why = sm.refusal(sm.ATTACK, [])
    assert why and "claims" in why.lower()
    assert "prior-art search" in why


def test_the_attack_accepts_claims():
    assert sm.refusal(sm.ATTACK, CLAIMS) == ""


def test_the_fast_search_never_refuses():
    assert sm.refusal(sm.FAST, []) == ""


# ------------------------------------------------------------------ fast is a different shape
def test_fast_retrieval_is_light_and_skips_the_cross_encoder():
    kw = sm.agent_kwargs(sm.FAST)
    assert kw["fast_mode"] is True
    assert kw["max_rounds"] == 0, "refinement rounds added 5 to 15 families a pass"
    #  MEASURED back to back on the same subject and corpus: 31.5s with the cross-encoder against
    #  5.8s without, to score TWELVE documents, and 18 of the top 20 were unchanged by it. It is
    #  not the model load either: a warm second pass over the same head took 17.0s against 21.2s
    #  cold. Fusion ranks the fast search.
    assert kw["final_rerank"] is False
    cfg = AgentConfig(**kw)
    assert cfg.fast_mode and cfg.fast_queries >= 1 and cfg.fast_elements >= 1


def test_the_attack_still_reranks():
    assert "final_rerank" not in sm.agent_kwargs(sm.ATTACK), \
        "the attack keeps AgentConfig's default, which reranks"
    assert AgentConfig().final_rerank is True


def test_the_attack_keeps_the_full_shape():
    assert sm.agent_kwargs(sm.ATTACK) == {}
    assert AgentConfig().fast_mode is False


def test_only_the_attack_reads_or_charts_or_offers_papers():
    assert sm.runs_deep(sm.ATTACK) and not sm.runs_deep(sm.FAST)
    assert sm.shows_claim_grid(sm.ATTACK) and not sm.shows_claim_grid(sm.FAST)
    assert sm.offers_submission(sm.ATTACK) and not sm.offers_submission(sm.FAST)


def test_the_fast_budget_cannot_start_a_read():
    b = search_profile.budget_for(search_profile.for_input(CLAIMS), depth="fast")
    for knob in ("CHART_TOP", "CHART_TOP_MAX", "ENRICH_TOP", "PRESCREEN_ENRICH_TOP",
                 "CLAIM_REACH_CAP", "SCREEN_TOP"):
        assert b[knob] == 0, knob
    assert b["BATCH_TAIL_MAX"] == -1


def test_the_attack_budget_is_untouched():
    b = search_profile.budget_for(search_profile.for_input(CLAIMS), depth="submission")
    assert b.get("CHART_TOP", 0) > 0 and b.get("ENRICH_TOP", 0) > 0


# ------------------------------------------------------------------ the attack's settings
def test_the_forum_is_a_real_choice_with_a_safe_default():
    assert sm.normalise_jurisdiction("EP") == "EP"
    assert sm.normalise_jurisdiction("ep") == "EP"
    for junk in ("", None, "XX", "DE"):
        assert sm.normalise_jurisdiction(junk) == "US"


def test_read_top_is_clamped_not_trusted():
    assert sm.normalise_read_top(45) == 45
    assert sm.normalise_read_top(1) == 10
    assert sm.normalise_read_top(10 ** 6) == 1000
    assert sm.normalise_read_top("nonsense") == sm.READ_TOP_DEFAULT


def test_the_description_records_what_the_run_was_given():
    d = sm.describe(sm.ATTACK, read_top=200, jurisdiction="EP", third_party=True,
                    concept_expansions=False)
    assert d["mode"] == sm.ATTACK and d["reads_in_full"] and d["claim_grid"]
    assert d["read_top"] == 200 and d["jurisdiction"] == "EP"
    assert d["third_party"] is True and d["concept_expansions"] is False


def test_a_fast_description_carries_no_read_settings():
    d = sm.describe(sm.FAST)
    assert d["reads_in_full"] is False and d["claim_grid"] is False
    for k in ("read_top", "jurisdiction", "concept_expansions"):
        assert k not in d, k
