"""The durable run store: a search lives in Postgres, not in a process global.

WHAT THIS REPLACES
------------------
`webapp._JOBS` is a dict inside one gunicorn worker. A deploy destroys everything in it, which has
cost two production searches (adhoc-ed62f27d3c2a died to the 2026-08-17 22:38 restart).
`src/run_queue.py` fixed half of that: the REQUEST survives because it is a row. The WORK did not,
because a re-queued run started again from zero.

INTEGRATION STATUS
------------------
The production web route and SSE stream still use `run_queue` and `_JOBS`, and no Supervisor
worker is enabled: the route cutover is a coordinated step and has not been taken. Stage resume is
no longer part of that backlog. Every recorded stage is consulted on resume, at the granularity of
one retrieval channel, one reference read and one rescue round, and every checkpointed artifact
carries a content digest that is verified before it is trusted. See `runner.worker`.

This module provides the durable store. Eight tables (sql/009_durable_runs.sql):

    search_runs          one row per search: mode, input, config fingerprint, status, timings,
                         and the LEASE (worker_id, heartbeat_at, lease_expires_at)
    search_stages        one row per stage per run, with counts, durations and a CHECKPOINT
    search_queries       every query issued, its channel, its parameters
    retrieval_hits       raw per-channel hits, before fusion
    search_candidates    the fused / screened / read candidate set with per-stage scores
    provider_usage       per provider, per tier, per run: calls, tokens, latency, errors, cost
    shard_leases         which shard VM a run has woken, when taken, when it expires
    corpus_ingest_queue  publications a run wants in the next permanent corpus release

THE STATE MACHINE
-----------------
    queued  --claim-->  running  --finish-->        done
                           |     --fail, attempts remaining-->  queued
                           |     --fail, attempts spent------>  failed
                           |     --lease expired, reaped---->   queued (resumes) or failed

Claiming is `SELECT ... FOR UPDATE SKIP LOCKED` over a PARTIAL index of queued rows only, so two
workers can never claim the same run and neither ever waits on the other's row lock.

RESUME
------
`Stage` is the resumable unit exposed by this store, and `substage` is the same thing one level
down: `retrieve:<channel>`, `read:<publication>`, `rescue:<round>`. Each returns an existing
checkpoint instead of running a completed body again, and every one of them is consulted by the
stage that owns it, so a worker killed after screening restarts at reading rather than at
decomposition, and one killed halfway through the reading re-reads only what is missing. Outputs
too large for a row checkpoint a reference to an atomic file plus its digest (`runartifact`); a
digest that does not match is treated as no checkpoint at all.

SIDE EFFECTS
------------
Charging a run and mailing its owner are per RUN, not per ATTEMPT. `run_side_effects`
(sql/013_run_side_effects.sql) is the ledger, keyed (run_id, kind) with a primary key, and
`settle()` writes the row in the same transaction that makes the run terminal.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import traceback
import uuid

import db

# ---------------------------------------------------------------------------------------------
# knobs
# ---------------------------------------------------------------------------------------------
#  How long a claim is good for without a heartbeat. Must be comfortably longer than the longest
#  stage that does not tick (the cross-encoder head measured 56.4 s), and short enough that a
#  killed worker's run is back in the queue while the user is still on the page.
LEASE_SECONDS = int(os.environ.get("RUN_LEASE_SECONDS", "90"))
#  Heartbeat interval. A third of the lease: two consecutive misses still leave a margin.
HEARTBEAT_SECONDS = float(os.environ.get("RUN_HEARTBEAT_SECONDS", str(LEASE_SECONDS / 3.0)))
MAX_ATTEMPTS = int(os.environ.get("RUN_MAX_ATTEMPTS", "3"))
SHARD_LEASE_SECONDS = int(os.environ.get("SHARD_LEASE_SECONDS", "300"))

#  Columns added after 009 that the store writes unconditionally.
REQUIRED_RUN_COLUMNS = ("admitted", "admitted_at", "charged_day")

REQUIRED_TABLES = (
    "search_runs", "search_stages", "search_queries", "retrieval_hits",
    "search_candidates", "provider_usage", "shard_leases", "corpus_ingest_queue",
)

#  013. The once-per-run ledger for side effects that must not repeat across attempts.
SIDE_EFFECT_TABLE = "run_side_effects"

TERMINAL = ("done", "failed", "cancelled")

#  The pipeline's stages, in order. Persisting after each of these is the contract: query
#  decomposition, global results, shard routing, fused candidates, screening, EACH reference
#  analysis, the coverage ledger, EACH rescue round, the final report.
STAGES = [
    "decompose",         # query -> technical elements / claim elements
    "global",            # global (L3) + external API results
    "shard_route",       # which dormant domain VMs to wake
    "fuse",              # fused, family-deduped candidate set
    "screen",            # cheap API screening
    "read",              # full reference reading (one checkpoint per reference underneath)
    "ledger",            # the coverage ledger
    "rescue",            # gap-directed rescue rounds (one checkpoint per round underneath)
    "report",            # the final report on disk
]
STAGE_SEQ = {name: i for i, name in enumerate(STAGES)}

_schema_ready = threading.Event()
_schema_lock = threading.Lock()


class LeaseLost(RuntimeError):
    """This worker no longer owns the run: another worker reclaimed it after a missed heartbeat.
    Whatever we are doing must stop, because someone else is doing it."""


# ---------------------------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------------------------
def ensure_schema(force=False):
    """Verify that the controlled migration installed the durable-run schema.

    Runtime processes never apply DDL. Doing so bypasses the migration ledger, turns an ordinary
    test import into a production schema change, and lets two worker starts race table creation.
    """
    if _schema_ready.is_set() and not force:
        return
    with _schema_lock:
        if _schema_ready.is_set() and not force:
            return
        with db.cursor(autocommit=True, readonly=True) as cur:
            cur.execute(
                "SELECT name FROM unnest(%s::text[]) AS required(name) "
                "WHERE to_regclass(required.name) IS NULL ORDER BY name",
                (list(REQUIRED_TABLES),))
            missing = [row["name"] for row in cur.fetchall() or []]
        if missing:
            raise RuntimeError(
                "durable-run schema is missing controlled migrations for: "
                + ", ".join(missing)
                + ". Apply sql/009_durable_runs.sql through the migration runner before "
                  "starting the worker; runtime schema creation is prohibited.")
        _schema_ready.set()


def admission_capable():
    """Whether THIS database carries the admission columns (012). RAISES on a detection failure.

    NOT CACHED, on purpose. A cached answer is keyed by nothing: one process that retargets its
    connection, or one test suite that hands the module a different database, then reuses a 012
    answer on a 009 database and claims rows it was never admitted to spend on. A schema-qualified
    catalog read is a few hundred microseconds and it is always about the database in front of it.

    An error is NOT capability information. Swallowing it as False collapses "I could not find
    out" into "this is confirmed legacy", which is the one answer that lets a caller spend money
    it was not admitted to spend. The exception propagates and the caller refuses.
    """
    with db.cursor(autocommit=True, readonly=True) as cur:
        #  table_schema too: another schema on the search_path can hold a `search_runs` with these
        #  column names, and confirming 012 from someone else's table would put this process on
        #  the admitted predicate against a database that does not have it.
        cur.execute("SELECT count(*) n FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name='search_runs' "
                    "AND column_name = ANY(%s)",
                    (list(REQUIRED_RUN_COLUMNS),))
        return int((cur.fetchone() or {}).get("n") or 0) == len(REQUIRED_RUN_COLUMNS)


def side_effects_capable():
    """Whether THIS database carries the once-per-run side-effect ledger (013). RAISES on failure.

    NOT CACHED, for the same reason `admission_capable` is not: a cached answer is keyed by
    nothing, and one process that retargets its connection would then reuse a 013 answer against a
    database that does not have the table, which is precisely how a side effect gets performed
    twice.
    """
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute("SELECT to_regclass(%s) AS t", (SIDE_EFFECT_TABLE,))
        return (cur.fetchone() or {}).get("t") is not None


def require_side_effect_schema():
    """Refuse a once-per-run side effect when 013 has not been applied.

    Scoped like `require_admission_schema`, and for the same reason: refusing every durable
    feature on a database that carries 009 and 012 but not 013 is a bigger outage than the bug it
    prevents. The hazard is real though. Without the table there is nothing keyed on run_id with a
    uniqueness constraint, so "have I already charged this run" is answerable only from memory,
    and memory is a different process on the retry.
    """
    ensure_schema()
    if not side_effects_capable():
        raise RuntimeError(
            f"once-per-run side effects need the {SIDE_EFFECT_TABLE} table, which is not present. "
            "Apply sql/013_run_side_effects.sql through the migration runner.")


def require_admission_schema():
    """Refuse an ADMISSION operation when 012 has not been applied.

    Scoped deliberately. Folding this into `ensure_schema` refused every durable feature on a
    database that carries 009 but not 012, including reads that never touch the new columns, which
    is a bigger outage than the bug it prevents. The hazard is still real: without it the first
    enqueue fails at runtime, on a user's search, with UndefinedColumn.
    """
    ensure_schema()
    if not admission_capable():
        raise RuntimeError(
            "durable-run admission needs columns that are not present on search_runs: "
            + ", ".join(sorted(REQUIRED_RUN_COLUMNS))
            + ". Apply sql/012_run_admission.sql through the migration runner.")


def _cur(autocommit=True):
    ensure_schema()
    return db.cursor(autocommit=autocommit)


def worker_id():
    """Stable within a process, unique across processes and hosts."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------------------------
