"""Durable execution: the claim, the lease, the reaper and the resume point.

These run against the real Postgres like the rest of the suite, on throwaway run ids under a
`test-durable-*` slug, and delete everything they made. `reap()` is called with an explicit
`run_ids` list in every test: the store is shared with the live app, and a test that reaped a real
run would be the second time a pytest process settled a production search.
"""
import inspect
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

import runctx
import runstore


def test_schema_check_is_read_only(monkeypatch):
    """Worker startup may verify migrations, but it must never execute DDL itself."""
    statements = []

    class Cursor:
        def execute(self, sql, params=None):
            statements.append((sql, params))

        def fetchall(self):
            return []

    @contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield Cursor()

    monkeypatch.setattr(runstore.db, "cursor", fake_cursor)
    runstore.ensure_schema(force=True)
    assert statements and all("CREATE" not in sql.upper() for sql, _params in statements)


def test_schema_check_names_missing_migrations(monkeypatch):
    class Cursor:
        def execute(self, _sql, _params=None):
            pass

        def fetchall(self):
            return [{"name": "search_runs"}, {"name": "search_stages"}]

    @contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield Cursor()

    monkeypatch.setattr(runstore.db, "cursor", fake_cursor)
    with pytest.raises(RuntimeError) as exc:
        runstore.ensure_schema(force=True)
    assert "search_runs" in str(exc.value) and "search_stages" in str(exc.value)


@pytest.fixture()
def slug():
    s = f"test-durable-{uuid.uuid4().hex[:10]}"
    yield s
    import db
    with db.cursor() as cur:
        #  ON DELETE CASCADE takes the stages, queries, hits, candidates and usage with it.
        cur.execute("DELETE FROM corpus_ingest_queue WHERE requested_by_run IN "
                    "(SELECT run_id FROM search_runs WHERE slug=%s)", (s,))
        cur.execute("DELETE FROM search_runs WHERE slug=%s", (s,))


def _lapse(run_id):
    """Make this run's lease already expired, without waiting for it. The reaper's clock is the
    database's, so this is the same event as a worker that stopped heartbeating."""
    import db
    with db.cursor() as cur:
        cur.execute("UPDATE search_runs SET lease_expires_at = now() - interval '1 second' "
                    "WHERE run_id=%s", (run_id,))


# ------------------------------------------------------------------ claiming
def test_two_workers_never_claim_the_same_run(slug):
    """The defect this replaces: a run's identity lived in a process, so 'who is running it' was
    unanswerable across processes. FOR UPDATE SKIP LOCKED makes it a single fact."""
    rid = runstore.enqueue(slug, {"query": "a vacuum gripper"}, depth="quick", lane="quick")
    a, b = runstore.worker_id(), runstore.worker_id()
    first = runstore.claim(a, lanes=["quick"])
    second = runstore.claim(b, lanes=["quick"])
    assert first and first["run_id"] == rid
    assert second is None, "a second worker claimed a run that was already running"
    assert runstore.get(rid)["worker_id"] == a


def test_two_workers_racing_two_runs_take_one_each(slug):
    """SKIP LOCKED, not a lock convoy: the loser of a race on one row takes the NEXT row rather
    than waiting for the winner to commit."""
    r1 = runstore.enqueue(slug, {"query": "one"}, lane="quick", run_id=f"{slug}-1")
    r2 = runstore.enqueue(slug + "-b", {"query": "two"}, lane="quick", run_id=f"{slug}-2")
    try:
        a, b = runstore.worker_id(), runstore.worker_id()
        got = {runstore.claim(a, lanes=["quick"])["run_id"],
               runstore.claim(b, lanes=["quick"])["run_id"]}
        assert got == {r1, r2}
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM search_runs WHERE run_id = ANY(%s)", ([r1, r2],))


