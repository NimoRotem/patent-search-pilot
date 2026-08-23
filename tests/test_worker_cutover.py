"""Durable worker execution: observer outage safety, admission, lease loss and resume.

Persistence runs against a THROWAWAY database created and dropped per pytest process, never the
live corpus. The durable schema comes from `sql/009_durable_runs.sql`, because
`runstore.ensure_schema` refuses to create tables at runtime.
"""
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

#  WHERE THE THROWAWAY DATABASE LIVES.
#  These tests need a Postgres they may CREATE DATABASE on, which the live corpus box is not. The
#  original fixture found one by asking `docker inspect` about a container called
#  deep-research-postgres, and on a host with no docker, which the patents VM is, all 123 of them
#  skipped silently. That is the whole cutover: the route, the SSE stream and the worker loop,
#  unverified on the machine the cutover is deployed to.
#
#  So the location is overridable, and a password in the environment is enough on its own. To run
#  them against a throwaway cluster of your own:
#      initdb -D /tmp/pgtest -U deep --auth=trust
#      pg_ctl -D /tmp/pgtest -o "-p 55432 -k /tmp/pgtest" -l /tmp/pgtest/log start
#      createdb -h /tmp/pgtest -p 55432 -U deep deep_research
#      PATENTS_TEST_PG_PASSWORD=x PATENTS_TEST_PG_PORT=55432 PATENTS_TEST_PG_HOST=/tmp/pgtest pt ...
ADMIN = dict(host=os.environ.get("PATENTS_TEST_PG_HOST", "127.0.0.1"),
             port=int(os.environ.get("PATENTS_TEST_PG_PORT", "5432")),
             user=os.environ.get("PATENTS_TEST_PG_USER", "deep"),
             dbname=os.environ.get("PATENTS_TEST_PG_DB", "deep_research"))
TESTDB = "patents_workercut_test_%d" % os.getpid()

PAYLOAD = dict(query="a vacuum gripper with a sealing lip", subject="US1234567B2",
               mode="novelty", wide=True, doc_token="tok-abc", search_focus="claims",
               depth="deep")


def _admin_password():
    env = os.environ.get("PATENTS_TEST_PG_PASSWORD")
    if env:
        return env
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
    from psycopg.rows import dict_row
    import contextlib
    adm = psycopg.connect(autocommit=True, password=pw, **ADMIN)
    with adm.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TESTDB}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{TESTDB}"')
    adm.close()

    dsn = dict(ADMIN, dbname=TESTDB, password=pw)
    boot = psycopg.connect(autocommit=True, row_factory=dict_row, **dsn)
    with boot.cursor() as cur:
        for fn in ("009_durable_runs.sql", "012_run_admission.sql", "013_run_side_effects.sql"):
            cur.execute(open(os.path.join(ROOT, "sql", fn), encoding="utf-8").read())
    boot.close()

    import db as real_db

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
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=4, daily_cap=100, quick_max=4,
                                     quick_daily_cap=100, state_path=tmp_path / "gate.json"))
    with webapp._JOB_LOCK:
        webapp._JOBS.clear()
    monkeypatch.setattr(webapp, "_run_job", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "_subject_obj", lambda subject: None)
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    return {"tmp": tmp_path}


def _flag(monkeypatch, on):
    monkeypatch.setenv("DURABLE_SEARCH_RUNS", "1" if on else "0")


def _drain_sse(client, path, max_chunks=8, deadline=6.0):
    """Read a streaming response without ever hanging, and report whether it ENDED."""
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


SECRET = "FATAL: password authentication failed for user patents on host 10.128.0.53"