def config_fingerprint(inp, config):
    """What decided this run. Two runs with the same fingerprint asked the same question of the
    same pipeline; two with different ones are not comparable and must not share a checkpoint."""
    blob = json.dumps({"input": inp or {}, "config": config or {}},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def new_run_id(slug):
    return f"{slug}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def enqueue(slug, inp, *, search_mode="CONCEPT_SEARCH", mode="novelty", depth="deep",
            lane=None, config=None, priority=100, run_id=None, max_attempts=None,
            with_created=False):
    """Add a run. -> run_id, or (run_id, created) when `with_created` is set.

    A slug that already has a live (queued or running) run RETURNS THAT RUN rather than starting a
    second one: the slug is the report's cache identity, and two runs writing one report file is
    the race the in-process `_JOBS` claim used to prevent.

    This API does NOT set admission. A row it creates is unadmitted, and only the single
    serialized authority in `admit_waiting` may make it runnable, so there is no second path by
    which something becomes spendable.

    `with_created` exists because the CALLER has a side effect that must happen exactly once per
    run and not once per submission: charging the daily budget. Without it every retry and every
    racing request looks identical to the winner, and a user who reloads a slow page three times
    pays for three searches while one runs. Default off, so existing callers are unchanged.
    """
    def _ret(rid, created):
        return (rid, created) if with_created else rid

    lane = lane or depth
    config = config or {}
    fp = config_fingerprint(inp, config)
    with _cur() as cur:
        cur.execute("SELECT run_id, status FROM search_runs WHERE slug=%s "
                    "AND status IN ('queued','running') ORDER BY enqueued_at DESC LIMIT 1",
                    (slug,))
        row = cur.fetchone()
        if row:
            return _ret(row["run_id"], False)
        rid = run_id or new_run_id(slug)
        # ux_search_runs_live_slug settles the race where another transaction inserted after the
        # SELECT above. Partial-index inference uses the same live-state predicate as the index.
        cur.execute(
            """INSERT INTO search_runs (run_id, slug, search_mode, mode, depth, lane, input,
                                        config, config_fingerprint, status, priority,
                                        max_attempts)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s)
               ON CONFLICT (slug) WHERE status IN ('queued','running') DO NOTHING
               RETURNING run_id""",
            (rid, slug, search_mode, mode, depth, lane, json.dumps(inp or {}, default=str),
             json.dumps(config, default=str), fp, int(priority),
             int(max_attempts if max_attempts is not None else MAX_ATTEMPTS)))
        inserted = cur.fetchone()
        if inserted:
            return _ret(inserted["run_id"], True)
        cur.execute("SELECT run_id FROM search_runs WHERE slug=%s "
                    "AND status IN ('queued','running') ORDER BY enqueued_at DESC LIMIT 1",
                    (slug,))
        winner = cur.fetchone()
        if not winner:
            raise RuntimeError("live-run uniqueness conflict resolved without a visible winner")
        #  Lost the insert race: another transaction created the run, so this caller is NOT the
        #  creator and must not charge the budget for it.
        return _ret(winner["run_id"], False)


#  OUTSTANDING work in a lane, which is what a concurrency limit actually bounds: runs a worker
#  is executing PLUS runs already admitted and waiting to be claimed. Counting only 'running'
#  admits one more on every sweep, because admitting does not itself make a row running, so three
#  serialized sweeps against a limit of one admit three.
_OUTSTANDING_SQL = ("SELECT count(*) n FROM search_runs WHERE lane=%s "
                    "AND (status='running' OR (status='queued' AND admitted))")


def enqueue_admitted(slug, inp, *, search_mode="CONCEPT_SEARCH", mode="novelty", depth="deep",
                     lane=None, config=None, priority=100, daily_cap=0, max_concurrent=0):
    """Insert a run, then let the single admission authority decide. -> (run_id, created, admitted)

    THE INSERT AND THE DECISION ARE DELIBERATELY SEPARATE. Every new row is born unadmitted, and
    only `admit_waiting` may make one runnable, oldest first. Admitting a newcomer inline inside
    this transaction would let it jump whatever was already waiting, and it would be a second
    admission path that could disagree with the sweep. A worker can never observe the brief
    unadmitted state as runnable, and if this process dies between the insert and the sweep, the
    worker's own sweep recovers the row.

    POSTGRES IS THE AUTHORITY throughout. A process-local gate cannot decide admission: two
    gunicorn workers and the sweeper each hold their own counters, so each can believe it holds
    the last slot. Caps arrive here as CONFIGURED LIMITS only; the counting happens against rows.
    """
    require_admission_schema()
    lane = lane or depth
    config = config or {}
    fp = config_fingerprint(inp, config)
    with _cur(autocommit=False) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"admit:{lane}",))

        cur.execute("SELECT run_id, admitted FROM search_runs WHERE slug=%s "
                    "AND status IN ('queued','running') ORDER BY enqueued_at DESC LIMIT 1",
                    (slug,))
        row = cur.fetchone()
        if row:
            return row["run_id"], False, bool(row["admitted"])

        rid = new_run_id(slug)
        cur.execute(
            """INSERT INTO search_runs (run_id, slug, search_mode, mode, depth, lane, input,
                                        config, config_fingerprint, status, priority,
                                        max_attempts, admitted, admitted_at, charged_day)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s,
                       false, NULL, NULL)
               ON CONFLICT (slug) WHERE status IN ('queued','running') DO NOTHING
               RETURNING run_id""",
            (rid, slug, search_mode, mode, depth, lane, json.dumps(inp or {}, default=str),
             json.dumps(config, default=str), fp, int(priority), MAX_ATTEMPTS))
        inserted = cur.fetchone()
        if inserted:
            created_id = inserted["run_id"]
        else:
            cur.execute("SELECT run_id, admitted FROM search_runs WHERE slug=%s "
                        "AND status IN ('queued','running') ORDER BY enqueued_at DESC LIMIT 1",
                        (slug,))
            winner = cur.fetchone()
            if not winner:
                raise RuntimeError(
                    "live-run uniqueness conflict resolved without a visible winner")
            return winner["run_id"], False, bool(winner["admitted"])

    #  EVERY new row is born unadmitted, then the ONE serialized authority decides, oldest
    #  first. Admitting the newcomer inline would let it jump whatever was already waiting, and
    #  it would be a second admission path that could disagree with the sweep. If this process
    #  dies between the insert and the sweep, the worker's own sweep recovers the row.
    admitted_ids = admit_waiting(lane=lane, daily_cap=daily_cap, max_concurrent=max_concurrent)
    return created_id, True, created_id in admitted_ids