def test_a_lane_filter_does_not_starve_the_other_lane(slug):
    """Quick and deep have separate concurrency for a reason: a cents-class interactive search
    must never wait behind two multi-hour attacks."""
    deep = runstore.enqueue(slug, {"query": "deep"}, lane="deep", run_id=f"{slug}-d")
    quick = runstore.enqueue(slug + "-q", {"query": "quick"}, lane="quick", run_id=f"{slug}-q")
    try:
        assert runstore.claim(runstore.worker_id(), lanes=["quick"])["run_id"] == quick
        assert runstore.claim(runstore.worker_id(), lanes=["deep"])["run_id"] == deep
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM search_runs WHERE run_id = ANY(%s)", ([deep, quick],))


# ------------------------------------------------------------------ the lease
def test_the_holder_can_heartbeat_and_nobody_else_can(slug):
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    mine = runstore.worker_id()
    runstore.claim(mine, lanes=["quick"])
    assert runstore.heartbeat(rid, mine) is True
    assert runstore.heartbeat(rid, runstore.worker_id()) is False


def test_an_expired_lease_is_reclaimed(slug):
    """A SIGKILLed worker stops heartbeating. The run must come back, not sit 'running' forever
    the way a report stuck on its first phase for 76 minutes did."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    dead = runstore.worker_id()
    runstore.claim(dead, lanes=["quick"])
    assert runstore.get(rid)["status"] == "running"
    _lapse(rid)
    out = runstore.reap(run_ids=[rid])
    assert out["requeued"] == [rid]
    row = runstore.get(rid)
    assert row["status"] == "queued" and row["worker_id"] is None
    #  and the next worker really can take it
    assert runstore.claim(runstore.worker_id(), lanes=["quick"])["run_id"] == rid


def test_a_stale_worker_cannot_fail_a_requeued_run(slug):
    """Once the reaper clears ownership, the dead worker cannot settle the queued retry."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    stale = runstore.worker_id()
    runstore.claim(stale, lanes=["quick"])
    _lapse(rid)
    assert runstore.reap(run_ids=[rid])["requeued"] == [rid]
    assert runstore.fail(rid, stale, "late exception", retry=False) is None
    assert runstore.get(rid)["status"] == "queued"


def test_a_run_that_burns_its_attempts_fails_instead_of_looping(slug):
    """An infinite retry of a run that kills its worker is a loop, not resilience."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick", max_attempts=2)
    for _ in range(2):
        runstore.claim(runstore.worker_id(), lanes=["quick"])
        _lapse(rid)
        runstore.reap(run_ids=[rid])
    row = runstore.get(rid)
    assert row["status"] == "failed"
    assert "lease expired" in (row["error"] or "")


def test_the_heartbeat_thread_reports_a_lost_lease(slug):
    """`Heartbeat.lost` is the worker's stop signal: continuing after the reaper has handed the
    run on means two workers writing one report."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    mine = runstore.worker_id()
    runstore.claim(mine, lanes=["quick"])
    hb = runstore.Heartbeat(rid, mine, interval=0.05, lease_seconds=60).start()
    try:
        time.sleep(0.2)
        assert not hb.lost.is_set()
        hb.check()
        #  Another worker takes it over. Written as the single UPDATE a reclaim produces rather
        #  than as lapse + reap + claim: this heartbeat is ticking twenty times a second and would
        #  push the lease forward again between the lapse and the reap, which is the heartbeat
        #  doing its job and a race in the test, not in the product.
        import db
        with db.cursor() as cur:
            cur.execute("UPDATE search_runs SET worker_id=%s, status='running' WHERE run_id=%s",
                        (runstore.worker_id(), rid))
        deadline = time.time() + 3
        while time.time() < deadline and not hb.lost.is_set():
            time.sleep(0.05)
        assert hb.lost.is_set()
        with pytest.raises(runstore.LeaseLost):
            hb.check()
    finally:
        hb.stop()


def test_run_context_confirms_database_ownership_at_stage_boundaries(monkeypatch):
    class Heartbeat:
        lease_seconds = 90

        def check(self):
            pass

    ctx = runctx.RunContext("run-1", "slug", worker="worker-a", heartbeat=Heartbeat())
    monkeypatch.setattr(runstore, "heartbeat", lambda *_args, **_kwargs: False)
    with pytest.raises(runstore.LeaseLost):
        ctx.check_lease()


