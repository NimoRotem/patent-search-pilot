"""The cold shard lifecycle: routing to a shard, waking it, refusing to serve from it while it is
still building, handing back a connection, and stopping it again without cutting a live query.

Everything except the lease tests runs against a fake GCE and a fake shard agent. That is
deliberate: the real state machine is what is under test, and a mock of the state machine would
test nothing. The lease tests use the real `shard_leases` table, because there is exactly one
lease store and a test against a second one would prove the wrong thing; they clean up after
themselves, like tests/test_durable_runs.py.
"""
import importlib.util
import os
import time
import uuid

import pytest

import runstore
from retrieval import shard_backend, shard_manager, shard_router

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ fakes
class FakeGce:
    """Enough Compute API to answer the state machine, and a log of what was asked of it."""

    def __init__(self, instances=None, capacity_failures=0):
        self.instances = instances or {}
        self.calls = []
        #  How many `start` operations fail with a zone stockout before one sticks. MEASURED
        #  2026-08-22: `instances.start` on a TERMINATED c4-highmem-16 in us-central1-b fails
        #  with ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS about six seconds after the POST
        #  returned success, and the same call succeeds later. A stopped VM holds no capacity
        #  reservation, so this is the cold tier's steady state and not a build time accident.
        self.capacity_failures = int(capacity_failures)
        self.starts_attempted = 0

    def instance(self, vm, zone, max_age=0.0):
        return self.instances.get(vm)

    def start(self, vm, zone):
        self.calls.append(("start", vm))
        inst = self.instances.get(vm)
        if inst is None:
            raise RuntimeError("no such instance")
        self.starts_attempted += 1
        if self.starts_attempted <= self.capacity_failures:
            return {"name": f"op-{self.starts_attempted}", "status": "RUNNING", "failed": True}
        inst["status"] = "RUNNING"
        inst.setdefault("ip", "10.0.0.1")
        return {"name": f"op-{self.starts_attempted}", "status": "RUNNING"}

    def wait_operation(self, op, zone, timeout=30.0, interval=0.0):
        if (op or {}).get("failed"):
            return False, "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS", \
                "the zone does not have enough resources available"
        return True, "", ""

    def stop(self, vm, zone):
        self.calls.append(("stop", vm))
        self.instances[vm]["status"] = "TERMINATED"
        return {}


def table():
    return [
        shard_backend.Shard("02-lifting", "vm-lifting", "z", ["B66"]),
        shard_backend.Shard("03-transport", "vm-transport", "z", ["B60", "B65"]),
        shard_backend.Shard("07-instruments", "vm-instruments", "z", ["G", "H"]),
        shard_backend.Shard("08-unclassified", "vm-unclassified", "z", ["*"]),
    ]


def health(state="hot", index="ready", available=None):
    return {"state": state, "available": (state == "hot") if available is None else available,
            "index": {"state": index, "generation": "g", "n_chunks": 7},
            "prewarm": {"state": "done"}, "postgres": {"accepting": True, "active_queries": 0}}


def backend(instances=None, probe=None, **kw):
    return shard_backend.ShardBackend(table=table(), gce=FakeGce(instances or {}),
                                      probe=probe or (lambda ip, port=0, timeout=0: None), **kw)


@pytest.fixture(autouse=True)
def _no_installed_backend():
    """Never leave a real backend registered behind a test: the seams are process globals."""
    yield
    shard_backend.uninstall()
    shard_backend.bind_run(None)


# ------------------------------------------------------------------ the shard table
def test_the_real_shard_table_routes_every_domain_somewhere():
    """`route()` can emit any CPC subclass and always emits `unclassified`. A domain with no shard
    is a query channel that silently does nothing, so the table must be total."""
    real = shard_backend.load_table()
    assert len(real) == 8, "the architecture is eight shards"
    for domain in ("B66C", "B25J", "B65G", "B23Q", "F16B", "G06F", "A61B", "unclassified", "ZZZZ"):
        assert shard_backend.shard_of(domain, real) is not None, domain
    unclassified = shard_backend.shard_of(shard_router.UNCLASSIFIED, real)
    assert unclassified is not None and unclassified.shard == "domain_08"


def test_the_unclassified_shard_answers_to_both_of_its_names():
    """`shard_router.domain_of` returns "unclassified" where `corpus_niche.subclass_of` returns "".
    They are the SAME 1,024,320 publications, 20.6% of the corpus. A shard reachable under only
    one of the two names makes that share quietly unreachable, and `hot_domains` would never
    match it because it compares the router's spelling against the manager's."""
    real = shard_backend.load_table()
    by_router = shard_backend.shard_of(shard_router.UNCLASSIFIED, real)
    by_niche = shard_backend.shard_of("", real)
    assert by_router is not None and by_niche is not None
    assert by_router.shard == by_niche.shard == "domain_08"
    #  And the catch-all covers a symbol the partition has never seen, which is the same problem.
    assert shard_backend.shard_of("ZZ99", real).shard == "domain_08"


def test_the_shard_table_is_workstream_o_s_partition_and_not_a_second_one():
    """There must not be two eight way partitions of the CPC subclasses. A subclass loaded onto
    one shard and routed to another is a shard that wakes, is queried and answers nothing, which
    downstream is indistinguishable from a genuine miss."""
    real = shard_backend.load_table()
    seen = {}
    for s in real:
        for p in s.prefixes:
            if p == "*":
                continue
            assert p not in seen, f"{p} is on both {seen.get(p)} and {s.shard}"
            seen[p] = s.shard
    #  602 entries in workstream O's plan: 601 CPC subclasses plus the `unclassified` route,
    #  which route() always emits and which the plan assigns like any other domain.
    assert len(seen) == 602, f"the partition covers {len(seen)} domains, not 602"
    assert seen.get("UNCLASSIFIED") == "domain_08"