def admission_stats(lane):
    """The persisted counts an operator needs for one lane. RAISES; the caller decides.

    /healthz used to read the process-local gate, which durable submissions never touch. After
    cutover that reports zero active and zero spent while Postgres holds running and charged work,
    which is worse than no number at all: it is a confident wrong answer to the first question
    anybody asks during an incident.
    """
    require_admission_schema()
    with _cur() as cur:
        cur.execute("""SELECT
              count(*) FILTER (WHERE status = 'running')                        AS running,
              count(*) FILTER (WHERE status = 'queued' AND admitted)            AS admitted_queued,
              count(*) FILTER (WHERE status = 'queued' AND NOT admitted)        AS waiting,
              count(*) FILTER (WHERE charged_day = (now() AT TIME ZONE 'UTC')::date) AS today
            FROM search_runs WHERE lane = %s""", (lane,))
        r0 = cur.fetchone() or {}
    return {k: int(r0.get(k) or 0) for k in ("running", "admitted_queued", "waiting", "today")}


def charged_today(lane):
    """How many runs in this lane have spent from today's budget.

    The ROWS are the ledger. There is no counter to reset, so a UTC rollover is a date changing
    rather than an invariant somebody has to remember, and a crash cannot lose the count or
    double it.
    """
    with _cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE lane=%s "
                    "AND charged_day = (now() AT TIME ZONE 'UTC')::date", (lane,))
        return int((cur.fetchone() or {}).get("n") or 0)


def admit_waiting(lane, daily_cap, max_concurrent, limit=None):
    """Admit queued-but-unadmitted runs in this lane, up to what the caps genuinely allow.

    -> the run_ids admitted, oldest first.

    Every new row is queued and NOT runnable. This is the ONLY path by which one becomes
    runnable, for producers and workers alike, and it re-checks both caps against the database
    rather than against any process's memory.

    The whole decision runs inside ONE transaction holding a per-lane advisory lock. Counting in
    one transaction and admitting in another is precisely the race that lets two sweeps each see
    room for one and admit two: the lock makes admission per lane serial, and it is released on
    commit or rollback, so a worker that dies mid-sweep does not wedge the lane.
    """
    require_admission_schema()
    admitted = []
    with _cur(autocommit=False) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"admit:{lane}",))
        cur.execute("SELECT count(*) n FROM search_runs WHERE lane=%s "
                    "AND charged_day = (now() AT TIME ZONE 'UTC')::date", (lane,))
        spent = int((cur.fetchone() or {}).get("n") or 0)
        #  Counted on the same terms it is admitted on, or the lane reserves concurrency for
        #  fixture rows production will never claim and a real search waits behind them.
        cur.execute(_OUTSTANDING_SQL + " " + _live_only(), (lane,))
        running = int((cur.fetchone() or {}).get("n") or 0)

        room = min(int(daily_cap) - spent, int(max_concurrent) - running)
        if limit is not None:
            room = min(room, int(limit))
        if room <= 0:
            return []

        cur.execute("""UPDATE search_runs SET admitted = true, admitted_at = now(),
                              charged_day = (now() AT TIME ZONE 'UTC')::date,
                              updated_at = now(), event_seq = event_seq + 1
                        WHERE run_id IN (
                            SELECT run_id FROM search_runs
                             WHERE lane=%s AND status='queued' AND NOT admitted
                             """ + _live_only() + """
                             ORDER BY priority, enqueued_at
                             LIMIT %s)
                        RETURNING run_id""", (lane, room))
        admitted = [r["run_id"] for r in cur.fetchall() or []]
    return admitted


def queue_position(run_id):
    """How many queued runs are ahead of this one in its own lane, or None if it is not queued."""
    with _cur() as cur:
        cur.execute("""SELECT count(*) n FROM search_runs q
                       WHERE q.status='queued' AND q.lane = (SELECT lane FROM search_runs
                                                             WHERE run_id=%s)
                         AND (q.priority, q.enqueued_at) <
                             (SELECT priority, enqueued_at FROM search_runs WHERE run_id=%s
                              AND status='queued')""", (run_id, run_id))
        r = cur.fetchone()
    return int(r["n"]) if r and r["n"] is not None else None


# ---------------------------------------------------------------------------------------------
# claim / lease / reap
# ---------------------------------------------------------------------------------------------
#  ONE STATEMENT, ONE TRANSACTION. The CTE takes the row lock with SKIP LOCKED and the UPDATE
#  releases it on commit, so the window in which any row is locked is the width of one UPDATE.
#  Two workers polling at the same instant take two different rows; neither blocks.
#  ---- fixture rows are not production work ---------------------------------------------------
#  The suite runs against this database on purpose: `search_runs` has foreign keys into the corpus
#  and a separate store could not honour them. That was harmless while no worker existed. It stopped
#  being harmless the moment the durable workers went live, because a queued row is a queued row:
#  MEASURED 2026-08-22, the production quick worker claimed `test-durable-*` and `test-resume-*`
#  rows straight out of `test_durable_runs.py` and ran real searches on them, spending model calls
#  on fixtures and failing them, while the tests that created them went red because their own
#  `claim()` found nothing left to claim.
#
#  Admission is the second half of the same defect: `admit_waiting` was admitting those rows too,
#  and an admitted row counts against the lane's concurrency in `_OUTSTANDING_SQL`, so fixtures
#  could hold a slot a real search was waiting for.
#
#  A reserved slug prefix is enough because production cannot produce one: `search_slug` returns
#  `adhoc-<sha1>`, the bench harness writes `bench-*`, and the gold ids are a fixed list.
#  `test_run_store_isolation.py` holds all three of those shut.
TEST_SLUG_PREFIX = "test-"
#  Default FAIL SAFE, so a new production entry point is isolated without having to know this
#  exists. The suite flips it once, in tests/conftest.py.
ALLOW_TEST_SLUGS = False
#  'test%', not 'test-%': on 2026-08-23 the restarted deep worker claimed testq-f64e31773d0a
#  and spent real model calls decomposing a fixture, because the guard knew one test prefix and
#  the suite had grown a second. Production cannot produce a test-leading slug for the same
#  reason as before: search_slug returns adhoc-<sha1>, the bench harness writes bench-*, and the
#  gold ids are a fixed list.
_LIVE_ONLY_SQL = "AND slug NOT LIKE 'test%%'"


def _live_only():
    """The SQL that keeps production off fixture rows. Empty inside the suite."""
    return "" if ALLOW_TEST_SLUGS else _LIVE_ONLY_SQL


