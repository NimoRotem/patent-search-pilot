"""Oracle injection: a diagnostic that must be impossible to mistake for a measurement."""
import pytest

import oracle


GOLD = ["fam-1", "fam-2", "fam-3"]


def test_disarmed_by_default():
    """Three independent conditions, because a diagnostic that leaks into a headline number is
    worse than no diagnostic at all."""
    assert not oracle.Oracle()
    assert not oracle.Oracle(stage="before_screen", enabled=True)          # no gold
    assert not oracle.Oracle(gold_families=GOLD, enabled=True)             # no stage
    assert not oracle.Oracle(stage="before_screen", gold_families=GOLD, enabled=False)
    assert not oracle.Oracle(stage="nonsense", gold_families=GOLD, enabled=True)
    assert oracle.Oracle(stage="before_screen", gold_families=GOLD, enabled=True)


def test_injection_only_fires_at_its_own_stage():
    o = oracle.Oracle(stage="before_read", gold_families=GOLD, enabled=True)
    assert o.inject(["a"], "before_screen") == ["a"]
    assert o.inject(["a"], "before_read") == ["fam-1", "fam-2", "fam-3", "a"]


def test_families_already_retrieved_are_not_injected_again():
    """What retrieval already found is itself the measurement; injecting it again would hide it."""
    o = oracle.Oracle(stage="before_screen", gold_families=GOLD, enabled=True)
    out = o.inject(["fam-2", "x"], "before_screen")
    assert out == ["fam-1", "fam-3", "fam-2", "x"]
    assert o.stamp()["n_injected"] == 2 and o.stamp()["already_present"] == 1


def test_a_stamped_report_cannot_be_scored_as_a_real_run():
    rep = {oracle.REPORT_KEY: {"stage": "before_screen"}}
    assert oracle.is_injected(rep)
    with pytest.raises(RuntimeError, match="oracle injection"):
        oracle.guard_report(rep)
    oracle.guard_report({})            # a clean report must pass


def test_the_stamp_says_the_numbers_are_an_upper_bound():
    o = oracle.Oracle(stage="before_portfolio", gold_families=GOLD, enabled=True)
    o.inject([], "before_portfolio")
    assert "upper bound" in o.stamp()["WARNING"]
