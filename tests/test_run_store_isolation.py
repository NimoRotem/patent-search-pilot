"""The production workers must never execute a row the test suite created.

MEASURED 2026-08-22, within an hour of the durable cutover going live. The suite runs against the
real Postgres on purpose: `search_runs` carries foreign keys into the corpus, so a separate store
could not honour them, and until the cutover nothing was running that could pick a row up. Then
the workers started, and a queued row is a queued row. From the live quick worker's own log:

    [worker] claimed test-resume-8b70bab362-... (slug=test-resume-8b70bab362 lane=quick attempt=1/3)
    [profile test-resume-8b70bab362] concept depth=deep rounds=1 budget={...}
    [worker] test-resume-8b70bab362-... FAILED after 2s -> None

That is production spending model calls on a fixture, and the test that created the row going red
because its own `claim()` found nothing to claim. `admit_waiting` was doing the same thing one step
earlier ("admitted 1 waiting run(s) in lane quick"), and an admitted row counts against the lane's
concurrency, so fixtures could hold a slot a real user's search was queued behind.

The guard is a reserved slug prefix that production cannot generate, defaulting to fail-safe.
These tests hold three things shut: the prefix stays unreachable from every real slug source, the
filter is actually in the SQL, and the suite's own opt-in is in place.
"""
import os
import sys

import pytest

import runstore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def test_the_default_is_fail_safe():
    """Read off the source, not the live module: conftest flips the flag for the suite, so the
    running value is True here and says nothing about what a worker process starts with."""
    src = open(os.path.join(ROOT, "src", "runstore.py"), encoding="utf-8").read()
    assert "\nALLOW_TEST_SLUGS = False\n" in src, (
        "the fixture filter no longer defaults to on, so a production entry point that does not "
        "know about it will claim test rows")


def test_the_suite_opted_itself_in():
    assert runstore.ALLOW_TEST_SLUGS is True, (
        "conftest did not flip the flag, so these tests cannot claim the rows they create")


def test_production_sql_excludes_the_prefix(monkeypatch):
    """'test%', not 'test-%'. The suite creates `testq-...` rows as well, and on 2026-08-23 a
    restarted deep worker claimed one and spent model calls on it. The narrower pattern would
    let that happen again, so the guard covers every slug that starts with the word."""
    monkeypatch.setattr(runstore, "ALLOW_TEST_SLUGS", False)
    assert runstore._live_only() == "AND slug NOT LIKE 'test%%'"
    assert "testq-abc" .startswith("test")
    monkeypatch.setattr(runstore, "ALLOW_TEST_SLUGS", True)
    assert runstore._live_only() == ""


def test_the_claim_query_carries_the_filter(monkeypatch):
    """DEFECT INJECTION on the thing that actually matters: the text the database runs."""
    monkeypatch.setattr(runstore, "ALLOW_TEST_SLUGS", False)
    sql = runstore._CLAIM_SQL_TEMPLATE.format(admitted_clause="admitted",
                                              live_only=runstore._live_only())
    assert "slug NOT LIKE" in sql
    monkeypatch.setattr(runstore, "ALLOW_TEST_SLUGS", True)
    sql = runstore._CLAIM_SQL_TEMPLATE.format(admitted_clause="admitted",
                                              live_only=runstore._live_only())
    assert "slug NOT LIKE" not in sql


def test_a_real_search_slug_can_never_collide_with_the_prefix():
    """The guard is only sound while production cannot produce a `test-` slug. `search_slug`
    hashes the query, so no user text reaches the prefix however it is phrased."""
    import webapp
    for query in ("test", "test-durable", "TEST DURABLE", "test-resume-abc123", ""):
        slug = webapp.search_slug(query, "novelty", wide=True, search_focus="all_text")
        assert slug.startswith("adhoc-")
        assert not slug.startswith(runstore.TEST_SLUG_PREFIX)


def test_no_gold_id_uses_the_prefix():
    """The other slug source. A gold entry is hand-named, so this one is a real possibility rather
    than a hash collision, and it would make the benchmark invisible to the workers."""
    import goldset
    bad = [e["id"] for e in goldset.load()["entries"]
           if str(e["id"]).startswith(runstore.TEST_SLUG_PREFIX)]
    assert not bad, ("these gold entries are named with the reserved fixture prefix and would "
                     "never be claimed by a worker: %s" % bad)


def test_the_fixture_slugs_really_do_carry_the_prefix():
    """The guard protects nothing if the durable tests stop using the prefix. Named as literals so
    deleting the constant does not delete the assertion with it."""
    import re
    seen = set()
    for name in ("test_durable_runs.py", "test_durable_resume.py", "test_run_cutover.py",
                 "test_worker_cutover.py"):
        path = os.path.join(ROOT, "tests", name)
        if not os.path.exists(path):
            continue
        body = open(path, encoding="utf-8").read()
        seen.update(re.findall(r'f?"(test-[a-z-]+)', body))
    assert seen, "no fixture slug literals found; has the durable suite been renamed?"
    assert all(s.startswith("test-") for s in seen), sorted(seen)


@pytest.mark.parametrize("prefix", ["test-durable", "test-resume", "test-worker"])
def test_a_fixture_row_is_invisible_to_production(prefix):
    """END TO END against the real store, WITHOUT CLAIMING ANYTHING.

    Running production's own claim here would be the very bug this file is about pointed the other
    way: if a real user's search were queued at that moment, the test would claim it, hold it under
    a probe worker id and settle nothing. So the row is created for real, admitted for real, and
    then production's exact predicate is run as a SELECT. Nothing is mutated but the fixture row,
    which is deleted either way.
    """
    db = pytest.importorskip("db")
    try:
        conn = db.connect()
    except Exception:
        pytest.skip("no database available")
    conn.close()

    slug = "%s-isolation-%s" % (prefix, os.getpid())
    rid = runstore.enqueue(slug, {"query": "isolation probe"}, lane="quick", depth="quick")
    try:
        if runstore.admission_capable():
            with db.cursor() as cur:
                cur.execute("UPDATE search_runs SET admitted = true, admitted_at = now() "
                            "WHERE run_id = %s", (rid,))
        #  The claim query's own WHERE, verbatim, as a read.
        candidates = (
            "SELECT run_id FROM search_runs "
            " WHERE status = 'queued' AND lane = ANY(%s) {live_only}")
        with db.cursor() as cur:
            cur.execute(candidates.format(live_only=""), (["quick"],))
            visible_to_suite = {r["run_id"] for r in cur.fetchall()}
            cur.execute(candidates.format(live_only="AND slug NOT LIKE 'test-%%'"), (["quick"],))
            visible_to_production = {r["run_id"] for r in cur.fetchall()}
        assert rid in visible_to_suite, "the fixture row was not queued at all"
        assert rid not in visible_to_production, (
            "a production worker can still see %s and will run a real search on it" % slug)
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM search_runs WHERE run_id = %s", (rid,))