_CLAIM_SQL_TEMPLATE = """
WITH picked AS (
    SELECT run_id
      FROM search_runs
     WHERE status = 'queued'
       AND {admitted_clause}
       {live_only}
       AND (%(lanes)s::text[] IS NULL OR lane = ANY(%(lanes)s::text[]))
     ORDER BY priority, enqueued_at
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE search_runs r
   SET status           = 'running',
       worker_id        = %(worker)s,
       heartbeat_at     = now(),
       lease_expires_at = now() + make_interval(secs => %(lease)s),
       attempts         = r.attempts + 1,
       started_at       = COALESCE(r.started_at, now()),
       error            = NULL,
       updated_at       = now(),
       -- A STATUS TRANSITION IS AN EVENT. The SSE tail emits only when event_seq advances, so a
       -- claim that changed the row without bumping it let a connected client sit on its last
       -- progress message while the run started, finished or failed underneath it. Bumped in the
       -- SAME successful update, so an update that matches no row cannot bump anything.
       event_seq        = r.event_seq + 1
  FROM picked p
 WHERE r.run_id = p.run_id
 RETURNING r.*
"""


def claim(worker, lanes=None, lease_seconds=None, admitted_only=None):
    """Claim the next runnable run for `worker`. -> the run row (dict) or None.

    `admitted_only`: True or None, and nothing else.

      True  filter on admission. The real worker passes this immediately after
            `require_admission_schema`, so its safety is visible at the call site and it does not
            pay for a second catalog probe.
      None  detect the schema and filter whenever 012 exists. A foundation caller on 009 uses
            this and gets the legacy shape because the columns genuinely are not there.

    There is deliberately NO explicit legacy mode. `False` on a 012 database would claim
    admitted=false rows, which is exactly the cap bypass this whole path exists to close, and no
    caller needs it: a database without the columns is already handled by detection.

    `lanes`: restrict to these lanes (e.g. ['quick']) so a cheap interactive search is never stuck
    behind two multi-hour attacks. None means any lane.
    """
    with _cur() as cur:
        #  NEVER spend on a row admission refused. On a 009 database there is no such concept,
        #  so every queued row is claimable exactly as it was before.
        if admitted_only is False:
            raise ValueError(
                "claim(admitted_only=False) is not a supported mode: on a database with 012 it "
                "would claim rows admission refused. Use None to detect the schema, or True "
                "after require_admission_schema().")
        want = admission_capable() if admitted_only is None else True
        clause = "admitted" if want else "true"
        cur.execute(_CLAIM_SQL_TEMPLATE.format(admitted_clause=clause,
                                               live_only=_live_only()),
                    {"lanes": list(lanes) if lanes else None,
                                 "worker": worker,
                                 "lease": float(lease_seconds or LEASE_SECONDS)})
        row = cur.fetchone()
    return _row(row)


def heartbeat(run_id, worker, lease_seconds=None):
    """Extend the lease. -> True while this worker still owns the run, False once it does not.

    False is not a warning, it is a stop signal: the reaper has already given the run to somebody
    else, and continuing would produce two workers writing one report.
    """
    with _cur() as cur:
        cur.execute("""UPDATE search_runs
                          SET heartbeat_at = now(),
                              lease_expires_at = now() + make_interval(secs => %s),
                              updated_at = now()
                        WHERE run_id = %s AND worker_id = %s AND status = 'running'
                        RETURNING run_id""",
                    (float(lease_seconds or LEASE_SECONDS), run_id, worker))
        return cur.fetchone() is not None


def reap(now_grace_seconds=0, run_ids=None):
    """Return work whose lease expired. -> {"requeued": [...], "failed": [...]}.

    `run_ids` restricts the sweep to those runs. Production passes None (sweep everything); the
    tests pass their own ids, because this store is shared with the live app and a test that
    reaped a real run would be the second time a pytest process settled a production search.

    A worker that was SIGKILLed stops heartbeating; `lease_expires_at` passes; the row goes back to
    'queued' with its stages intact, so the next worker resumes rather than restarts. A run that
    has burned its attempts is failed instead, because an infinite retry of a run that kills its
    worker is a loop, not resilience.
    """
    requeued, failed = [], []
    with _cur() as cur:
        cur.execute("""UPDATE search_runs
                          SET status = CASE WHEN attempts >= max_attempts THEN 'failed'
                                            ELSE 'queued' END,
                              worker_id = NULL,
                              lease_expires_at = NULL,
                              error = CASE WHEN attempts >= max_attempts
                                           THEN 'lease expired ' || attempts || ' times; the '
                                                || 'worker died before finishing'
                                           ELSE 'lease expired; requeued to resume' END,
                              finished_at = CASE WHEN attempts >= max_attempts THEN now()
                                                 ELSE NULL END,
                              updated_at = now(),
                              event_seq = search_runs.event_seq + 1
                        WHERE status = 'running'
                          AND lease_expires_at < now() - make_interval(secs => %s)
                          AND (%s::text[] IS NULL OR run_id = ANY(%s::text[]))
                        RETURNING run_id, status""",
                    (float(now_grace_seconds), list(run_ids) if run_ids else None,
                     list(run_ids) if run_ids else None))
        for r in cur.fetchall() or []:
            (failed if r["status"] == "failed" else requeued).append(r["run_id"])
    if requeued or failed:
        print(f"[runstore] reaper: {len(requeued)} run(s) requeued to resume, "
              f"{len(failed)} out of attempts", flush=True)
    return {"requeued": requeued, "failed": failed}


def finish(run_id, worker=None, status="done", error=None):
    """Settle a run. Terminal, and only by the worker that holds it (worker=None forces)."""
    with _cur() as cur:
        cur.execute("""UPDATE search_runs
                          SET status=%s, error=%s, finished_at=now(), updated_at=now(),
                              worker_id=NULL, lease_expires_at=NULL,
                              event_seq = search_runs.event_seq + 1
                        WHERE run_id=%s AND (%s::text IS NULL OR worker_id=%s)
                        RETURNING run_id""",
                    (status, (str(error)[:4000] if error else None), run_id, worker, worker))
        return cur.fetchone() is not None


def fail(run_id, worker, error, retry=True):
    """A run that raised. Requeued for another attempt while attempts remain, else failed.

    The retry is the point: a worker that dies to an OOM on the reading stage comes back and
    resumes at the reading stage, having kept the retrieval it already paid for.
    """
    with _cur() as cur:
        cur.execute("""UPDATE search_runs
                          SET status = CASE WHEN %s AND attempts < max_attempts THEN 'queued'
                                            ELSE 'failed' END,
                              error = %s,
                              worker_id = NULL,
                              lease_expires_at = NULL,
                              finished_at = CASE WHEN %s AND attempts < max_attempts THEN NULL
                                                 ELSE now() END,
                              updated_at = now(),
                              event_seq = search_runs.event_seq + 1
                        WHERE run_id = %s AND worker_id = %s AND status = 'running'
                        RETURNING status""",
                    (bool(retry), str(error)[:4000], bool(retry), run_id, worker))
        r = cur.fetchone()
    return r["status"] if r else None


def cancel(run_id, reason="cancelled by request"):
    return finish(run_id, worker=None, status="cancelled", error=reason)


