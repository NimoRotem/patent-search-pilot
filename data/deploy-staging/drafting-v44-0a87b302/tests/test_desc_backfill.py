"""Tests for the description backfill, one per defect that actually happened.

Every test here is red against the code as it stood before 2026-08-22 08:15. They are written
against the failure, not against the fix, so they stay meaningful if the fix is rewritten.

The pure-logic tests need no database and no network. The lease tests need Postgres, take only
advisory locks and write nothing, and skip cleanly if the corpus box is unreachable.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops"))
import desc_backfill as bf                                          # noqa: E402


# --------------------------------------------------------------------------- retry policy

def test_a_hung_socket_is_retryable_not_fatal():
    """The 08:00:29 wedge: a timeout must be classified retryable, or the worker either dies or,
    worse, blocks for ever."""
    assert bf.retryable(Exception("504 Deadline Exceeded"))
    assert bf.retryable(Exception("Read timed out"))
    assert bf.retryable(TimeoutError("timeout"))


def test_capacity_failures_are_retryable():
    for msg in ("429 Too Many Requests", "RESOURCE_EXHAUSTED", "quota exceeded",
                "503 Service Unavailable", "500 Internal", "connection reset by peer"):
        assert bf.retryable(Exception(msg)), msg


def test_a_bad_request_is_not_retryable():
    """Retrying a 400 burns quota and never succeeds."""
    assert not bf.retryable(Exception("400 INVALID_ARGUMENT: input too long"))
    assert not bf.retryable(ValueError("dimension mismatch"))


def test_attempts_are_bounded_at_three():
    assert bf.MAX_ATTEMPTS <= 3


def test_backoff_is_jittered_so_workers_do_not_retry_in_lockstep():
    """Without jitter, six workers that hit the same 429 all retry in the same millisecond and
    recreate the burst. Two calls at the same attempt must not return the same delay."""
    seen = {bf.backoff_delay(2) for _ in range(50)}
    assert len(seen) > 1, "backoff is deterministic, so every worker retries in lockstep"


def test_backoff_grows_and_is_capped():
    lo = bf.backoff_delay(0, rand=lambda: 0.0)
    hi = bf.backoff_delay(5, rand=lambda: 0.0)
    assert hi > lo
    assert bf.backoff_delay(50, rand=lambda: 1.0) <= 20.0


def test_the_client_carries_a_deadline():
    """A client with no timeout is what turned a slow call into a permanent stall."""
    assert bf.CALL_TIMEOUT_MS > 0


# --------------------------------------------------------------------------- config

def test_the_embedding_model_is_config_driven(monkeypatch):
    """Pinning the model in code means a model change is a code change on four hosts."""
    monkeypatch.setenv("EMBED_MODEL", "some-other-model")
    monkeypatch.setenv("EMBED_DIM", "1024")
    import importlib
    reloaded = importlib.reload(bf)
    try:
        assert reloaded.EMBED_MODEL == "some-other-model"
        assert reloaded.EMBED_DIM == 1024
    finally:
        monkeypatch.undo()
        importlib.reload(bf)


def test_database_credentials_are_not_embedded_in_the_worker():
    """The committed worker must load deployment credentials, never carry a reusable password."""
    source = open(bf.__file__, encoding="utf-8").read()
    assert "patents_pilot_local" not in source


def test_the_worker_loads_its_declared_environment_file(monkeypatch, tmp_path):
    """The deployed standalone copy cannot rely on the web app importing src.config first."""
    env_file = tmp_path / "backfill.env"
    env_file.write_text(
        "PGHOST=192.0.2.44\nPGPORT=6543\nPGDATABASE=unit_db\n"
        "PGUSER=unit_user\nPGPASSWORD=unit-test-only\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BACKFILL_ENV_FILE", str(env_file))
    for key in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        monkeypatch.delenv(key, raising=False)

    import importlib
    reloaded = importlib.reload(bf)
    try:
        assert reloaded.PG == {
            "host": "192.0.2.44",
            "port": 6543,
            "dbname": "unit_db",
            "user": "unit_user",
            "password": "unit-test-only",
        }
    finally:
        monkeypatch.undo()
        importlib.reload(bf)


# --------------------------------------------------------------------------- lease keys

def test_lease_key_is_stable_across_processes():
    """hash() is randomised per process, so it would give two workers different keys for the same
    work and the lease would never collide. That is the whole bug this guards."""
    assert bf.lease_key("core", 0) == bf.lease_key("core", 0)
    assert bf.lease_key("core", 0) != bf.lease_key("rest", 0)
    assert bf.lease_key("core", 0) != bf.lease_key("core", 1)
    assert 0 <= bf.lease_key("core", 0) <= 0x7FFFFFFF


def test_lease_key_matches_a_known_value():
    """Pin it, so a refactor that changes the hash cannot silently let two workers both start."""
    import zlib
    assert bf.lease_key("core", 0) == (zlib.crc32(b"core:0") & 0x7FFFFFFF)


# --------------------------------------------------------------------------- database

def _conn():
    try:
        return bf._connect()
    except Exception as exc:                                        # noqa: BLE001
        pytest.skip(f"corpus DB unreachable: {exc}")


def test_a_second_worker_is_refused_the_lease():
    """The 08:12 duplicate: two workers on one (pass, shard) means duplicate paid calls and two
    writers on one watermark row."""
    a, b = _conn(), _conn()
    try:
        assert bf._take_lease(a, "test_pass_unused", 99) is True
        assert bf._take_lease(b, "test_pass_unused", 99) is False, "a second worker got the lease"
    finally:
        a.close()
        b.close()


def test_the_lease_is_released_when_the_worker_dies():
    """A killed worker must not leave the pass permanently locked. Session advisory locks drop
    with the connection, which is why they are used here rather than a table row."""
    a = _conn()
    assert bf._take_lease(a, "test_pass_unused2", 98) is True
    a.close()                                                        # simulate the worker dying
    b = _conn()
    try:
        assert bf._take_lease(b, "test_pass_unused2", 98) is True, "lease stuck after a worker died"
    finally:
        b.close()


def test_already_staged_rows_are_dropped_before_the_paid_call():
    """A rerun used to embed the row, send it to Vertex, then throw the result away at insert
    time. Correct, but at full price."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ref_id FROM chunks_stage_v3 "
                        "WHERE kind = 'paragraph' AND ref_id IS NOT NULL LIMIT 5")
            staged = [r["ref_id"] for r in cur.fetchall()]
        if not staged:
            pytest.skip("stage table has no rows yet")
        rows = [{"id": r} for r in staged] + [{"id": -1}]
        kept = bf._filter_staged(conn, rows)
        assert [r["id"] for r in kept] == [-1], "already staged rows were sent to the API again"
    finally:
        conn.close()


