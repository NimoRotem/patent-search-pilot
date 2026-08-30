"""Page MEMBERSHIP, as opposed to page order.

Both frozen expert sets showed the same thing: references that were in the corpus, retrieved,
screened and READ IN FULL with grounded cells still never reached the 60-card page. On the Nguyen
set, Ristau ranked 103, Preta 85, Schmierer 247. `claim_first` sorts `order[:window]` and so cannot
reach them; `guarantee` only fires for a claim with nothing on the page at all.

The quota is off by default (it measured +1 in 30 with a regression on one set), but the mechanism
has to be correct for the switch to be worth having.
"""
import coverage_rank as CR


def _entry(**cells):
    """A by_pub entry whose grounded cells are {limitation: verdict}."""
    return {"covered": [{"item": k, "verdict": v, "grounding": "verified"}
                        for k, v in cells.items()]}


def _fixture():
    """The shape both expert sets actually showed, which is subtler than "a claim with nothing".

    Claim 2 IS already answered on the page, weakly, by US-0. So the existing promotion path in
    `claim_first` never fires for it — a claim with an answer is not a claim in trouble by that
    rule. Meanwhile the far better answer to claim 2 sits at rank 9, outside the window, and no
    amount of sorting the window can reach it. That is Ristau at 103 and Preta at 85: their claims
    had answers, so nothing promoted them, and they were never in the window to be sorted.
    """
    by_pub = {f"US-{i}": _entry(**{"claim 1[a]": "disclosed"}) for i in range(8)}
    by_pub["US-0"] = _entry(**{"claim 1[a]": "disclosed", "claim 2[a]": "partial"})
    by_pub["US-far"] = _entry(**{"claim 2[a]": "disclosed"})
    by_pub["US-tail"] = _entry(**{"claim 1[a]": "partial"})
    order = [f"US-{i}" for i in range(8)] + ["US-far", "US-tail"]
    return order, by_pub, ["claim 1", "claim 2"]


def test_claim_first_alone_cannot_reach_past_the_window():
    """The defect the quota exists for. Not a criticism of claim_first: it sorts, it does not select."""
    order, by_pub, claims = _fixture()
    cf = CR.claim_first(order, by_pub, claims, window=5)
    assert "US-far" not in cf["order"][:5]


def test_the_quota_pulls_an_out_of_window_discloser_onto_the_page():
    order, by_pub, claims = _fixture()
    q = CR.claim_quota(order, by_pub, claims, window=5, per_claim=1)
    assert "US-far" in q["order"][:5]
    assert "US-far" in q["promoted"], "a card taken from outside the window is a promotion"


def test_the_quota_is_bounded_by_max_fraction():
    """It decides membership; it must not take the whole page from the global ordering."""
    order, by_pub, claims = _fixture()
    q = CR.claim_quota(order, by_pub, claims, window=4, per_claim=10, max_fraction=0.5)
    assert len(q["reserved"]) <= 2


def test_the_quota_is_round_robin_so_a_crowded_claim_cannot_starve_a_rare_one():
    order, by_pub, claims = _fixture()
    q = CR.claim_quota(order, by_pub, claims, window=10, per_claim=2)
    assert q["per_claim"]["claim 2"] >= 1, q["per_claim"]


def test_the_quota_never_drops_or_duplicates_a_reference():
    order, by_pub, claims = _fixture()
    q = CR.claim_quota(order, by_pub, claims, window=5, per_claim=2)
    assert sorted(q["order"]) == sorted(order)
    assert len(q["order"]) == len(set(q["order"]))


def test_no_claims_or_no_order_is_inert():
    order, by_pub, _ = _fixture()
    assert CR.claim_quota(order, by_pub, [], window=5) is None
    assert CR.claim_quota([], by_pub, ["claim 1"], window=5) is None


def test_it_is_off_by_default():
    """The sweep measured +1 in 30 with a regression on one expert set. Off until that changes."""
    assert CR.RESERVE_PER_CLAIM == 0