# ---------------------------------------------------------------------------------------------
# once-per-run side effects (013)
# ---------------------------------------------------------------------------------------------
#  WHY THIS IS A TABLE AND NOT A FLAG
#  A run is retried. Charging a user's quota and sending "your search is ready" are per RUN, not
#  per ATTEMPT, and the attempt that already did it is usually a different process on a different
#  day. The only thing both attempts can see is the row, so the row is the authority: a primary key
#  on (run_id, kind) makes the second claim lose, in Postgres, without anybody having to remember.
def claim_side_effect(run_id, kind, *, attempt=1, detail=None):
    """Claim the right to perform `kind` for this run. -> True for the ONE owner, else False.

    False is not an error: it means another attempt already did it and this one must not.
    """
    require_side_effect_schema()
    with _cur() as cur:
        cur.execute(f"""INSERT INTO {SIDE_EFFECT_TABLE} (run_id, kind, attempt, detail)
                        VALUES (%s,%s,%s,%s)
                        ON CONFLICT (run_id, kind) DO NOTHING
                        RETURNING kind""",
                    (run_id, kind, int(attempt), json.dumps(detail or {}, default=str)))
        return cur.fetchone() is not None


def side_effects_of(run_id):
    """{kind: row} for every side effect already performed for this run."""
    require_side_effect_schema()
    with _cur() as cur:
        cur.execute(f"SELECT * FROM {SIDE_EFFECT_TABLE} WHERE run_id=%s ORDER BY kind", (run_id,))
        return {r["kind"]: _row(r) for r in cur.fetchall() or []}


def settle(run_id, worker=None, status="done", error=None, side_effects=(), attempt=1,
           detail=None):
    """Settle a run AND claim its once-per-run side effects, in ONE transaction.

    -> (settled: bool, claimed: [kind, ...])

    The single transaction is the point. Settling in one transaction and recording the charge in
    another leaves a window in which the run is terminal and the charge is not written, and a
    worker that dies inside that window either charges twice or never. Here the terminal state and
    the receipt commit together or neither does.

    `claimed` names the kinds THIS attempt owns and must therefore perform. A kind that is absent
    from it was already done by an earlier attempt and must not be repeated.
    """
    kinds = [k for k in (side_effects or ()) if k]
    if not kinds:
        return finish(run_id, worker=worker, status=status, error=error), []
    require_side_effect_schema()
    claimed = []
    with _cur(autocommit=False) as cur:
        cur.execute("""UPDATE search_runs
                          SET status=%s, error=%s, finished_at=now(), updated_at=now(),
                              worker_id=NULL, lease_expires_at=NULL,
                              event_seq = search_runs.event_seq + 1
                        WHERE run_id=%s AND (%s::text IS NULL OR worker_id=%s)
                        RETURNING run_id""",
                    (status, (str(error)[:4000] if error else None), run_id, worker, worker))
        if cur.fetchone() is None:
            #  Ownership was lost. Claim NOTHING: the worker that owns the run now is the one
            #  entitled to charge it and to send its mail.
            return False, []
        for kind in kinds:
            cur.execute(f"""INSERT INTO {SIDE_EFFECT_TABLE} (run_id, kind, attempt, detail)
                            VALUES (%s,%s,%s,%s)
                            ON CONFLICT (run_id, kind) DO NOTHING
                            RETURNING kind""",
                        (run_id, kind, int(attempt), json.dumps(detail or {}, default=str)))
            if cur.fetchone() is not None:
                claimed.append(kind)
    return True, claimed


