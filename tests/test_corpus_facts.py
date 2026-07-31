import threading
import time

import corpus_facts


def _reset_cache(monkeypatch, *, value=None, timestamp=0.0):
    monkeypatch.setattr(
        corpus_facts,
        "_CACHE",
        {
            "t": timestamp,
            "v": value,
            "refreshing": False,
            "last_attempt": 0.0,
        },
    )


def _live(publications):
    return {
        "publications": publications,
        "chunks": publications * 3,
        "max_date": None,
        "min_date": None,
        "jurisdictions": ["US", "EP"],
        "jurisdictions_trace": [],
    }


def test_cold_cache_never_blocks_page_rendering(monkeypatch):
    _reset_cache(monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def slow_query():
        entered.set()
        assert release.wait(2)
        return _live(123)

    monkeypatch.setattr(corpus_facts, "_query_db", slow_query)

    started = time.monotonic()
    facts = corpus_facts.facts()
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert facts["publications"] is None
    assert entered.wait(1)
    release.set()

    deadline = time.monotonic() + 1
    while corpus_facts._CACHE["refreshing"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert corpus_facts.facts()["publications"] == 123


def test_expired_cache_is_served_while_one_refresh_runs(monkeypatch):
    _reset_cache(monkeypatch, value=_live(100), timestamp=1.0)
    monkeypatch.setattr(corpus_facts.time, "time", lambda: 10_000.0)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_query():
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return _live(200)

    monkeypatch.setattr(corpus_facts, "_query_db", slow_query)

    assert corpus_facts.facts()["publications"] == 100
    assert entered.wait(1)
    assert corpus_facts.facts()["publications"] == 100
    assert calls == 1
    release.set()


def test_force_refresh_remains_synchronous(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(corpus_facts, "_query_db", lambda: _live(321))

    assert corpus_facts.facts(force=True)["publications"] == 321
    assert corpus_facts._CACHE["v"]["publications"] == 321