def _store_down(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(runstore, "latest_for_slug", boom)


# =========================================================================== 1. outage ambiguity


def test_status_returns_a_generic_retryable_error_when_the_store_is_unreachable(
        app_env, durable_db, monkeypatch, capsys):
    """THE GAP. `_durable_run_for` collapses a store OUTAGE into the same None as "no such run",
    so /status silently fell through to the legacy view and reported whatever stale memory said,
    or nothing at all. An unknown state is not an absent one."""
    _flag(monkeypatch, True)
    _store_down(monkeypatch)
    body = webapp.app.test_client().get("/status/outage").get_json()
    assert body["status"] == "error", body
    assert body["done"] is False
    assert body["ready"] is False
    assert body.get("retryable") is True, body
    for leak in ("password", "FATAL", "10.128.0.53", "patents", "RuntimeError"):
        assert leak not in json.dumps(body), f"the response leaked {leak!r}: {body}"
    assert SECRET in capsys.readouterr().err, "the detail was not logged server side"


def test_events_ends_the_stream_on_an_unknown_store_state(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    _store_down(monkeypatch)
    body, ended, _ = _drain_sse(webapp.app.test_client(), "/events/outage",
                                max_chunks=4, deadline=5.0)
    assert "data:" in body, body
    ev = json.loads(body.split("data: ", 1)[1].split("\n\n", 1)[0])
    assert ev["status"] == "error", ev
    assert ev.get("retryable") is True, ev
    assert ended, "the stream stayed open on an unknown store state"
    for leak in ("password", "FATAL", "10.128.0.53"):
        assert leak not in body, f"the stream leaked {leak!r}"


def test_a_live_legacy_claim_keeps_its_observer_during_an_outage(app_env, durable_db,
                                                                 monkeypatch):
    """If the process KNOWS a legacy run is live, that is real information and must survive a
    run-store outage: the page keeps reporting the run that is actually executing."""
    _flag(monkeypatch, True)
    _store_down(monkeypatch)
    with webapp._JOB_LOCK:
        webapp._JOBS["known-live"] = {"status": "running", "msg": "Reading references",
                                      "t0": time.time(), "tok0": 0}
    body = webapp.app.test_client().get("/status/known-live").get_json()
    assert body["status"] == "running", body
    assert body["msg"] == "Reading references"


def test_the_outage_error_is_one_fixed_message(app_env, durable_db, monkeypatch):
    """A fixed string, so no variant of it can carry a detail from the exception."""
    _flag(monkeypatch, True)
    seen = set()
    for exc_text in ("FATAL: role patents does not exist",
                     "could not connect to 10.128.0.53:5433",
                     "SSL SYSCALL error: EOF detected"):
        def boom(*a, _t=exc_text, **k):
            raise RuntimeError(_t)
        monkeypatch.setattr(runstore, "latest_for_slug", boom)
        body = webapp.app.test_client().get(f"/status/fixed-{len(seen)}").get_json()
        seen.add(body["msg"])
    assert len(seen) == 1, f"the message varies with the exception: {seen}"


def test_flag_off_is_unaffected_by_a_store_outage(app_env, durable_db, monkeypatch):
    """Legacy behaviour under the off state must not change, including when the store is down."""
    _flag(monkeypatch, False)
    _store_down(monkeypatch)
    with webapp._JOB_LOCK:
        webapp._JOBS["legacy-slug"] = {"status": "running", "msg": "legacy", "t0": time.time()}
    body = webapp.app.test_client().get("/status/legacy-slug").get_json()
    assert body["status"] == "running"
    assert body["msg"] == "legacy"
    assert "retryable" not in body


def test_durable_lookup_is_tri_state(app_env, durable_db, monkeypatch):
    """Unknown, absent and present must be three distinct answers at the seam itself."""
    _flag(monkeypatch, True)
    assert webapp._durable_lookup("nothing-here") == ("absent", None)
    rid = runstore.enqueue("present", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    state, row = webapp._durable_lookup("present")
    assert state == "present" and row["run_id"] == rid
    _store_down(monkeypatch)
    state, row = webapp._durable_lookup("present")
    assert state == "unknown" and row is None


def test_the_outage_status_uses_a_code_the_poller_retries(app_env, durable_db, monkeypatch):
    """Why 503 and not a 200 error frame.

    `static/app.js` poll(): `if (!r.ok){ setTimeout(poll, 2000); return null; }` so a non-200 keeps
    the page asking. A 200 carrying status "error" is instead handed to onEvent(), which treats a
    terminal frame as the end and STOPS polling: a transient run-store outage would permanently
    freeze the page on an error. The retryable condition has to be expressed in the status code,
    because that is the field the existing client actually branches on.
    """
    _flag(monkeypatch, True)
    _store_down(monkeypatch)
    resp = webapp.app.test_client().get("/status/retry-code")
    assert resp.status_code == 503, resp.status_code
    assert resp.get_json()["retryable"] is True

    js = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    assert "if (!r.ok){ setTimeout(poll, 2000); return null; }" in js, (
        "the client no longer retries on a non-200; revisit the code choice above")


# =========================================================================== 6. startup flag


def _worker():
    import runner.worker as w
    return w


def test_search_workers_cannot_claim_drafting_turns():
    """Importing webapp for a search must never start its in-process drafting poller."""
    with open(os.path.join(ROOT, "src", "runner", "worker.py"), encoding="utf-8") as source:
        worker = source.read()
    with open(os.path.join(ROOT, "patent-search-worker.conf"), encoding="utf-8") as source:
        config = source.read()

    assert 'os.environ.setdefault("DRAFT_TURN_WORKER", "0")' in worker
    assert config.count('DRAFT_TURN_WORKER="0"') == 2


def test_the_worker_refuses_to_start_without_an_explicit_flag(monkeypatch):
    """Execution is opt-in. A worker that runs because somebody typed the module name is a worker
    that spends money nobody asked it to."""
    w = _worker()
    monkeypatch.delenv("DURABLE_WORKER_ENABLED", raising=False)
    started = []
    monkeypatch.setattr(w, "loop", lambda **k: started.append(k))
    rc = w.main([])
    assert rc != 0, "the worker started with no flag set"
    assert not started, "the loop ran despite the refusal"


@pytest.mark.parametrize("value", ["maybe", "2", "yes please", "on/off", " ", "TRUEish"])
def test_a_malformed_worker_flag_fails_closed(monkeypatch, value):
    """Unparseable configuration must refuse, not guess. Guessing 'off' silently is survivable;
    guessing 'on' spends money, and a config typo must not be the difference."""
    w = _worker()
    monkeypatch.setenv("DURABLE_WORKER_ENABLED", value)
    started = []
    monkeypatch.setattr(w, "loop", lambda **k: started.append(k))
    rc = w.main([])
    assert rc != 0, f"{value!r} was accepted as a flag value"
    assert not started


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_an_explicit_true_lets_the_worker_start(monkeypatch, value):
    w = _worker()
    monkeypatch.setenv("DURABLE_WORKER_ENABLED", value)
    started = []
    monkeypatch.setattr(w, "loop", lambda **k: started.append(k))
    monkeypatch.setattr(w, "corpus_guard", type("G", (), {"arm": staticmethod(lambda *a: None)}))
    rc = w.main([])
    assert rc == 0
    assert started, "an explicitly enabled worker did not start"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_an_explicit_false_keeps_the_worker_down(monkeypatch, value):
    w = _worker()
    monkeypatch.setenv("DURABLE_WORKER_ENABLED", value)
    started = []
    monkeypatch.setattr(w, "loop", lambda **k: started.append(k))
    rc = w.main([])
    assert rc != 0
    assert not started


def test_the_reaper_only_mode_also_requires_the_flag(monkeypatch):
    """--reaper-only mutates run state, so it is not a read-only escape hatch."""
    w = _worker()
    monkeypatch.delenv("DURABLE_WORKER_ENABLED", raising=False)
    reaped = []
    monkeypatch.setattr(w.runstore, "reap", lambda *a, **k: reaped.append(1) or {})
    rc = w.main(["--reaper-only"])
    assert rc != 0
    assert not reaped, "the reaper ran without the flag"


@pytest.mark.parametrize("value", ["maybe", "2", "yes please"])
def test_a_malformed_producer_flag_falls_back_to_legacy_loudly(monkeypatch, capsys, value):
    """The WEB flag is the opposite trade. Refusing to serve would take the site down over a
    typo, so an unparseable value is treated as OFF, which is the legacy path, and says so."""
    monkeypatch.setenv("DURABLE_SEARCH_RUNS", value)
    assert webapp.durable_runs_enabled() is False
    err = capsys.readouterr().err
    assert "DURABLE_SEARCH_RUNS" in err and value in err, (
        f"a malformed flag was silently ignored: {err!r}")


@pytest.mark.parametrize("value", ["1", "true", "on", "0", "false", "off", ""])
def test_a_well_formed_producer_flag_says_nothing(monkeypatch, capsys, value):
    monkeypatch.setenv("DURABLE_SEARCH_RUNS", value)
    webapp.durable_runs_enabled()
    assert "DURABLE_SEARCH_RUNS" not in capsys.readouterr().err


# =========================================================================== 1b. one snapshot


SECOND_READ_SECRET = "FATAL: role patents does not exist on 10.128.0.53:5433"


def _one_good_then_explode(monkeypatch, row):
    """First call answers, every later call raises with a secret-bearing message.

    That is the real shape of a store going away mid-request, and it is the shape that turns a
    double read into a fallthrough: read one sees a live run, read two raises, and the caller
    quietly reports whatever stale memory holds instead.
    """
    calls = {"n": 0}

    def flaky(slug):
        calls["n"] += 1
        if calls["n"] == 1:
            return row
        raise RuntimeError(SECOND_READ_SECRET)

    monkeypatch.setattr(runstore, "latest_for_slug", flaky)
    return calls


def _live_row(slug, run_id="rid-1"):
    return {"run_id": run_id, "slug": slug, "status": "running", "stage": "screen",
            "attempts": 1, "event_seq": 7, "progress": {"msg": "Screening candidates"},
            "error": None, "enqueued_at": None, "started_at": None,
            "t0": time.time() - 30, "t_start": time.time() - 20}


def test_status_takes_exactly_one_durable_snapshot(app_env, durable_db, monkeypatch):
    """THE GAP. status() asked the store whether the state was unknown, then asked AGAIN through
    _durable_run_for. A store that answered once and then failed produced a legacy fallthrough on
    a slug whose live durable row had just been read successfully."""
    _flag(monkeypatch, True)
    calls = _one_good_then_explode(monkeypatch, _live_row("snap"))
    with webapp._JOB_LOCK:
        webapp._JOBS["snap"] = {"status": "done", "msg": "stale legacy", "t0": time.time()}

    body = webapp.app.test_client().get("/status/snap").get_json()
    assert calls["n"] == 1, f"the route read the store {calls['n']} times"
    assert body["status"] == "running", body
    assert body["msg"] == "Screening candidates", body
    assert body["done"] is False
    assert "stale legacy" not in json.dumps(body), "it fell through to stale legacy state"
    assert SECOND_READ_SECRET not in json.dumps(body)


def test_events_takes_exactly_one_durable_snapshot(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    calls = _one_good_then_explode(monkeypatch, _live_row("snap-ev"))
    with webapp._JOB_LOCK:
        webapp._JOBS["snap-ev"] = {"status": "done", "msg": "stale legacy", "t0": time.time()}

    body, ended, code = _drain_sse(webapp.app.test_client(), "/events/snap-ev",
                                   max_chunks=1, deadline=4.0)
    assert code == 200, code
    assert calls["n"] == 1, f"the route read the store {calls['n']} times before streaming"
    ev = json.loads(body.split("data: ", 1)[1].split("\n\n", 1)[0])
    assert ev["status"] == "running", ev
    assert ev["seq"] == 7, ev
    assert "stale legacy" not in body
    assert SECOND_READ_SECRET not in body
    assert not ended, "a live durable run closed its stream"


def test_status_reads_the_store_once_in_the_ordinary_case(app_env, durable_db, monkeypatch):
    """Not only correctness: the doubled read was on every observer request, and the report page
    polls one of these every two seconds per open tab."""
    _flag(monkeypatch, True)
    runstore.enqueue("counted", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    real = runstore.latest_for_slug
    calls = {"n": 0}

    def counting(slug):
        calls["n"] += 1
        return real(slug)

    monkeypatch.setattr(runstore, "latest_for_slug", counting)
    webapp.app.test_client().get("/status/counted")
    assert calls["n"] == 1, f"{calls['n']} store reads for one status poll"


def test_events_reads_the_store_once_before_it_starts_streaming(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    runstore.enqueue("counted-ev", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    real = runstore.latest_for_slug
    calls = {"n": 0}

    def counting(slug):
        calls["n"] += 1
        return real(slug)

    monkeypatch.setattr(runstore, "latest_for_slug", counting)
    _drain_sse(webapp.app.test_client(), "/events/counted-ev", max_chunks=1, deadline=4.0)
    assert calls["n"] == 1, f"{calls['n']} store reads before the first frame"


def test_a_settled_row_and_a_live_legacy_claim_still_prefers_legacy_on_one_snapshot(
        app_env, durable_db, monkeypatch):
    """The precedence rule from the run-cutover milestone, preserved through the refactor."""
    _flag(monkeypatch, True)
    settled = dict(_live_row("prec"), status="done")
    calls = _one_good_then_explode(monkeypatch, settled)
    with webapp._JOB_LOCK:
        webapp._JOBS["prec"] = {"status": "running", "msg": "legacy still working",
                                "t0": time.time()}
    body = webapp.app.test_client().get("/status/prec").get_json()
    assert calls["n"] == 1
    assert body["status"] == "running", body
    assert body["msg"] == "legacy still working", body


def test_an_initial_unknown_still_gives_the_generic_error_on_one_snapshot(
        app_env, durable_db, monkeypatch):
    """The FIRST read failing is still the generic retryable error, and still only one read."""
    _flag(monkeypatch, True)
    calls = {"n": 0}

    def always_boom(slug):
        calls["n"] += 1
        raise RuntimeError(SECRET)

    monkeypatch.setattr(runstore, "latest_for_slug", always_boom)
    resp = webapp.app.test_client().get("/status/init-unknown")
    assert resp.status_code == 503
    assert calls["n"] == 1, f"{calls['n']} reads for one unknown lookup"
    assert resp.get_json()["retryable"] is True


# =========================================================================== 2. admission


@pytest.fixture
def admission_db(durable_db):
    """Kept as a name for the admission tests; the schema now includes 010 by default, because
    runstore.enqueue cannot work without it."""
    return durable_db


def _row_of(run_id):
    with runstore._cur() as cur:
        cur.execute("SELECT * FROM search_runs WHERE run_id=%s", (run_id,))
        return cur.fetchone()


def _charged_today(lane):
    with runstore._cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE lane=%s "
                    "AND charged_day = (now() AT TIME ZONE 'UTC')::date", (lane,))
        return int(cur.fetchone()["n"])


def test_the_producer_admits_and_charges_a_run_it_lets_through(app_env, admission_db,
                                                               monkeypatch):
    _flag(monkeypatch, True)
    webapp.ensure_report("adm-ok", **PAYLOAD)
    row = _row_of(runstore.latest_for_slug("adm-ok")["run_id"])
    assert row["admitted"] is True
    assert row["admitted_at"] is not None
    assert row["charged_day"] is not None
    assert _charged_today("deep") == 1


def test_a_cap_full_row_is_queued_but_not_admitted(app_env, admission_db, monkeypatch):
    """The user-facing behaviour is unchanged: still queued, still reported running. What changes
    is that a worker may not execute it, because the gate never let it through."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=4, daily_cap=0, quick_max=4,
                                     quick_daily_cap=0, state_path=app_env["tmp"] / "c.json"))
    st, _ = webapp.ensure_report("adm-cap", **PAYLOAD)
    assert st == "running"
    row = _row_of(runstore.latest_for_slug("adm-cap")["run_id"])
    assert row["status"] == "queued"
    assert row["admitted"] is False, "a cap-full row was marked runnable"
    assert row["charged_day"] is None, "a cap-full row charged the budget"


def test_a_concurrency_full_row_is_queued_but_not_admitted(app_env, admission_db, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100, state_path=app_env["tmp"] / "g.json"))
    webapp.ensure_report("adm-conc", **PAYLOAD)
    row = _row_of(runstore.latest_for_slug("adm-conc")["run_id"])
    assert row["admitted"] is False
    assert row["charged_day"] is None


def test_a_worker_never_claims_an_unadmitted_row(app_env, admission_db, monkeypatch):
    """THE GAP. runstore.claim took any queued row, so a worker would have spent on searches the
    gate explicitly turned away."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=4, daily_cap=0, quick_max=4,
                                     quick_daily_cap=0, state_path=app_env["tmp"] / "c2.json"))
    webapp.ensure_report("never-claim", **PAYLOAD)
    assert runstore.claim("w", lanes=["deep"]) is None, "a worker claimed an unadmitted row"


def test_a_worker_claims_an_admitted_row(app_env, admission_db, monkeypatch):
    _flag(monkeypatch, True)
    webapp.ensure_report("do-claim", **PAYLOAD)
    got = runstore.claim("w", lanes=["deep"])
    assert got is not None and got["slug"] == "do-claim"


def test_a_duplicate_submission_charges_the_budget_once(app_env, admission_db, monkeypatch):
    _flag(monkeypatch, True)
    for _ in range(3):
        with webapp._JOB_LOCK:
            webapp._JOBS.clear()
        webapp.ensure_report("dup-charge", **PAYLOAD)
    assert _charged_today("deep") == 1


def test_a_retry_does_not_charge_the_budget_again(app_env, admission_db, monkeypatch):
    """fail(retry=True) puts the run back to queued. It has already been paid for; requeueing is
    not a new search."""
    _flag(monkeypatch, True)
    webapp.ensure_report("retry-charge", **PAYLOAD)
    rid = runstore.latest_for_slug("retry-charge")["run_id"]
    runstore.claim("w", lanes=["deep"])
    assert runstore.fail(rid, "w", "transient", retry=True) == "queued"
    assert _charged_today("deep") == 1
    assert _row_of(rid)["admitted"] is True, "a requeued run lost its admission"
    assert runstore.claim("w2", lanes=["deep"]) is not None, "a paid retry became unrunnable"


def test_admit_waiting_respects_the_daily_cap(app_env, admission_db, monkeypatch):
    """A cap-blocked row becomes eligible only through the explicit sweep, and only when the cap
    genuinely allows it. It must never be admitted by accident."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=4, daily_cap=0, quick_max=4,
                                     quick_daily_cap=0, state_path=app_env["tmp"] / "c3.json"))
    webapp.ensure_report("sweep-capped", **PAYLOAD)
    rid = runstore.latest_for_slug("sweep-capped")["run_id"]

    assert runstore.admit_waiting(lane="deep", daily_cap=0, max_concurrent=4) == []
    assert _row_of(rid)["admitted"] is False, "the sweep admitted past a zero cap"

    assert runstore.admit_waiting(lane="deep", daily_cap=5, max_concurrent=4) == [rid]
    assert _row_of(rid)["admitted"] is True
    assert _charged_today("deep") == 1


def test_admit_waiting_respects_concurrency(app_env, admission_db, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100, state_path=app_env["tmp"] / "g3.json"))
    webapp.ensure_report("sweep-conc-a", **PAYLOAD)
    webapp.ensure_report("sweep-conc-b", **dict(PAYLOAD, query="another gripper"))
    assert runstore.admit_waiting(lane="deep", daily_cap=100, max_concurrent=0) == []
    got = runstore.admit_waiting(lane="deep", daily_cap=100, max_concurrent=1)
    assert len(got) == 1, f"the sweep admitted {len(got)} against a concurrency of 1"


def test_the_daily_ledger_rolls_over_at_utc_midnight(app_env, admission_db, monkeypatch):
    """The ledger is the rows themselves, keyed by the UTC day they charged, so a rollover is a
    date changing rather than a counter somebody has to remember to reset."""
    _flag(monkeypatch, True)
    webapp.ensure_report("rollover", **PAYLOAD)
    rid = runstore.latest_for_slug("rollover")["run_id"]
    assert _charged_today("deep") == 1
    with runstore._cur() as cur:                    # pretend it was charged yesterday
        cur.execute("UPDATE search_runs SET charged_day = charged_day - 1 WHERE run_id=%s",
                    (rid,))
    assert _charged_today("deep") == 0, "yesterday's spend still counts against today"


def test_the_quick_lane_has_its_own_budget(app_env, admission_db, monkeypatch):
    _flag(monkeypatch, True)
    webapp.ensure_report("lane-q", **dict(PAYLOAD, depth="quick"))
    webapp.ensure_report("lane-d", **dict(PAYLOAD, depth="deep", query="deep one"))
    assert _charged_today("quick") == 1
    assert _charged_today("deep") == 1


def test_two_workers_do_not_over_admit(app_env, admission_db, monkeypatch):
    """Concurrent admission must not exceed the cap. Counting queued rows in one transaction and
    admitting in another is exactly the race that lets two sweeps both see room for one."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100, state_path=app_env["tmp"] / "g4.json"))
    for i in range(6):
        webapp.ensure_report(f"race-{i}", **dict(PAYLOAD, query=f"gripper {i}"))
    assert _charged_today("deep") == 0

    out, errors = [], []
    ready = threading.Barrier(4, timeout=25)

    def sweep():
        try:
            ready.wait()
            out.extend(runstore.admit_waiting(lane="deep", daily_cap=2, max_concurrent=10))
        except Exception as exc:                                     # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=sweep) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not errors, errors
    assert len(out) <= 2, f"{len(out)} rows admitted against a daily cap of 2"
    assert _charged_today("deep") == len(out)



def test_the_schema_check_refuses_a_database_missing_the_admission_columns(durable_db):
    """009 without 012 used to pass ensure_schema and then fail at runtime with UndefinedColumn
    on the first enqueue. A missing migration must be refused up front, with the file to apply
    named, not discovered when a user presses search."""
    with runstore._cur() as cur:
        cur.execute("ALTER TABLE search_runs DROP COLUMN admitted")
    runstore._schema_ready.clear()
    with pytest.raises(RuntimeError) as e:
        runstore.require_admission_schema()
    assert "admitted" in str(e.value)
    assert "012_run_admission.sql" in str(e.value)


# =========================================================================== 2b. DB authority


def _apply_012():
    ddl = open(os.path.join(ROOT, "sql", "012_run_admission.sql"), encoding="utf-8").read()
    with runstore._cur() as cur:
        cur.execute(ddl)


def test_the_admission_migration_is_replay_safe(app_env, durable_db, monkeypatch):
    """A rerun must not admit rows the caps genuinely refused.

    `false` cannot identify pre-migration rows, because after the migration `false` is also the
    legitimate state of a refused row. Backfilling on `NOT admitted` therefore turns an ordinary
    redeploy into a cap bypass. The sentinel is NULL, used once, before the column becomes total.
    """
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=4, daily_cap=0, quick_max=4,
                                     quick_daily_cap=0, state_path=app_env["tmp"] / "r.json"))
    webapp.ensure_report("replay-refused", **PAYLOAD)
    rid = runstore.latest_for_slug("replay-refused")["run_id"]
    assert _row_of(rid)["admitted"] is False
    assert _row_of(rid)["charged_day"] is None

    _apply_012()                                   # the redeploy

    row = _row_of(rid)
    assert row["admitted"] is False, "re-running the migration admitted a refused row"
    assert row["charged_day"] is None, "re-running the migration charged a refused row"
    assert runstore.claim("w", lanes=["deep"]) is None


def test_the_producer_does_not_decide_admission_in_process(app_env, durable_db, monkeypatch):
    """Postgres is the authority. Copying a process-local gate's answer into the row leaves two
    gunicorn processes and the sweeper each believing they have the last slot."""
    _flag(monkeypatch, True)
    calls = {"n": 0}
    real = auth.RunGate.try_begin

    def counting(self, depth="deep"):
        calls["n"] += 1
        return real(self, depth=depth)

    monkeypatch.setattr(auth.RunGate, "try_begin", counting)
    webapp.ensure_report("no-local-decision", **PAYLOAD)
    assert calls["n"] == 0, (
        "the durable producer asked the process-local gate to decide admission")


def test_two_producers_do_not_over_admit_at_cap_one(app_env, durable_db, monkeypatch):
    """Simultaneous producers, daily cap of one. Exactly one row may be admitted and charged."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=10, daily_cap=1, quick_max=10,
                                     quick_daily_cap=1, state_path=app_env["tmp"] / "p1.json"))
    ready = threading.Barrier(4, timeout=25)
    errors = []

    def go(i):
        try:
            ready.wait()
            with webapp._JOB_LOCK:
                webapp._JOBS.clear()
            webapp.ensure_report(f"cap1-{i}", **dict(PAYLOAD, query=f"gripper {i}"))
        except Exception as exc:                                     # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not errors, errors
    assert _charged_today("deep") == 1, (
        f"{_charged_today('deep')} runs charged against a daily cap of 1")
    with runstore._cur() as cur:
        cur.execute("SELECT count(*) n FROM search_runs WHERE admitted")
        assert int(cur.fetchone()["n"]) == 1


def test_a_producer_and_a_sweeper_do_not_over_admit(app_env, durable_db, monkeypatch):
    """The race the reviewer named: not two sweepers, but a producer admitting while a sweeper
    admits. Both must serialize on the same lane lock in Postgres."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=10, daily_cap=0, quick_max=10,
                                     quick_daily_cap=0, state_path=app_env["tmp"] / "ps.json"))
    for i in range(4):                              # four waiting rows, none admitted
        webapp.ensure_report(f"wait-{i}", **dict(PAYLOAD, query=f"waiting {i}"))
    assert _charged_today("deep") == 0

    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=10, daily_cap=1, quick_max=10,
                                     quick_daily_cap=1, state_path=app_env["tmp"] / "ps2.json"))
    ready = threading.Barrier(2, timeout=25)
    errors = []

    def producer():
        try:
            ready.wait()
            with webapp._JOB_LOCK:
                webapp._JOBS.clear()
            webapp.ensure_report("new-arrival", **dict(PAYLOAD, query="new arrival"))
        except Exception as exc:                                     # noqa: BLE001
            errors.append(exc)

    def sweeper():
        try:
            ready.wait()
            runstore.admit_waiting(lane="deep", daily_cap=1, max_concurrent=10)
        except Exception as exc:                                     # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=producer), threading.Thread(target=sweeper)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not errors, errors
    assert _charged_today("deep") == 1, (
        f"producer and sweeper together charged {_charged_today('deep')} against a cap of 1")