def test_every_shard_is_in_a_zone_and_no_zone_holds_the_whole_fleet():
    """MEASURED 2026-08-22: a c4-highmem-16 `start` in us-central1-b fails with
    ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS, and a TERMINATED VM holds no capacity reservation,
    so this is the cold tier's steady state failure mode and not a build time one. Eight shards in
    one zone means one stockout takes the whole cold tier out at once."""
    real = shard_backend.load_table()
    zones = {}
    for s in real:
        assert s.zone, f"{s.shard} has no zone"
        zones.setdefault(s.zone, []).append(s.shard)
    assert len(zones) >= 3, f"the fleet is concentrated in {sorted(zones)}"
    assert max(len(v) for v in zones.values()) <= len(real) // 2, \
        f"one zone holds too much of the fleet: {zones}"


def test_a_longer_prefix_wins_and_a_shard_id_routes_to_itself():
    t = table()
    assert shard_backend.shard_of("B65G", t).shard == "03-transport"
    assert shard_backend.shard_of("B66C", t).shard == "02-lifting"
    assert shard_backend.shard_of("G01N", t).shard == "07-instruments"
    assert shard_backend.shard_of("QQQQ", t).shard == "08-unclassified"
    assert shard_backend.shard_of("03-transport", t).shard == "03-transport"


# ------------------------------------------------------------------ the state machine
def test_an_instance_that_does_not_exist_is_unknown_and_is_never_started():
    """Seven of the eight shards are not created, on purpose, because eight of these VMs and their
    disks is a spend decision. `unknown` is what keeps `ensure` from creating that bill."""
    b = backend({})                                   # GCE knows about nothing
    assert b.state("B65G") == "unknown"
    assert b.ensure(["B65G"], timeout=0.2) == {"B65G": "unknown"}
    assert b.gce.calls == [], "ensure tried to start an instance that does not exist"


def test_a_terminated_instance_is_cold_and_ensure_starts_it_exactly_once():
    inst = {"vm-transport": {"status": "TERMINATED", "ip": ""}}
    b = backend(inst, probe=lambda *a, **k: None)
    assert b.state("B65G") == "cold"
    b.ensure(["B65G", "B60J"], timeout=0.3)           # two domains, ONE shard
    assert b.gce.calls == [("start", "vm-transport")]


def test_a_shard_that_is_up_but_still_building_is_waking_and_serves_nothing():
    """THE GATE. `available()` must be able to say False while an index is mid build: an empty
    result set and a genuine miss are indistinguishable to fusion, and a miss is scored as a
    recall failure. A shard that is up and empty must say `waking`, not `hot`."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health(state="waking", index="building"))
    assert b.state("B65G") == "waking"
    assert b.connection("B65G") is None
    assert b.ensure(["B65G"], timeout=0.3) == {"B65G": "waking"}


def test_a_shard_that_claims_hot_without_available_is_still_not_served():
    """Defect injection for the gate above: a shard that says `hot` but reports available=False,
    which is exactly what a mid build index produces, must be downgraded by the controller. If
    `_state_of` stopped checking `available`, this test is the one that goes red."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health(state="hot", index="building", available=False))
    assert b.state("B65G") == "waking"
    assert b.connection("B65G") is None


def test_a_ready_shard_is_hot_and_hands_back_a_connection(monkeypatch):
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health(), dsn_for=lambda s: "host=x")
    assert b.state("B65G") == "hot"

    made = []
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: made.append(k) or RecordingConn())
    conn = b.connection("B65G")
    assert conn is not None
    assert made and "default_transaction_read_only=on" in made[0].get("options", ""), \
        "a retrieval connection to a shard must be read only, like the corpus path"
    b.release("B65G", conn)
    assert b.connection("B65G") is conn, "release must return the connection to the pool"


# ------------------------------------------------------------------ the session reset on release
class RecordingCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.conn.execute_error:
            raise RuntimeError(self.conn.execute_error)
        self.conn.executed.append(sql)

    def close(self):
        return None


class RecordingConn:
    """A connection that remembers every statement, so the reset is checkable rather than assumed."""

    def __init__(self, execute_error=""):
        self.closed = False
        self.executed = []
        self.rollbacks = 0
        self.execute_error = execute_error

    def cursor(self):
        return RecordingCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_release_resets_the_session_before_the_connection_is_pooled(monkeypatch):
    """REQUIRED BY docs/shard_and_global_seams.md. `cold.bind` applies the ANN scan profile
    (hnsw.ef_search and friends) to whatever connection it is handed, exactly as the hot path does
    to its own. A pool that hands a connection back without resetting it gives the NEXT caller the
    previous caller's scan width: a narrow element pass that follows a wide seed pass would run at
    seed width, silently, at a different recall. Nothing fails; the answer is just different."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health(), dsn_for=lambda s: "host=x")
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: RecordingConn())

    conn = b.connection("B65G")
    conn.executed.append("SET hnsw.ef_search = 900")          # what cold.bind does
    b.release("B65G", conn)

    assert "RESET ALL" in conn.executed, "release pooled a connection without resetting it"
    assert conn.rollbacks == 1, "a connection in a failed transaction refuses RESET; roll back first"
    assert not conn.closed
    assert b.connection("B65G") is conn


def test_release_does_not_deallocate_prepared_statements(monkeypatch):
    """RESET ALL and NOT `DISCARD ALL`. DISCARD ALL implies DEALLOCATE ALL, which throws away the
    server side prepared statements psycopg is still tracking client side, and the next query on
    that connection fails with `prepared statement "_pg3_N" does not exist`."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health(), dsn_for=lambda s: "host=x")
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: RecordingConn())
    conn = b.connection("B65G")
    b.release("B65G", conn)
    joined = " ".join(conn.executed).upper()
    assert "DISCARD" not in joined and "DEALLOCATE" not in joined


