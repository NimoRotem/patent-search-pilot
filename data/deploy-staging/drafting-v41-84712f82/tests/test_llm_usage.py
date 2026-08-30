"""Per-search LLM accounting must stay isolated across long-lived/concurrent workers."""

from concurrent.futures import ThreadPoolExecutor

import llm


def test_usage_session_starts_zero_and_preserves_process_totals(monkeypatch):
    monkeypatch.setattr(
        llm, "_usage", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    llm._record_usage(prompt_tokens=10, completion_tokens=2)
    assert llm.usage() == {"calls": 1, "prompt_tokens": 10, "completion_tokens": 2}

    with llm.usage_session():
        assert llm.usage() == {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        llm._record_usage(prompt_tokens=7, completion_tokens=3)
        assert llm.usage() == {"calls": 1, "prompt_tokens": 7, "completion_tokens": 3}

    assert llm.usage() == {"calls": 2, "prompt_tokens": 17, "completion_tokens": 5}
    with llm.usage_session():
        assert llm.usage()["calls"] == 0


def test_concurrent_usage_sessions_do_not_consume_each_others_budget(monkeypatch):
    monkeypatch.setattr(
        llm, "_usage", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})

    def record(n):
        with llm.usage_session():
            for _ in range(n):
                llm._record_usage(prompt_tokens=2, completion_tokens=1)
            return llm.usage()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(record, (3, 5))

    assert first == {"calls": 3, "prompt_tokens": 6, "completion_tokens": 3}
    assert second == {"calls": 5, "prompt_tokens": 10, "completion_tokens": 5}
    assert llm.usage()["calls"] == 8