def test_concurrency_one_admits_one_under_simultaneous_transactions(app_env, durable_db,
                                                                    monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100, state_path=app_env["tmp"] / "c1.json"))
    for i in range(5):
        webapp.ensure_report(f"conc1-{i}", **dict(PAYLOAD, query=f"conc {i}"))

    out, errors = [], []
    ready = threading.Barrier(3, timeout=25)

    def sweep():
        try:
            ready.wait()
            out.extend(runstore.admit_waiting(lane="deep", daily_cap=100, max_concurrent=1))
        except Exception as exc:                                     # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=sweep) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not errors, errors
    assert len(out) == 1, f"{len(out)} admitted against a concurrency of 1"


def test_a_terminal_run_releases_its_concurrency_slot(app_env, durable_db, monkeypatch):
    """Concurrency counts RUNNING rows, so finishing one must free room for the next."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=1, daily_cap=100, quick_max=1,
                                     quick_daily_cap=100, state_path=app_env["tmp"] / "t.json"))
    webapp.ensure_report("term-a", **PAYLOAD)
    rid = runstore.latest_for_slug("term-a")["run_id"]
    runstore.claim("w", lanes=["deep"])             # now one running

    webapp.ensure_report("term-b", **dict(PAYLOAD, query="second"))
    b = runstore.latest_for_slug("term-b")["run_id"]
    assert _row_of(b)["admitted"] is False, "admitted past a concurrency of 1"

    runstore.finish(rid, "w", status="done")        # the slot frees
    assert runstore.admit_waiting(lane="deep", daily_cap=100, max_concurrent=1) == [b]
    assert _row_of(b)["admitted"] is True


def test_ensure_schema_returns_promptly_and_does_not_deadlock_on_itself(app_env, durable_db):
    """REGRESSION. ensure_schema holds a non-reentrant lock, and its column probe used _cur(),
    which calls ensure_schema() again. The first call after any cache clear then never returned:
    the process deadlocked against itself and every test using the store hung.
    """
    done = threading.Event()
    err = []

    def probe():
        try:
            runstore._schema_ready.clear()
            runstore.ensure_schema(force=True)
        except Exception as exc:                                     # noqa: BLE001
            err.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    assert done.wait(timeout=15), "ensure_schema did not return: it is deadlocked on its own lock"
    assert not err, err


def test_ensure_schema_refuses_promptly_when_a_column_is_missing(app_env, durable_db):
    with runstore._cur() as cur:
        cur.execute("ALTER TABLE search_runs DROP COLUMN IF EXISTS admitted_at")
    runstore._schema_ready.clear()
    done, err = threading.Event(), []

    def probe():
        try:
            runstore.require_admission_schema()
        except Exception as exc:                                     # noqa: BLE001
            err.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    assert done.wait(timeout=15), "ensure_schema hung instead of refusing"
    assert err and "admitted_at" in str(err[0])
    assert "012_run_admission.sql" in str(err[0])


# =========================================================================== 2c. 009 seam


def _drop_admission_columns():
    with runstore._cur() as cur:
        cur.execute("ALTER TABLE search_runs DROP COLUMN IF EXISTS admitted, "
                    "DROP COLUMN IF EXISTS admitted_at, DROP COLUMN IF EXISTS charged_day")
    #  Nothing to clear: capability is read from the catalog every time, on purpose.


def test_a_009_only_database_can_still_claim(app_env, durable_db, monkeypatch):
    """The foundation branch has 009 and not 012, and its own suite claims directly. Filtering on
    a column that does not exist there turned a compatibility gap into UndefinedColumn on every
    claim. The predicate follows the schema."""
    _flag(monkeypatch, True)
    rid = runstore.enqueue("legacy-shape", dict(PAYLOAD), mode="novelty", depth="deep",
                           lane="deep")
    _drop_admission_columns()
    assert runstore.admission_capable() is False
    got = runstore.claim("w", lanes=["deep"])
    assert got is not None and got["run_id"] == rid


def test_after_012_claim_never_takes_an_unadmitted_row(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    assert runstore.admission_capable() is True
    runstore.enqueue("unadmitted", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    assert runstore.claim("w", lanes=["deep"]) is None, "claim took an unadmitted row"
    runstore.admit_waiting(lane="deep", daily_cap=10, max_concurrent=10)
    assert runstore.claim("w", lanes=["deep"]) is not None


def test_the_worker_refuses_a_009_database_before_it_executes_anything(app_env, durable_db,
                                                                       monkeypatch):
    """DURABLE_WORKER_ENABLED=1 must never degrade into claiming unadmitted rows. The real worker
    checks the admission schema BEFORE it sweeps or claims."""
    w = _worker()
    _drop_admission_columns()
    monkeypatch.setenv("DURABLE_WORKER_ENABLED", "1")
    executed = []
    monkeypatch.setattr(w, "execute", lambda *a, **k: executed.append(1))
    with pytest.raises(RuntimeError) as e:
        w.run_once("worker-1", lanes=["deep"])
    assert "012_run_admission.sql" in str(e.value)
    assert not executed, "the worker executed against a 009 schema"


def test_the_worker_sweeps_waiting_rows_before_it_claims(app_env, durable_db, monkeypatch):
    """admit_waiting had ZERO production callers, so a row refused for concurrency or today's cap
    stayed unadmitted for ever: nothing re-checked it after a run finished or the day rolled."""
    w = _worker()
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=1, daily_cap=100, quick_max=1,
                                     quick_daily_cap=100, state_path=app_env["tmp"] / "sw.json"))
    webapp.ensure_report("run-a", **PAYLOAD)
    a = runstore.latest_for_slug("run-a")["run_id"]
    webapp.ensure_report("run-b", **dict(PAYLOAD, query="second one"))
    b = runstore.latest_for_slug("run-b")["run_id"]
    assert _row_of(b)["admitted"] is False, "B was admitted past a concurrency of 1"

    monkeypatch.setattr(w, "execute", lambda *a, **k: None)
    assert w.run_once("worker-1", lanes=["deep"]) == a          # A is the only runnable row
    runstore.finish(a, "worker-1", status="done")

    #  No new web request. The next worker iteration must admit B and claim it.
    assert w.run_once("worker-2", lanes=["deep"]) == b, "the worker never swept the waiting row"
    assert _row_of(b)["admitted"] is True


def test_the_worker_sweep_admits_after_a_utc_day_rollover(app_env, durable_db, monkeypatch):
    w = _worker()
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=10, daily_cap=1, quick_max=10,
                                     quick_daily_cap=1, state_path=app_env["tmp"] / "ro.json"))
    webapp.ensure_report("day-a", **PAYLOAD)
    a = runstore.latest_for_slug("day-a")["run_id"]
    webapp.ensure_report("day-b", **dict(PAYLOAD, query="tomorrow"))
    b = runstore.latest_for_slug("day-b")["run_id"]
    assert _row_of(b)["admitted"] is False

    with runstore._cur() as cur:                    # yesterday's spend
        cur.execute("UPDATE search_runs SET charged_day = charged_day - 1 WHERE run_id=%s", (a,))
        cur.execute("UPDATE search_runs SET status='done' WHERE run_id=%s", (a,))
    monkeypatch.setattr(w, "execute", lambda *a_, **k: None)
    assert w.run_once("worker-1", lanes=["deep"]) == b, "the rollover never freed the budget"


def test_an_older_waiting_row_gets_the_freed_slot_before_a_newcomer(app_env, durable_db,
                                                                    monkeypatch):
    """Queue order. Admitting a new arrival directly starves whatever was already waiting."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100, state_path=app_env["tmp"] / "o.json"))
    webapp.ensure_report("older", **PAYLOAD)
    older = runstore.latest_for_slug("older")["run_id"]
    time.sleep(0.02)

    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=1, daily_cap=100, quick_max=1,
                                     quick_daily_cap=100, state_path=app_env["tmp"] / "o2.json"))
    webapp.ensure_report("newcomer", **dict(PAYLOAD, query="just arrived"))
    newcomer = runstore.latest_for_slug("newcomer")["run_id"]

    assert _row_of(older)["admitted"] is True, "the older row did not get the only slot"
    assert _row_of(newcomer)["admitted"] is False, "a newcomer jumped the queue"