def test_a_connection_that_will_not_reset_is_closed_and_never_pooled(monkeypatch):
    """Defect injection: if the reset itself fails we do not know what is set on that session, so
    reusing it would leak exactly the state the reset exists to remove."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health(), dsn_for=lambda s: "host=x")
    conns = []

    def connect(*a, **k):
        c = RecordingConn(execute_error="server closed the connection unexpectedly")
        conns.append(c)
        return c

    monkeypatch.setattr("psycopg.connect", connect)
    conn = b.connection("B65G")
    b.release("B65G", conn)
    assert conn.closed, "a connection that would not reset was put back in the pool"
    assert b.connection("B65G") is not conn


def test_ensure_gives_up_at_the_timeout_and_reports_waking():
    """A shard that is not hot within SHARD_WAKE_TIMEOUT is NOT waited for. The cold tier runs
    beside the hot one so a cold miss costs the art it would have added and nothing else."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: None)     # the agent never answers
    t0 = time.time()
    out = b.ensure(["B65G"], timeout=1.0)
    elapsed = time.time() - t0
    assert out == {"B65G": "waking"}
    assert elapsed < 4.0, f"ensure blocked for {elapsed:.1f}s on a shard that never came up"


def test_hot_domains_returns_only_the_shards_that_can_actually_answer():
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"},
            "vm-lifting": {"status": "TERMINATED", "ip": ""}}

    def probe(ip, port=0, timeout=0):
        return health() if ip == "10.0.0.1" else None

    shard_backend.install(backend(inst, probe=probe))
    routes = [{"domain": "B65G", "weight": 0.6}, {"domain": "B66C", "weight": 0.4}]
    assert shard_manager.hot_domains(routes, timeout=0.3) == ["B65G"]


# ------------------------------------------------------------------ the seams degrade
def test_the_module_seams_swallow_a_backend_that_raises():
    """Every module level wrapper degrades: unknown state, no connection, no crash. An
    unavailable shard is never a failed search."""
    class Broken(shard_manager.ShardManagerBackend, shard_router.ShardRouterBackend):
        def state(self, domain):
            raise RuntimeError("boom")

        def ensure(self, domains, timeout=20.0):
            raise RuntimeError("boom")

        def connection(self, domain):
            raise RuntimeError("boom")

        def release(self, domain, conn):
            raise RuntimeError("boom")

        def wake(self, routes):
            raise RuntimeError("boom")

    shard_backend.install(Broken())
    assert shard_manager.state("B65G") == "unknown"
    assert shard_manager.ensure(["B65G"]) == {"B65G": "unknown"}
    assert shard_manager.connection("B65G") is None
    shard_manager.release("B65G", object())                       # must not raise
    assert shard_router.wake([{"domain": "B65G"}])["state"] == "failed"


def test_install_and_uninstall_move_both_seams_together():
    assert not shard_manager.available()
    b = backend({})
    shard_backend.install(b)
    assert shard_manager.available() and shard_manager.backend() is b
    shard_backend.uninstall()
    assert not shard_manager.available()


def test_wake_reports_what_it_started_and_what_does_not_exist():
    inst = {"vm-transport": {"status": "TERMINATED", "ip": ""},
            "vm-unclassified": {"status": "RUNNING", "ip": "10.0.0.9"}}
    b = backend(inst, probe=lambda ip, port=0, timeout=0: health() if ip == "10.0.0.9" else None)
    out = b.wake([{"domain": "B65G"}, {"domain": "unclassified"}, {"domain": "B66C"}])
    assert out["woken"] == ["03-transport"]
    assert out["already"] == ["08-unclassified"]
    assert out["absent"] == ["02-lifting"], "an uncreated shard must be reported, not started"
    assert ("start", "vm-lifting") not in b.gce.calls


# ------------------------------------------------------------------ leases (the real table)
@pytest.fixture()
def slug():
    s = f"test-shard-{uuid.uuid4().hex[:10]}"
    yield s
    import db
    with db.cursor() as cur:
        cur.execute("DELETE FROM search_runs WHERE slug=%s", (s,))


def test_waking_a_shard_takes_a_lease_in_the_table_that_already_exists(slug):
    """`shard_leases` is the store, and there is not a second one. The lease is the only thing
    that keeps a shard awake, so a wake that does not take one is a shard the reaper will stop
    under a live search."""
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    shard_backend.bind_run(rid)
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health())
    out = b.wake([{"domain": "B65G"}])
    assert out["leases"] == 1
    held = runstore.held_shards(rid)
    assert [h["shard"] for h in held] == ["03-transport"]
    assert held[0]["host"] == "10.0.0.1"
    assert b.held(b.shard_for("B65G")) == 1
    runstore.release_shards(rid)
    assert b.held(b.shard_for("B65G")) == 0


def test_ensure_refreshes_the_lease_rather_than_taking_a_second_one(slug):
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    shard_backend.bind_run(rid)
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health())
    b.ensure(["B65G"], timeout=0.3)
    b.ensure(["B65G"], timeout=0.3)
    assert len(runstore.held_shards(rid)) == 1