def test_worker_settles_success_before_stopping_its_heartbeat(monkeypatch):
    """No reaper window may open between producing the report and settling the owned run."""
    from runner import worker

    events = []
    row = {"run_id": "run-1", "slug": "slug", "lane": "quick", "attempts": 1,
           "max_attempts": 3}

    class Heartbeat:
        def start(self):
            events.append("heartbeat-start")
            return self

        def stop(self):
            events.append("heartbeat-stop")

    monkeypatch.setattr(runstore, "claim", lambda *_args, **_kwargs: row)
    monkeypatch.setattr(runstore, "Heartbeat", lambda *_args, **_kwargs: Heartbeat())
    monkeypatch.setattr(worker, "execute", lambda *_args, **_kwargs: events.append("execute"))

    def finish(*_args, **_kwargs):
        events.append("finish")
        return True

    monkeypatch.setattr(runstore, "finish", finish)
    assert worker.run_once("worker-a", lanes=["quick"]) == "run-1"
    assert events == ["heartbeat-start", "execute", "finish", "heartbeat-stop"]


def test_a_core_checkpoint_failure_fails_closed(monkeypatch):
    ctx = runctx.RunContext("run-1", "slug", worker="worker-a")
    monkeypatch.setattr(runstore, "stage_start",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("db down")))
    with pytest.raises(runctx.DurabilityError):
        ctx.checkpoint("fuse", {"path": "x"})


def test_fused_checkpoint_files_are_scoped_to_the_run():
    """Two attempts with the same report slug must not overwrite one shared fused checkpoint."""
    import webapp

    try:
        runctx.bind("same-slug", runctx.RunContext("run-one", "same-slug"))
        first = webapp._fused_checkpoint_path("same-slug")
        runctx.bind("same-slug", runctx.RunContext("run-two", "same-slug"))
        second = webapp._fused_checkpoint_path("same-slug")
    finally:
        runctx.unbind("same-slug")
    assert first != second
    assert first.parent == second.parent == webapp.REPORTS
    assert "run-one" not in first.name and "run-two" not in second.name


# ------------------------------------------------------------------ resume
def test_a_killed_run_resumes_at_the_right_stage(slug):
    """THE WHOLE POINT. A worker dies after screening; the next one starts at reading, keeping the
    retrieval and the screen it already paid for."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    dead = runstore.worker_id()
    row = runstore.claim(dead, lanes=["quick"])
    for stage, payload in (("decompose", {"n_elements": 12}),
                           ("global", {"n_families": 40}),
                           ("shard_route", {"shards": []}),
                           ("fuse", {"report_path": "/tmp/x.fused.json"}),
                           ("screen", {"scores": {"US-1-A": 0.8}})):
        with runstore.Stage(rid, stage, attempt=row["attempts"]) as st:
            st.done(payload)
    assert runstore.resume_point(rid)[0] == "read"

    #  the worker is SIGKILLed here: no finish, no heartbeat
    _lapse(rid)
    runstore.reap(run_ids=[rid])
    again = runstore.claim(runstore.worker_id(), lanes=["quick"])
    assert again["attempts"] == 2
    stage, done = runstore.resume_point(rid)
    assert stage == "read", "a resumed run must not repeat the retrieval it already paid for"
    assert done["screen"]["scores"] == {"US-1-A": 0.8}
    assert done["fuse"]["report_path"] == "/tmp/x.fused.json"


def test_a_resumed_stage_does_not_run_its_body_again(slug):
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    runstore.claim(runstore.worker_id(), lanes=["quick"])
    calls = []
    for attempt in (1, 2):
        with runstore.Stage(rid, "screen", attempt=attempt) as st:
            if not st.resumed:
                calls.append(attempt)
                st.done({"scores": {"US-1-A": 0.5}})
    assert calls == [1], "the second attempt re-ran a stage that was already checkpointed"


def test_a_stage_whose_body_raises_is_not_a_checkpoint(slug):
    """A failed stage must be redone, and nothing before it."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    runstore.claim(runstore.worker_id(), lanes=["quick"])
    with pytest.raises(ValueError), runstore.Stage(rid, "screen", attempt=1):
        raise ValueError("the screener died")
    assert "screen" not in runstore.completed_stages(rid)
    assert runstore.resume_point(rid)[0] == "decompose"


