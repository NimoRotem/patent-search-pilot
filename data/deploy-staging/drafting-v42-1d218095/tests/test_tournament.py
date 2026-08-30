"""Tournament ordering: the failure modes that made the first measurement meaningless.

The module is NOT wired into the pipeline (measured and refuted, see its docstring). These tests
exist because the two defects below made the first A/B unreadable, and both are the kind that
report a plausible number instead of an error.
"""
import types

import pytest

import tournament


FEATURES = ["a portable vacuum gripper", "a bracing structure"]


def _by_pub(pubs):
    return {p: {"title": f"title {p}", "chars_read": 50_000, "n_disclosed": 2, "n_partial": 1,
                "n_features": 4,
                "covered": [{"item": FEATURES[0], "verdict": "disclosed", "idf": 2.0,
                             "location": "claim 1", "quote": f"quote for {p}"}]}
            for p in pubs}


def test_a_failed_comparison_scores_nothing(monkeypatch):
    """A failed comparison must NOT return its input order.

    It did, and the caller could not tell the difference, so it awarded Borda points by incoming
    POSITION -- which ties the top of group two with the top of group one and scrambles the global
    ranking. Measured: with every comparison silently failing, 10 cited references in the top 50
    became 6, and the tournament looked like a bad idea rather than a broken one.
    """
    monkeypatch.setattr(tournament.llm, "chat_json", lambda *a, **k: {})
    assert tournament._ask(FEATURES, ["[0] x", "[1] y"], "c", [0, 1], retries=0) is None


def test_a_wholly_failed_round_keeps_the_pointwise_order(monkeypatch):
    monkeypatch.setattr(tournament.llm, "chat_json", lambda *a, **k: {})
    order = [f"US-{i}-A" for i in range(30)]
    out = tournament.rank(FEATURES, _by_pub(order), order, top=30, rounds=1, group=6)
    assert out == order


def test_the_model_order_is_parsed_not_ignored(monkeypatch):
    """The parser silently returned the identity order when the model answered correctly."""
    monkeypatch.setattr(tournament.llm, "chat_json",
                        lambda *a, **k: {"order": [5, 0, 4, 3, 1, 2]})
    assert tournament._ask(FEATURES, ["c"] * 6, "c", list(range(6))) == [5, 0, 4, 3, 1, 2]


def test_a_partial_answer_is_completed_not_discarded(monkeypatch):
    monkeypatch.setattr(tournament.llm, "chat_json", lambda *a, **k: {"order": [3, 1]})
    got = tournament._ask(FEATURES, ["c"] * 4, "c", [0, 1, 2, 3])
    assert got[:2] == [3, 1]
    assert sorted(got) == [0, 1, 2, 3]


def test_blend_at_zero_is_the_pointwise_order_exactly():
    """The blend must be a true superset of 'change nothing', or a sweep cannot include a control."""
    order = [f"US-{i}-A" for i in range(20)]
    head = order[:10]
    pts = {p: float(i) for i, p in enumerate(head)}      # deliberately the WORST possible order
    assert tournament.blend(order, head, pts, share=0.0) == order


def test_blend_at_one_is_the_tournament_order():
    order = [f"US-{i}-A" for i in range(6)]
    head = list(order)
    pts = {p: float(i) for i, p in enumerate(head)}      # last place has the most points
    assert tournament.blend(order, head, pts, share=1.0)[0] == order[-1]


def test_card_shows_evidence_not_just_a_title():
    """The comparator's whole advantage is that it sees the grounded quotes."""
    c = tournament.card("US-1-A", _by_pub(["US-1-A"])["US-1-A"], 0)
    assert "US-1-A" in c and "quote for US-1-A" in c and FEATURES[0] in c
    assert len(c) <= tournament.CARD_CHARS