# ------------------------------------------------------------------ the idle reaper
def test_a_held_lease_keeps_a_shard_awake(slug):
    rid = runstore.enqueue(slug, {"query": "q"}, lane="quick")
    runstore.lease_shard(rid, "03-transport", host="10.0.0.1", seconds=300)
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health())
    acted = {a["shard"]: a for a in b.reap(idle_minutes=0.0)}
    assert acted["03-transport"]["action"] == "keep"
    assert "lease" in acted["03-transport"]["reason"]
    assert b.gce.calls == []


def test_an_in_flight_query_keeps_a_shard_that_has_no_lease_at_all(monkeypatch):
    """THE GUARD. A lease can expire under a query that is genuinely still running: the thing that
    heartbeats it is a thread, and a thread can die while the database keeps working. Stopping the
    VM under that query loses the work and returns a wrong answer to a live search."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health())
    monkeypatch.setattr(b, "idle_seconds", lambda shard: 9999.0)     # no lease, long idle
    monkeypatch.setattr(b, "activity", lambda shard: 2)              # but two queries running
    acted = {a["shard"]: a for a in b.reap(idle_minutes=15.0)}
    assert acted["03-transport"]["action"] == "keep"
    assert "in flight" in acted["03-transport"]["reason"]
    assert b.gce.calls == [], "the reaper stopped a shard with a query running on it"


def test_the_same_shard_is_stopped_the_moment_nothing_is_running(monkeypatch):
    """Defect injection for the guard above, from the other side: identical inputs except that
    the activity probe returns zero. If the in-flight check were dropped, the previous test would
    produce this result instead, so the two together pin the behaviour."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health())
    monkeypatch.setattr(b, "idle_seconds", lambda shard: 9999.0)
    monkeypatch.setattr(b, "activity", lambda shard: 0)
    acted = {a["shard"]: a for a in b.reap(idle_minutes=15.0)}
    assert acted["03-transport"]["action"] == "stop"
    assert b.gce.calls == [("stop", "vm-transport")]


def test_inside_the_idle_window_a_leaseless_shard_is_left_alone(monkeypatch):
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health())
    monkeypatch.setattr(b, "idle_seconds", lambda shard: 60.0)       # one minute, not fifteen
    monkeypatch.setattr(b, "activity", lambda shard: 0)
    acted = {a["shard"]: a for a in b.reap(idle_minutes=15.0)}
    assert acted["03-transport"]["action"] == "keep"
    assert b.gce.calls == []