def test_missing_limits_refuse_admission_rather_than_spending(app_env, durable_db, monkeypatch):
    """A missing gate must never mean unlimited. Fail-open here is fail-open on money."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(auth, "run_gate", None)
    caps = webapp._lane_caps(None, "deep")
    assert caps["daily_cap"] == 0 and caps["max_concurrent"] == 0, caps
    webapp.ensure_report("no-gate", **PAYLOAD)
    row = _row_of(runstore.latest_for_slug("no-gate")["run_id"])
    assert row["admitted"] is False, "admission was granted with no configured limits"


CATALOG_SECRET = "FATAL: terminating connection, role patents on 10.128.0.53:5433"


def test_a_transient_capability_error_never_becomes_legacy_capability(app_env, durable_db,
                                                                      monkeypatch):
    """THE GAP. Catching every detection failure as False collapsed UNKNOWN into confirmed 009,
    and caching it meant one transient catalog blip permanently downgraded a 012 process to the
    legacy claim: it would then take admitted=false rows and spend past the cap.
    """
    _flag(monkeypatch, True)
    runstore.enqueue("transient", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")

    real_cursor = webapp.db.cursor
    state = {"boom": True}

    import contextlib

    @contextlib.contextmanager
    def flaky(*a, **k):
        if state["boom"]:
            state["boom"] = False
            raise RuntimeError(CATALOG_SECRET)
        with real_cursor(*a, **k) as cur:
            yield cur

    monkeypatch.setattr(webapp.db, "cursor", flaky)
    with pytest.raises(Exception) as e:
        runstore.claim("w", lanes=["deep"])
    assert CATALOG_SECRET in str(e.value) or isinstance(e.value, RuntimeError)

    #  NOT monkeypatch.undo(): that would also revert the fixture's redirection of db.cursor to
    #  the throwaway database, so the probe would answer about the live corpus instead. The flaky
    #  wrapper has already disarmed itself, so calls now pass through.
    assert runstore.admission_capable() is True, "a transient error was latched as legacy"
    assert runstore.claim("w2", lanes=["deep"]) is None, (
        "after recovery the claim stopped filtering unadmitted rows")


def test_a_capability_failure_changes_no_row(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    rid = runstore.enqueue("untouched", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    before = _row_of(rid)

    def boom(*a, **k):
        raise RuntimeError(CATALOG_SECRET)

    monkeypatch.setattr(runstore, "admission_capable", boom)
    with pytest.raises(RuntimeError):
        runstore.claim("w", lanes=["deep"])
    after = _row_of(rid)
    assert after["status"] == before["status"] and after["worker_id"] is None
    assert after["attempts"] == before["attempts"]


def test_ensure_schema_has_no_dead_capability_block():
    src = open(os.path.join(ROOT, "src", "runstore.py"), encoding="utf-8").read()
    assert "if False:" not in src, "a dead branch was left in ensure_schema"
    assert "import functools" not in src, "an unused import was left behind"


def test_worker_and_web_resolve_identical_lane_caps(monkeypatch):
    """A worker that invents its own variable names silently ignores the operator's configuration
    and admits against defaults nobody chose."""
    import auth as auth_mod
    import runner.worker as w
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "7")
    monkeypatch.setenv("DAILY_RUN_CAP", "77")
    monkeypatch.setenv("MAX_CONCURRENT_QUICK", "9")
    monkeypatch.setenv("QUICK_DAILY_CAP", "99")
    assert w.lane_caps("deep") == auth_mod.lane_limits("deep") == {"max_concurrent": 7,
                                                                   "daily_cap": 77}
    assert w.lane_caps("quick") == auth_mod.lane_limits("quick") == {"max_concurrent": 9,
                                                                     "daily_cap": 99}


def test_the_worker_uses_the_canonical_variable_names():
    src = open(os.path.join(ROOT, "src", "runner", "worker.py"), encoding="utf-8").read()
    for invented in ("RUN_QUICK_DAILY_CAP", "RUN_QUICK_MAX", "RUN_DEEP_DAILY_CAP",
                     "RUN_DEEP_MAX"):
        assert invented not in src, f"the worker still reads the invented {invented}"


def test_generic_ensure_schema_passes_on_a_009_database(app_env, durable_db):
    """009 must satisfy ensure_schema. Only the admission paths require 012."""
    _drop_admission_columns()
    runstore._schema_ready.clear()
    runstore.ensure_schema(force=True)          # must not raise
    with pytest.raises(RuntimeError) as e:
        runstore.require_admission_schema()
    assert "012_run_admission.sql" in str(e.value)


def test_one_process_switching_from_012_to_009_gets_both_answers_right(app_env, durable_db,
                                                                      monkeypatch):
    """No manual cache clearing anywhere in this test, deliberately.

    A cached capability is keyed by nothing. One process that retargets its connection, or one
    suite that hands the module a different database, then reuses a 012 answer on a 009 database
    and claims rows it was never admitted to spend on. The catalog read is cheap and is always
    about the database actually in front of it.
    """
    _flag(monkeypatch, True)
    rid = runstore.enqueue("switcher", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    assert runstore.admission_capable() is True
    assert runstore.claim("w", lanes=["deep"]) is None, "claim took an unadmitted row on 012"

    with runstore._cur() as cur:                 # the same process now faces a 009 schema
        cur.execute("ALTER TABLE search_runs DROP COLUMN admitted, "
                    "DROP COLUMN admitted_at, DROP COLUMN charged_day")

    assert runstore.admission_capable() is False, "a stale 012 answer survived the switch"
    got = runstore.claim("w", lanes=["deep"])
    assert got is not None and got["run_id"] == rid, "the legacy claim shape was not used"


def test_the_worker_claims_with_an_explicit_admitted_mode():
    """The worker's safety should be readable at the call site, not inferred from a global."""
    src = open(os.path.join(ROOT, "src", "runner", "worker.py"), encoding="utf-8").read()
    assert "admitted_only=True" in src
    i = src.index("require_admission_schema")
    j = src.index("admitted_only=True")
    assert i < j, "the worker claims before it verifies the admission schema"


