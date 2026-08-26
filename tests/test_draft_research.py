"""Re-search: the reading, the request it puts to the agent, and the loop that joins them.

The claim this feature makes is that running it repeatedly leaves the draft further from the art
each time. That claim is only checkable because every round stores a number, so most of what is
pinned here is the number: what it counts, what it refuses to count, and that it cannot be quietly
faked by a round that charted nothing.
"""
import json
from unittest.mock import Mock

import pytest

import draft_novelty
import draft_research
import draft_studio


# =============================================================================================
# A claim chart shaped exactly like the ones webview builds
# =============================================================================================
def cell(pub, verdict=None):
    if verdict is None:
        return {"pub": pub, "covered": False}
    return {"pub": pub, "covered": True, "verify": verdict, "confidence": 0.9}


def chart(rows, pubs, titles=None):
    return {"source": "reading",
            "columns": [{"pub": p, "title": (titles or {}).get(p, "")} for p in pubs],
            "rows": rows}


def element(name, cells, *, independent=True, preamble=False):
    return {"element": name, "cells": cells, "independent": independent, "preamble": preamble}


PUBS = ["US-1111111-A", "US-2222222-B2", "US-3333333-B1"]


def test_studio_schema_bootstrap_includes_research_rounds():
    names = [path.name for path in draft_studio._MIGRATIONS]
    assert "020_draft_research_rounds.sql" in names
    assert names[-1] == "021_draft_project_settings.sql"


# =============================================================================================
# What the reading counts
# =============================================================================================
def test_the_headline_is_the_nearest_single_reference_not_the_field():
    """Novelty falls to ONE document disclosing every element, not to ten sharing them out."""
    rows = [
        element("a body", [cell(PUBS[0], "discloses"), cell(PUBS[1], "discloses"), cell(PUBS[2])]),
        element("a pump", [cell(PUBS[0], "discloses"), cell(PUBS[1]), cell(PUBS[2], "discloses")]),
        element("a seal removable by hand",
                [cell(PUBS[0], "discloses"), cell(PUBS[1]), cell(PUBS[2])]),
        element("a vent", [cell(PUBS[0]), cell(PUBS[1]), cell(PUBS[2])]),
    ]
    out = draft_novelty.reading(chart(rows, PUBS))
    assert out["closest_pub"] == PUBS[0]
    assert out["closest_coverage"] == 0.75
    #  Between them the three references reach three of four elements; that is an obviousness
    #  question and is reported separately so it can never move the novelty headline.
    assert out["combination"] == 0.75
    assert out["uncovered_elements"] == ["a vent"]


def test_a_weak_or_unchecked_cell_is_not_a_disclosure():
    rows = [element("a body", [cell(PUBS[0], "weak")]),
            element("a pump", [cell(PUBS[0], "unchecked")]),
            element("a seal", [cell(PUBS[0], "discloses")])]
    out = draft_novelty.reading(chart(rows, PUBS[:1]))
    assert out["closest_coverage"] == round(1 / 3, 4)
    assert sorted(out["uncovered_elements"]) == ["a body", "a pump"]


def test_the_preamble_is_never_counted():
    """"An apparatus comprising" is disclosed by everything and would put a floor under the score."""
    rows = [element("an apparatus comprising", [cell(PUBS[0], "discloses")], preamble=True),
            element("a seal removable by hand", [cell(PUBS[0])])]
    out = draft_novelty.reading(chart(rows, PUBS[:1]))
    assert out["n_elements"] == 1
    assert out["closest_coverage"] == 0.0


def test_dependent_claims_do_not_dilute_the_reading():
    rows = [element("a body", [cell(PUBS[0], "discloses")]),
            element("a battery", [cell(PUBS[0])], independent=False),
            element("a strap", [cell(PUBS[0])], independent=False)]
    out = draft_novelty.reading(chart(rows, PUBS[:1]))
    assert out["n_elements"] == 1
    assert out["closest_coverage"] == 1.0


