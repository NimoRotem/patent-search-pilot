"""Several users at once: the queue must not undo the lanes the gate provides.

Quick and deep have separate concurrency and separate daily budgets precisely so a cents-class
interactive search never waits behind two multi-hour attacks. The dispatcher used to fetch only the
single oldest queued row, so a queued DEEP run that could not start was retried for ever while
every quick run behind it sat untouched with its own lane free.
"""
import auth
import run_queue


class _Q:
    """The queue as the dispatcher sees it, without a database."""

    def __init__(self, rows):
        self.rows = list(rows)

    def batch(self, limit=None):
        return [r for r in self.rows if r.get("state") == "queued"][:limit or 12]

    def mark(self, slug, state):
        for r in self.rows:
            if r["slug"] == slug:
                r["state"] = state


def _drive(q, gate, ticks=3):
    """One tick of the real dispatcher loop, minus the sleep and the database."""
    started = []
    for _ in range(ticks):
        for row in q.batch():
            ok, _why = gate.try_begin(row["payload"].get("depth") or "deep")
            if not ok:
                continue                       # this lane is full; try the next row
            started.append(row["slug"])
            q.mark(row["slug"], "running")
            break
    return started


def _gate():
    return auth.RunGate(max_concurrent=2, daily_cap=50, quick_max=3, quick_daily_cap=200)


def test_a_quick_run_is_not_blocked_by_a_queued_deep_run():
    """The defect, exactly: deep lane full, quick lane empty, quick queued behind deep."""
    g = _gate()
    assert g.try_begin("deep")[0] and g.try_begin("deep")[0]      # deep lane full
    q = _Q([{"slug": "deep3", "state": "queued", "payload": {"depth": "deep"}},
            {"slug": "quick1", "state": "queued", "payload": {"depth": "quick"}}])
    assert _drive(q, g, ticks=1) == ["quick1"]


def test_fifo_still_holds_within_a_lane():
    """Looking past a blocked row must not turn the queue into a free-for-all."""
    g = _gate()
    q = _Q([{"slug": "q1", "state": "queued", "payload": {"depth": "quick"}},
            {"slug": "q2", "state": "queued", "payload": {"depth": "quick"}},
            {"slug": "q3", "state": "queued", "payload": {"depth": "quick"}}])
    assert _drive(q, g, ticks=3) == ["q1", "q2", "q3"]


def test_the_deep_run_still_starts_once_its_own_lane_frees():
    """Overtaking must not mean starvation: the blocked run is still first when a slot opens."""
    g = _gate()
    assert g.try_begin("deep")[0] and g.try_begin("deep")[0]
    q = _Q([{"slug": "deep3", "state": "queued", "payload": {"depth": "deep"}},
            {"slug": "quick1", "state": "queued", "payload": {"depth": "quick"}}])
    assert _drive(q, g, ticks=1) == ["quick1"]
    g.end("deep")                                                  # a deep search finishes
    assert _drive(q, g, ticks=1) == ["deep3"]


def test_a_full_queue_in_one_lane_does_not_spin_on_the_others():
    g = _gate()
    assert g.try_begin("deep")[0] and g.try_begin("deep")[0]
    q = _Q([{"slug": "d%d" % i, "state": "queued", "payload": {"depth": "deep"}}
            for i in range(5)])
    assert _drive(q, g, ticks=3) == []          # nothing starts, nothing is wrongly marked
    assert all(r["state"] == "queued" for r in q.rows)


def test_the_lookahead_is_bounded():
    """The dispatcher must not scan an unbounded queue on every tick."""
    assert 1 <= run_queue.LOOKAHEAD <= 100


def test_next_queued_batch_really_returns_more_than_the_head():
    """Against the REAL queue, not a stand-in.

    The first version of these tests drove a fake queue object, so neutering
    `next_queued_batch` to return only the head left every one of them green: they were testing
    the dispatcher's loop, not the function the loop depends on. This one hits Postgres.
    """
    import db
    pre = "zztestlane-"
    with db.cursor() as cur:
        cur.execute("DELETE FROM app_run_queue WHERE slug LIKE %s", (pre + "%",))
    try:
        run_queue.enqueue(pre + "a", {"depth": "deep"})
        run_queue.enqueue(pre + "b", {"depth": "quick"})
        run_queue.enqueue(pre + "c", {"depth": "quick"})
        got = [r["slug"] for r in run_queue.next_queued_batch()
               if r["slug"].startswith(pre)]
        assert got[:3] == [pre + "a", pre + "b", pre + "c"], got
        assert len(got) >= 3, "the dispatcher can only skip a blocked lane if it sees past the head"
        head = run_queue.next_queued()
        assert head is not None                      # the single-row helper still works
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug LIKE %s", (pre + "%",))


def test_the_two_lanes_really_are_independent():
    g = _gate()
    assert g.try_begin("deep")[0] and g.try_begin("deep")[0]
    assert g.try_begin("deep")[0] is False
    for _ in range(3):
        assert g.try_begin("quick")[0] is True
    assert g.try_begin("quick")[0] is False
    st = g.stats()
    assert st["active"] == 2 and st["active_quick"] == 3