def test_each_reference_and_each_rescue_round_is_its_own_row(slug):
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    runstore.claim(runstore.worker_id(), lanes=["quick"])
    for pub in ("US-1-A", "US-2-B2", "EP-3-A1"):
        runstore.substage(rid, "read", pub, payload={"found": True, "chars": 4000})
    runstore.substage(rid, "rescue", "1", payload={"claims": ["9", "11"]})
    reads = runstore.substages_done(rid, "read")
    assert set(reads) == {"US-1-A", "US-2-B2", "EP-3-A1"}
    assert reads["US-1-A"]["chars"] == 4000
    assert runstore.substages_done(rid, "rescue")["1"]["claims"] == ["9", "11"]
    #  substages never satisfy the pipeline resume point
    assert runstore.resume_point(rid)[0] == "decompose"


# ------------------------------------------------------------------ the ledgers
def test_the_candidate_row_accumulates_across_stages(slug):
    """Fusion writes a rank, screening writes a score, reading writes a verdict, and none of them
    erases what the last one recorded."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    runstore.upsert_candidates(rid, [{"pub": "US-1-A", "fused_rank": 3, "fused_score": 0.71,
                                      "stage": "fused"}])
    runstore.upsert_candidates(rid, [{"pub": "US-1-A", "screen_score": 88, "stage": "screened"}])
    runstore.upsert_candidates(rid, [{"pub": "US-1-A", "read_status": "read",
                                      "final_rank": 1, "stage": "final"}])
    c = runstore.candidates(rid)[0]
    assert (c["fused_rank"], c["screen_score"], c["read_status"], c["final_rank"], c["stage"]) \
        == (3, 88.0, "read", 1, "final")


def test_provider_usage_accumulates_per_run_not_per_process(slug):
    """The old receipt differenced a PROCESS-WIDE token counter, so two concurrent searches each
    attributed the other's spend to themselves. A row keyed by run cannot."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    runstore.record_usage(rid, "vertex", tier="FAST", model="flash-lite", calls=3,
                          prompt_tokens=1000, completion_tokens=120, latency_ms=900)
    runstore.record_usage(rid, "vertex", tier="FAST", model="flash-lite", calls=2,
                          prompt_tokens=500, errors=1)
    runstore.record_usage(rid, "vertex", tier="READ", model="flash", calls=1, prompt_tokens=9000)
    rows = {(u["tier"], u["model"]): u for u in runstore.usage(rid)}
    assert rows[("FAST", "flash-lite")]["calls"] == 5
    assert rows[("FAST", "flash-lite")]["prompt_tokens"] == 1500
    assert rows[("FAST", "flash-lite")]["errors"] == 1
    assert rows[("READ", "flash")]["prompt_tokens"] == 9000


def test_the_raw_hits_are_recorded_before_fusion(slug):
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    qid = runstore.record_query(rid, "dense", query_text="vacuum gripper sealing lip",
                                params={"ef_search": 200}, n_hits=2)
    assert runstore.record_hits(rid, "dense", [
        {"publication_number": "US-1-A", "rank": 1, "score": 0.91},
        {"publication_number": "US-2-B2", "rank": 2, "score": 0.83}], query_id=qid) == 2
    import db
    with db.cursor() as cur:
        cur.execute("SELECT channel, count(*) n FROM retrieval_hits WHERE run_id=%s "
                    "GROUP BY channel", (rid,))
        assert {r["channel"]: int(r["n"]) for r in cur.fetchall()} == {"dense": 2}