def test_a_chart_with_no_independent_rows_still_reads_on_its_features():
    """A quick search charts features rather than claims. Dividing by zero there would report a
    spurious 0.0, which reads as "nothing came close" when nothing was measured."""
    rows = [element("a suction cup", [cell(PUBS[0], "discloses")], independent=False),
            element("a vacuum pump", [cell(PUBS[0])], independent=False)]
    out = draft_novelty.reading(chart(rows, PUBS[:1]))
    assert out["ok"] is True
    assert out["n_elements"] == 2
    assert out["closest_coverage"] == 0.5


def test_a_round_that_charted_nothing_reports_no_reading_rather_than_zero():
    out = draft_novelty.reading({"columns": [], "rows": []})
    assert out["ok"] is False
    assert out["closest_coverage"] is None
    assert "charted no reference" in out["detail"]


def test_the_detail_line_states_the_raw_counts_not_only_a_percentage():
    rows = [element(f"e{i}", [cell(PUBS[0], "discloses" if i < 3 else None)]) for i in range(4)]
    out = draft_novelty.reading(chart(rows, PUBS[:1]))
    assert "3 of 4" in out["detail"]


# =============================================================================================
# Comparing rounds
# =============================================================================================
def test_a_falling_score_is_reported_as_progress():
    out = draft_novelty.improvement({"closest_coverage": 0.75}, {"closest_coverage": 0.25})
    assert out["comparable"] is True and out["delta"] == -0.5
    assert "further from the claims" in out["verdict"]


def test_a_rising_score_is_reported_as_the_loop_not_working():
    out = draft_novelty.improvement({"closest_coverage": 0.25}, {"closest_coverage": 0.75})
    assert out["delta"] == 0.5
    assert "has not moved away" in out["verdict"]


def test_an_unmeasured_round_is_not_silently_treated_as_zero():
    out = draft_novelty.improvement({"closest_coverage": 0.5}, {"closest_coverage": None})
    assert out["comparable"] is False
    assert out["delta"] is None


# =============================================================================================
# What the agent is told
# =============================================================================================
def test_the_agent_is_given_the_measurement_and_the_ground_to_build_on():
    reading = {"ok": True, "n_elements": 4, "closest_coverage": 0.75,
               "closest_pub": "US-1111111-A", "closest_title": "Handheld lifter",
               "uncovered_elements": ["a seal removable without a tool"]}
    text = draft_research.drafting_request(2, reading, [{"publication_number": "US-1111111-A"}],
                                           previous={"closest_coverage": 1.0})
    assert "US-1111111-A" in text
    assert "3 of their 4 elements" in text
    assert "75%" in text and "100%" in text
    assert "a seal removable without a tool" in text
    #  The failure mode this loop has to avoid is buying the number with scope.
    assert "DO NOT buy a lower score with scope" in text
    assert "\u2014" not in text


def test_the_agent_is_told_plainly_when_everything_was_disclosed():
    reading = {"ok": True, "n_elements": 3, "closest_coverage": 1.0,
               "closest_pub": "US-1111111-A", "closest_title": "", "uncovered_elements": []}
    text = draft_research.drafting_request(1, reading, [])
    assert "Every element of the independent claims was disclosed" in text


def test_an_unmeasured_round_still_asks_the_agent_to_read_the_art():
    text = draft_research.drafting_request(1, {"ok": False}, [])
    assert "no measurement this round" in text
    assert "Read the attached references" in text or "Read every newly attached" in text


# =============================================================================================
# The loop
# =============================================================================================
def _round_harness(monkeypatch, view, *, ready_after=0):
    stored = {"status": "", "reading": {}, "turn_id": None, "imported_count": 0, "round_no": 3}
    monkeypatch.setattr(draft_research, "update_round",
                        lambda _id, **values: stored.update(values))
    monkeypatch.setattr(draft_research, "_cursor", lambda **_k: _FakeCursor(stored))
    calls = {"ready": 0}

    def is_ready(_slug):
        calls["ready"] += 1
        return calls["ready"] > ready_after

    attached = {}

    def attach(_slug, pubs):
        attached["pubs"] = list(pubs)
        return len(pubs)

    sent = {}

    def enqueue(message):
        sent["message"] = message
        return 4242

    return stored, is_ready, attach, enqueue, attached, sent


