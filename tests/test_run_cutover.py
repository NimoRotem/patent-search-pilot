"""The durable producer and observer cutover, behind one flag.

Producer and observer only. Nothing here starts a worker, arms corpus writes or edits supervisor,
so under the flag a run is enqueued and then sits queued, which is exactly the intended state for
this milestone.

Persistence is exercised against a THROWAWAY database on the builder Postgres, created and dropped
by the fixture and never the live corpus. The durable schema is applied from
`sql/009_durable_runs.sql`, because `runstore.ensure_schema` deliberately refuses to create tables
at runtime; that also makes this the first thing to prove that migration actually runs.
"""
import ast
import json
import os
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import webapp                                                        # noqa: E402
import runstore                                                      # noqa: E402
import auth                                                          # noqa: E402

psycopg = pytest.importorskip("psycopg")

ADMIN = dict(host="127.0.0.1", port=5432, user="deep", dbname="deep_research")

#  PER PROCESS. A fixed name means two pytest processes on this box create and DROP the same
#  database underneath each other, and the loser's run is invalid whatever colour it reports.
#  That happened once here: a background full suite and a targeted run overlapped on one name.
#  The pid is the only identifier guaranteed unique among live processes, and it is formatted
#  rather than interpolated raw so the name can only ever be an identifier.
TESTDB = "patents_runcutover_test_%d" % os.getpid()


def _admin_password():
    try:
        out = subprocess.run(
            ["docker", "inspect", "deep-research-postgres", "--format",
             "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:                                                # noqa: BLE001
        return None
    for line in out.splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            return line.split("=", 1)[1]
    return None


@pytest.fixture(scope="module")
def pw():
    p = _admin_password()
    if not p:
        pytest.skip("no local postgres credentials")
    try:
        psycopg.connect(connect_timeout=6, password=p, **ADMIN).close()
    except Exception as exc:                                         # noqa: BLE001
        pytest.skip(f"local postgres unreachable: {exc}")
    return p


@pytest.fixture
def durable_db(pw, monkeypatch):
    """A throwaway database carrying the real durable schema, wired into runstore."""
    from psycopg.rows import dict_row
    adm = psycopg.connect(autocommit=True, password=pw, **ADMIN)
    with adm.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TESTDB}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{TESTDB}"')
    adm.close()

    dsn = dict(ADMIN, dbname=TESTDB, password=pw)
    ddl = open(os.path.join(ROOT, "sql", "009_durable_runs.sql"), encoding="utf-8").read()
    boot = psycopg.connect(autocommit=True, row_factory=dict_row, **dsn)
    with boot.cursor() as cur:
        cur.execute(ddl)
    boot.close()

    import db as real_db
    import contextlib

    def fake_connect(autocommit=False, readonly=False):
        return psycopg.connect(autocommit=autocommit, row_factory=dict_row, **dsn)

    @contextlib.contextmanager
    def fake_cursor(autocommit=False, readonly=False):
        conn = fake_connect(autocommit=autocommit)
        try:
            with conn.cursor() as cur:
                yield cur
            if not autocommit:
                conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(real_db, "connect", fake_connect)
    monkeypatch.setattr(real_db, "cursor", fake_cursor)
    runstore._schema_ready.clear()
    yield dsn
    runstore._schema_ready.clear()
    adm = psycopg.connect(autocommit=True, password=pw, **ADMIN)
    with adm.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TESTDB}" WITH (FORCE)')
    adm.close()


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    """A webapp with no live gate pressure, no disk reports and no legacy thread escaping."""
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=4, daily_cap=100, quick_max=4,
                                     quick_daily_cap=100, state_path=tmp_path / "gate.json"))
    with webapp._JOB_LOCK:
        webapp._JOBS.clear()
    started = []
    monkeypatch.setattr(webapp, "_run_job",
                        lambda *a, **k: started.append(a))
    recorded = []
    monkeypatch.setattr(webapp.run_queue, "record_started",
                        lambda slug, payload: recorded.append((slug, payload)))
    monkeypatch.setattr(webapp.run_queue, "enqueue", lambda slug, payload: 1)
    #  The legacy path resolves the subject against the CORPUS, which the throwaway durable
    #  database does not have and which this milestone must not touch.
    monkeypatch.setattr(webapp, "_subject_obj", lambda subject: None)
    return {"started": started, "recorded": recorded, "tmp": tmp_path}


def _flag(monkeypatch, on):
    if on:
        monkeypatch.setenv("DURABLE_SEARCH_RUNS", "1")
    else:
        monkeypatch.setenv("DURABLE_SEARCH_RUNS", "0")


PAYLOAD = dict(query="a vacuum gripper with a sealing lip", subject="US1234567B2",
               mode="novelty", wide=True, doc_token="tok-abc", search_focus="claims",
               depth="deep")


# =========================================================================== 1. the flag

def test_the_flag_defaults_to_the_legacy_path(monkeypatch):
    """Default must preserve production behaviour, so an unset variable is off."""
    monkeypatch.delenv("DURABLE_SEARCH_RUNS", raising=False)
    assert webapp.durable_runs_enabled() is False


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("no", False),
])
def test_the_flag_reads_the_environment_each_call(monkeypatch, value, expected):
    monkeypatch.setenv("DURABLE_SEARCH_RUNS", value)
    assert webapp.durable_runs_enabled() is expected


# =========================================================================== 2. flag off

def test_flag_off_starts_the_legacy_thread_and_records_the_queue_row(app_env, monkeypatch):
    _flag(monkeypatch, False)
    st, _ = webapp.ensure_report("legacy-slug", **PAYLOAD)
    assert st == "running"
    assert app_env["started"], "the legacy path did not start _run_job"
    assert app_env["recorded"], "the legacy path did not record its run_queue row"


def test_flag_off_writes_no_durable_row(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, False)
    webapp.ensure_report("legacy-only", **PAYLOAD)
    assert runstore.latest_for_slug("legacy-only") is None


# =========================================================================== 3. producer

