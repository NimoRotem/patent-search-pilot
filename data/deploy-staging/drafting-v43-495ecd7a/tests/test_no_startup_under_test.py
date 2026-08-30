"""Importing the app from a test process must not touch production.

The startup block reconciles the live database and starts the queue dispatcher. It was guarded by
`"PYTEST_CURRENT_TEST" not in os.environ` — and pytest sets that variable at the start of each
TEST, not at import. Collection imports webapp with it unset, so the guard was open for exactly the
moment the module body runs, which is the moment that matters.

Measured 2026-08-20: a suite run called `recover_interrupted_searches()` against the live database
and marked adhoc-8dcf2436929a and adhoc-bad747ed6f77 FAILED while both were running normally,
minutes after they had been started. `run_queue.start_dispatcher()` ran in the same breath, which
can launch real searches from a pytest process.

This test is the one that would have caught it: it asks the guard the question collection asks.
"""
import os
import re
import sys

import webapp


def test_the_guard_is_true_during_collection_not_only_during_a_test():
    """The whole bug in one assertion. `PYTEST_CURRENT_TEST` is absent at import time."""
    seen = dict(os.environ)
    seen.pop("PYTEST_CURRENT_TEST", None)
    seen.pop("PATENTS_NO_STARTUP", None)
    old = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(seen)
        assert "PYTEST_CURRENT_TEST" not in os.environ
        #  pytest is in sys.modules for the whole run, collection included, which is what makes
        #  this answerable at import time.
        assert "pytest" in sys.modules
        assert webapp._is_import_for_test() is True, (
            "importing webapp during collection would run recovery against the live database")
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_an_explicit_opt_out_is_honoured(monkeypatch):
    """For anything else that imports the module for its functions: a script, a one-off, a shell."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("PATENTS_NO_STARTUP", "1")
    assert webapp._is_import_for_test() is True


def test_the_startup_block_is_behind_that_guard():
    src = open(webapp.__file__.replace(".pyc", ".py")).read()
    m = re.search(r"\nif not _is_import_for_test\(\):\n(.*?)\n\n", src, re.S)
    assert m, "the startup block is no longer behind _is_import_for_test"
    body = m.group(1)
    for call in ("recover_interrupted_searches()", "recover_interrupted_turns()",
                 "start_dispatcher"):
        assert call in body, "%s escaped the guard" % call
    #  and nothing else in the module runs them at import time
    for call in ("\nrecover_interrupted_searches()", "\nrun_queue.start_dispatcher("):
        assert call not in src, "%s runs unguarded at import" % call.strip()


def test_recovery_only_settles_rows_older_than_the_grace_period():
    """A search started seconds ago is not a stale row from a dead process."""
    assert webapp.RECOVERY_GRACE_SECONDS >= 60
    src = open(webapp.__file__.replace(".pyc", ".py")).read()
    m = re.search(r"def recover_interrupted_searches\(\):.*?RECOVERY_GRACE_SECONDS", src, re.S)
    assert m and "updated_at <" in m.group(0)