class _FakeCursor:
    def __init__(self, stored):
        self._stored = stored

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, *_a, **_k):
        return None

    def fetchone(self):
        return {"round_no": self._stored.get("round_no", 3)}


def test_a_round_measures_attaches_the_closest_art_and_raises_a_drafting_turn(monkeypatch):
    rows = [element("a body", [cell(PUBS[0], "discloses"), cell(PUBS[1], "discloses")]),
            element("a pump", [cell(PUBS[0], "discloses"), cell(PUBS[1])]),
            element("a vent", [cell(PUBS[0]), cell(PUBS[1])])]
    view = {"claim_chart": chart(rows, PUBS[:2], {PUBS[0]: "Handheld lifter"}), "cards": []}
    stored, is_ready, attach, enqueue, attached, sent = _round_harness(monkeypatch, view)

    out = draft_research.run_round(
        project_id=6, user_id=4, round_id=1, slug="adhoc-x",
        load_view=lambda _s: view, is_ready=is_ready, attach=attach, enqueue=enqueue,
        timeout=30, poll=0)

    assert out["ok"] is True
    assert stored["status"] == "complete"
    assert stored["closest_coverage"] == round(2 / 3, 4)
    #  Attached in order of how near the chart says they are, closest first.
    assert attached["pubs"][0] == PUBS[0]
    assert out["turn_id"] == 4242
    assert "a vent" in sent["message"]


def test_a_search_that_never_finishes_fails_the_round_rather_than_hanging(monkeypatch):
    stored, _is_ready, attach, enqueue, _attached, _sent = _round_harness(monkeypatch, {})
    out = draft_research.run_round(
        project_id=6, user_id=4, round_id=1, slug="adhoc-x",
        load_view=lambda _s: {}, is_ready=lambda _s: False, attach=attach, enqueue=enqueue,
        timeout=0.05, poll=0.01)
    assert out["ok"] is False and out["reason"] == "timeout"
    assert stored["status"] == "failed"
    assert "time budget" in stored["note"]


def test_a_round_whose_chart_is_empty_still_attaches_the_ranked_cards(monkeypatch):
    """No reading is not no result: the references are still worth putting in front of the agent."""
    view = {"claim_chart": {"columns": [], "rows": []},
            "cards": [{"pub": "US-9999999-B2", "title": "Something"}]}
    stored, is_ready, attach, enqueue, attached, sent = _round_harness(monkeypatch, view)
    out = draft_research.run_round(
        project_id=6, user_id=4, round_id=1, slug="adhoc-x",
        load_view=lambda _s: view, is_ready=is_ready, attach=attach, enqueue=enqueue,
        timeout=30, poll=0)
    assert out["ok"] is True
    assert attached["pubs"] == ["US-9999999-B2"]
    assert stored["closest_coverage"] is None


def test_a_failure_to_attach_does_not_lose_the_round(monkeypatch):
    """The measurement is the durable part. Losing it because a reference would not resolve would
    throw away the whole search."""
    rows = [element("a body", [cell(PUBS[0], "discloses")])]
    view = {"claim_chart": chart(rows, PUBS[:1]), "cards": []}
    stored, is_ready, _attach, enqueue, _attached, _sent = _round_harness(monkeypatch, view)

    def attach(_slug, _pubs):
        raise RuntimeError("corpus unavailable")

    out = draft_research.run_round(
        project_id=6, user_id=4, round_id=1, slug="adhoc-x",
        load_view=lambda _s: view, is_ready=is_ready, attach=attach, enqueue=enqueue,
        timeout=30, poll=0)
    assert out["ok"] is True
    assert stored["closest_coverage"] == 1.0
    assert "corpus unavailable" in stored["note"]
