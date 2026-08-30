"""Search history tells the truth WHILE a search runs, not only after it finishes.

`recover_interrupted_searches` marks every stale `running` row failed at startup, correctly: the
process that owned it is gone. The dispatcher then restarts the interrupted run, also correctly.
Nothing put the row back, so on 2026-08-20 adhoc-c5830687f3ce sat in the history as "failed" while
it was at "Evidence sweep: batch 242 of 242".
"""
import re

import accounts
import webapp


def test_there_is_a_way_to_say_a_search_is_running_again():
    assert hasattr(accounts, "mark_search_running")


def test_generate_says_so_before_it_starts_working():
    src = open(webapp.__file__.replace(".pyc", ".py")).read()
    m = re.search(r"def _generate\(.*?run_id = f", src, re.S)
    assert m, "the _generate preamble moved"
    assert "accounts.mark_search_running(slug)" in m.group(0), (
        "a restarted run never tells the history it is running again")


def test_a_completed_search_is_not_dragged_back_to_running(monkeypatch):
    """The one state it must never touch. A late duplicate start must not un-complete a report
    the user has already been emailed about."""
    seen = {}

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            seen["sql"] = " ".join(sql.split())

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(accounts, "ensure_schema", lambda: None)
    monkeypatch.setattr(accounts.db, "cursor", lambda *a, **k: _Ctx())
    accounts.mark_search_running("adhoc-x")
    assert "status <> 'complete'" in seen["sql"], seen["sql"]
    assert "completed_at=NULL" in seen["sql"]


def test_completion_still_wins_over_a_failed_row():
    """The self-correcting half that already worked: mark_search_complete has no state guard, so a
    row wrongly marked failed becomes complete when the run finishes."""
    src = open(accounts.__file__.replace(".pyc", ".py")).read()
    m = re.search(r"def mark_search_complete\(slug\):.*?RETURNING", src, re.S)
    assert m and "WHERE slug=%s" in m.group(0)
    assert "status=" not in m.group(0).split("WHERE")[1]