# ------------------------------------------------------------------ shard leases
def test_a_shard_lease_is_taken_refreshed_and_reaped(slug):
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    first = runstore.lease_shard(rid, "shard-vacuum-01", host="10.128.0.90", seconds=60)
    again = runstore.lease_shard(rid, "shard-vacuum-01", host="10.128.0.90", seconds=60)
    assert first["id"] == again["id"], "a refresh must extend the lease, not take a second one"
    assert len(runstore.held_shards(rid)) == 1
    import db
    with db.cursor() as cur:
        cur.execute("UPDATE shard_leases SET expires_at = now() - interval '1 second' "
                    "WHERE run_id=%s", (rid,))
    assert "shard-vacuum-01" in runstore.reap_shards()
    assert runstore.held_shards(rid) == [], "a dead run must not keep a shard VM awake"


# ------------------------------------------------------------------ the ingest queue
def test_repeat_demand_for_one_publication_bumps_the_count(slug):
    """A publication four searches wanted is worth more to the next corpus release than one that
    one search wanted, and that ranking has to come from the data."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    pub = f"US-{uuid.uuid4().hex[:8].upper()}-A1"
    try:
        a = runstore.queue_for_ingest(pub, run_id=rid, reason="no claims in corpus",
                                      source="epo:ops", scratch_ref=f"sources_docstore:{pub}")
        b = runstore.queue_for_ingest(pub, run_id=rid, reason="no claims in corpus")
        assert a["request_count"] == 1 and b["request_count"] == 2
        assert any(r["publication_number"] == pub for r in runstore.pending_ingest(limit=50))
        claimed = [r["publication_number"] for r in runstore.claim_ingest(limit=50)]
        assert pub in claimed
        assert runstore.mark_ingested([pub], corpus_release="test") == 1
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM corpus_ingest_queue WHERE publication_number=%s", (pub,))


# ------------------------------------------------------------------ progress
def test_progress_bumps_a_sequence_so_a_tail_can_see_new_events(slug):
    """The SSE endpoint tails this instead of a process global dict."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    before = runstore.progress_of(rid)["event_seq"]
    runstore.progress(rid, {"kind": "screening", "msg": "Screening 600 candidates…"})
    runstore.progress(rid, {"kind": "reading", "msg": "Read 40 of 420 references in full…"})
    after = runstore.progress_of(rid)
    assert after["event_seq"] == before + 2
    assert after["progress"]["kind"] == "reading"


def test_one_slug_never_has_two_live_runs(slug):
    """Two runs writing one report file is the race the in-process claim used to prevent."""
    a = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    b = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    assert a == b
    runstore.finish(a, None, "done")
    c = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    assert c != a, "a re-request after a finished run is a new run"
    import db
    with db.cursor() as cur:
        cur.execute("DELETE FROM search_runs WHERE run_id=%s", (c,))


def test_one_live_run_per_slug_is_enforced_by_postgres_not_a_select_race():
    """Two enqueue transactions can both observe no row, so the constraint must settle the race."""
    schema = Path(runstore.__file__).parents[1] / "sql" / "009_durable_runs.sql"
    ddl = schema.read_text()
    source = inspect.getsource(runstore.enqueue)
    assert "ux_search_runs_live_slug" in ddl and "UNIQUE INDEX" in ddl
    assert "ON CONFLICT" in source and "ux_search_runs_live_slug" in source


def test_concurrent_enqueues_converge_on_one_live_run(slug):
    barrier = threading.Barrier(9)
    run_ids, errors = [], []
    result_lock = threading.Lock()

    def enqueue_at_once():
        barrier.wait()
        try:
            run_id = runstore.enqueue(slug, {"query": "same"}, lane="quick")
            with result_lock:
                run_ids.append(run_id)
        except Exception as exc:  # the assertion below reports all thread failures together
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=enqueue_at_once) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors and len(run_ids) == 8
    assert len(set(run_ids)) == 1
    import db
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM search_runs WHERE slug=%s "
                    "AND status IN ('queued','running')", (slug,))
        assert cur.fetchone()["n"] == 1
