"""Benchmark runs fail; production degrades but never silently.

Every measurement in this project that turned out to be wrong was wrong the same way: something
failed, a fallback produced a plausible value, and the run reported a number instead of an error.
"""
import pytest

import failclosed as fc
import llm

#  Captured at IMPORT, which happens before conftest.no_paid_apis patches it. Without this these
#  tests exercise the stub and pass whether the real separation exists or not -- the same trap
#  that made an earlier guard test a false pass.
_REAL_CHAT_JSON = llm.chat_json


@pytest.fixture(autouse=True)
def clean():
    fc.reset()
    yield
    fc.reset()


def test_production_degrades_and_records():
    with fc.force(False):
        assert fc.fallback("x.y", "vertex 503", {"a": 1}) == {"a": 1}
    got = fc.used()
    assert len(got) == 1 and got[0]["where"] == "x.y" and "503" in got[0]["reason"]


def test_benchmark_mode_refuses_to_produce_a_number():
    with fc.force(True):
        with pytest.raises(fc.DegradedRun):
            fc.fallback("x.y", "vertex 503", {"a": 1})


def test_an_empty_result_is_not_a_failure():
    """'zero hits' and 'the adapter 401d' are identical in a result count and must never be
    recorded as the same thing. Lens returned 401 on every search for the whole project while
    reporting healthy."""
    with fc.force(False):
        fc.empty_result("ipaustralia")
        fc.source_failed("lens", "401")
    kinds = {r["kind"] for r in fc.used()}
    assert kinds == {"empty_result", "source_failure"}
    s = fc.summary()
    assert s["counts"]["empty_result"] == 1 and s["counts"]["source_failure"] == 1


def test_empty_result_never_raises_even_in_benchmark_mode():
    """A source that honestly found nothing must not kill a benchmark run."""
    with fc.force(True):
        fc.empty_result("ipaustralia")     # must not raise
    assert fc.used()[0]["kind"] == "empty_result"


def test_stage_swallows_in_production_and_fails_in_benchmark():
    with fc.force(False):
        with fc.stage("screen"):
            raise ValueError("boom")
    assert any(r["kind"] == "stage_skipped" for r in fc.used())

    with fc.force(True):
        with pytest.raises(ValueError):
            with fc.stage("screen"):
                raise ValueError("boom")


def test_llm_separates_a_failed_call_from_an_empty_answer(monkeypatch):
    """{} is what a 503, a quota refusal, a truncated response AND a genuinely empty answer all
    returned. The tournament A/B was lost to exactly this: every comparison truncated at
    max_tokens, returned {}, and the run reported a 40% regression that was a broken call."""
    class _R:
        text = '{"order": []}'
        usage_metadata = None

    monkeypatch.setattr(llm, "chat_json", _REAL_CHAT_JSON)
    monkeypatch.setattr(llm, "_call", lambda *a, **k: _R())
    with fc.force(True):
        assert llm.chat_json("s", "u") == {"order": []}      # a real empty answer: fine
    assert fc.used() == []

    def boom(*a, **k):
        raise RuntimeError("vertex 503")

    monkeypatch.setattr(llm, "_call", boom)
    with fc.force(True):
        with pytest.raises(fc.DegradedRun):
            llm.chat_json("s", "u")


def test_llm_truncated_json_is_a_failure_not_an_empty_answer(monkeypatch):
    class _R:
        text = '{"order": [1, 2, 3'          # truncated at max_tokens
        usage_metadata = None

    monkeypatch.setattr(llm, "chat_json", _REAL_CHAT_JSON)
    monkeypatch.setattr(llm, "_call", lambda *a, **k: _R())
    with fc.force(False):
        assert llm.chat_json("s", "u") == {}
    assert any(r["kind"] == "llm_bad_json" for r in fc.used())


def test_identity_rerank_is_recorded_as_not_having_ranked(monkeypatch):
    """Identity order is 'we did not rank', which is indistinguishable downstream from 'the
    cross-encoder agreed with the incoming order' -- a very different statement.

    Exercises the IN-PROCESS implementation explicitly. rerank_pool.install() replaces
    rerank.rerank with a child-process wrapper at import, so a test that called rerank.rerank
    would monkeypatch a _load the wrapper never consults: it passed in isolation and measured
    nothing in the full suite, which is exactly the failure mode this module exists to stop.
    """
    import rerank
    fn = getattr(rerank, "_inprocess_rerank", rerank.rerank)
    monkeypatch.setattr(rerank, "_load", lambda: None)
    with fc.force(False):
        out = fn("q", ["a", "b"])
    assert out == [(0, 0.0), (1, 0.0)]
    assert any(r["kind"] == "rerank_identity" for r in fc.used())

    with fc.force(True):
        with pytest.raises(fc.DegradedRun):
            fn("q", ["a", "b"])
