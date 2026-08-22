"""Workstream E's implementation of the two shard seams, against real GCE instances.

`shard_manager` and `shard_router` define what retrieval may ask for; this is the thing that
actually starts a VM, decides whether it is serving, hands back a connection to it and gives it up
again. `install()` registers it with both. Nothing here is imported for effect: a process that
never calls `install()` keeps the inert `_NoShards` / `_NoWake` behaviour, which is what the whole
test suite and every offline script want.

THE STATE MACHINE, and who is entitled to each answer.

    cold      the CONTROLLER says this: the instance is TERMINATED, SUSPENDED or on its way there.
    waking    the instance is up but the box has not said it is serving. That covers ninety
              seconds of kernel boot, Postgres starting, and the blocking half of prewarm.
    hot       THE SHARD says this, never the controller: Postgres accepts, `shard_status` says the
              corpus on it is `ready`, and prewarm's blocking phase has returned.
    unknown   no such instance, or GCE would not answer. Never started, never waited for.

The asymmetry is the point. RUNNING is not serving, and only the box knows which of the several
not-serving states it is in, so `hot` is the shard agent's word (ops/shards/shard_agent.py) and
the controller may only ever downgrade it.

WHY A SHARD THAT IS UP AND EMPTY IS NOT HOT. `available()` has to be able to say False while an
index is mid build. An empty result set and a genuine miss are indistinguishable to fusion, and a
miss is scored as a recall failure, so a shard that cannot answer must say so rather than answer
nothing. `shard_status.state` is what says it, and it is `building` from bootstrap until whoever
loaded the shard commits the flip in the load's own transaction.

THE LEASE IS THE ONLY THING KEEPING A SHARD AWAKE. `shard_leases` (sql/009_durable_runs.sql,
runstore.lease_shard) already exists and is the store; there is not a second one. `ensure` and
`wake` take or refresh a lease for the run bound with `bind_run`, the run's own heartbeat thread
refreshes it (runstore.Heartbeat), and `reap()` stops any shard that no run holds and no query is
running on. A shard nobody leased is a shard nobody is paying for on purpose.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request

from . import shard_manager, shard_router
from .shard_manager import WAKE_TIMEOUT

#  Where the shard table lives. One file, read by this and by ops/shards/shardctl.sh.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHARD_TABLE = os.environ.get("SHARD_TABLE", os.path.join(_REPO, "ops", "shards", "shards.tsv"))

AGENT_PORT = int(os.environ.get("SHARD_AGENT_PORT", "8639"))
SHARD_PGPORT = int(os.environ.get("SHARD_PGPORT", "5432"))
SHARD_PGDATABASE = os.environ.get("SHARD_PGDATABASE", "patents")
SHARD_PGUSER = os.environ.get("SHARD_PGUSER", "patents")
#  How long a wake may keep a lease without a heartbeat. runstore's own default; named here so a
#  reader can see that this is not a second lease policy.
LEASE_SECONDS = int(os.environ.get("SHARD_LEASE_SECONDS", "300"))
#  A shard with no lease is stopped after this long. The architecture's 15 minutes.
IDLE_MINUTES = float(os.environ.get("SHARD_IDLE_MINUTES", "15"))
#  A shard we cannot get an activity answer out of is stopped after this long regardless: it
#  cannot be serving a query for anybody if nothing can reach it, and burn is not a safe default.
HARD_IDLE_MINUTES = float(os.environ.get("SHARD_HARD_IDLE_MINUTES", "60"))
HEALTH_TIMEOUT = float(os.environ.get("SHARD_HEALTH_TIMEOUT", "2.0"))
#  A wake poll is cheap; the instance describe behind it is not, so it is cached for this long.
INSTANCE_CACHE_SECONDS = float(os.environ.get("SHARD_INSTANCE_CACHE_SECONDS", "3.0"))
POLL_SECONDS = float(os.environ.get("SHARD_POLL_SECONDS", "1.0"))
MAX_POOLED = int(os.environ.get("SHARD_MAX_POOLED_CONNECTIONS", "4"))
#  How many times a `start` that failed on ZONE CAPACITY is reissued, and how long to wait between
#  attempts. MEASURED 2026-08-22: `instances.start` on a TERMINATED c4-highmem-16 in us-central1-b
#  fails with ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS and the same call succeeds minutes later.
#  A stopped VM holds no capacity reservation, so this is a permanent property of the cold tier and
#  not a one-off. The retry runs OFF the wake path: `ensure` never waits for it.
START_RETRIES = int(os.environ.get("SHARD_START_RETRIES", "5"))
START_RETRY_SECONDS = float(os.environ.get("SHARD_START_RETRY_SECONDS", "20"))
START_OP_TIMEOUT = float(os.environ.get("SHARD_START_OP_TIMEOUT", "60"))
#  Capacity errors, and only capacity errors, are worth reissuing. A permission or quota error will
#  never clear by asking again.
_CAPACITY_CODES = ("ZONE_RESOURCE_POOL_EXHAUSTED", "RESOURCE_POOL_EXHAUSTED",
                   "QUOTA_EXCEEDED_ZONE_RESOURCE", "RESOURCE_AVAILABILITY")

_COLD_STATUSES = {"TERMINATED", "STOPPED", "STOPPING", "SUSPENDED", "SUSPENDING"}
_UP_STATUSES = {"PROVISIONING", "STAGING", "RUNNING"}


# ---------------------------------------------------------------------------------------------
# the shard table: domain (a CPC subclass) -> shard
# ---------------------------------------------------------------------------------------------
class Shard:
    __slots__ = ("shard", "vm", "zone", "prefixes")

    def __init__(self, shard, vm, zone, prefixes):
        self.shard, self.vm, self.zone = shard, vm, zone
        self.prefixes = tuple(p for p in prefixes if p)

    def __repr__(self):
        return f"<Shard {self.shard} {self.vm}@{self.zone}>"


def load_table(path=None):
    """-> [Shard] in file order. The last row with prefix `*` is the catch-all."""
    rows = []
    with open(path or SHARD_TABLE) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            prefixes = parts[3].split(",") if len(parts) > 3 else []
            rows.append(Shard(parts[0].strip(), parts[1].strip(), parts[2].strip(),
                              [p.strip().upper() for p in prefixes]))
    return rows


def shard_of(domain, table):
    """A router domain -> the shard that holds it. Longest matching prefix, then the catch-all.

    A shard id is accepted as itself, so `shardctl.sh wake 03-transport` and a router route both
    reach the same code rather than two spellings of it.
    """
    d = str(domain or "").strip().upper()
    for s in table:
        if s.shard.upper() == d:
            return s
    best = None
    for s in table:
        for p in s.prefixes:
            if p != "*" and d.startswith(p) and (best is None or len(p) > best[0]):
                best = (len(p), s)
    if best:
        return best[1]
    for s in table:
        if "*" in s.prefixes:
            return s
    return None


# ---------------------------------------------------------------------------------------------
# GCE, over REST rather than the gcloud CLI
# ---------------------------------------------------------------------------------------------
class GceClient:
    """Just enough Compute API to start, stop and look at an instance.

    REST and not `gcloud`, because the CLI costs about a second and a half of Python startup per
    call and the entire wake budget is twenty seconds. urllib and not a client library, because
    this has to import cleanly in the search worker's venv without adding a dependency to it.
    """

    METADATA = "http://metadata.google.internal/computeMetadata/v1"
    BASE = "https://compute.googleapis.com/compute/v1"

    def __init__(self, project=None, token=None):
        self._project = project
        self._static_token = token or os.environ.get("SHARD_GCE_TOKEN") or None
        self._token, self._token_expires = None, 0.0
        self._lock = threading.Lock()
        self._cache = {}

    # -- auth ---------------------------------------------------------------------------------
    def project(self):
        if self._project:
            return self._project
        self._project = os.environ.get("GCP_PROJECT") or self._metadata("project/project-id") \
            or "nimo-gpt"
        return self._project

    def _metadata(self, path, timeout=1.0):
        req = urllib.request.Request(f"{self.METADATA}/{path}",
                                     headers={"Metadata-Flavor": "Google"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode().strip()
        except Exception:
            return ""

    def token(self):
        if self._static_token:
            return self._static_token
        with self._lock:
            if self._token and time.time() < self._token_expires:
                return self._token
            raw = self._metadata("instance/service-accounts/default/token", timeout=2.0)
            if raw:
                try:
                    tok = json.loads(raw)
                    self._token = tok["access_token"]
                    self._token_expires = time.time() + max(60, int(tok.get("expires_in", 3600))) - 60
                    return self._token
                except Exception:
                    pass
            #  Off a GCE box (a laptop, a CI runner) fall back to the CLI once and cache it.
            try:
                out = subprocess.run(["gcloud", "auth", "print-access-token"],
                                     capture_output=True, text=True, timeout=30)
                if out.returncode == 0 and out.stdout.strip():
                    self._token = out.stdout.strip()
                    self._token_expires = time.time() + 1800
                    return self._token
            except Exception:
                pass
            return ""

    def _call(self, method, url, timeout=10.0):
        req = urllib.request.Request(url, method=method,
                                     headers={"Authorization": f"Bearer {self.token()}",
                                              "Content-Length": "0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")

    # -- instances ----------------------------------------------------------------------------
    def instance(self, vm, zone, max_age=INSTANCE_CACHE_SECONDS):
        """-> {"status", "ip"} or None when there is no such instance."""
        key = (vm, zone)
        hit = self._cache.get(key)
        if hit and (time.time() - hit[0]) < max_age:
            return hit[1]
        url = f"{self.BASE}/projects/{self.project()}/zones/{zone}/instances/{vm}"
        try:
            body = self._call("GET", url)
            nics = body.get("networkInterfaces") or [{}]
            info = {"status": body.get("status", ""), "ip": nics[0].get("networkIP", ""),
                    "last_start": body.get("lastStartTimestamp", "")}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                info = None
            else:
                raise
        self._cache[key] = (time.time(), info)
        return info

    def _act(self, vm, zone, verb):
        self._cache.pop((vm, zone), None)         # never answer from a cache we just invalidated
        url = f"{self.BASE}/projects/{self.project()}/zones/{zone}/instances/{vm}/{verb}"
        return self._call("POST", url, timeout=30.0)

    def start(self, vm, zone):
        return self._act(vm, zone, "start")

    def stop(self, vm, zone):
        return self._act(vm, zone, "stop")

    def operation(self, name, zone):
        """One zone operation by name. -> the operation body."""
        url = f"{self.BASE}/projects/{self.project()}/zones/{zone}/operations/{name}"
        return self._call("GET", url)

    def wait_operation(self, op, zone, timeout=30.0, interval=0.5):
        """Poll a zone operation to DONE. -> (ok, error_code, message).

        MEASURED 2026-08-22: a `start` POST returns `status: RUNNING` with no error even when the
        zone has no capacity, and the operation reaches DONE about six seconds later carrying
        `error.errors[0].code = ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS`. So a start that will
        fail is knowable well inside the 20 s wake budget, but only by asking.
        """
        name = (op or {}).get("name")
        if not name:
            return True, "", ""
        deadline = time.time() + float(timeout)
        body = op
        while True:
            if (body or {}).get("status") == "DONE":
                break
            if time.time() >= deadline:
                return False, "TIMEOUT", f"operation {name} still {(body or {}).get('status')}"
            time.sleep(interval)
            try:
                body = self.operation(name, zone)
            except Exception as e:                                       # noqa: BLE001
                return False, "UNREACHABLE", str(e)[:200]
        err = (body or {}).get("error") or {}
        errors = err.get("errors") or []
        if not errors:
            return True, "", ""
        return False, str(errors[0].get("code") or "ERROR"), str(errors[0].get("message") or "")[:300]


def probe_agent(ip, port=AGENT_PORT, timeout=HEALTH_TIMEOUT):
    """The shard's own health payload, or None when it is not answering."""
    try:
        with urllib.request.urlopen(f"http://{ip}:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------
# the run this process is waking shards for
# ---------------------------------------------------------------------------------------------
_RUN_LOCK = threading.Lock()
_RUN_ID = None


def bind_run(run_id):
    """Attribute every lease this backend takes to `run_id`.

    A module global and not a contextvar, for the reason runctx gives: the pipeline fans out over
    ThreadPoolExecutors and contextvars do not survive `submit`. A worker process runs one search,
    so one bound run is the whole truth; when nothing is bound, shards are woken WITHOUT a lease
    and the reaper falls back to the idle window from the instance's own start time.
    """
    global _RUN_ID
    with _RUN_LOCK:
        _RUN_ID = run_id or None
    return _RUN_ID


def current_run():
    with _RUN_LOCK:
        return _RUN_ID


# ---------------------------------------------------------------------------------------------
# the backend
# ---------------------------------------------------------------------------------------------
class ShardBackend(shard_manager.ShardManagerBackend, shard_router.ShardRouterBackend):
    """Both seams in one object: the thing that knows a shard's state is the thing that wakes it.

    Everything is injectable, so the tests exercise the real state machine against a fake GCE and
    a fake agent rather than a mock of the state machine itself.
    """

    def __init__(self, table=None, gce=None, probe=None, dsn_for=None, run_id=None,
                 agent_port=AGENT_PORT):
        self.table = table if table is not None else load_table()
        self.gce = gce if gce is not None else GceClient()
        self.probe = probe or probe_agent
        self.agent_port = agent_port
        self._dsn_for = dsn_for
        self._run_id = run_id
        self._pool = {}
        self._pool_lock = threading.Lock()
        self._starting = {}
        self._start_errors = {}
        self._start_lock = threading.Lock()

    # -- identity -----------------------------------------------------------------------------
    def shard_for(self, domain):
        return shard_of(domain, self.table)

    def run_id(self):
        return self._run_id or current_run()

    def dsn(self, shard):
        if self._dsn_for:
            return self._dsn_for(shard)
        info = self.gce.instance(shard.vm, shard.zone)
        if not info or not info.get("ip"):
            return None
        try:
            from config import PG
            password = os.environ.get("SHARD_PGPASSWORD") or PG["password"]
        except Exception:
            password = os.environ.get("SHARD_PGPASSWORD", "")
        return (f"host={info['ip']} port={SHARD_PGPORT} dbname={SHARD_PGDATABASE} "
                f"user={SHARD_PGUSER} password={password} connect_timeout=5")

    # -- ShardManagerBackend ------------------------------------------------------------------
    def state(self, domain):
        shard = self.shard_for(domain)
        if shard is None:
            return "unknown"
        return self._state_of(shard)[0]

    def _state_of(self, shard):
        """-> (state, health payload or None). The only place a state is decided."""
        try:
            info = self.gce.instance(shard.vm, shard.zone)
        except Exception:
            traceback.print_exc()
            return "unknown", None
        if info is None:
            #  Not created. Seven of the eight are in exactly this state until the spend decision
            #  is made, and `unknown` is what stops `ensure` trying to start something that does
            #  not exist.
            return "unknown", None
        status = (info.get("status") or "").upper()
        if status in _COLD_STATUSES:
            return "cold", None
        if status not in _UP_STATUSES:
            return "unknown", None
        ip = info.get("ip")
        if not ip:
            return "waking", None
        health = self.probe(ip, self.agent_port)
        if not health:
            return "waking", None
        reported = str(health.get("state") or "").lower()
        if reported == "hot" and health.get("available"):
            return "hot", health
        #  The controller may downgrade the shard's answer, never upgrade it.
        return ("waking" if reported in ("hot", "waking", "") else reported), health

    def ensure(self, domains, timeout=WAKE_TIMEOUT):
        """Start whatever is cold, wait at most `timeout`, report honestly. Never raises.

        A shard that is not hot when the clock runs out is left starting and reported as `waking`.
        It is NOT waited for: the cold tier runs beside the hot one so that a cold miss costs the
        art it would have added and nothing else.
        """
        domains = [d for d in (domains or [])]
        if not domains:
            return {}
        want = {}
        for d in domains:
            s = self.shard_for(d)
            if s is not None:
                want.setdefault(s.shard, s)

        deadline = time.time() + float(timeout)
        states = {}

        def refresh(shard):
            states[shard.shard] = self._state_of(shard)[0]

        self._parallel(refresh, want.values())

        cold = [s for s in want.values() if states.get(s.shard) == "cold"]
        if cold:
            self._parallel(self._start, cold)
            for s in cold:
                states[s.shard] = "waking"

        while time.time() < deadline and any(v == "waking" for v in states.values()):
            time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.time())))
            pending = [s for s in want.values() if states.get(s.shard) == "waking"]
            if not pending:
                break
            self._parallel(refresh, pending)

        self._lease(s for s in want.values() if states.get(s.shard) in ("hot", "waking"))
        return {d: states.get(getattr(self.shard_for(d), "shard", ""), "unknown") for d in domains}

    def connection(self, domain):
        """A psycopg connection to `domain`'s shard, or None. Read only, like the corpus path."""
        shard = self.shard_for(domain)
        if shard is None or self._state_of(shard)[0] != "hot":
            return None
        with self._pool_lock:
            pool = self._pool.setdefault(shard.shard, [])
            while pool:
                conn = pool.pop()
                if not getattr(conn, "closed", True):
                    return conn
        dsn = self.dsn(shard)
        if not dsn:
            return None
        import psycopg
        from psycopg.rows import dict_row
        #  A session GUC and not `conn.read_only`, for the reason db.connect gives: psycopg
        #  applies the transaction property on BEGIN and an autocommit connection never issues
        #  one, so the attribute is silently a no-op exactly where retrieval uses it.
        return psycopg.connect(dsn, autocommit=True, row_factory=dict_row,
                               options="-c default_transaction_read_only=on")

    def release(self, domain, conn):
        """Give a connection back. RESETS THE SESSION FIRST, and closes it if that fails.

        `cold.bind` applies the ANN scan profile to whatever connection it is handed
        (`hnsw.ef_search`, `hnsw.iterative_scan`, `hnsw.max_scan_tuples`), exactly as the hot path
        does to its own, because a dense channel at the shard's defaults is a different query. A
        pool that hands a connection back without resetting it therefore hands the NEXT caller the
        previous caller's scan width: a narrow element pass that ran after a wide seed pass would
        silently run at seed width, which is slower and a different recall, and nothing would fail.
        `docs/shard_and_global_seams.md` requires the reset; this is it.
        """
        if conn is None:
            return
        shard = self.shard_for(domain)
        if shard is None or getattr(conn, "closed", True):
            self._close(conn)
            return
        if not self._reset_session(conn):
            #  A connection we could not clean is not safe to reuse: we do not know what is set on
            #  it. Closing costs one reconnect, which is cheaper than a wrong scan profile that
            #  never announces itself.
            self._close(conn)
            return
        with self._pool_lock:
            pool = self._pool.setdefault(shard.shard, [])
            if len(pool) < MAX_POOLED:
                pool.append(conn)
                return
        self._close(conn)

    @staticmethod
    def _reset_session(conn):
        """Undo every `SET` this connection has seen. -> False when the connection is unusable.

        RESET ALL and NOT `DISCARD ALL`. `DISCARD ALL` implies `DEALLOCATE ALL`, which throws away
        the server side prepared statements psycopg is still tracking client side, and the next
        query on that connection fails with `prepared statement "_pg3_N" does not exist`. RESET ALL
        returns every run-time parameter to its session default, which for a connection opened with
        `options=-c default_transaction_read_only=on` is still `on`: a startup packet value IS the
        session default, so the read only guard survives the reset. There is a test for exactly
        that, because getting it wrong makes a cold shard writable and nothing would say so.
        """
        #  A connection left in a failed transaction refuses everything, RESET included, so the
        #  rollback comes first. On an autocommit connection with nothing open it is a no-op.
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            with conn.cursor() as cur:
                cur.execute("RESET ALL")
            return True
        except Exception:
            return False

    @staticmethod
    def _close(conn):
        try:
            conn.close()
        except Exception:
            pass

    # -- ShardRouterBackend -------------------------------------------------------------------
    def wake(self, routes):
        """Start the shards these routes name. Returns immediately: waiting is `ensure`'s job."""
        routes = list(routes or [])
        domains = [r.get("domain") for r in routes if isinstance(r, dict)] or \
                  [r for r in routes if isinstance(r, str)]
        want, unknown = {}, []
        for d in domains:
            s = self.shard_for(d)
            if s is None:
                unknown.append(d)
            else:
                want.setdefault(s.shard, s)

        woken, already, absent = [], [], []
        for s in want.values():
            state = self._state_of(s)[0]
            if state == "cold":
                self._start(s)
                woken.append(s.shard)
            elif state == "unknown":
                absent.append(s.shard)
            else:
                already.append(s.shard)
        leases = self._lease(s for s in want.values() if s.shard not in absent)
        return {"woken": woken, "already": already, "absent": absent,
                "state": "ok", "routes": domains, "unrouted": unknown,
                "leases": leases, "run_id": self.run_id(),
                "errors": {k: v for k, v in self.start_errors().items() if k in want}}

    # -- lifecycle ----------------------------------------------------------------------------
    def _start(self, shard):
        """Start once, and watch the operation off the wake path.

        Two channels routing to the same shard in the same second must not send two start calls,
        and GCE answers the second one with a 400 rather than a shrug, so the issue is deduped.

        THE WATCHER IS WHY THIS IS NOT ONE LINE. A `start` POST returns success and the operation
        fails six seconds later with a zone capacity error, leaving the instance TERMINATED. The
        search must not wait for that: `ensure` polls state, sees TERMINATED, reports `cold` and
        moves on, which is the fail-soft contract. But somebody has to reissue the start, or the
        shard stays down until the next query happens to ask for it. A daemon thread does the
        reissue and records the last error, which `wake()` returns so a run can say WHY a domain
        contributed nothing.
        """
        with self._start_lock:
            last = self._starting.get(shard.shard, 0.0)
            if time.time() - last < 30.0:
                return False
            self._starting[shard.shard] = time.time()
        try:
            op = self.gce.start(shard.vm, shard.zone)
        except Exception as e:                                            # noqa: BLE001
            traceback.print_exc()
            self._record_start_error(shard, "CALL_FAILED", str(e)[:200])
            return False
        t = threading.Thread(target=self._watch_start, args=(shard, op), daemon=True)
        t.start()
        return True

    def _record_start_error(self, shard, code, message):
        with self._start_lock:
            if code:
                self._start_errors[shard.shard] = {"code": code, "message": message,
                                                   "at": time.time()}
            else:
                self._start_errors.pop(shard.shard, None)

    def _watch_start(self, shard, op):
        """Follow a start operation and reissue it while the zone has no capacity."""
        for attempt in range(1, START_RETRIES + 1):
            try:
                ok, code, message = self.gce.wait_operation(op, shard.zone,
                                                            timeout=START_OP_TIMEOUT)
            except Exception as e:                                        # noqa: BLE001
                ok, code, message = False, "UNREACHABLE", str(e)[:200]
            if ok:
                self._record_start_error(shard, "", "")
                return True
            self._record_start_error(shard, code, message)
            if not any(c in code for c in _CAPACITY_CODES) or attempt >= START_RETRIES:
                return False
            time.sleep(START_RETRY_SECONDS)
            self.gce._cache.pop((shard.vm, shard.zone), None)
            info = self.gce.instance(shard.vm, shard.zone, max_age=0.0) or {}
            if (info.get("status") or "").upper() in _UP_STATUSES:
                self._record_start_error(shard, "", "")
                return True                       # somebody else got it up
            try:
                op = self.gce.start(shard.vm, shard.zone)
            except Exception as e:                                        # noqa: BLE001
                self._record_start_error(shard, "CALL_FAILED", str(e)[:200])
                return False
        return False

    def start_errors(self):
        """The last start failure per shard, or {} when every start has stuck."""
        with self._start_lock:
            return {k: dict(v) for k, v in self._start_errors.items()}

    def stop(self, shard):
        try:
            self.gce.stop(shard.vm, shard.zone)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def _lease(self, shards):
        """Take or refresh this run's lease on each shard. The lease is the ONLY thing that keeps
        a shard awake, and it lives in `shard_leases`, which already existed."""
        run_id = self.run_id()
        if not run_id:
            return 0
        n = 0
        for s in shards:
            try:
                import runstore
                info = self.gce.instance(s.vm, s.zone) or {}
                runstore.lease_shard(run_id, s.shard, host=info.get("ip", ""),
                                     seconds=LEASE_SECONDS,
                                     meta={"vm": s.vm, "zone": s.zone})
                n += 1
            except Exception:
                #  A lease that will not persist must not fail a search. The consequence is that
                #  the reaper falls back to the idle window, which errs toward keeping the shard.
                traceback.print_exc()
        return n

    @staticmethod
    def _parallel(fn, items):
        items = list(items)
        if len(items) <= 1:
            for it in items:
                try:
                    fn(it)
                except Exception:
                    traceback.print_exc()
            return
        threads = [threading.Thread(target=fn, args=(it,), daemon=True) for it in items]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

    # -- the idle reaper ----------------------------------------------------------------------
    def activity(self, shard):
        """In flight queries on this shard, or None when we cannot tell.

        Asked of Postgres first, because `pg_stat_activity` is the authority on whether a query is
        running and the agent is only a proxy for it.
        """
        info = self.gce.instance(shard.vm, shard.zone)
        ip = (info or {}).get("ip")
        if not ip:
            return None
        conn = None
        try:
            dsn = self.dsn(shard)
            if dsn:
                import psycopg
                conn = psycopg.connect(dsn, autocommit=True)
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                                "AND state <> 'idle'")
                    return int(cur.fetchone()[0])
        except Exception:
            pass
        finally:
            if conn is not None:
                self._close(conn)
        try:
            with urllib.request.urlopen(f"http://{ip}:{self.agent_port}/activity",
                                        timeout=HEALTH_TIMEOUT) as r:
                return int(json.loads(r.read().decode())["active_queries"])
        except Exception:
            return None

    def idle_seconds(self, shard):
        """How long since anybody held a lease on this shard. None when it has no history.

        Derived from `shard_leases` rather than from a second table, because a second table is a
        second truth: the lease IS the record of who wanted this shard awake.
        """
        try:
            import db
            with db.cursor(autocommit=True) as cur:
                cur.execute("SELECT extract(epoch FROM now() - max(GREATEST(heartbeat_at, "
                            "  COALESCE(released_at, heartbeat_at)))) AS s "
                            " FROM shard_leases WHERE shard = %s", (shard.shard,))
                row = cur.fetchone() or {}
                s = row.get("s") if isinstance(row, dict) else row[0]
                return None if s is None else float(s)
        except Exception:
            traceback.print_exc()
            return None

    def held(self, shard):
        try:
            import db
            with db.cursor(autocommit=True) as cur:
                cur.execute("SELECT count(*) AS n FROM shard_leases "
                            " WHERE shard = %s AND state = 'held' AND expires_at > now()",
                            (shard.shard,))
                row = cur.fetchone() or {}
                return int(row.get("n") if isinstance(row, dict) else row[0])
        except Exception:
            traceback.print_exc()
            return 0

    def reap(self, idle_minutes=IDLE_MINUTES, hard_idle_minutes=HARD_IDLE_MINUTES,
             dry_run=False, now=None):
        """Stop every running shard nobody is using. -> [{shard, action, reason}].

        THE ORDER OF THE TESTS IS THE SAFETY PROPERTY.
          1. expire the leases nobody heartbeats, so a dead run stops holding a VM open,
          2. a held lease keeps the shard, full stop,
          3. inside the idle window, keep,
          4. AN IN FLIGHT QUERY KEEPS THE SHARD EVEN WITH NO LEASE. A lease can expire under a
             query that is genuinely still running, because the thing that heartbeats it is a
             thread that can die while the database keeps working. Stopping the VM under that
             query loses the work and returns a wrong answer to a live search.
          5. cannot tell whether anything is running: keep until the hard ceiling, then stop and
             say so, because an unreachable shard is not serving anybody and burn is not a safe
             default.
        """
        try:
            import runstore
            runstore.reap_shards()
        except Exception:
            traceback.print_exc()

        out = []
        for shard in self.table:
            try:
                info = self.gce.instance(shard.vm, shard.zone, max_age=0.0)
            except Exception:
                traceback.print_exc()
                continue
            if not info or (info.get("status") or "").upper() not in _UP_STATUSES:
                continue

            if self.held(shard):
                out.append({"shard": shard.shard, "action": "keep", "reason": "lease held"})
                continue

            idle = self.idle_seconds(shard)
            if idle is None:
                idle = self._since_start(info, now=now)
            if idle is not None and idle < idle_minutes * 60.0:
                out.append({"shard": shard.shard, "action": "keep",
                            "reason": f"idle {idle:.0f}s < {idle_minutes * 60:.0f}s"})
                continue

            busy = self.activity(shard)
            if busy is None:
                if idle is not None and idle >= hard_idle_minutes * 60.0:
                    out.append({"shard": shard.shard,
                                "action": "stop" if not dry_run else "would-stop",
                                "reason": f"unreachable and idle {idle:.0f}s past the hard ceiling"})
                    if not dry_run:
                        self.stop(shard)
                else:
                    out.append({"shard": shard.shard, "action": "keep",
                                "reason": "cannot read activity; not stopping yet"})
                continue
            if busy > 0:
                out.append({"shard": shard.shard, "action": "keep",
                            "reason": f"{busy} in flight quer{'y' if busy == 1 else 'ies'}"})
                continue

            out.append({"shard": shard.shard, "action": "stop" if not dry_run else "would-stop",
                        "reason": f"no lease, idle {idle if idle is None else int(idle)}s, nothing running"})
            if not dry_run:
                self.stop(shard)
        return out

    @staticmethod
    def _since_start(info, now=None):
        ts = (info or {}).get("last_start")
        if not ts:
            return None
        try:
            from datetime import datetime, timezone
            started = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return ((now or datetime.now(timezone.utc)) - started).total_seconds()
        except Exception:
            return None