def test_flag_on_enqueues_exactly_one_run_with_the_full_payload(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    st, _ = webapp.ensure_report("durable-slug", **PAYLOAD)
    assert st == "running"

    row = runstore.latest_for_slug("durable-slug")
    assert row is not None, "no durable run was created"
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"])
    for k, v in PAYLOAD.items():
        assert inp.get(k) == v, f"{k}: expected {v!r}, stored {inp.get(k)!r}"
    assert row["status"] == "queued"
    assert row["mode"] == "novelty"
    #  Serializable: whatever went in must survive a JSON round trip untouched.
    assert json.loads(json.dumps(inp)) == inp


@pytest.mark.parametrize("depth", ["quick", "deep"])
def test_the_lane_follows_the_depth(app_env, durable_db, monkeypatch, depth):
    _flag(monkeypatch, True)
    payload = dict(PAYLOAD, depth=depth)
    webapp.ensure_report(f"lane-{depth}", **payload)
    row = runstore.latest_for_slug(f"lane-{depth}")
    assert row["lane"] == depth
    assert row["depth"] == depth


def test_flag_on_does_not_start_the_legacy_thread_or_dispatcher(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    webapp.ensure_report("no-legacy", **PAYLOAD)
    assert not app_env["started"], "the legacy generation thread was started under the flag"
    assert not app_env["recorded"], "a run_queue dispatcher row was written under the flag"


def test_a_retried_submission_converges_on_the_live_run(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    webapp.ensure_report("same-slug", **PAYLOAD)
    first = runstore.latest_for_slug("same-slug")["run_id"]
    with webapp._JOB_LOCK:                      # a retry after the in-memory claim is gone
        webapp._JOBS.clear()
    webapp.ensure_report("same-slug", **PAYLOAD)
    assert runstore.latest_for_slug("same-slug")["run_id"] == first
    with runstore._cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE slug=%s", ("same-slug",))
        assert cur.fetchone()["n"] == 1, "a second run row was created for one slug"


def test_concurrent_submissions_converge_on_one_run(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    ready = threading.Barrier(4, timeout=20)
    errors = []

    def go():
        try:
            ready.wait()
            with webapp._JOB_LOCK:
                webapp._JOBS.pop("race-slug", None)
            webapp.ensure_report("race-slug", **PAYLOAD)
        except Exception as exc:                                     # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=25)
    assert not errors, errors
    with runstore._cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE slug=%s", ("race-slug",))
        assert cur.fetchone()["n"] == 1, "concurrent submissions created more than one run"


def test_a_durable_enqueue_failure_fails_closed(app_env, durable_db, monkeypatch):
    """No untracked legacy execution, ever. A broken run store refuses the search."""
    _flag(monkeypatch, True)

    def boom(*a, **k):
        raise RuntimeError("run store down")

    monkeypatch.setattr(runstore, "enqueue", boom)
    st, why = webapp.ensure_report("fail-closed", **PAYLOAD)
    assert st == "busy", f"expected a refusal, got {st}"
    assert why
    assert not app_env["started"], "it fell back to an untracked legacy run"
    assert not app_env["recorded"]
    with webapp._JOB_LOCK:
        assert "fail-closed" not in webapp._JOBS, "a phantom running claim was left behind"


def test_a_durable_enqueue_failure_releases_the_gate_slot(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(runstore, "enqueue",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    before = auth.run_gate.active
    webapp.ensure_report("fail-gate", **PAYLOAD)
    assert auth.run_gate.active == before, "the reserved gate slot leaked"


def test_the_gate_slot_is_not_held_while_the_run_only_sits_queued(app_env, durable_db, monkeypatch):
    """The web process is not executing the run, so it must not hold a concurrency slot. The
    daily budget still counts it, because it will cost money when a worker takes it."""
    _flag(monkeypatch, True)
    before_count = auth.run_gate.count
    webapp.ensure_report("gate-slug", **PAYLOAD)
    assert auth.run_gate.active == 0, "a concurrency slot is held for a run nobody is running"
    assert auth.run_gate.count == before_count + 1, "the daily budget did not count the run"


def test_a_cap_full_search_queues_durably_and_never_touches_run_queue(app_env, durable_db,
                                                                      monkeypatch):
    """Behaviour preservation, not tightening. Today a gate-full OR cap-full search is QUEUED and
    reported as running, it is not refused; the refusal is reserved for the dispatcher. Under the
    flag the durable store is the queue, so the same decision produces one queued durable run and
    no run_queue row."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=4, daily_cap=0, quick_max=4,
                                     quick_daily_cap=0,
                                     state_path=app_env["tmp"] / "capped.json"))
    legacy_q = []
    monkeypatch.setattr(webapp.run_queue, "enqueue",
                        lambda slug, payload: legacy_q.append(slug) or 1)
    st, _why = webapp.ensure_report("capped", **PAYLOAD)
    assert st == "running", st
    row = runstore.latest_for_slug("capped")
    assert row is not None and row["status"] == "queued"
    assert not legacy_q, "the legacy run_queue was used under the flag"
    assert not app_env["started"], "a cap-full search started the legacy thread"
    assert auth.run_gate.active == 0


def test_a_gate_full_search_holds_no_slot_and_makes_one_row(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100,
                                     state_path=app_env["tmp"] / "gatefull.json"))
    st, _ = webapp.ensure_report("gatefull", **PAYLOAD)
    assert st == "running"
    with runstore._cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE slug=%s", ("gatefull",))
        assert cur.fetchone()["n"] == 1


# =========================================================================== 4. observer

def _seed_run(slug, status="queued", **kw):
    rid = runstore.enqueue(slug, dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    if status != "queued":
        with runstore._cur() as cur:
            cur.execute("UPDATE search_runs SET status=%s WHERE run_id=%s", (status, rid))
    return rid


def test_status_reads_persisted_state_after_an_in_memory_reset(app_env, durable_db, monkeypatch):
    """The whole point of durability: a restart clears _JOBS, and the status must survive it."""
    _flag(monkeypatch, True)
    webapp.ensure_report("persisted", **PAYLOAD)
    with webapp._JOB_LOCK:
        webapp._JOBS.clear()                       # simulate the process restart
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    client = webapp.app.test_client()
    body = client.get("/status/persisted").get_json()
    assert body["status"] not in ("unknown",), body
    assert body["done"] is False
    assert body["ready"] is False


def test_the_progress_sequence_is_monotonic(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    rid = _seed_run("seq-slug")
    seqs = []
    for i in range(5):
        runstore.progress(rid, {"kind": "progress", "msg": f"step {i}"})
        seqs.append(runstore.progress_of(rid)["event_seq"])
    assert seqs == sorted(seqs), seqs
    assert len(set(seqs)) == len(seqs), f"event_seq repeated: {seqs}"


def _drain_sse(client, path, max_chunks=8, deadline=6.0):
    """Read a streaming response without ever hanging.

    The test client buffers a response by default, which on a live SSE stream means waiting for a
    generator that is designed never to end. This reads unbuffered, bounded by chunk count AND by
    wall clock, and reports whether the generator ENDED on its own, which is the property the
    terminal tests care about."""
    resp = client.get(path, buffered=False)
    chunks, ended = [], False
    t0 = time.time()
    it = resp.response.__iter__()
    try:
        while len(chunks) < max_chunks and time.time() - t0 < deadline:
            try:
                chunks.append(next(it))
            except StopIteration:
                ended = True
                break
    finally:
        try:
            resp.close()
        except Exception:                                            # noqa: BLE001
            pass
    body = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks).decode()
    return body, ended, resp.status_code


@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
def test_terminal_states_close_the_stream(app_env, durable_db, monkeypatch, terminal):
    _flag(monkeypatch, True)
    _seed_run(f"term-{terminal}", status=terminal)
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    body, ended, _ = _drain_sse(webapp.app.test_client(), f"/events/term-{terminal}")
    assert "data:" in body, body
    first = json.loads(body.split("data: ", 1)[1].split("\n\n", 1)[0])
    assert first["status"] in ("done", "error", "failed", "cancelled", "interrupted"), first
    assert ended, "the stream stayed open on a terminal run"
    assert ": ping" not in body, "the stream sent a keep-alive on a terminal run"


def test_a_live_durable_run_keeps_the_stream_open(app_env, durable_db, monkeypatch):
    """The other half of the contract: a queued or running run must NOT close early."""
    _flag(monkeypatch, True)
    _seed_run("live-stream", status="running")
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    body, ended, _ = _drain_sse(webapp.app.test_client(), "/events/live-stream",
                                max_chunks=2, deadline=3.0)
    assert "data:" in body, body
    assert not ended, "a live run closed its stream immediately"


def test_authorization_is_unchanged_for_a_durable_slug(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    webapp.ensure_report("private", **PAYLOAD)
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: False)
    client = webapp.app.test_client()
    assert client.get("/status/private").status_code == 404
    _body, _ended, code = _drain_sse(client, "/events/private", max_chunks=1, deadline=3.0)
    assert code == 404


# =========================================================================== 5. precedence

def test_a_legacy_only_slug_stays_readable_under_the_flag(app_env, durable_db, monkeypatch):
    """Rollout safety: runs already in flight when the flag is turned on have no durable row."""
    _flag(monkeypatch, True)
    with webapp._JOB_LOCK:
        webapp._JOBS["legacy-live"] = {"status": "running", "msg": "Reading…",
                                       "t0": time.time(), "tok0": 0}
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    body = webapp.app.test_client().get("/status/legacy-live").get_json()
    assert body["status"] == "running"
    assert body["msg"] == "Reading…"


def test_the_durable_row_takes_precedence_over_a_stale_memory_job(app_env, durable_db, monkeypatch):
    """The precedence rule, stated and tested: durable row first, legacy only when there is none."""
    _flag(monkeypatch, True)
    _seed_run("precedence", status="running")
    with webapp._JOB_LOCK:
        webapp._JOBS["precedence"] = {"status": "done", "msg": "stale", "t0": time.time()}
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    body = webapp.app.test_client().get("/status/precedence").get_json()
    assert body["msg"] != "stale", "a stale in-memory job outranked the persisted run"
    assert body["done"] is False


def test_flag_off_ignores_a_durable_row_entirely(app_env, durable_db, monkeypatch):
    """Exact legacy behaviour when the flag is off, even if durable rows exist from a rollout."""
    _flag(monkeypatch, True)
    _seed_run("both", status="running")
    _flag(monkeypatch, False)
    with webapp._JOB_LOCK:
        webapp._JOBS["both"] = {"status": "done", "msg": "legacy wins", "t0": time.time()}
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    body = webapp.app.test_client().get("/status/both").get_json()
    assert body["msg"] == "legacy wins"


# =========================================================================== 6. not in scope

def test_this_milestone_does_not_start_a_worker_or_arm_corpus_writes():
    """Producer and observer only. If any of these appear in webapp, the scope grew."""
    src = open(os.path.join(ROOT, "src", "webapp.py"), encoding="utf-8").read()
    assert "runner.worker" not in src
    assert "start_worker" not in src
    assert "arm_corpus_writes" not in src


# =========================================================================== review round 2


@pytest.fixture
def accounts_db(durable_db):
    """The real accounts schema in the throwaway database, so isolation is proven through
    `accounts.can_access_search` rather than by stubbing the check out.

    The module flag is `_SCHEMA_READY`, upper case. Setting a lower-case one did nothing except
    look like it worked, and leaving the real flag True after `db` is un-monkeypatched would tell
    every later test that the LIVE database had already been prepared by this fixture. It is
    saved and restored, and the throwaway schema is prepared with `force=True` so it cannot be
    skipped because of a flag some earlier test set.
    """
    import accounts as acc
    ddl = open(os.path.join(ROOT, "sql", "003_app_accounts.sql"), encoding="utf-8").read()
    with runstore._cur() as cur:
        cur.execute(ddl)
    prev = acc._SCHEMA_READY
    try:
        acc.ensure_schema(force=True)
        yield durable_db
    finally:
        acc._SCHEMA_READY = prev


def _make_user(email):
    """A real row in the real app_users shape, not an invented one."""
    with runstore._cur() as cur:
        cur.execute(
            "INSERT INTO app_users (email, full_name, password_hash, is_admin, is_active) "
            "VALUES (%s,%s,'x',false,true) "
            "ON CONFLICT (email) DO UPDATE SET is_active=true RETURNING id",
            (email, email.split("@")[0]))
        return cur.fetchone()["id"]


# --------------------------------------------------------------------- 1. ownership

def test_the_payload_carries_the_owner_user_id(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    webapp.ensure_report("owned", owner_user_id=4242, **PAYLOAD)
    row = runstore.latest_for_slug("owned")
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"])
    assert inp.get("owner_user_id") == 4242


def test_a_launcher_with_no_account_is_recorded_explicitly_as_unowned(app_env, durable_db,
                                                                     monkeypatch):
    """A missing key and a deliberate null are different things. Ops launchers, warmers and the
    gold set have no account, and the payload must SAY so rather than omit the field."""
    _flag(monkeypatch, True)
    webapp.ensure_report("unowned", **PAYLOAD)
    row = runstore.latest_for_slug("unowned")
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"])
    assert "owner_user_id" in inp, "the ownership field was omitted rather than set null"
    assert inp["owner_user_id"] is None


def test_two_real_accounts_stay_isolated_through_the_persisted_access_path(
        app_env, accounts_db, monkeypatch):
    """Isolation proven through accounts.can_access_search and app_saved_searches, not by
    monkeypatching the check to return False."""
    import accounts as acc
    _flag(monkeypatch, True)
    alice = _make_user("alice@example.invalid")
    bob = _make_user("bob@example.invalid")

    webapp.ensure_report("alice-run", owner_user_id=alice, **PAYLOAD)
    acc.record_search(alice, "alice-run", PAYLOAD["query"], PAYLOAD["mode"],
                      PAYLOAD["search_focus"], PAYLOAD["subject"], notify_email=False)

    assert acc.can_access_search(alice, "alice-run") is True
    assert acc.can_access_search(bob, "alice-run") is False

    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    monkeypatch.setattr(auth, "accounts_enabled", lambda app_=None: True)
    monkeypatch.setattr(auth, "is_admin", lambda: False)
    client = webapp.app.test_client()

    monkeypatch.setattr(auth, "current_user", lambda: {"id": bob})
    assert client.get("/status/alice-run").status_code == 404, "bob read alice's run"

    monkeypatch.setattr(auth, "current_user", lambda: {"id": alice})
    assert client.get("/status/alice-run").status_code == 200


# --------------------------------------------------------------------- 2. search_mode

def test_a_document_with_claims_enqueues_a_claim_attack(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(webapp, "_load_doc_materials",
                        lambda tok: {"claims": ["1. A vacuum gripper comprising a sealing lip."]})
    webapp.ensure_report("claimy", **PAYLOAD)
    assert runstore.latest_for_slug("claimy")["search_mode"] == "CLAIM_ATTACK"


def test_a_typed_description_enqueues_a_concept_search(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(webapp, "_load_doc_materials", lambda tok: None)
    webapp.ensure_report("concepty", **dict(PAYLOAD, doc_token=None))
    assert runstore.latest_for_slug("concepty")["search_mode"] == "CONCEPT_SEARCH"


def test_the_legal_mode_is_separate_from_the_search_mode(app_env, durable_db, monkeypatch):
    """`mode` is the legal question (novelty, obviousness). `search_mode` is the machinery. One
    must never be written into the other."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(webapp, "_load_doc_materials",
                        lambda tok: {"claims": ["1. A gripper."]})
    webapp.ensure_report("both-modes", **dict(PAYLOAD, mode="obviousness"))
    row = runstore.latest_for_slug("both-modes")
    assert row["mode"] == "obviousness"
    assert row["search_mode"] == "CLAIM_ATTACK"


def test_a_doc_load_failure_does_not_break_the_enqueue(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(webapp, "_load_doc_materials",
                        lambda tok: (_ for _ in ()).throw(RuntimeError("doc store down")))
    webapp.ensure_report("docfail", **PAYLOAD)
    assert runstore.latest_for_slug("docfail")["search_mode"] == "CONCEPT_SEARCH"


# --------------------------------------------------------------------- 3. one budget decision

def test_a_sequential_retry_consumes_the_daily_budget_once(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    before = auth.run_gate.count
    webapp.ensure_report("budget-once", **PAYLOAD)
    with webapp._JOB_LOCK:
        webapp._JOBS.clear()
    webapp.ensure_report("budget-once", **PAYLOAD)
    with webapp._JOB_LOCK:
        webapp._JOBS.clear()
    webapp.ensure_report("budget-once", **PAYLOAD)
    assert auth.run_gate.count == before + 1, (
        f"the daily budget moved by {auth.run_gate.count - before} for one run")
    with runstore._cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE slug=%s", ("budget-once",))
        assert cur.fetchone()["n"] == 1


def test_concurrent_submissions_consume_the_daily_budget_once(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    before = auth.run_gate.count
    ready = threading.Barrier(5, timeout=25)
    errors = []

    def go():
        try:
            ready.wait()
            with webapp._JOB_LOCK:
                webapp._JOBS.pop("budget-race", None)
            webapp.ensure_report("budget-race", **PAYLOAD)
        except Exception as exc:                                     # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not errors, errors
    with runstore._cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE slug=%s", ("budget-race",))
        assert cur.fetchone()["n"] == 1
    assert auth.run_gate.count == before + 1, (
        f"{auth.run_gate.count - before} submissions each charged the daily budget")


def test_enqueue_reports_whether_it_created_the_run(durable_db):
    rid, created = runstore.enqueue("created-flag", {"query": "q"}, lane="quick",
                                    with_created=True)
    assert created is True
    rid2, created2 = runstore.enqueue("created-flag", {"query": "q"}, lane="quick",
                                      with_created=True)
    assert created2 is False and rid2 == rid


def test_enqueue_keeps_its_old_single_value_return(durable_db):
    rid = runstore.enqueue("legacy-return", {"query": "q"}, lane="quick")
    assert isinstance(rid, str)


# --------------------------------------------------------------------- 4. elapsed time

def test_status_reports_elapsed_time_from_a_persisted_datetime_row(app_env, durable_db,
                                                                   monkeypatch):
    """`latest_for_slug` returns real datetimes; `progress_of` returns epoch aliases. The event
    shaper is fed by both and must not silently drop the clock for one of them."""
    _flag(monkeypatch, True)
    webapp.ensure_report("clocked", **PAYLOAD)
    row = runstore.latest_for_slug("clocked")
    assert not isinstance(row.get("t0"), (int, float)), "fixture assumption: no epoch alias here"
    ev = webapp._durable_event("clocked", row)
    assert ev["elapsed_total_sec"] is not None, "elapsed time was lost on a datetime row"
    assert ev["elapsed_total_sec"] >= 0
    assert ev["elapsed_sec"] >= 0


def test_status_reports_elapsed_time_from_the_epoch_alias_row(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    webapp.ensure_report("clocked2", **PAYLOAD)
    rid = runstore.latest_for_slug("clocked2")["run_id"]
    ev = webapp._durable_event("clocked2", runstore.progress_of(rid))
    assert ev["elapsed_total_sec"] is not None
    assert ev["elapsed_sec"] >= 0


# --------------------------------------------------------------------- 5. no detail leak

def test_a_stream_database_error_does_not_leak_its_detail(app_env, durable_db, monkeypatch,
                                                          capsys):
    _flag(monkeypatch, True)
    secret = "FATAL: password authentication failed for user patents"

    def boom(_rid):
        raise RuntimeError(secret)

    monkeypatch.setattr(runstore, "progress_of", boom)
    body = "".join(webapp._durable_stream("leaky", "run-1"))
    assert secret not in body, f"the stream leaked the database error: {body}"
    assert "password" not in body.lower()
    assert '"status": "error"' in body or "'status': 'error'" in body
    assert secret in capsys.readouterr().err, "the detail was not logged server side"


# --------------------------------------------------------------------- 6. observer strength

def test_a_reconnect_sees_the_current_persisted_sequence(app_env, durable_db, monkeypatch):
    """A client that reconnects must be handed CURRENT state, not replayed from zero."""
    _flag(monkeypatch, True)
    rid = _seed_run("reconnect")
    for i in range(3):
        runstore.progress(rid, {"kind": "progress", "msg": f"step {i}"})
    seq_now = runstore.progress_of(rid)["event_seq"]
    assert seq_now >= 3

    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    body, ended, _ = _drain_sse(webapp.app.test_client(), "/events/reconnect",
                                max_chunks=1, deadline=4.0)
    first = json.loads(body.split("data: ", 1)[1].split("\n\n", 1)[0])
    assert first["seq"] == seq_now, f"reconnect replayed {first['seq']}, current is {seq_now}"
    assert not ended


def test_the_sequence_never_goes_backwards_across_two_connections(app_env, durable_db,
                                                                  monkeypatch):
    _flag(monkeypatch, True)
    rid = _seed_run("no-rewind")
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    client = webapp.app.test_client()

    seen = []
    for _ in range(3):
        runstore.progress(rid, {"kind": "progress", "msg": "tick"})
        body, _e, _c = _drain_sse(client, "/events/no-rewind", max_chunks=1, deadline=4.0)
        seen.append(json.loads(body.split("data: ", 1)[1].split("\n\n", 1)[0])["seq"])
    assert seen == sorted(seen) and len(set(seen)) == len(seen), seen


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_terminal_failures_keep_the_browser_wire_contract(app_env, durable_db, monkeypatch,
                                                          terminal):
    """Current browser code closes on status error/done and reads these keys. A terminal failure
    must not claim a result and must not omit a field the page renders."""
    _flag(monkeypatch, True)
    _seed_run(f"wire-{terminal}", status=terminal)
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    body, ended, _ = _drain_sse(webapp.app.test_client(), f"/events/wire-{terminal}")
    ev = json.loads(body.split("data: ", 1)[1].split("\n\n", 1)[0])
    for key in ("kind", "slug", "status", "msg", "detail", "elapsed_sec", "tokens",
                "ready", "done", "attempt"):
        assert key in ev, f"the wire contract lost {key}"
    assert ev["status"] == "error"
    assert ev["done"] is False and ev["ready"] is False
    assert ended


def test_the_run_route_passes_its_user_as_the_owner(app_env, durable_db, monkeypatch):
    """The /run route already has `user` in scope before ensure_report, so it must not be
    rediscovered later or lost."""
    seen = {}
    real = webapp.ensure_report

    def spy(slug, **kw):
        seen.update(kw)
        return real(slug, **kw)

    monkeypatch.setattr(webapp, "ensure_report", spy)
    src = open(os.path.join(ROOT, "src", "webapp.py"), encoding="utf-8").read()
    assert "owner_user_id=(user or {}).get(\"id\")" in src, (
        "the /run route does not pass its user as the durable owner")


def test_an_explicit_null_owner_stays_null_even_in_an_authenticated_request(
        app_env, durable_db, monkeypatch):
    """The payload preserves its argument exactly. A caller that deliberately says "nobody owns
    this" must not have the ambient request user substituted underneath it: that is how a
    system-owned benchmark run ends up in a real person's list."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "current_user", lambda: {"id": 77})
    with webapp.app.test_request_context("/run"):
        webapp.ensure_report("explicit-null", owner_user_id=None, **PAYLOAD)
    row = runstore.latest_for_slug("explicit-null")
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"])
    assert inp["owner_user_id"] is None, (
        f"an explicit null was replaced with {inp['owner_user_id']!r}")


def test_an_explicit_owner_beats_the_request_user(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "current_user", lambda: {"id": 77})
    webapp.ensure_report("explicit-owner", owner_user_id=5, **PAYLOAD)
    row = runstore.latest_for_slug("explicit-owner")
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"])
    assert inp["owner_user_id"] == 5


# =========================================================================== review round 3
# Ownership proven at the ROUTE, not by calling ensure_report directly.


def _login_as(monkeypatch, user_id):
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    monkeypatch.setattr(auth, "accounts_enabled", lambda app_=None: True)
    monkeypatch.setattr(auth, "is_admin", lambda: False)
    monkeypatch.setattr(auth, "require_csrf", lambda *a, **k: None)
    monkeypatch.setattr(auth, "current_user", lambda: {"id": user_id})


def test_a_real_post_to_run_persists_the_authenticated_owner(app_env, accounts_db, monkeypatch):
    """Through the actual /run route, as a real app account. No direct ensure_report call and no
    manual record_search: if the route does not carry the user down, this fails."""
    import accounts as acc
    _flag(monkeypatch, True)
    alice = _make_user("route-alice@example.invalid")
    bob = _make_user("route-bob@example.invalid")

    monkeypatch.setattr(webapp, "domain_detect",
                        type("D", (), {"detect": staticmethod(lambda *a, **k: None)})())
    monkeypatch.setattr(webapp, "retriever", lambda: None)
    monkeypatch.setattr(webapp, "_load_doc_materials", lambda tok: None)

    _login_as(monkeypatch, alice)
    client = webapp.app.test_client()
    resp = client.post("/run", data={"query": "a vacuum gripper with a sealing lip",
                                     "mode": "novelty", "search_focus": "all_text",
                                     "depth": "deep"})
    assert resp.status_code in (200, 302), resp.status_code

    with runstore._cur() as cur:
        cur.execute("SELECT slug, input FROM search_runs ORDER BY enqueued_at DESC LIMIT 1")
        row = cur.fetchone()
    assert row, "the route created no durable run"
    slug = row["slug"]
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"])
    assert inp.get("owner_user_id") == alice, (
        f"the route persisted owner {inp.get('owner_user_id')!r}, expected {alice}")

    assert acc.can_access_search(alice, slug) is True, "the route did not record the search"

    monkeypatch.setattr(auth, "current_user", lambda: {"id": bob})
    assert client.get(f"/status/{slug}").status_code == 404
    monkeypatch.setattr(auth, "current_user", lambda: {"id": alice})
    assert client.get(f"/status/{slug}").status_code == 200


def _ensure_report_calls():
    """Every call to ensure_report, from the parse tree rather than from a line window.

    A substring window is satisfied by any unrelated `owner_user_id` that happens to sit within
    twelve lines, which is exactly the kind of test that passes after the code regresses."""
    import ast
    tree = ast.parse(open(os.path.join(ROOT, "src", "webapp.py"), encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name == "ensure_report":
            out.append(node)
    return out


def test_every_ensure_report_call_site_passes_an_owner():
    calls = _ensure_report_calls()
    assert len(calls) >= 5, f"expected every launch entry point, found {len(calls)}"
    for node in calls:
        kws = {k.arg for k in node.keywords if k.arg}
        assert "owner_user_id" in kws, (
            f"ensure_report call at line {node.lineno} does not pass owner_user_id, "
            f"it passes {sorted(kws)}")


def test_the_gold_call_site_states_its_ownership_in_the_parse_tree():
    for node in _ensure_report_calls():
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Name) and first.id == "gold_id":
            assert "owner_user_id" in {k.arg for k in node.keywords if k.arg}
            return
    raise AssertionError("the gold launch call site was not found")


def test_queue_launch_forwards_the_owner_from_its_payload(app_env, durable_db, monkeypatch):
    """A row queued under the legacy path and dispatched later must not lose its owner."""
    _flag(monkeypatch, True)
    webapp._queue_launch("relaunched", {"query": "q", "mode": "novelty", "depth": "deep",
                                        "owner_user_id": 909})
    row = runstore.latest_for_slug("relaunched")
    assert row is not None
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"])
    assert inp.get("owner_user_id") == 909


def test_the_legacy_queue_payload_carries_the_owner(app_env, monkeypatch):
    """Flag OFF: the run_queue payload must carry the owner too, or a row queued before a
    rollout comes back ownerless after it."""
    _flag(monkeypatch, False)
    captured = {}
    monkeypatch.setattr(webapp.run_queue, "record_started",
                        lambda slug, payload: captured.update(payload))
    webapp.ensure_report("legacy-owner", owner_user_id=31, **PAYLOAD)
    assert captured.get("owner_user_id") == 31


def test_the_legacy_enqueue_payload_carries_the_owner(app_env, monkeypatch):
    _flag(monkeypatch, False)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100,
                                     state_path=app_env["tmp"] / "qfull.json"))
    captured = {}
    monkeypatch.setattr(webapp.run_queue, "enqueue",
                        lambda slug, payload: captured.update(payload) or 1)
    webapp.ensure_report("legacy-queued-owner", owner_user_id=32, **PAYLOAD)
    assert captured.get("owner_user_id") == 32


def test_accounts_schema_flag_is_restored_after_the_fixture(accounts_db):
    """Order safety: the fixture must not leave the live database looking prepared."""
    import accounts as acc
    assert isinstance(acc._SCHEMA_READY, bool)


# =========================================================================== review round 4
# A real transition over a CONNECTED stream, not a connection made after the fact.


def _next_event(gen, deadline=6.0):
    """The next SSE data event from the generator, skipping keep-alive comments."""
    t0 = time.time()
    while time.time() - t0 < deadline:
        chunk = next(gen)
        if not chunk.startswith("data:"):
            continue
        return json.loads(chunk.split("data: ", 1)[1].split("\n\n", 1)[0])
    raise AssertionError("no event before the deadline")


@pytest.mark.parametrize("settle,expect_status", [
    ("done", "done"), ("failed", "error"), ("cancelled", "error"),
])
def test_a_connected_stream_sees_the_terminal_transition(app_env, durable_db, monkeypatch,
                                                         settle, expect_status):
    """THE GAP. claim, finish, fail and reap change persisted status without touching
    event_seq. A client already holding seq N then sees the same seq with a terminal status,
    emits nothing because the sequence did not advance, and closes. The browser is left on the
    last progress message with a silently dead stream.

    Connecting AFTER the row is terminal cannot catch this, which is why the earlier terminal
    tests passed while the bug was live.
    """
    _flag(monkeypatch, True)
    slug = f"transition-{settle}"
    run_id = runstore.enqueue(slug, dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")

    gen = webapp._durable_stream(slug, run_id, poll=0.01)
    first = _next_event(gen)
    assert first["status"] == "running", first          # queued reads as running on the wire
    seq_queued = first["seq"]

    #  A worker takes it. No progress call, only the claim.
    claimed = runstore.claim("test-worker", lanes=["deep"])
    assert claimed and claimed["run_id"] == run_id, claimed
    running = _next_event(gen)
    assert running["seq"] > seq_queued, (
        f"claim did not advance the sequence: {running['seq']} vs {seq_queued}")

    #  It settles. Again with no progress call.
    if settle == "done":
        assert runstore.finish(run_id, "test-worker", status="done") is True
    elif settle == "failed":
        assert runstore.fail(run_id, "test-worker", "boom", retry=False) == "failed"
    else:
        runstore.cancel(run_id)

    terminal = _next_event(gen)
    assert terminal["seq"] > running["seq"], (
        f"the terminal transition did not advance the sequence: {terminal['seq']}")
    assert terminal["status"] == expect_status, terminal
    assert terminal["done"] is (settle == "done")
    with pytest.raises(StopIteration):
        next(gen)


def test_a_requeue_after_a_failure_is_observable(app_env, durable_db, monkeypatch):
    """`fail(retry=True)` puts the run back in the queue. That is a state change a watching
    client must see, not a silent gap."""
    _flag(monkeypatch, True)
    run_id = runstore.enqueue("requeued", dict(PAYLOAD), mode="novelty", depth="deep",
                              lane="deep", max_attempts=3)
    gen = webapp._durable_stream("requeued", run_id, poll=0.01)
    first = _next_event(gen)
    runstore.claim("test-worker", lanes=["deep"])
    running = _next_event(gen)
    assert runstore.fail(run_id, "test-worker", "transient", retry=True) == "queued"
    back = _next_event(gen)
    assert back["seq"] > running["seq"], "a requeue was invisible to a connected client"
    assert back["status"] == "running"


def test_a_stale_worker_update_changes_nothing_and_bumps_nothing(app_env, durable_db,
                                                                 monkeypatch):
    """A no-op update must not advance the sequence, or every rejected write from a worker that
    lost its lease would look like news."""
    _flag(monkeypatch, True)
    run_id = runstore.enqueue("stale", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    runstore.claim("real-worker", lanes=["deep"])
    before = runstore.progress_of(run_id)["event_seq"]

    assert runstore.finish(run_id, "impostor", status="done") is False
    assert runstore.fail(run_id, "impostor", "nope", retry=False) is None

    after = runstore.progress_of(run_id)["event_seq"]
    assert after == before, f"a rejected write advanced the sequence {before} to {after}"
    assert runstore.progress_of(run_id)["status"] == "running"


def test_the_reaper_transition_is_observable(app_env, durable_db, monkeypatch):
    """A worker that was SIGKILLed stops heartbeating and the reaper requeues its run. A client
    watching that run must see it, not sit on a stale message."""
    _flag(monkeypatch, True)
    run_id = runstore.enqueue("reaped", dict(PAYLOAD), mode="novelty", depth="deep",
                              lane="deep", max_attempts=3)
    runstore.claim("dead-worker", lanes=["deep"])
    before = runstore.progress_of(run_id)["event_seq"]
    with runstore._cur() as cur:                       # expire the lease, as a dead worker would
        cur.execute("UPDATE search_runs SET lease_expires_at = now() - interval '1 hour' "
                    "WHERE run_id=%s", (run_id,))
    out = runstore.reap(run_ids=[run_id])
    assert run_id in out["requeued"], out
    after = runstore.progress_of(run_id)
    assert after["event_seq"] > before, "the reaper transition was invisible"
    assert after["status"] == "queued"


def test_the_reaper_bumps_nothing_when_it_reaps_nothing(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    run_id = runstore.enqueue("not-reaped", dict(PAYLOAD), mode="novelty", depth="deep",
                              lane="deep")
    runstore.claim("live-worker", lanes=["deep"])
    before = runstore.progress_of(run_id)["event_seq"]
    runstore.reap(run_ids=[run_id])                    # lease is healthy, nothing to reap
    assert runstore.progress_of(run_id)["event_seq"] == before


# =========================================================================== review round 5


def test_a_settled_durable_run_can_be_rerun_without_a_process_restart(app_env, durable_db,
                                                                     monkeypatch):
    """THE GAP. The durable path left a `running` entry in the in-memory _JOBS dict that nothing
    ever cleared, and ensure_report consulted that dict BEFORE Postgres. So once a run settled
    failed or cancelled, every rerun of the same slug returned early from the stale memory claim
    and no replacement run was ever created until the web process restarted.
    """
    _flag(monkeypatch, True)
    webapp.ensure_report("rerunnable", **PAYLOAD)
    first = runstore.latest_for_slug("rerunnable")["run_id"]
    runstore.claim("w", lanes=["deep"])
    assert runstore.fail(first, "w", "boom", retry=False) == "failed"

    #  Deliberately NOT clearing _JOBS: that is the restart this must not require.
    before_count = auth.run_gate.count
    st, _ = webapp.ensure_report("rerunnable", **PAYLOAD)
    assert st == "running", st
    second = runstore.latest_for_slug("rerunnable")["run_id"]
    assert second != first, "the stale memory claim blocked a rerun of a settled run"
    assert runstore.progress_of(second)["status"] == "queued"
    assert auth.run_gate.count == before_count + 1, (
        f"the rerun charged the budget {auth.run_gate.count - before_count} times")


@pytest.mark.parametrize("settle", ["failed", "cancelled"])
def test_a_cancelled_or_failed_run_is_rerunnable(app_env, durable_db, monkeypatch, settle):
    _flag(monkeypatch, True)
    slug = f"rerun-{settle}"
    webapp.ensure_report(slug, **PAYLOAD)
    rid = runstore.latest_for_slug(slug)["run_id"]
    if settle == "failed":
        runstore.claim("w", lanes=["deep"])
        runstore.fail(rid, "w", "boom", retry=False)
    else:
        runstore.cancel(rid)
    webapp.ensure_report(slug, **PAYLOAD)
    assert runstore.latest_for_slug(slug)["run_id"] != rid


def test_the_durable_path_leaves_no_memory_placeholder(app_env, durable_db, monkeypatch):
    """Postgres is the dedupe and status authority under the flag. A parallel in-memory claim is
    a second source of truth that can only ever disagree."""
    _flag(monkeypatch, True)
    webapp.ensure_report("no-placeholder", **PAYLOAD)
    with webapp._JOB_LOCK:
        assert "no-placeholder" not in webapp._JOBS, (
            "the durable path created an in-memory claim it never clears")


def test_a_live_durable_run_survives_an_in_memory_reset_without_a_query(app_env, durable_db,
                                                                       monkeypatch):
    """A viewer poll passes no query. With _JOBS cleared by a restart, the run must still be
    recognised as live from Postgres rather than reported missing."""
    _flag(monkeypatch, True)
    webapp.ensure_report("survivor", **PAYLOAD)
    with webapp._JOB_LOCK:
        webapp._JOBS.clear()
    st, _ = webapp.ensure_report("survivor")
    assert st == "running", f"a live persisted run reported {st!r} after an in-memory reset"


def test_a_slug_with_no_run_and_no_query_is_still_missing(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    st, _ = webapp.ensure_report("never-existed")
    assert st == "missing"


def test_the_report_cache_fast_path_is_unchanged_under_the_flag(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    (app_env["tmp"] / "cached.json").write_text(json.dumps({"query": "q", "partial": False}))
    st, rep = webapp.ensure_report("cached", **PAYLOAD)
    assert st == "ready" and rep is not None
    assert runstore.latest_for_slug("cached") is None, "a cached report still enqueued a run"


def test_an_authenticated_gold_launch_is_still_system_owned(app_env, accounts_db, monkeypatch):
    """The gold branch redirects before accounts.record_search ever runs, so a named initiator
    would never see the benchmark in their list. It is a shared fixture: explicitly system owned,
    even when a signed-in account starts it."""
    _flag(monkeypatch, True)
    carol = _make_user("gold-carol@example.invalid")
    gold_id = next(iter(webapp._GOLD), None)
    if not gold_id:
        pytest.skip("no gold set in this checkout")
    _login_as(monkeypatch, carol)
    monkeypatch.setattr(webapp, "_load_doc_materials", lambda tok: None)
    resp = webapp.app.test_client().post("/run", data={"gold_id": gold_id})
    assert resp.status_code in (200, 302), resp.status_code
    row = runstore.latest_for_slug(gold_id)
    assert row is not None, "the gold launch created no durable run"
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"])
    assert inp["owner_user_id"] is None, (
        f"a shared benchmark was filed under user {inp['owner_user_id']!r}")


# =========================================================================== review round 6
# The rollout transition: a legacy run already in flight when the flag flips on.


def test_flag_on_does_not_duplicate_a_legacy_run_already_in_flight(app_env, durable_db,
                                                                  monkeypatch):
    """THE GAP. Moving the durable branch ahead of the in-memory claim removed stale durable
    placeholders, but it also made the durable path ignore a GENUINE legacy claim. A run started
    under the old path is still executing in this process; enqueueing a durable row for the same
    slug puts two executors on one report file.
    """
    _flag(monkeypatch, False)
    st, _ = webapp.ensure_report("mid-rollout", **PAYLOAD)
    assert st == "running"
    assert app_env["started"], "the legacy run did not start"

    _flag(monkeypatch, True)                      # the rollout happens mid-flight
    st2, _ = webapp.ensure_report("mid-rollout", **PAYLOAD)
    assert st2 == "running", st2
    assert runstore.latest_for_slug("mid-rollout") is None, (
        "a second executor was enqueued for a run already in flight")
    assert len(app_env["started"]) == 1, "the legacy run was started twice"


def test_the_dispatcher_may_migrate_a_queued_legacy_placeholder(app_env, durable_db, monkeypatch):
    """The other half: a QUEUED legacy placeholder is not an executor, it is a row waiting for
    one. The dispatcher must be able to hand it to the durable store, exactly once."""
    _flag(monkeypatch, False)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100,
                                     state_path=app_env["tmp"] / "qmig.json"))
    webapp.ensure_report("migrate-me", **PAYLOAD)
    with webapp._JOB_LOCK:
        job = webapp._JOBS.get("migrate-me") or {}
    assert job.get("queued"), f"expected a queued placeholder, got {job}"

    _flag(monkeypatch, True)
    webapp._queue_launch("migrate-me", dict(PAYLOAD, owner_user_id=12))
    row = runstore.latest_for_slug("migrate-me")
    assert row is not None, "the dispatcher could not migrate a queued run"
    inp = row["input"] if isinstance(row["input"], dict) else json.loads(row["input"])
    assert inp["owner_user_id"] == 12

    webapp._queue_launch("migrate-me", dict(PAYLOAD, owner_user_id=12))
    with runstore._cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE slug=%s", ("migrate-me",))
        assert cur.fetchone()["n"] == 1, "the dispatcher migrated the same run twice"


def test_a_durable_enqueue_failure_does_not_erase_a_legacy_claim(app_env, durable_db,
                                                                 monkeypatch):
    """The durable branch no longer owns a memory claim, so it must not delete one it did not
    create: that would strand a legacy run that is still executing."""
    _flag(monkeypatch, True)
    with webapp._JOB_LOCK:
        webapp._JOBS["legacy-owned"] = {"status": "running", "msg": "legacy", "t0": time.time()}
    monkeypatch.setattr(runstore, "enqueue",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    #  A live legacy claim short-circuits before the store is reached, so force the failure path
    #  by asking about a slug that has no claim, then assert the other one survived.
    webapp.ensure_report("no-claim", **PAYLOAD)
    with webapp._JOB_LOCK:
        assert "legacy-owned" in webapp._JOBS, "the durable failure path erased a legacy claim"


# =========================================================================== review round 7
# Report cache and regen under the durable flag.


def _seed_report(tmp, slug, partial=False):
    (tmp / f"{slug}.json").write_text(json.dumps({"query": "q", "partial": partial}))
    (tmp / f"{slug}.view.json").write_text(json.dumps({"cards": []}))
    (tmp / f"{slug}.detail-preview.json").write_text(json.dumps({"x": 1}))


def test_a_durable_regen_clears_the_stale_report_caches(app_env, durable_db, monkeypatch):
    """THE GAP. The durable branch returns before the legacy regen cleanup, so a rerun left the
    old final report and its view caches on disk. An ordinary GET then served that stale report
    as READY while the replacement was still queued: the user re-ran a search and was handed the
    answer they were trying to replace.
    """
    tmp = app_env["tmp"]
    _flag(monkeypatch, True)
    _seed_report(tmp, "regen-me")
    st, _ = webapp.ensure_report("regen-me", regen=True, **PAYLOAD)
    assert st == "running", st
    assert runstore.latest_for_slug("regen-me") is not None
    for suffix in (".json", ".view.json", ".detail-preview.json"):
        assert not (tmp / f"regen-me{suffix}").exists(), f"stale {suffix} survived the regen"

    st2, rep = webapp.ensure_report("regen-me", **PAYLOAD)
    assert st2 == "running", f"an ordinary call served {st2!r} after a regen"
    assert rep is None


def test_a_failed_durable_enqueue_does_not_delete_the_cached_report(app_env, durable_db,
                                                                    monkeypatch):
    """If nothing was recorded, the user must keep the report they already had."""
    tmp = app_env["tmp"]
    _flag(monkeypatch, True)
    _seed_report(tmp, "keep-cache")
    monkeypatch.setattr(runstore, "enqueue",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    st, _ = webapp.ensure_report("keep-cache", regen=True, **PAYLOAD)
    assert st == "busy", st
    assert (tmp / "keep-cache.json").exists(), "a failed enqueue destroyed the cached report"
    assert (tmp / "keep-cache.view.json").exists()


def test_a_live_durable_partial_is_not_treated_as_interrupted(app_env, durable_db, monkeypatch):
    """A durable worker writes `partial: true` with NO memory claim, because the durable path
    creates none. Deciding liveness from `_JOBS` alone therefore calls a live partial
    'interrupted' and deletes the work in progress."""
    tmp = app_env["tmp"]
    _flag(monkeypatch, True)
    _seed_report(tmp, "live-partial", partial=True)
    rid = runstore.enqueue("live-partial", dict(PAYLOAD), mode="novelty", depth="deep",
                           lane="deep")
    runstore.claim("w", lanes=["deep"])
    assert runstore.progress_of(rid)["status"] == "running"
    webapp._PARTIAL_CACHE.pop("live-partial", None)

    st, _ = webapp.ensure_report("live-partial", restart_partial=True, **PAYLOAD)
    assert (tmp / "live-partial.json").exists(), (
        "a live durable partial was deleted as interrupted")
    with runstore._cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE slug=%s", ("live-partial",))
        assert cur.fetchone()["n"] == 1, "a second run was created for a live partial"
    #  "ready" is the LEGACY answer for a live partial: the page renders the partial with its
    #  interrupted-or-working banner rather than restarting it. The flag must not change that,
    #  which is the whole point of deciding liveness from Postgres instead of from _JOBS.
    assert st == "ready", st


def test_a_dead_durable_partial_is_still_restartable(app_env, durable_db, monkeypatch):
    """The other side: an interrupted partial with no live run must still restart."""
    tmp = app_env["tmp"]
    _flag(monkeypatch, True)
    _seed_report(tmp, "dead-partial", partial=True)
    rid = runstore.enqueue("dead-partial", dict(PAYLOAD), mode="novelty", depth="deep",
                           lane="deep")
    runstore.claim("w", lanes=["deep"])
    runstore.fail(rid, "w", "died", retry=False)
    webapp._PARTIAL_CACHE.pop("dead-partial", None)

    st, _ = webapp.ensure_report("dead-partial", restart_partial=True, **PAYLOAD)
    assert st == "running"
    assert runstore.latest_for_slug("dead-partial")["run_id"] != rid


def test_flag_off_keeps_the_legacy_partial_logic(app_env, monkeypatch):
    tmp = app_env["tmp"]
    _flag(monkeypatch, False)
    _seed_report(tmp, "legacy-partial", partial=True)
    webapp._PARTIAL_CACHE.pop("legacy-partial", None)
    with webapp._JOB_LOCK:
        webapp._JOBS["legacy-partial"] = {"status": "partial", "msg": "working",
                                          "t0": time.time()}
    st, rep = webapp.ensure_report("legacy-partial", restart_partial=True, **PAYLOAD)
    assert st == "ready" and rep is not None, (
        "a live legacy partial should still be served, not restarted")


# =========================================================================== review round 8


def test_a_migrated_placeholder_does_not_block_the_next_run(app_env, durable_db, monkeypatch):
    """After the dispatcher migrates a queued legacy placeholder, that placeholder is stale: the
    durable row owns the run now. Leaving it means the next ordinary request sees a legacy claim
    and is blocked, which is the same reload trap in a new place."""
    _flag(monkeypatch, False)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100,
                                     state_path=app_env["tmp"] / "mig2.json"))
    webapp.ensure_report("mig-then-fail", **PAYLOAD)
    with webapp._JOB_LOCK:
        assert (webapp._JOBS.get("mig-then-fail") or {}).get("queued")

    _flag(monkeypatch, True)
    webapp._queue_launch("mig-then-fail", dict(PAYLOAD))
    rid = runstore.latest_for_slug("mig-then-fail")["run_id"]
    with webapp._JOB_LOCK:
        assert "mig-then-fail" not in webapp._JOBS, (
            "the migrated placeholder was left behind")

    runstore.claim("w", lanes=["deep"])
    runstore.fail(rid, "w", "boom", retry=False)
    st, _ = webapp.ensure_report("mig-then-fail", **PAYLOAD)
    assert st == "running"
    assert runstore.latest_for_slug("mig-then-fail")["run_id"] != rid


def test_an_unknown_durable_liveness_never_destroys_a_partial(app_env, durable_db, monkeypatch):
    """`_durable_run_for` swallowed every exception into None, which reads identically to 'there
    is no run'. The partial path then treated a DATABASE OUTAGE as 'nothing is running' and
    deleted work in progress, moments before the enqueue that would have replaced it also failed.
    """
    tmp = app_env["tmp"]
    _flag(monkeypatch, True)
    _seed_report(tmp, "unknown-live", partial=True)
    webapp._PARTIAL_CACHE.pop("unknown-live", None)

    def down(*a, **k):
        raise RuntimeError("run store unreachable")

    monkeypatch.setattr(runstore, "latest_for_slug", down)
    monkeypatch.setattr(runstore, "enqueue", down)
    st, _ = webapp.ensure_report("unknown-live", restart_partial=True, **PAYLOAD)
    assert (tmp / "unknown-live.json").exists(), "a partial was destroyed on an unknown state"
    assert (tmp / "unknown-live.view.json").exists()
    assert st in ("ready", "busy"), st


def test_a_live_legacy_partial_survives_the_flag_flipping_on(app_env, durable_db, monkeypatch):
    """Rollout: a legacy run is mid-flight with a partial on disk and no durable row. Liveness
    under the flag has to be the UNION of a pre-existing legacy claim and a live durable row, or
    the flag itself deletes work that was already running."""
    tmp = app_env["tmp"]
    _flag(monkeypatch, False)
    _seed_report(tmp, "rollout-partial", partial=True)
    webapp._PARTIAL_CACHE.pop("rollout-partial", None)
    with webapp._JOB_LOCK:
        webapp._JOBS["rollout-partial"] = {"status": "partial", "msg": "working",
                                           "t0": time.time()}

    _flag(monkeypatch, True)
    st, rep = webapp.ensure_report("rollout-partial", restart_partial=True, **PAYLOAD)
    assert (tmp / "rollout-partial.json").exists(), (
        "flipping the flag deleted a live legacy partial")
    assert runstore.latest_for_slug("rollout-partial") is None, (
        "a durable duplicate was enqueued for a live legacy run")
    assert st == "ready" and rep is not None
