"""The exact channel declines a phrase it cannot afford to rank.

MEASURED 2026-08-22 on the live corpus, EP 3 707 092, four phrases a model produced for it:
'air extraction means' 0.33 s, 'vacuum seal element' 2.80 s, 'rigid base element' 3.27 s, and
'contact surface' 97.26 s. One generic two-word phrase was 94% of the channel's 103 s. The probe
that catches it costs 1.16 s at a limit of 5,000 and 3.70 s at 40,000; a selective phrase pays
only its own match count, 0.30 s for 1,111 chunks.
"""
import threading

import pytest

from retrieval import Retriever, exact


def _retriever(rows_for):
    log = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.sql = " ".join(str(sql).split())
            self.params = list(params or [])
            log.append((self.sql, self.params))

        def fetchall(self):
            return rows_for(self.sql, self.params)

    class Conn:
        def cursor(self):
            return Cur()

    r = object.__new__(Retriever)
    r._fam = {}
    r._wide = False
    object.__setattr__(r, "_conn", Conn())
    r.__dict__["_conn_tid"] = threading.get_ident()
    return r, log


def _rows(matches):
    """`matches` is {phrase: chunk count}; anything else is a ranking query returning one row."""
    def rows_for(sql, params):
        if "count(*)" in sql:
            n = matches.get(params[0], 1)
            return [{"n": min(n, params[1])}]
        return [{"publication_id": params[0], "score": 1.0}]
    return rows_for


def test_a_selective_phrase_is_ranked():
    r, log = _retriever(_rows({"vacuum seal element": 1111}))
    out = r.channel_exact(["vacuum seal element"])
    assert [s for s, _p in log if "ts_rank_cd" in s], "the phrase was declined"
    assert out


def test_a_phrase_that_fills_the_probe_is_declined_and_never_ranked():
    """Not truncated. Reading the first 20,000 matches and ranking those looks like a result and
    is an arbitrary subset of one, which for a PRECISION channel is the one failure it must not
    have."""
    r, log = _retriever(_rows({"contact surface": 10 ** 6}))
    out = r.channel_exact(["contact surface"])
    assert out == []
    assert not [s for s, _p in log if "ts_rank_cd" in s], "the expensive query ran anyway"


def test_declining_one_phrase_does_not_decline_the_others():
    r, log = _retriever(_rows({"contact surface": 10 ** 6, "vacuum seal element": 1111}))
    out = r.channel_exact(["contact surface", "vacuum seal element"])
    ranked = [p[0] for s, p in log if "ts_rank_cd" in s]
    assert ranked == ["vacuum seal element"], ranked
    assert out


def test_the_probe_is_bounded_by_the_threshold():
    r, log = _retriever(_rows({}))
    r.channel_exact(["anything"])
    probe = [(s, p) for s, p in log if "count(*)" in s]
    assert probe, "no probe was issued"
    assert probe[0][1][-1] == exact.PHRASE_MAX_CHUNKS
    assert "LIMIT %s" in probe[0][0]


def test_the_threshold_is_above_what_the_channel_can_return():
    """A phrase over the threshold could not have had its ranking respected anyway: the channel
    reads PHRASE_CAP x FAMILY_OVERFETCH rows and the LIMIT truncates the rest."""
    from retrieval.base import FAMILY_OVERFETCH
    assert exact.PHRASE_MAX_CHUNKS > exact.PHRASE_CAP * FAMILY_OVERFETCH


def test_the_guard_can_be_turned_off(monkeypatch):
    """An escape hatch, because a backend that is fast enough should not be rationed by a limit
    measured against `to_tsvector`."""
    monkeypatch.setattr(exact, "PHRASE_MAX_CHUNKS", 0)
    r, log = _retriever(_rows({"contact surface": 10 ** 6}))
    r.channel_exact(["contact surface"])
    assert not [s for s, _p in log if "count(*)" in s], "the probe ran with the guard off"
    assert [s for s, _p in log if "ts_rank_cd" in s]


@pytest.mark.parametrize("phrases", [None, [], ()])
def test_no_phrases_is_no_query(phrases):
    r, log = _retriever(_rows({}))
    assert r.channel_exact(phrases) == []
    assert log == []