def test_there_is_no_unkeyed_capability_cache():
    src = open(os.path.join(ROOT, "src", "runstore.py"), encoding="utf-8").read()
    for latch in ("_capable_confirmed", "_admission_ready", "lru_cache"):
        assert latch not in src, f"{latch} is an unkeyed capability latch across databases"


def test_claim_rejects_an_explicit_legacy_mode_on_a_capable_database(app_env, durable_db,
                                                                     monkeypatch):
    """No caller needs it, and on 012 it claims exactly the rows admission refused."""
    _flag(monkeypatch, True)
    rid = runstore.enqueue("bypass", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    with pytest.raises(ValueError) as e:
        runstore.claim("w", lanes=["deep"], admitted_only=False)
    assert "admitted_only=False" in str(e.value)
    assert _row_of(rid)["status"] == "queued", "the refused call still touched the row"
    assert _row_of(rid)["worker_id"] is None


def test_claim_has_only_two_safe_modes(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    runstore.enqueue("modes", dict(PAYLOAD), mode="novelty", depth="deep", lane="deep")
    assert runstore.claim("w", lanes=["deep"], admitted_only=None) is None    # detected 012
    assert runstore.claim("w", lanes=["deep"], admitted_only=True) is None    # explicit
    runstore.admit_waiting(lane="deep", daily_cap=10, max_concurrent=10)
    assert runstore.claim("w", lanes=["deep"], admitted_only=True) is not None


# =========================================================================== health


def test_health_reports_persisted_counts_under_the_flag(app_env, durable_db, monkeypatch):
    """THE GAP. /healthz read the process-local gate, which durable submissions no longer touch.
    After cutover it would report active=0 and today=0 while Postgres held running and charged
    work: an operator's first question answered with a confident wrong number."""
    _flag(monkeypatch, True)
    webapp.ensure_report("h-run", **PAYLOAD)
    rid = runstore.latest_for_slug("h-run")["run_id"]
    runstore.claim("w", lanes=["deep"], admitted_only=True)

    monkeypatch.setattr(auth, "run_gate",
                        auth.RunGate(max_concurrent=0, daily_cap=100, quick_max=0,
                                     quick_daily_cap=100, state_path=app_env["tmp"] / "h.json"))
    webapp.ensure_report("h-wait", **dict(PAYLOAD, query="waiting one"))

    runs = webapp.app.test_client().get("/healthz").get_json()["runs"]
    assert runs["source"] == "postgres", runs
    assert runs["active"] == 1, runs
    assert runs["today"] == 1, runs
    assert runs["waiting"] == 1, runs
    assert runs["daily_cap"] == 100 and runs["max_concurrent"] == 0, runs

    runstore.finish(rid, "w", status="done")
    runs = webapp.app.test_client().get("/healthz").get_json()["runs"]
    assert runs["active"] == 0, runs
    assert runs["today"] == 1, "a finished run stopped counting against the daily budget"


def test_health_counts_admitted_queued_separately_from_waiting(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)
    webapp.ensure_report("h-admitted", **PAYLOAD)
    runs = webapp.app.test_client().get("/healthz").get_json()["runs"]
    assert runs["admitted_queued"] == 1, runs
    assert runs["waiting"] == 0, runs


def test_health_never_leaks_a_stats_failure(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, True)

    def boom(*a, **k):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(runstore, "admission_stats", boom)
    body = webapp.app.test_client().get("/healthz").get_json()
    runs = body["runs"]
    assert runs.get("source") == "unavailable", runs
    for leak in ("password", "FATAL", "10.128.0.53", "RuntimeError"):
        assert leak not in json.dumps(body), f"health leaked {leak!r}"
    assert body["ok"] is False, (
        "durable execution could not be observed, so health must not claim to be ok")


def test_flag_off_health_is_unchanged(app_env, durable_db, monkeypatch):
    _flag(monkeypatch, False)
    runs = webapp.app.test_client().get("/healthz").get_json()["runs"]
    assert "source" not in runs, runs
    assert set(runs) >= {"active", "today", "daily_cap", "max_concurrent"}
