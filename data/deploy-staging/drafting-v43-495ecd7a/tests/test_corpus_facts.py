import pathlib
import tempfile
import threading
import time

import corpus_facts


def _reset_cache(monkeypatch, *, value=None, timestamp=0.0, snapshot=None):
    #  Point the on-disk snapshot into a FRESH temporary directory unless a test supplies its own,
    #  so "cold cache" here means genuinely cold. A fixed path under data/ does not work: any test
    #  that forces a refresh writes it, and the next test then starts warm. In production the
    #  snapshot is what stops a restart rendering the public scope statement from the fallback.
    monkeypatch.setattr(
        corpus_facts, "_SNAPSHOT",
        snapshot or pathlib.Path(tempfile.mkdtemp(prefix="corpus-facts-")) / "snapshot.json")
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


def test_the_last_good_answer_survives_a_restart(monkeypatch, tmp_path):
    """A cold cache used to render the PUBLIC scope statement from the config fallback: "millions
    of publications" and the four originally-configured offices, for the first minutes after every
    restart. The exact scans take about a minute, so the last successful answer is persisted."""
    snap = tmp_path / "corpus_facts.json"
    _reset_cache(monkeypatch, snapshot=snap)
    monkeypatch.setattr(corpus_facts, "_query_db", lambda: _live(4_954_362))
    warm = corpus_facts.facts(force=True)
    assert warm["publications"] == 4_954_362
    assert snap.exists(), "a forced refresh must persist what it learned"

    #  restart: cache empty, database slow or unreachable
    _reset_cache(monkeypatch, snapshot=snap)

    def unavailable():
        raise RuntimeError("database is busy")

    monkeypatch.setattr(corpus_facts, "_query_db", unavailable)
    cold = corpus_facts.facts()
    assert cold["publications"] == 4_954_362, "the restart must not fall back to the constant"
