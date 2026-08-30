"""The read budget is shared across claims, not handed to the loudest one.

On adhoc-db64a3dd7c98 the pool grounded claim 1[a] with 581 references and claim 9[b] with 21, and
the read set was chosen by a single whole-invention screen score. Ten limitations finished the read
still uncovered and were rescued afterwards at 1h51m. A quota is what stops the crowded claim from
spending the budget the starved one needed.
"""
import claim_reach as CR


def test_quota_is_round_robin_not_best_first():
    """The crowded claim must not take the whole budget before the starved one is served."""
    by_claim = {
        "claim 1[a]": [f"US-crowded-{i}" for i in range(50)],
        "claim 9[b]": ["US-rare-1", "US-rare-2"],
    }
    picked = CR.quota(by_claim, per_claim=6)
    assert "US-rare-1" in picked and "US-rare-2" in picked
    #  The rare claim's only two candidates are reached in the first handful of slots, not at 50.
    assert picked.index("US-rare-2") < 6, picked[:8]


def test_quota_respects_per_claim_and_dedupes():
    by_claim = {"a": ["p1", "p2", "p3", "p4"], "b": ["p3", "p4", "p5"]}
    picked = CR.quota(by_claim, per_claim=2)
    assert len(picked) == len(set(picked)), "a reference must never be read twice"
    assert set(picked) <= {"p1", "p2", "p3", "p4", "p5"}


def test_quota_honours_exclude_so_it_never_re_reads_the_head():
    by_claim = {"a": ["p1", "p2", "p3"]}
    picked = CR.quota(by_claim, per_claim=3, exclude={"p1"})
    assert "p1" not in picked
    assert picked == ["p2", "p3"]


def test_quota_honours_a_hard_cap():
    by_claim = {"a": [f"x{i}" for i in range(20)], "b": [f"y{i}" for i in range(20)]}
    assert len(CR.quota(by_claim, per_claim=10, cap=7)) <= 7


def test_quota_of_nothing_is_nothing():
    assert CR.quota({}) == []
    assert CR.quota({"a": []}) == []


def test_query_carries_the_claim_and_is_bounded():
    """The blurb keeps a dependent claim inside the field; the claim makes it specific."""
    q = CR._query("a portable vacuum gripper for handling stone slabs",
                  "wherein the maximum acceleration is greater than that of the first object " * 60)
    assert "portable vacuum gripper" in q
    assert "maximum acceleration" in q
    assert len(q) <= CR.MAX_BRIEF_CHARS + CR.MAX_CLAIM_CHARS + 4


def test_reach_is_inert_without_a_retriever():
    """Every caller path must survive the stage being off or unavailable."""
    assert CR.reach([{"label": "claim 1", "text": "a gripper"}], [{"pub": "US-1", "fam": "f1"}],
                    retriever=None) == {}