# ---------------------------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------------------------
def _row(row):
    if row is None:
        return None
    d = dict(row)
    for k in ("input", "config", "progress", "payload", "detail", "meta", "params"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


def get(run_id):
    with _cur() as cur:
        cur.execute("SELECT * FROM search_runs WHERE run_id=%s", (run_id,))
        return _row(cur.fetchone())


def latest_for_slug(slug):
    """The run a status poll is asking about: the newest run for this slug."""
    with _cur() as cur:
        cur.execute("SELECT * FROM search_runs WHERE slug=%s ORDER BY enqueued_at DESC LIMIT 1",
                    (slug,))
        return _row(cur.fetchone())


def progress_of(run_id):
    """The one-row read the SSE tail performs. Deliberately narrow: no jsonb decode of anything
    but the progress blob, and it is served from the primary key."""
    with _cur() as cur:
        cur.execute("SELECT run_id, slug, status, stage, attempts, event_seq, progress, error, "
                    "extract(epoch FROM enqueued_at) t0, "
                    "extract(epoch FROM started_at)  t_start "
                    "FROM search_runs WHERE run_id=%s", (run_id,))
        return _row(cur.fetchone())


def stages(run_id):
    with _cur() as cur:
        cur.execute("SELECT * FROM search_stages WHERE run_id=%s ORDER BY id", (run_id,))
        return [_row(r) for r in cur.fetchall() or []]


def completed_stages(run_id):
    """{stage: payload} for every stage this run has already finished, across attempts."""
    out = {}
    with _cur() as cur:
        cur.execute("SELECT DISTINCT ON (stage) stage, payload FROM search_stages "
                    "WHERE run_id=%s AND status='done' ORDER BY stage, id DESC", (run_id,))
        for r in cur.fetchall() or []:
            payload = r["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    pass
            out[r["stage"]] = payload
    return out


def resume_point(run_id):
    """The stage a restarted worker should begin at: the first pipeline stage not yet done.

    Returns (stage_name, {done stage -> payload}). A run with nothing done resumes at STAGES[0],
    which is a fresh start; a run whose last completed stage is 'screen' resumes at 'read'.
    """
    done = completed_stages(run_id)
    for name in STAGES:
        if name not in done:
            return name, done
    return None, done


# ---------------------------------------------------------------------------------------------
# stage checkpoints
# ---------------------------------------------------------------------------------------------
def stage_start(run_id, stage, attempt=1, n_in=None, detail=None):
    with _cur() as cur:
        cur.execute("""INSERT INTO search_stages (run_id, stage, attempt, seq, status, n_in,
                                                  detail)
                       VALUES (%s,%s,%s,%s,'running',%s,%s)
                       ON CONFLICT (run_id, stage, attempt) DO UPDATE
                          SET status='running', started_at=now(), n_in=EXCLUDED.n_in,
                              detail=EXCLUDED.detail, error=NULL
                       RETURNING id""",
                    (run_id, stage, int(attempt), STAGE_SEQ.get(stage, 99), n_in,
                     json.dumps(detail or {}, default=str)))
        sid = cur.fetchone()["id"]
        cur.execute("UPDATE search_runs SET stage=%s, updated_at=now() WHERE run_id=%s",
                    (stage, run_id))
    return sid


def stage_finish(stage_id, run_id, stage, *, status="done", n_out=None, payload=None,
                 detail=None, error=None):
    with _cur() as cur:
        cur.execute("""UPDATE search_stages
                          SET status=%s, n_out=%s, payload=%s,
                              detail = COALESCE(%s::jsonb, detail),
                              error=%s, finished_at=now(),
                              duration_ms = (extract(epoch FROM now() - started_at) * 1000)::bigint
                        WHERE id=%s""",
                    (status, n_out,
                     None if payload is None else json.dumps(payload, default=str),
                     None if detail is None else json.dumps(detail, default=str),
                     (str(error)[:4000] if error else None), stage_id))
        if status == "done":
            cur.execute("UPDATE search_runs SET resume_stage=%s, updated_at=now() "
                        "WHERE run_id=%s", (stage, run_id))


class _Skip(Exception):
    pass


class Stage:
    """One checkpointed pipeline stage.

        with runstore.Stage(run_id, "screen", attempt) as st:
            if st.resumed:                      # already done on an earlier attempt
                result = st.payload
            else:
                result = do_the_expensive_thing()
                st.done(result, n_out=len(result))

    `done()` is what makes the checkpoint; a stage whose body raises is recorded failed with the
    exception, and the run's `resume_stage` stays where it was, so the retry redoes exactly this
    stage and nothing before it.
    """

    def __init__(self, run_id, stage, attempt=1, n_in=None, detail=None, force=False):
        self.run_id, self.stage, self.attempt = run_id, stage, int(attempt)
        self.n_in, self.detail, self.force = n_in, detail, force
        self.resumed, self.payload, self.id = False, None, None
        self._settled = False

    def __enter__(self):
        if not self.force:
            done = completed_stages(self.run_id)
            if self.stage in done:
                self.resumed, self.payload = True, done[self.stage]
                return self
        self.id = stage_start(self.run_id, self.stage, self.attempt, self.n_in, self.detail)
        return self

    def done(self, payload=None, n_out=None, detail=None):
        if self.resumed or self._settled:
            return
        stage_finish(self.id, self.run_id, self.stage, status="done", n_out=n_out,
                     payload=payload, detail=detail)
        self._settled = True

    def __exit__(self, exc_type, exc, tb):
        if self.resumed or self._settled or self.id is None:
            return False
        if exc_type is not None:
            stage_finish(self.id, self.run_id, self.stage, status="failed",
                         error=f"{exc_type.__name__}: {exc}")
        else:
            #  A body that ran and never called done() has no checkpoint to resume from. Record
            #  it as done-with-no-payload rather than silently leaving it 'running', which would
            #  make the stage look interrupted forever.
            stage_finish(self.id, self.run_id, self.stage, status="done")
        return False


def substage(run_id, parent, key, *, attempt=1, n_out=None, payload=None, detail=None):
    """A checkpoint INSIDE a stage: one reference read, one rescue round.

    Stored as its own `search_stages` row named `<parent>:<key>` so `read:US-1234567-B2` and
    `rescue:2` are individually resumable and individually timed. They do not appear in STAGES,
    so they never satisfy `resume_point`; they are consulted by the stage body itself.
    """
    name = f"{parent}:{key}"
    with _cur() as cur:
        cur.execute("""INSERT INTO search_stages (run_id, stage, attempt, seq, status, n_out,
                                                  payload, detail, finished_at, duration_ms)
                       VALUES (%s,%s,%s,%s,'done',%s,%s,%s,now(),0)
                       ON CONFLICT (run_id, stage, attempt) DO UPDATE
                          SET status='done', n_out=EXCLUDED.n_out, payload=EXCLUDED.payload,
                              detail=EXCLUDED.detail, finished_at=now()""",
                    (run_id, name, int(attempt), STAGE_SEQ.get(parent, 99), n_out,
                     None if payload is None else json.dumps(payload, default=str),
                     json.dumps(detail or {}, default=str)))
    return name


def substages_done(run_id, parent):
    """{key: payload} for every completed substage of `parent`. One query, not one per item, so
    a 420-reference read stage costs a single round trip to find out what it already has."""
    out = {}
    prefix = f"{parent}:"
    with _cur() as cur:
        cur.execute("SELECT DISTINCT ON (stage) stage, payload FROM search_stages "
                    "WHERE run_id=%s AND stage LIKE %s AND status='done' "
                    "ORDER BY stage, id DESC", (run_id, prefix + "%"))
        for r in cur.fetchall() or []:
            payload = r["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    pass
            out[r["stage"][len(prefix):]] = payload
    return out


# ---------------------------------------------------------------------------------------------
# progress (what the SSE endpoint tails)
# ---------------------------------------------------------------------------------------------
def progress(run_id, event):
    """Publish one progress event. Bumps `event_seq`, which is how a tail knows there is
    something new without diffing the blob."""
    try:
        with _cur() as cur:
            cur.execute("""UPDATE search_runs
                              SET progress = %s::jsonb,
                                  event_seq = event_seq + 1,
                                  stage = COALESCE(NULLIF(%s,''), stage),
                                  updated_at = now()
                            WHERE run_id = %s
                            RETURNING event_seq""",
                        (json.dumps(event or {}, default=str), str(event.get("stage") or ""),
                         run_id))
            r = cur.fetchone()
        return int(r["event_seq"]) if r else 0
    except Exception:
        #  Progress is a heartbeat for the page, not the run. It must never fail the search.
        traceback.print_exc()
        return 0


# ---------------------------------------------------------------------------------------------
# the retrieval ledger
# ---------------------------------------------------------------------------------------------
def record_query(run_id, channel, *, stage="", query_text="", params=None, round=0,
                 element_id=None, n_hits=None, duration_ms=None):
    with _cur() as cur:
        cur.execute("""INSERT INTO search_queries (run_id, stage, channel, round, element_id,
                                                   query_text, params, n_hits, duration_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (run_id, stage, channel, int(round or 0), element_id,
                     (query_text or "")[:8000], json.dumps(params or {}, default=str),
                     n_hits, duration_ms))
        return cur.fetchone()["id"]


def record_hits(run_id, channel, hits, query_id=None, shard=""):
    """`hits`: iterable of dicts with publication_number and optionally rank/score/family_id.
    Raw, pre-fusion. -> number written."""
    rows = []
    for i, h in enumerate(hits or []):
        pub = h.get("publication_number") or h.get("pub")
        if not pub:
            continue
        rows.append((run_id, query_id, channel, h.get("rank", i + 1), pub,
                     h.get("family_id"), h.get("score"), shard or h.get("shard") or "",
                     json.dumps(h.get("meta") or {}, default=str)))
    if not rows:
        return 0
    with _cur() as cur:
        cur.executemany("""INSERT INTO retrieval_hits (run_id, query_id, channel, rank,
                                                       publication_number, family_id, score,
                                                       shard, meta)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
    return len(rows)


#  ONE RUN ISSUES THE SAME CHANNEL MANY TIMES. The agent runs a whole-invention pass and roughly
#  twenty element passes, and every one of them queries `dense`, `bm25`, `cpc` and the rest. So
#  (run_id, channel) is NOT the identity of a retrieval checkpoint: it is the identity of a
#  channel, and keying on it makes pass seven read pass one's hits for a completely different
#  query. This is the pass, fingerprinted from everything that decides what the pass returns, and
#  it is the third part of the key.
_PASS_META = "pass"


def replace_hits(run_id, channel, hits, pass_key="", query_id=None, shard=""):
    """Record ONE PASS's raw hit set for `channel`, replacing what that pass left before. -> count.

    `record_hits` appends, which is right for a ledger of everything that was ever issued and
    wrong for a resume: a channel re-run after an interruption would leave two interleaved copies
    of itself, and reading them back would hand fusion a hit list with every rank duplicated. The
    delete and the insert are one transaction, so a reader never sees the channel empty.

    THE DELETE IS SCOPED TO THE PASS. Without that, the twenty element passes of one run overwrite
    each other's rows on the way through: the last writer wins the ledger, every earlier pass's
    checkpoint then disagrees with what the ledger holds, and the two passes that raced leave a
    checkpoint that matches nothing at all. Measured on a live quick search before this was
    scoped: seven channels, one surviving row set each, and six "the checkpoint does not match the
    hit ledger" warnings inside a single uninterrupted attempt.

    `channel` still means the channel, so `retrieval_hits` stays readable as the per-channel
    evidence it was designed to be; the pass lives in `meta` beside it.
    """
    rows = []
    for i, h in enumerate(hits or []):
        pub = h.get("publication_number") or h.get("pub")
        if pub is None or pub == "":
            continue
        meta = dict(h.get("meta") or {})
        if pass_key:
            meta[_PASS_META] = str(pass_key)
        rows.append((run_id, query_id, channel, h.get("rank", i + 1), str(pub),
                     h.get("family_id"), h.get("score"), shard or h.get("shard") or "",
                     json.dumps(meta, default=str)))
    with _cur(autocommit=False) as cur:
        cur.execute("DELETE FROM retrieval_hits WHERE run_id=%s AND channel=%s "
                    "AND COALESCE(meta->>%s, '') = %s",
                    (run_id, channel, _PASS_META, str(pass_key or "")))
        if rows:
            cur.executemany("""INSERT INTO retrieval_hits (run_id, query_id, channel, rank,
                                                           publication_number, family_id, score,
                                                           shard, meta)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
    return len(rows)


#  HOW LONG THE RAW HIT LEDGER IS KEPT AFTER A RUN SETTLES.
#  Scoping the checkpoint to the pass is what makes it correct, and it is also what makes it big:
#  one pass of a quick search wrote 4,900 rows across seven channels, and a run issues one pass per
#  element plus one for the whole invention. Measured on a live quick search: 14,712 rows after
#  three passes, about 157 bytes each. A deep run is several times that, and the box has 62 GB.
#  While the run is alive the rows ARE the checkpoint and must not be touched. Once it is settled
#  they are evidence, and evidence has a retention, not an eternity.
HITS_TTL_DAYS = float(os.environ.get("RETRIEVAL_HITS_TTL_DAYS", "7"))
#  One sweep deletes at most this many rows, so the delete is a bounded statement on a live box
#  rather than one that takes a lock for as long as it takes.
HITS_PRUNE_BATCH = int(os.environ.get("RETRIEVAL_HITS_PRUNE_BATCH", "20000"))


def prune_hits(ttl_days=None, batch=None):
    """Drop the raw hit ledger of runs that settled longer ago than the retention. -> rows deleted.

    ONLY TERMINAL RUNS, and only ones that finished before the cutoff. A queued or running row's
    hits are its retrieval checkpoint: deleting them would make a resumed run re-issue every
    channel it had already paid for, which is the exact cost this whole mechanism exists to avoid.
    """
    ttl = float(HITS_TTL_DAYS if ttl_days is None else ttl_days)
    if ttl <= 0:
        return 0
    with _cur() as cur:
        cur.execute("""DELETE FROM retrieval_hits
                        WHERE id IN (SELECT h.id
                                       FROM retrieval_hits h
                                       JOIN search_runs r ON r.run_id = h.run_id
                                      WHERE r.status IN ('done','failed','cancelled')
                                        AND r.finished_at IS NOT NULL
                                        AND r.finished_at < now() - make_interval(secs => %s)
                                      LIMIT %s)""",
                    (ttl * 86400.0, int(batch or HITS_PRUNE_BATCH)))
        return cur.rowcount or 0


def channel_hits(run_id, channel, pass_key=""):
    """One pass's recorded hits for `channel`, in the rank order they were recorded in.

    Ordered by `rank` and then by insertion id, so a channel that recorded no ranks still comes
    back in the order it was written. Fusion consumes rank order and nothing else, which is what
    makes a resumed run's ranking identical to an uninterrupted one's.
    """
    with _cur() as cur:
        cur.execute("SELECT publication_number, rank, score, family_id, shard, meta "
                    "FROM retrieval_hits WHERE run_id=%s AND channel=%s "
                    "AND COALESCE(meta->>%s, '') = %s "
                    "ORDER BY rank NULLS LAST, id",
                    (run_id, channel, _PASS_META, str(pass_key or "")))
        return [_row(r) for r in cur.fetchall() or []]


_CANDIDATE_FIELDS = ("family_id", "channels", "fused_rank", "fused_score", "screen_score",
                     "screen_verdict", "read_score", "read_status", "final_rank", "final_score",
                     "stage", "detail")


def upsert_candidates(run_id, candidates):
    """Write or update the fused / screened / read candidate rows. Only the keys present in each
    dict are touched, so the screening pass does not erase what fusion recorded. -> count."""
    n = 0
    with _cur() as cur:
        for c in candidates or []:
            pub = c.get("publication_number") or c.get("pub")
            if not pub:
                continue
            cols, vals = ["run_id", "publication_number"], [run_id, pub]
            for f in _CANDIDATE_FIELDS:
                if f in c:
                    cols.append(f)
                    vals.append(json.dumps(c[f], default=str) if f == "detail" else c[f])
            sets = ", ".join(f"{f}=EXCLUDED.{f}" for f in cols[2:]) or "updated_at=now()"
            cur.execute(
                f"INSERT INTO search_candidates ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(vals))}) "
                f"ON CONFLICT (run_id, publication_number) DO UPDATE SET {sets}, "
                f"updated_at=now()", vals)
            n += 1
    return n


def candidates(run_id):
    with _cur() as cur:
        cur.execute("SELECT * FROM search_candidates WHERE run_id=%s "
                    "ORDER BY COALESCE(final_rank, fused_rank, 1 << 30), id", (run_id,))
        return [_row(r) for r in cur.fetchall() or []]


# ---------------------------------------------------------------------------------------------
# provider usage
# ---------------------------------------------------------------------------------------------
def record_usage(run_id, provider, *, tier="", model="", stage="", calls=1, prompt_tokens=0,
                 completion_tokens=0, cached_tokens=0, latency_ms=0, errors=0, cost_usd=0):
    """Accumulate one provider call onto this run's row. Keyed by run, so two concurrent searches
    cannot attribute each other's spend to themselves the way the process-wide counter did."""
    try:
        with _cur() as cur:
            cur.execute("""INSERT INTO provider_usage (run_id, provider, tier, model, stage,
                               calls, prompt_tokens, completion_tokens, cached_tokens,
                               latency_ms_total, errors, cost_usd)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (run_id, provider, tier, model, stage) DO UPDATE SET
                               calls = provider_usage.calls + EXCLUDED.calls,
                               prompt_tokens = provider_usage.prompt_tokens
                                               + EXCLUDED.prompt_tokens,
                               completion_tokens = provider_usage.completion_tokens
                                                   + EXCLUDED.completion_tokens,
                               cached_tokens = provider_usage.cached_tokens
                                               + EXCLUDED.cached_tokens,
                               latency_ms_total = provider_usage.latency_ms_total
                                                  + EXCLUDED.latency_ms_total,
                               errors = provider_usage.errors + EXCLUDED.errors,
                               cost_usd = provider_usage.cost_usd + EXCLUDED.cost_usd,
                               updated_at = now()""",
                        (run_id, provider, tier, model, stage, int(calls), int(prompt_tokens),
                         int(completion_tokens), int(cached_tokens), int(latency_ms),
                         int(errors), cost_usd))
    except Exception:
        traceback.print_exc()          # a metering failure must never fail a search


def usage(run_id):
    with _cur() as cur:
        cur.execute("SELECT * FROM provider_usage WHERE run_id=%s ORDER BY provider, tier, stage",
                    (run_id,))
        return [_row(r) for r in cur.fetchall() or []]


# ---------------------------------------------------------------------------------------------
# shard leases
# ---------------------------------------------------------------------------------------------
def lease_shard(run_id, shard, host="", seconds=None, meta=None):
    """Take (or refresh) this run's lease on a shard VM. -> the lease row."""
    with _cur() as cur:
        cur.execute("""INSERT INTO shard_leases (run_id, shard, host, state, expires_at, meta)
                       VALUES (%s,%s,%s,'held', now() + make_interval(secs => %s), %s)
                       ON CONFLICT (run_id, shard) WHERE state='held' DO UPDATE
                          SET heartbeat_at = now(),
                              expires_at   = EXCLUDED.expires_at,
                              host         = EXCLUDED.host
                       RETURNING *""",
                    (run_id, shard, host, float(seconds or SHARD_LEASE_SECONDS),
                     json.dumps(meta or {}, default=str)))
        return _row(cur.fetchone())


def heartbeat_shards(run_id, seconds=None):
    with _cur() as cur:
        cur.execute("""UPDATE shard_leases
                          SET heartbeat_at=now(),
                              expires_at = now() + make_interval(secs => %s)
                        WHERE run_id=%s AND state='held' RETURNING id""",
                    (float(seconds or SHARD_LEASE_SECONDS), run_id))
        return len(cur.fetchall() or [])


def release_shards(run_id, shard=None):
    with _cur() as cur:
        cur.execute("""UPDATE shard_leases SET state='released', released_at=now()
                        WHERE run_id=%s AND state='held' AND (%s::text IS NULL OR shard=%s)
                        RETURNING id""", (run_id, shard, shard))
        return len(cur.fetchall() or [])


def reap_shards():
    """Expire leases nobody is heartbeating, so a shard VM is not kept awake by a dead run."""
    with _cur() as cur:
        cur.execute("UPDATE shard_leases SET state='expired', released_at=now() "
                    "WHERE state='held' AND expires_at < now() RETURNING shard")
        return [r["shard"] for r in cur.fetchall() or []]


def held_shards(run_id=None):
    with _cur() as cur:
        cur.execute("SELECT * FROM shard_leases WHERE state='held' "
                    "AND (%s::text IS NULL OR run_id=%s) ORDER BY acquired_at", (run_id, run_id))
        return [_row(r) for r in cur.fetchall() or []]


# ---------------------------------------------------------------------------------------------
# corpus ingest queue: the ONLY thing a search may leave pointing at the corpus
# ---------------------------------------------------------------------------------------------
def queue_for_ingest(publication_number, *, run_id=None, reason="", source="", scratch_ref="",
                     payload=None, priority=100):
    """Ask for a publication to be added in the next PERMANENT corpus release.

    This is where search-time demand goes instead of into `chunks`. A repeat request bumps
    `request_count`, which is the demand signal the release process ranks by: a publication four
    searches wanted is worth more than one that one search wanted.
    """
    with _cur() as cur:
        cur.execute("""INSERT INTO corpus_ingest_queue (publication_number, requested_by_run,
                           reason, source, scratch_ref, payload, priority)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (publication_number) DO UPDATE SET
                           request_count = corpus_ingest_queue.request_count + 1,
                           last_requested_at = now(),
                           priority = LEAST(corpus_ingest_queue.priority, EXCLUDED.priority),
                           reason = CASE WHEN corpus_ingest_queue.state='pending'
                                         THEN EXCLUDED.reason ELSE corpus_ingest_queue.reason END,
                           scratch_ref = CASE WHEN EXCLUDED.scratch_ref <> ''
                                              THEN EXCLUDED.scratch_ref
                                              ELSE corpus_ingest_queue.scratch_ref END
                       RETURNING id, request_count, state""",
                    (str(publication_number), run_id, reason, source, scratch_ref,
                     json.dumps(payload or {}, default=str), int(priority)))
        return _row(cur.fetchone())


def pending_ingest(limit=500):
    with _cur() as cur:
        cur.execute("""SELECT * FROM corpus_ingest_queue WHERE state='pending'
                       ORDER BY priority, request_count DESC, first_requested_at
                       LIMIT %s""", (int(limit),))
        return [_row(r) for r in cur.fetchall() or []]


def claim_ingest(limit=100):
    """The offline release process takes a batch. SKIP LOCKED, same as run claiming, so two
    release workers never take the same publication."""
    with _cur() as cur:
        cur.execute("""WITH picked AS (
                           SELECT id FROM corpus_ingest_queue WHERE state='pending'
                           ORDER BY priority, request_count DESC, first_requested_at
                           FOR UPDATE SKIP LOCKED LIMIT %s)
                       UPDATE corpus_ingest_queue q SET state='claimed'
                         FROM picked p WHERE q.id = p.id RETURNING q.*""", (int(limit),))
        return [_row(r) for r in cur.fetchall() or []]


def mark_ingested(publication_numbers, corpus_release="", note=""):
    with _cur() as cur:
        cur.execute("""UPDATE corpus_ingest_queue
                          SET state='ingested', ingested_at=now(), corpus_release=%s, note=%s
                        WHERE publication_number = ANY(%s) RETURNING id""",
                    (corpus_release, note, list(publication_numbers)))
        return len(cur.fetchall() or [])


# ---------------------------------------------------------------------------------------------
# heartbeat thread
# ---------------------------------------------------------------------------------------------
class Heartbeat:
    """Keeps a run's lease (and its shard leases) alive while the worker holds it.

    `lost` is set the moment a heartbeat comes back False, which is the worker's signal to stop:
    the reaper has already handed the run to somebody else.

    DETECTING THE LOSS WAS NEVER THE PROBLEM. Nothing downstream heard it: a run whose lease had
    been reaped kept issuing provider calls until the pipeline finished on its own and only then
    discovered it could not settle. `on_lost` is how the detection reaches the threads that are
    spending. Callbacks run on THIS thread, must not block, and are called exactly once.
    """

    def __init__(self, run_id, worker, interval=None, lease_seconds=None):
        self.run_id, self.worker = run_id, worker
        self.interval = float(interval or HEARTBEAT_SECONDS)
        self.lease_seconds = float(lease_seconds or LEASE_SECONDS)
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._t = None
        self._on_lost = []
        self._cb_lock = threading.Lock()

    def on_lost(self, callback):
        """Call `callback` the moment this run's lease is gone.

        Registering AFTER the loss fires immediately, so a context built during a slow start
        cannot miss the one edge it exists to observe.
        """
        with self._cb_lock:
            self._on_lost.append(callback)
            already = self.lost.is_set()
        if already:
            self._fire(callback)
        return self

    @staticmethod
    def _fire(callback):
        try:
            callback()
        except Exception:                                            # noqa: BLE001
            #  A listener that raises must not stop the others, and must not take down the
            #  heartbeat thread: the lease is already lost, which is the important fact.
            traceback.print_exc()

    def _publish_lost(self):
        self.lost.set()
        with self._cb_lock:
            callbacks = list(self._on_lost)
        for cb in callbacks:
            self._fire(cb)

    def start(self):
        self._t = threading.Thread(target=self._loop, name=f"hb-{self.run_id}", daemon=True)
        self._t.start()
        return self

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                if not heartbeat(self.run_id, self.worker, self.lease_seconds):
                    print(f"[runstore] LEASE LOST for {self.run_id}; another worker owns it now",
                          flush=True)
                    self._publish_lost()
                    return
                heartbeat_shards(self.run_id)
            except Exception:
                #  A transient DB error must not drop the lease on its own; the lease will expire
                #  by itself if the database is genuinely unreachable.
                traceback.print_exc()

    def check(self):
        if self.lost.is_set():
            raise LeaseLost(f"{self.run_id} was reclaimed by the reaper")

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=2.0)


# ---------------------------------------------------------------------------------------------
# operational views
# ---------------------------------------------------------------------------------------------
def stats():
    with _cur() as cur:
        cur.execute("SELECT status, count(*) n FROM search_runs GROUP BY status")
        by_status = {r["status"]: int(r["n"]) for r in cur.fetchall() or []}
        cur.execute("SELECT count(*) n FROM search_runs WHERE status='running' "
                    "AND lease_expires_at < now()")
        expired = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT count(*) n FROM corpus_ingest_queue WHERE state='pending'")
        pending = int((cur.fetchone() or {}).get("n") or 0)
    return {"runs": by_status, "expired_leases": expired, "ingest_pending": pending}