def test_a_shard_nothing_can_reach_is_kept_until_the_hard_ceiling_then_stopped(monkeypatch):
    """Neither Postgres nor the agent answers, so we cannot prove nothing is running. Keep it for
    a while, because burn is cheaper than a lost search; stop it eventually, because an
    unreachable shard is not serving anybody and burn is not a safe default either."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: None)
    monkeypatch.setattr(b, "activity", lambda shard: None)

    monkeypatch.setattr(b, "idle_seconds", lambda shard: 20 * 60.0)
    assert b.reap(idle_minutes=15.0, hard_idle_minutes=60.0)[0]["action"] == "keep"
    assert b.gce.calls == []

    monkeypatch.setattr(b, "idle_seconds", lambda shard: 61 * 60.0)
    acted = b.reap(idle_minutes=15.0, hard_idle_minutes=60.0)
    assert acted[0]["action"] == "stop"
    assert b.gce.calls == [("stop", "vm-transport")]


def test_a_shard_that_is_already_cold_is_not_stopped_again():
    inst = {"vm-transport": {"status": "TERMINATED", "ip": ""}}
    b = backend(inst, probe=lambda *a, **k: None)
    assert b.reap(idle_minutes=0.0) == []
    assert b.gce.calls == []


def test_dry_run_stops_nothing(monkeypatch):
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1"}}
    b = backend(inst, probe=lambda *a, **k: health())
    monkeypatch.setattr(b, "idle_seconds", lambda shard: 9999.0)
    monkeypatch.setattr(b, "activity", lambda shard: 0)
    acted = b.reap(idle_minutes=15.0, dry_run=True)
    assert acted[0]["action"] == "would-stop"
    assert b.gce.calls == []


# ------------------------------------------------------------------ the agent on the shard
def _agent_module():
    path = os.path.join(REPO, "ops", "shards", "shard_agent.py")
    spec = importlib.util.spec_from_file_location("shard_agent_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_agent_says_waking_until_postgres_accepts(monkeypatch):
    agent = _agent_module()
    monkeypatch.setattr(agent, "_pg_accepting", lambda: False)
    monkeypatch.setattr(agent, "_tantivy", lambda: {"state": "down", "available": False})
    monkeypatch.setattr(agent, "CACHE_SECONDS", 0.0)
    snap = agent.snapshot()
    assert snap["state"] == "waking" and snap["available"] is False
    assert "not accepting" in snap["reason"]


def test_the_agent_refuses_to_call_an_unloaded_shard_hot(monkeypatch):
    """The same gate, at its source. A bootstrapped shard with no corpus on it is `building`, and
    an agent that called that `hot` would put an empty channel into fusion as if it were a real
    answer."""
    agent = _agent_module()
    monkeypatch.setattr(agent, "_pg_accepting", lambda: True)
    monkeypatch.setattr(agent, "_psql", lambda sql, timeout=3.0: "building||0")
    monkeypatch.setattr(agent, "_tantivy", lambda: {"state": "down", "available": False})
    monkeypatch.setattr(agent, "_active_queries", lambda: 0)
    monkeypatch.setattr(agent, "_prewarm", lambda: {"state": "done"})
    monkeypatch.setattr(agent, "CACHE_SECONDS", 0.0)
    snap = agent.snapshot()
    assert snap["state"] == "waking" and snap["available"] is False
    assert "building" in snap["reason"]


def test_the_agent_waits_for_the_blocking_half_of_prewarm(monkeypatch):
    """`hot` means the first query will not pay for the whole index. While the blocking phase is
    still reading, the shard is not that yet."""
    agent = _agent_module()
    monkeypatch.setattr(agent, "_pg_accepting", lambda: True)
    monkeypatch.setattr(agent, "_psql", lambda sql, timeout=3.0: "ready|rel-1|27000000")
    monkeypatch.setattr(agent, "_tantivy", lambda: {"state": "down", "available": False})
    monkeypatch.setattr(agent, "_active_queries", lambda: 0)
    monkeypatch.setattr(agent, "_prewarm", lambda: {"state": "running", "phase": "blocking"})
    monkeypatch.setattr(agent, "CACHE_SECONDS", 0.0)
    assert agent.snapshot()["state"] == "waking"

    monkeypatch.setattr(agent, "_prewarm", lambda: {"state": "background", "blocking_ms": 900})
    snap = agent.snapshot()
    assert snap["state"] == "hot" and snap["available"] is True
    assert snap["index"]["n_chunks"] == 27000000


def test_a_missing_tantivy_never_blocks_the_dense_channel(monkeypatch):
    """Workstream C owns the index; E owns that the process is there. A shard with no Tantivy is
    still hot, because the lexical channel has its own available() gate and falls back to
    Postgres, while the dense channel is unaffected."""
    agent = _agent_module()
    monkeypatch.setattr(agent, "_pg_accepting", lambda: True)
    monkeypatch.setattr(agent, "_psql", lambda sql, timeout=3.0: "ready|g|1")
    monkeypatch.setattr(agent, "_active_queries", lambda: 0)
    monkeypatch.setattr(agent, "_prewarm", lambda: {"state": "done"})
    monkeypatch.setattr(agent, "_tantivy", lambda: {"state": "absent", "available": False})
    monkeypatch.setattr(agent, "CACHE_SECONDS", 0.0)
    snap = agent.snapshot()
    assert snap["state"] == "hot"
    assert snap["tantivy"]["available"] is False


# =========================================================================== the real backend
# driving the real cold tier, against `retrieval.testing`'s synthetic shard. The point of that
# fixture living in `src/` is that the double and the real implementation are asserted by the same
# harness; these are those assertions, with the GCE calls and the psycopg connect faked and
# EVERYTHING ELSE the production code path.
def _cold_double(**attrs):
    """A Retriever with no database behind it, as tests/test_retrieval_cold.py builds one."""
    from retrieval import Retriever
    r = object.__new__(Retriever)
    r._fam = {}
    r._wide = False
    r.scan_profile = lambda wide=False: None
    r.channel_citation_family = lambda *a, **k: []
    r.channel_qbe = lambda *a, **k: []
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


@pytest.fixture
def real_backend_over_synthetic_shards(monkeypatch):
    """`shard_backend.ShardBackend` registered on both seams, with a fake GCE underneath and
    `retrieval.testing`'s synthetic shard as the thing on the other end of the connection."""
    from retrieval import cold, testing

    domains = ("B25J", "B65G")                          # domain_03 and domain_02 in the real table
    shards = {d: testing.shard(d, docs=[(100 + i, f"FAM-{d}-{i}") for i in range(3)])
              for d in domains}
    real = shard_backend.load_table()
    instances = {}
    for d in domains:
        sh = shard_backend.shard_of(d, real)
        instances[sh.vm] = {"status": "TERMINATED", "ip": f"10.9.0.{len(instances) + 1}"}

    b = shard_backend.ShardBackend(table=real, gce=FakeGce(instances),
                                   probe=lambda ip, port=0, timeout=0: health(),
                                   dsn_for=lambda s: f"host={s.vm}")

    def connect(dsn, **kw):
        for d in domains:
            if shard_backend.shard_of(d, real).vm in str(dsn):
                return testing.FakeConnection(shards[d])
        raise AssertionError(f"unexpected dsn {dsn!r}")

    monkeypatch.setattr("psycopg.connect", connect)
    monkeypatch.setattr(cold, "route_connection",
                        lambda: testing.routing_connection(
                            prior={"B25J": 900, "B65G": 600, shard_router.UNCLASSIFIED: 100}))
    shard_router.reset_prior()
    shard_backend.install(b)
    try:
        yield b, shards
    finally:
        shard_router.reset_prior()
        testing.reset()


def test_the_real_backend_wakes_routes_and_serves_the_cold_tier(real_backend_over_synthetic_shards):
    """End to end through the production seams: route -> wake -> ensure -> connection -> the same
    channel SQL -> release. Every hop is the real `ShardBackend`; only GCE and psycopg are faked."""
    from retrieval import cold
    b, shards = real_backend_over_synthetic_shards

    assert shard_manager.available(), "install() did not register the manager seam"
    assert shard_router.available(), "install() did not register the router seam"

    r = _cold_double(channel_dense=lambda *a, **k: [("hot1", 3.0), ("hot2", 2.0)])
    res = r.search("a vacuum gripper", config=["dense", "cold"], db_concurrency=2)

    cold_channels = [name for name in res.channel_hits if cold.is_cold(name)]
    assert cold_channels, f"no cold channel ran; tiers={res.tiers}"
    #  `opened`/`released` are the SyntheticShardManager's counters; the real backend is the
    #  manager here, so what proves a shard was served is that its SQL log has a channel query in
    #  it, and that the ids that came back are the shard's own.
    served = {d: sh for d, sh in shards.items() if sh.sql_log}
    assert served, "no synthetic shard was ever connected to"
    kinds = {kind for sh in served.values() for kind, _s, _p in sh.sql_log}
    assert kinds - {"set", "unknown", "families"}, \
        f"a shard was connected to but no channel query ran on it: {kinds}"
    cold_ids = {pid for name in cold_channels for pid in res.channel_hits[name]}
    assert cold_ids & {d.publication_id for sh in served.values() for d in sh.docs}, \
        "the cold channel returned nothing the shards actually hold"
    assert b._pool, "release did not pool a single connection"