# ---------------------------------------------------------------------------------------------
# installation
# ---------------------------------------------------------------------------------------------
_INSTALLED = None


def install(backend=None, **kw):
    """Register with both seams. Idempotent; returns the installed backend."""
    global _INSTALLED
    _INSTALLED = backend or ShardBackend(**kw)
    shard_manager.register_backend(_INSTALLED)
    shard_router.register_backend(_INSTALLED)
    return _INSTALLED


def enabled():
    return str(os.environ.get("SHARD_BACKEND_ENABLED", "")).strip().lower() \
        in ("1", "true", "yes", "on")


def install_if_enabled(**kw):
    """Install only when `SHARD_BACKEND_ENABLED` says so. -> the backend, or None.

    THE DEFAULT IS OFF, AND IT IS A CORPUS DECISION, NOT A CODE ONE. Registering a backend is what
    makes `cold.available()` True, and that has two costs a search pays whether or not a shard
    holds anything: `shard_router.route` reads the corpus-wide prior, and `wake` starts VMs at
    $1.04 an hour each. Until workstream O has loaded a release onto a shard, every one of them
    answers `shard_status.state = 'building'`, so `ensure` reports `waking`, `hot_domains` returns
    nothing and the cold tier contributes nothing: the search would pay the routing query and the
    VM time for a guaranteed empty answer. Flipping this on is therefore the same decision as
    "there is a corpus on the shards", and it belongs in the environment of whoever took it.

    Called at import from `webapp.py`, which is the one place a search is served from, so there is
    exactly one registration point and no module registers itself as a side effect of being
    imported.
    """
    if not enabled():
        return None
    try:
        return install(**kw)
    except Exception:
        #  A backend that cannot be built must not stop the app serving hot searches. The seams
        #  keep their inert defaults, which is the fail-soft contract.
        traceback.print_exc()
        return None


def uninstall():
    """Back to the inert seams, which is what every test that did not ask for shards wants."""
    global _INSTALLED
    _INSTALLED = None
    shard_manager.register_backend(None)
    shard_router.register_backend(None)


def installed():
    return _INSTALLED
