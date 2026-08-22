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

ADMIN = dict(host="127.0.0.1", port=5432, user="deep", dbname="deep_research")
TESTDB = "patents_workercut_test_%d" % os.getpid()

PAYLOAD = dict(query="a vacuum gripper with a sealing lip", subject="US1234567B2",
               mode="novelty", wide=True, doc_token="tok-abc", search_focus="claims",
               depth="deep")


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
    from psycopg.rows import dict_row
    import contextlib
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