def test_the_real_release_resets_the_session_on_the_path_cold_actually_uses(
        real_backend_over_synthetic_shards):
    """Not the unit test of `_reset_session` but the wiring: the connection `cold._query_domain`
    hands back through `shard_manager.release` must carry a RESET before it is pooled, because
    `cold.bind` set the ANN scan profile on it."""
    b, shards = real_backend_over_synthetic_shards
    r = _cold_double(channel_dense=lambda *a, **k: [("hot1", 3.0)])
    r.search("a vacuum gripper", config=["dense", "cold"], db_concurrency=2)

    used = [sh for sh in shards.values() if sh.sql_log]
    assert used, "no shard was queried"
    for sh in used:
        statements = [s for _kind, s, _p in sh.sql_log]
        assert any(s.startswith("SET hnsw.ef_search") for s in statements), \
            "cold.bind no longer applies the scan profile; this test is asserting nothing"
        assert "RESET ALL" in statements, \
            f"{sh.domain} was pooled without resetting the scan profile: {statements[-3:]}"


def test_uninstall_puts_both_seams_back_to_inert(real_backend_over_synthetic_shards):
    shard_backend.uninstall()
    assert not shard_manager.available()
    assert not shard_router.available()
    assert shard_manager.connection("B25J") is None
    assert shard_manager.ensure(["B25J"], timeout=0.1) == {"B25J": "unknown"}


# =========================================================================== registration
def test_the_backend_is_not_registered_unless_the_environment_says_so(monkeypatch):
    """The default is OFF and it is a corpus decision, not a code one. A registered backend makes
    `cold.available()` True, which costs a routing query and VM time on every search; until a
    release is loaded every shard answers `building`, so the cold tier can only pay and return
    nothing."""
    monkeypatch.delenv("SHARD_BACKEND_ENABLED", raising=False)
    assert shard_backend.enabled() is False
    assert shard_backend.install_if_enabled() is None
    assert not shard_manager.available()


def test_the_environment_switch_registers_both_seams(monkeypatch):
    monkeypatch.setenv("SHARD_BACKEND_ENABLED", "1")
    assert shard_backend.enabled() is True
    b = shard_backend.install_if_enabled(table=table(), gce=FakeGce({}),
                                         probe=lambda *a, **k: None)
    assert b is not None
    assert shard_manager.available() and shard_router.available()


