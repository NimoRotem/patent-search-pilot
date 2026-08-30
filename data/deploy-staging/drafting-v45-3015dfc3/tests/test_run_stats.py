"""What one search cost, and how honest the page is allowed to be about it.

Everything about a run's cost was live-only and died with the process: `_job_tokens` reads a
process-global counter minus the job's baseline, and `llm.usage_session()` is opened only by the
retrieval agent — its own docstring says a ContextVar scope does not cross into a
ThreadPoolExecutor. So a finished report carried `llm_usage: {calls: 7}` for a run that had just
read four hundred documents in full.

The rule these tests exist to hold: a number that was not measured is never printed as though it
was. An estimate dressed as a measurement is the one thing a cost page must not do.
"""
import json

import run_stats


REPORT = {
    "query": "a vacuum gripper",
    "mode": "novelty", "depth": "deep", "rounds": 2, "n_families": 5239,
    "query_document": {"publication_number": "US-20240316792-A1", "label": "US-20240316792-A1",
                       "title": "Vacuum gripper", "source": "link",
                       "claims": [{"claim_no": i} for i in range(14)]},
    "deep_rank": {"screened": 2500, "read_in_full": 400, "charted": 420,
                  "chars_read": 11132737, "screen_seconds": 45.6, "chart_seconds": 2144.5,
                  "seconds": 2369.7, "depth": "deep"},
    "external": {"n_queries": 54, "elapsed": 60.2},
    "llm_usage": {"calls": 7, "prompt_tokens": 2383, "completion_tokens": 2662},
}


def test_a_measured_run_is_written_and_read_back(tmp_path):
    got = run_stats.record(tmp_path, "adhoc-x", seconds=3873.6, tokens=209_744_540,
                           shared_process=False, report=REPORT)
    assert got["measured"] is True and got["tokens"] == 209_744_540
    back = run_stats.load(tmp_path, "adhoc-x")
    assert back["measured"] is True
    assert back["subject_pub"] == "US-20240316792-A1"
    assert back["read_in_full"] == 400
    s = run_stats.summarise(back)
    assert s["time"] == "1h 04m"
    assert s["tokens"] == "209.7M"


def test_a_run_from_before_the_receipt_says_so_rather_than_guessing(tmp_path):
    """The whole point. No sidecar, so nothing invents a token count."""
    st = run_stats.load(tmp_path, "adhoc-old", report=REPORT, seconds=3873.6)
    assert st["measured"] is False
    assert "tokens" not in st
    s = run_stats.summarise(st)
    assert s["tokens"] is None
    assert any("not recorded" in n for n in s["notes"])
    #  and the agent's seven calls are labelled as the agent's, not the run's
    assert "query agent only" in s["calls"]


def test_the_agent_calls_are_never_presented_as_the_runs_calls(tmp_path):
    st = run_stats.load(tmp_path, "adhoc-old", report=REPORT)
    assert st.get("agent_calls") == 7
    assert "calls" not in st, "the agent's calls were promoted to the run's calls"


def test_a_shared_worker_is_declared(tmp_path):
    """Several searches run concurrently in one gunicorn worker, so a global counter's delta over
    one run's window contains the others. Saying so is the difference between an upper bound and
    a wrong attribution."""
    run_stats.record(tmp_path, "adhoc-y", seconds=100, tokens=5_000_000, shared_process=True,
                     report=REPORT)
    s = run_stats.summarise(run_stats.load(tmp_path, "adhoc-y"))
    assert any("upper bound" in n for n in s["notes"])


def test_calls_derived_from_tokens_are_labelled_as_derived(tmp_path):
    run_stats.record(tmp_path, "adhoc-z", seconds=100, tokens=9_000_000, calls=0, report=REPORT)
    s = run_stats.summarise(run_stats.load(tmp_path, "adhoc-z"))
    assert s["calls"].startswith("~")
    assert any("derived" in n for n in s["notes"])


def test_counted_calls_beat_derived_ones(tmp_path):
    run_stats.record(tmp_path, "adhoc-c", seconds=100, tokens=9_000_000, calls=1234, report=REPORT)
    s = run_stats.summarise(run_stats.load(tmp_path, "adhoc-c"))
    assert s["calls"] == "1,234"
    assert not any("derived" in n for n in s["notes"])


def test_the_patent_it_started_from_survives_a_link_search(tmp_path):
    """A link search records no `subject`; the number is only in the query document, which is why
    the history row could not show it."""
    st = run_stats.load(tmp_path, "adhoc-old", report=REPORT)
    assert st["subject_pub"] == "US-20240316792-A1"
    assert st["subject_title"] == "Vacuum gripper"
    assert st["n_claims"] == 14


def test_a_search_with_no_patent_behind_it_claims_none(tmp_path):
    st = run_stats.load(tmp_path, "adhoc-typed", report={"query": "vacuum gripper", "mode": "novelty"})
    assert "subject_pub" not in st
    assert run_stats.summarise(st)["time"] is None


def test_writing_the_receipt_never_raises(tmp_path):
    """A search must not fail over its own receipt."""
    got = run_stats.record("/nonexistent/dir/nowhere", "adhoc-x", seconds=1, tokens=1)
    assert got["measured"] is True


def test_a_corrupt_receipt_falls_back_to_the_report(tmp_path):
    p = run_stats.path_for(tmp_path, "adhoc-bad")
    open(p, "w").write("{not json")
    st = run_stats.load(tmp_path, "adhoc-bad", report=REPORT)
    assert st["measured"] is False and st["subject_pub"] == "US-20240316792-A1"


def test_the_receipt_is_written_atomically(tmp_path):
    """A half-written receipt reads as garbage and would blank the panel."""
    src = open(run_stats.__file__.replace(".pyc", ".py")).read()
    assert "os.replace(tmp, p)" in src


def test_times_read_the_way_a_person_says_them():
    assert run_stats._hms(3873.6) == "1h 04m"
    assert run_stats._hms(125) == "2m 05s"
    assert run_stats._hms(9) == "9s"
    assert run_stats._si(209_744_540) == "209.7M"
    assert run_stats._si(5045) == "5.0k"
    assert run_stats._si(7) == "7"
