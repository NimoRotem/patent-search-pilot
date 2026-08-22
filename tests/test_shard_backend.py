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

    def __init__(self, instances=None):
        self.instances = instances or {}
        self.calls = []

    def instance(self, vm, zone, max_age=0.0):
        return self.instances.get(vm)

    def start(self, vm, zone):
        self.calls.append(("start", vm))
        inst = self.instances.get(vm)
        if inst is None:
            raise RuntimeError("no such instance")
        inst["status"] = "RUNNING"
        inst.setdefault("ip", "10.0.0.1")
        return {}

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
    assert shard_backend.shard_of("unclassified", real).shard == "08-unclassified"


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

    class Conn:
        closed = False

        def close(self):
            self.closed = True

    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: made.append(k) or Conn())
    conn = b.connection("B65G")
    assert conn is not None
    assert made and "default_transaction_read_only=on" in made[0].get("options", ""), \
        "a retrieval connection to a shard must be read only, like the corpus path"
    b.release("B65G", conn)
    assert b.connection("B65G") is conn, "release must return the connection to the pool"


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