def test_filter_staged_is_a_no_op_on_an_empty_batch():
    conn = _conn()
    try:
        assert bf._filter_staged(conn, []) == []
    finally:
        conn.close()


# --------------------------------------------------------------------------- packaged artefacts

OPS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops")
CONF = os.path.join(OPS, "patents-desc-backfill.conf")


def test_the_supervisor_conf_has_no_ssh_banner():
    """It was captured with `gcloud compute ssh ... | tail`, which prepended
    'Existing host keys found in ...' into the tracked file. Supervisor would refuse to parse it,
    and the tracked copy no longer matched the deployed one."""
    text = open(CONF, encoding="utf-8").read()
    assert "Existing host keys found" not in text
    assert ".ssh/google_compute_known_hosts" not in text


def test_the_supervisor_conf_starts_with_its_program_section():
    lines = [ln for ln in open(CONF, encoding="utf-8").read().splitlines() if ln.strip()]
    assert lines, "conf is empty"
    assert lines[0].strip() == "[program:patents-desc-backfill]", (
        f"first nonblank line is {lines[0]!r}, so something was prepended")


def test_the_supervisor_conf_will_not_restart_loop_on_a_lease_refusal():
    """A worker that cleanly declines the lease exits 0. With autorestart=true that becomes a
    restart loop hammering the database."""
    text = open(CONF, encoding="utf-8").read()
    assert "autorestart=unexpected" in text
    assert "exitcodes=0" in text


def test_the_supervisor_conf_points_at_the_private_environment_file():
    text = open(CONF, encoding="utf-8").read()
    assert 'BACKFILL_ENV_FILE="/home/nimrod_rotem/patent-search-pilot/.env"' in text
    assert "PGPASSWORD" not in text
