"""The claim-limitation search, the relationships it must keep, and the budget order.

Every case here is taken from US 2025/0033224 A1 (portable vacuum gripper), the subject of report
adhoc-f1410b74df48, because that run is where each of these failures was observed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import limitations                                                       # noqa: E402
import novelty_units as nu                                               # noqa: E402
import query_set                                                         # noqa: E402


#  The claim rows EXACTLY as webapp._stash_doc writes them for a document or a patent link:
#  claim_no, text, independent, and no `label`. That absence is the whole bug.
CLAIMS = [
    {"claim_no": 1, "independent": True, "text":
        "A vacuum gripper for gripping an object, the vacuum gripper comprising a base element, "
        "wherein the base element comprises one or more openings around a periphery of the base "
        "element; a vacuum seal element coupled to the base element, wherein the vacuum seal "
        "element is configured to surround a cavity; wherein the vacuum seal element comprises a "
        "first portion disposed on a second portion; wherein the first portion comprises a "
        "flexible and stretchable material; wherein the second portion comprises a compressible "
        "and deformable material; wherein the first portion comprises a higher hardness than that "
        "of the second portion, or the first portion is less compressible and less deformable "
        "than that of the second portion; wherein the first and second portions are configured to "
        "cause the vacuum seal element to reduce a gap size with a step in the object surface "
        "after the first portion is pressed through an opening of the one or more openings; an "
        "air extraction mechanism coupled to the base element and in fluid communication with the "
        "cavity, configured to extract gas from the cavity to create a suction force."},
    {"claim_no": 7, "independent": False, "text":
        "A vacuum gripper as in claim 1, wherein the second portion comprises multiple elastic "
        "compressible and deformable materials as discrete layers or a composite, or even a "
        "fluid-filled pneumatic or hydraulic element."},
    {"claim_no": 9, "independent": False, "text":
        "A vacuum gripper as in claim 1, further comprising a battery housed in a handle and an "
        "alarm for a battery level of the battery."},
]


# --------------------------------------------------------------- 1. the bug that started this
def test_split_claims_accepts_rows_with_no_label():
    """The stash carries claim_no and no label. This used to raise KeyError and return nothing."""
    rows = limitations.split_claims(CLAIMS, use_llm=False, log=lambda *a, **k: None)
    assert rows, "a claim set with no `label` key must still split"
    assert all(r["claim_label"] for r in rows)
    assert {r["claim_label"] for r in rows} == {"claim 1", "claim 7", "claim 9"}


def test_an_explicit_label_is_never_overwritten():
    rows = limitations.split_claims(
        [{"claim_no": 4, "label": "claim 4 (amended)", "independent": True,
          "text": "A gripper comprising a base and a seal disposed on the base, the seal being "
                  "softer than the base."}],
        use_llm=False, log=lambda *a, **k: None)
    assert rows and rows[0]["claim_label"] == "claim 4 (amended)"


def test_limitation_queries_are_actually_produced():
    """The end-to-end symptom: 65 limitations in the ledger, zero limitation-shaped searches."""
    specs = query_set.build("A portable vacuum gripper.", elements=["a vacuum gripper"],
                            claims=CLAIMS, want_llm=False)
    kinds = {s.kind for s in specs}
    assert "limitation" in kinds, "a claim attack must issue limitation queries"


# --------------------------------------------------- 2. relationships survive the decomposition
def test_a_relative_property_is_its_own_class():
    text = ("the first portion comprises a higher hardness than that of the second portion, or "
            "the first portion is less compressible and less deformable than that of the second")
    assert nu.classify(text) == nu.MATERIAL


def test_a_causal_mechanism_is_its_own_class():
    text = ("the first and second portions are configured to cause the vacuum seal element to "
            "reduce a gap size with a step in the object surface after the first portion is "
            "pressed through an opening")
    assert nu.classify(text) == nu.MECHANISM


def test_a_spatial_relation_is_structural():
    text = "one or more openings around a periphery of the base element expose sections of the seal"
    assert nu.classify(text) == nu.STRUCTURAL


def test_a_battery_in_a_handle_is_generic_however_spatial_the_words_are():
    assert nu.classify("a battery housed in a handle of the vacuum gripper") == nu.GENERIC
    assert nu.classify("an alarm for a cavity pressure and a battery level") == nu.GENERIC


def test_genericity_separates_the_battery_from_the_hardness_difference():
    battery = nu.genericity("a battery housed in a handle, and an alarm for the battery level")
    hardness = nu.genericity("the first portion comprises a higher hardness than the second "
                             "portion and is less compressible than it")
    assert battery > 0.6 and hardness < 0.3, (battery, hardness)


# ------------------------------------------------------- 3. generic parts do not get equal budget
def test_generic_requirements_are_capped_and_come_last():
    rows = limitations.split_claims(CLAIMS, use_llm=False, log=lambda *a, **k: None)
    analysis = nu.analyse(rows, want_llm=False)
    plan = nu.query_plan(analysis, max_clusters=6, max_limitations=12, max_generic=1)
    lims = [q for q in plan if q["kind"] == "limitation"]
    generic = [q for q in lims if q.get("unit_kind") == nu.GENERIC]
    assert len(generic) <= 1
    if generic and len(lims) > 1:
        assert lims.index(generic[0]) == len(lims) - 1, "a generic part is searched last or not at all"


def test_the_budget_order_is_clusters_then_core_then_generic():
    rows = limitations.split_claims(CLAIMS, use_llm=False, log=lambda *a, **k: None)
    plan = nu.query_plan(nu.analyse(rows, want_llm=False))
    assert plan and plan[0]["kind"] == "cluster", "the combination is asked first"
    priorities = [q["priority"] for q in plan]
    assert priorities == sorted(priorities), "the plan IS the budget, so it must be ordered"


# ------------------------------------------------------------ 4. combinations, not lone elements
def test_clusters_are_two_to_four_requirements():
    rows = limitations.split_claims(CLAIMS, use_llm=False, log=lambda *a, **k: None)
    analysis = nu.analyse(rows, want_llm=False)
    assert analysis["clusters"], "a claim with several requirements must produce combinations"
    for c in analysis["clusters"]:
        assert nu.CLUSTER_MIN <= len(c["members"]) <= nu.CLUSTER_MAX
        assert len(c["text"].split()) <= 60


def test_a_cluster_is_a_seed_query_and_an_element_query_is_not():
    specs = query_set.build("A portable vacuum gripper.", elements=["a seal"], claims=CLAIMS,
                            want_llm=False)
    seeds = {s.kind for s in query_set.seed_specs(specs)}
    assert "cluster" in seeds
    assert "element" not in seeds and "limitation" not in seeds


# ------------------------------------------------------ 5. alternative embodiments branch apart
def test_alternatives_split_into_separate_queries():
    text = ("the second portion comprises multiple elastic compressible and deformable materials "
            "as discrete layers or a composite, or even a fluid-filled pneumatic or hydraulic "
            "element")
    alts = nu.split_alternatives(text)
    assert len(alts) >= 2, alts
    joined = " ".join(alts).lower()
    assert "fluid-filled" in joined and ("composite" in joined or "layers" in joined)


def test_a_requirement_with_one_embodiment_does_not_branch():
    assert nu.split_alternatives("a base element comprising one or more openings") == []


# ------------------------------------------------------------------ 6. external order and 7. noise
def test_claim_first_aspects_come_from_the_clusters():
    rows = limitations.split_claims(CLAIMS, use_llm=False, log=lambda *a, **k: None)
    aspects = nu.claim_first_aspects(nu.analyse(rows, want_llm=False))
    assert aspects and all(a["claim_specific"] for a in aspects)
    assert aspects[0]["blurb"], "an aspect that reaches a semantic engine needs text"


def test_a_repeated_claim_clause_is_searched_once():
    """Pass 8 of the audited run was 4,352 characters, most of it the same clauses again."""
    doubled = ("A gripper comprising a base element; a seal coupled to the base element. "
               "a base element; a seal coupled to the base element.")
    once = query_set.clean_claim_text(doubled)
    assert once.lower().count("a seal coupled to the base element") == 1
    assert "base element" in once


def test_cleaning_a_claim_never_invents_words():
    src = ("A vacuum gripper comprising a base; a seal on the base; wherein the seal is softer "
           "than the base.")
    out = query_set.clean_claim_text(src)
    for clause in ("vacuum gripper comprising a base", "seal on the base", "softer"):
        assert clause in out


# ------------------------------------------------------------------------- degrade, do not hide
def test_a_broken_split_is_recorded_not_swallowed():
    import failclosed
    failclosed.reset()
    assert query_set._split_limitations([{"claim_no": 1}]) == []   # no text: simply empty
    saved = limitations.split_claims
    try:
        limitations.split_claims = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        assert query_set._split_limitations(CLAIMS) == []
        assert any(r["kind"] == "limitation_split_failed" for r in failclosed.used()), \
            "a claim split that fails must show up on the report as degraded"
    finally:
        limitations.split_claims = saved
        failclosed.reset()