def test_a_backend_that_cannot_be_built_does_not_stop_the_app(monkeypatch):
    """Defect injection: constructing the backend raises. The seams must keep their inert
    defaults rather than the import failing and taking the hot search path with it."""
    monkeypatch.setenv("SHARD_BACKEND_ENABLED", "1")
    monkeypatch.setattr(shard_backend, "ShardBackend",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no shard table")))
    assert shard_backend.install_if_enabled() is None
    assert not shard_manager.available()


def test_the_worker_binds_the_run_so_a_lease_has_an_owner():
    """`bind_run` had zero callers, so every wake took no lease and the reaper fell back to the
    instance's own start time: a shard stayed up for the whole idle window after a search that
    finished in twenty seconds."""
    import inspect

    from runner import worker
    src = inspect.getsource(worker.execute)
    assert "shard_backend.bind_run(run[\"run_id\"])" in src, \
        "the worker no longer attributes shard leases to the run it is executing"
    assert "shard_backend.bind_run(None)" in src, \
        "the worker no longer clears the bound run when the search finishes"


# =========================================================================== prewake
class _SymbolCursor:
    def __init__(self, symbols):
        self.symbols = list(symbols)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        assert "classifications" in sql

    def fetchall(self):
        return [{"symbol": s} for s in self.symbols]


class _SymbolConn:
    def __init__(self, symbols):
        self.symbols = symbols

    def cursor(self):
        return _SymbolCursor(self.symbols)

    def close(self):
        return None


def test_prewake_starts_the_subject_s_own_domains_before_the_cold_tier_asks(monkeypatch):
    """MEASURED: cold to hot is 25.0 s and SHARD_WAKE_TIMEOUT is 20 s, so a shard woken at the
    moment the cold tier wants it is never hot in time. The subject's own CPC symbols are 25% of
    the routing distribution and are knowable when the run is claimed, minutes earlier."""
    real = shard_backend.load_table()
    instances = {s.vm: {"status": "TERMINATED", "ip": ""} for s in real}
    b = shard_backend.ShardBackend(table=real, gce=FakeGce(instances),
                                   probe=lambda *a, **k: None)
    shard_backend.install(b)

    out = shard_backend.prewake_subject("EP1234567", conn=_SymbolConn(["B25J 9/00", "B66C 1/02"]))
    started = {vm for _verb, vm in b.gce.calls}
    assert shard_backend.shard_of("B25J", real).vm in started
    assert shard_backend.shard_of("B66C", real).vm in started
    #  route() always emits the unclassified route, 20.6% of the corpus. Its shard is always
    #  going to be asked for, so it is always worth starting.
    assert shard_backend.shard_of(shard_router.UNCLASSIFIED, real).vm in started
    assert out.get("woken")


def test_prewake_is_a_no_op_with_no_backend_installed():
    """It is called unconditionally from the worker, which must stay identical to today for
    anybody who has not turned the shard backend on."""
    shard_backend.uninstall()
    assert shard_backend.prewake_subject("EP1234567") == {}


def test_prewake_never_raises_when_the_lookup_fails(monkeypatch):
    class Boom:
        def cursor(self):
            raise RuntimeError("the corpus is unreachable")

    b = shard_backend.ShardBackend(table=table(), gce=FakeGce({}), probe=lambda *a, **k: None)
    shard_backend.install(b)
    assert shard_backend.prewake_subject("EP1234567", conn=Boom()) == {}


# =========================================================================== the zone stockout
def test_a_start_that_fails_on_zone_capacity_is_reissued_off_the_wake_path(monkeypatch):
    """MEASURED 2026-08-22, twice on patents-shard-03: the `start` POST returns success and the
    operation reaches DONE about six seconds later with ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS,
    leaving the instance TERMINATED. Nothing in the inherited backend looked at the operation, so
    a shard that failed to start looked exactly like a shard that was merely slow, and stayed down
    until some later query happened to ask for it again."""
    monkeypatch.setattr(shard_backend, "START_RETRY_SECONDS", 0.0)
    inst = {"vm-transport": {"status": "TERMINATED", "ip": ""}}
    gce = FakeGce(inst, capacity_failures=2)
    b = shard_backend.ShardBackend(table=table(), gce=gce, probe=lambda *a, **k: None)

    b._start(b.shard_for("B65G"))
    deadline = time.time() + 10.0
    while time.time() < deadline and inst["vm-transport"]["status"] != "RUNNING":
        time.sleep(0.05)
    assert inst["vm-transport"]["status"] == "RUNNING", \
        "a start that failed on zone capacity was never reissued"
    assert gce.starts_attempted == 3
    assert not b.start_errors(), f"the error was not cleared once the start stuck: {b.start_errors()}"


def test_ensure_does_not_wait_for_a_start_that_the_zone_refused(monkeypatch):
    """The retry must stay OFF the wake path. A search that waits for a zone to find capacity is
    a search that pays for the cold tier's problem, which is exactly what the 20 s budget forbids."""
    monkeypatch.setattr(shard_backend, "START_RETRY_SECONDS", 0.5)
    inst = {"vm-transport": {"status": "TERMINATED", "ip": ""}}
    b = shard_backend.ShardBackend(table=table(), gce=FakeGce(inst, capacity_failures=99),
                                   probe=lambda *a, **k: None)
    t0 = time.time()
    out = b.ensure(["B65G"], timeout=2.0)
    elapsed = time.time() - t0
    assert out == {"B65G": "cold"}, out
    assert elapsed < 3.0, f"ensure waited {elapsed:.1f}s on a zone with no capacity"


def test_wake_reports_why_a_domain_contributed_nothing(monkeypatch):
    """`wake()`'s payload lands in Result.tiers. A domain that answered nothing because the zone
    had no capacity must be distinguishable from one that answered nothing because it was empty."""
    monkeypatch.setattr(shard_backend, "START_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(shard_backend, "START_RETRIES", 1)
    inst = {"vm-transport": {"status": "TERMINATED", "ip": ""}}
    b = shard_backend.ShardBackend(table=table(), gce=FakeGce(inst, capacity_failures=99),
                                   probe=lambda *a, **k: None)
    b.wake([{"domain": "B65G", "weight": 1.0}])
    deadline = time.time() + 5.0
    while time.time() < deadline and not b.start_errors():
        time.sleep(0.05)
    out = b.wake([{"domain": "B65G", "weight": 1.0}])
    assert "ZONE_RESOURCE_POOL_EXHAUSTED" in out["errors"]["03-transport"]["code"]


# =========================================================================== the id gate
def _ops_module(name):
    path = os.path.join(REPO, "ops", "shards", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _RowCursor:
    """A cursor that answers the two queries verify_ids issues, and nothing else."""

    def __init__(self, rows):
        self.rows = rows
        self.out = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        low = " ".join(sql.split()).lower()
        if "count(*)" in low:
            self.out = [{"count": len(self.rows)}]
        elif "= any" in low:
            wanted = set((params or [[]])[0] or [])
            self.out = [{"id": i, "publication_number": n} for i, n in self.rows if i in wanted]
        else:
            self.out = [{"id": i, "publication_number": n} for i, n in self.rows]

    def fetchall(self):
        return list(self.out)

    def fetchone(self):
        return self.out[0] if self.out else None


class _RowConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _RowCursor(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        return None


def test_the_id_check_passes_when_the_shard_uses_the_corpus_ids():
    vi = _ops_module("verify_ids")
    shard_rows = [(11, "EP1A"), (12, "EP2A"), (13, "EP3A")]
    hot = _RowConn([(11, "EP1A"), (12, "EP2A"), (13, "EP3A"), (99, "EP9A")])
    matched, mismatched, absent = vi.compare(hot, shard_rows)
    assert (matched, mismatched, absent) == (3, [], 0)


def test_the_id_check_catches_a_renumbered_shard():
    """THE FAILURE IT EXISTS FOR. A shard that renumbered gives id 12 to a different publication.
    `retrieval.cold` hydrates the family key of every cold hit into the retriever's family map,
    filling gaps and never overwriting, so the hot corpus's family for id 12 would be attributed
    to the shard's document. Not a crash, not an empty result: a wrong answer that looks right."""
    vi = _ops_module("verify_ids")
    shard_rows = [(11, "EP1A"), (12, "JP-SOMETHING-ELSE"), (13, "EP3A")]
    hot = _RowConn([(11, "EP1A"), (12, "EP2A"), (13, "EP3A")])
    matched, mismatched, absent = vi.compare(hot, shard_rows)
    assert matched == 2 and absent == 0
    assert mismatched == [(12, "JP-SOMETHING-ELSE", "EP2A")]


def test_a_shard_holding_art_the_hot_corpus_does_not_is_not_a_failure():
    """Reaching art the hot corpus does not hold is the entire point of a cold shard, so an id
    the hot corpus has never seen is reported and is not a mismatch."""
    vi = _ops_module("verify_ids")
    shard_rows = [(11, "EP1A"), (77, "CN-ONLY-ON-THE-SHARD")]
    hot = _RowConn([(11, "EP1A")])
    matched, mismatched, absent = vi.compare(hot, shard_rows)
    assert (matched, mismatched, absent) == (1, [], 1)


def test_ready_refuses_a_shard_that_fails_the_id_check():
    """The gate is in shardctl.sh `ready`, which is the only thing that can make a shard `hot`."""
    script = open(os.path.join(REPO, "ops", "shards", "shardctl.sh")).read()
    body = script.split("cmd_ready() {", 1)[1].split("\ncmd_", 1)[0]
    assert "cmd_verify_ids" in body, "ready no longer runs the publication id check"
    assert "refusing to mark it ready" in body


# =========================================================================== the reap hold
def test_a_held_instance_is_never_reaped_however_idle(monkeypatch):
    """A corpus load runs for hours, holds no SEARCH lease because it is not a search, and is idle
    between batches, so the in-flight-query rule cannot protect it on its own. The reaper would
    stop the VM in one of the gaps and lose the load. `reap-hold` beats every other test."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1",
                             "labels": {"reap-hold": "on"}}}
    b = backend(inst, probe=lambda *a, **k: health())
    monkeypatch.setattr(b, "held", lambda shard: 0)
    monkeypatch.setattr(b, "idle_seconds", lambda shard: 99999.0)
    monkeypatch.setattr(b, "activity", lambda shard: 0)
    actions = {x["shard"]: x for x in b.reap(idle_minutes=0.0, hard_idle_minutes=0.0)}
    assert actions["03-transport"]["action"] == "keep"
    assert "reap-hold" in actions["03-transport"]["reason"]
    assert ("stop", "vm-transport") not in b.gce.calls


def test_without_the_hold_the_same_shard_is_stopped(monkeypatch):
    """Defect injection for the test above: remove the label and the identical shard goes."""
    inst = {"vm-transport": {"status": "RUNNING", "ip": "10.0.0.1", "labels": {}}}
    b = backend(inst, probe=lambda *a, **k: health())
    monkeypatch.setattr(b, "held", lambda shard: 0)
    monkeypatch.setattr(b, "idle_seconds", lambda shard: 99999.0)
    monkeypatch.setattr(b, "activity", lambda shard: 0)
    actions = {x["shard"]: x for x in b.reap(idle_minutes=0.0, hard_idle_minutes=0.0)}
    assert actions["03-transport"]["action"] == "stop"
    assert ("stop", "vm-transport") in b.gce.calls


def test_a_start_in_flight_is_waking_not_cold(monkeypatch):
    """REGRESSION, found by ops/shards/lifecycle.py against a real VM. GCE reports TERMINATED for
    several seconds after `instances.start` returns. `_state_of` read that back immediately,
    called the shard cold, and `ensure` returned in 1.6 s having woken nothing: every wake through
    the seam failed while wakebench, which polls GCE directly, succeeded."""

    class SlowGce(FakeGce):
        """A start that takes effect later, which is what a real one does."""

        def start(self, vm, zone):
            self.calls.append(("start", vm))
            return {"name": "op-1", "status": "RUNNING"}

        def wait_operation(self, op, zone, timeout=30.0, interval=0.0):
            return True, "", ""

    inst = {"vm-transport": {"status": "TERMINATED", "ip": ""}}
    b = shard_backend.ShardBackend(table=table(), gce=SlowGce(inst),
                                   probe=lambda *a, **k: None)
    assert b.state("B65G") == "cold", "an untouched TERMINATED shard is cold"
    out = b.ensure(["B65G"], timeout=1.0)
    assert out == {"B65G": "waking"}, \
        f"a start we just issued was reported as {out}; the wake gave up before GCE moved"
    assert b.gce.calls == [("start", "vm-transport")]


def test_a_start_the_zone_refused_is_cold_again_at_once(monkeypatch):
    """The other half: `waking` must not become a blanket excuse. Once the operation has come back
    with a capacity error the shard is not coming up, and a search must be told so immediately
    rather than spending its whole wake budget on it."""
    monkeypatch.setattr(shard_backend, "START_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(shard_backend, "START_RETRIES", 1)
    inst = {"vm-transport": {"status": "TERMINATED", "ip": ""}}
    b = shard_backend.ShardBackend(table=table(), gce=FakeGce(inst, capacity_failures=99),
                                   probe=lambda *a, **k: None)
    b._start(b.shard_for("B65G"))
    deadline = time.time() + 5.0
    while time.time() < deadline and not b.start_errors():
        time.sleep(0.05)
    assert b.state("B65G") == "cold", "a shard the zone refused was still being waited for"
