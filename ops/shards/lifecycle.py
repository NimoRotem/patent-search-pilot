#!/usr/bin/env python3
"""Prove the whole cold shard loop against a REAL shard, end to end, and print every number.

    ./lifecycle.py domain_03              wake it, query it, lease it, reset it, stop it
    ./lifecycle.py domain_03 --keep       leave it running (it will still be reaped)

This is not a unit test and it does not replace one. tests/test_shard_backend.py proves the state
machine against fakes; this proves that the state machine is wired to an actual VM, that the SQL
retrieval already runs works unchanged against a shard, that the connection comes back clean, and
that the lease and the reaper agree about whether the shard may be stopped.

WHAT IT ASSERTS, in order, each one printed with its measurement:

    1. the shard is cold to begin with, so the wake being measured is a real one
    2. `shard_manager.ensure` returns `hot`, and how long it took
    3. `shard_manager.connection` hands back a usable connection
    4. that connection is READ ONLY: a CREATE TABLE on it is refused
    5. the dense channel's own SQL runs on it, unmodified
    6. the ANN scan profile can be set on it, exactly as `retrieval.cold.bind` does
    7. `release` RESETS the session, so the next caller does not inherit that profile
    8. a lease exists in `shard_leases` for the bound run, and only one
    9. the reaper KEEPS a shard whose lease is held
   10. the reaper STOPS it once the lease is released and nothing is running
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

import runstore                                                            # noqa: E402
from retrieval import shard_backend, shard_manager                         # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(label)
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("shard", nargs="?", default="domain_03")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--keep", action="store_true", help="do not stop the shard at the end")
    a = ap.parse_args(argv)

    backend = shard_backend.install()
    shard = backend.shard_for(a.shard)
    if shard is None:
        raise SystemExit(f"no shard for {a.shard!r}")
    print(f"shard {shard.shard} on {shard.vm} in {shard.zone}")

    #  A run row, because `shard_leases.run_id` references `search_runs` and a lease with no run
    #  is not the lease the search path takes. This is the same store, not a second one.
    run_id = f"lifecycle-{uuid.uuid4().hex[:12]}"
    slug = f"lifecycle-{uuid.uuid4().hex[:8]}"
    run_id = runstore.enqueue(slug, {"query": "shard lifecycle proof"}, lane="quick",
                              run_id=run_id)
    shard_backend.bind_run(run_id)

    try:
        # 1 ------------------------------------------------------------------ cold to begin with
        state = backend.state(shard.shard)
        if state != "cold":
            print(f"  ... {shard.shard} is {state}; stopping it so the wake is a real one",
                  flush=True)
            backend.stop(shard)
            t0 = time.time()
            while time.time() - t0 < a.timeout and backend.state(shard.shard) != "cold":
                time.sleep(3)
            state = backend.state(shard.shard)
        check("1. the shard starts cold", state == "cold", f"state={state}")

        # 2 ------------------------------------------------------------------------------- wake
        t0 = time.time()
        states = shard_manager.ensure([shard.shard], timeout=a.timeout)
        wake_seconds = time.time() - t0
        check("2. ensure() reached hot", states.get(shard.shard) == "hot",
              f"{states} in {wake_seconds:.1f}s "
              f"(SHARD_WAKE_TIMEOUT={shard_manager.WAKE_TIMEOUT})")
        if states.get(shard.shard) != "hot":
            return 1

        # 3 ------------------------------------------------------------------------- connection
        conn = shard_manager.connection(shard.shard)
        check("3. connection() handed back a connection", conn is not None)
        if conn is None:
            return 1

        # 4 ------------------------------------------------------------------------- read only
        refused = ""
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE lifecycle_should_not_exist (x int)")
        except Exception as e:                                             # noqa: BLE001
            refused = str(e).splitlines()[0]
        check("4. the connection refuses a write", bool(refused), refused[:80])

        # 5 --------------------------------------------------------- the real retrieval SQL
        qvec = "[" + ",".join(["0.01"] * 768) + "]"
        with conn.cursor() as cur:
            t0 = time.time()
            cur.execute("SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score "
                        "  FROM chunks c WHERE c.embedding IS NOT NULL "
                        " ORDER BY c.embedding <=> %s::vector LIMIT %s", (qvec, qvec, 10))
            rows = cur.fetchall()
            dense_ms = (time.time() - t0) * 1000
            cur.execute("SELECT count(*) AS n FROM chunks")
            n_chunks = cur.fetchone()["n"]
            cur.execute("SELECT current_setting('server_version') AS v, "
                        "       (SELECT extversion FROM pg_extension WHERE extname='vector') AS pgv")
            ver = cur.fetchone()
        check("5. the dense channel SQL runs on the shard", True,
              f"{len(rows)} rows in {dense_ms:.0f} ms over {n_chunks} chunks; "
              f"PostgreSQL {ver['v']}, pgvector {ver['pgv']}")

        # 6/7 ------------------------------------------------------- the scan profile and reset
        from retrieval import base as _base
        _base._apply_scan_profile(conn, False)
        with conn.cursor() as cur:
            cur.execute("SHOW hnsw.ef_search")
            after_set = cur.fetchone()["hnsw.ef_search"]
        check("6. the ANN scan profile applies, as cold.bind does", True,
              f"hnsw.ef_search={after_set}")

        shard_manager.release(shard.shard, conn)
        again = shard_manager.connection(shard.shard)
        check("7a. release pooled the connection", again is conn)
        with again.cursor() as cur:
            cur.execute("SHOW hnsw.ef_search")
            after_release = cur.fetchone()["hnsw.ef_search"]
            cur.execute("SHOW default_transaction_read_only")
            still_ro = cur.fetchone()["default_transaction_read_only"]
        check("7b. release RESET the session", after_release != after_set,
              f"hnsw.ef_search {after_set} -> {after_release}")
        check("7c. the reset did not undo read only", still_ro == "on",
              f"default_transaction_read_only={still_ro}")
        shard_manager.release(shard.shard, again)

        # 8 ------------------------------------------------------------------------- the lease
        held = [row for row in runstore.held_shards(run_id) if row["shard"] == shard.shard]
        check("8. the wake took exactly one lease in shard_leases", len(held) == 1,
              f"{len(held)} held for {run_id}")

        # 9 --------------------------------------------------------- the reaper keeps a held one
        actions = {x["shard"]: x for x in backend.reap(idle_minutes=0.0, dry_run=True)}
        act = actions.get(shard.shard, {})
        check("9. the reaper keeps a shard whose lease is held",
              act.get("action") == "keep", f"{act.get('action')}: {act.get('reason')}")

        # 10 ------------------------------------------------------ and stops it once released
        runstore.release_shards(run_id)
        actions = {x["shard"]: x for x in backend.reap(idle_minutes=0.0, dry_run=True)}
        act = actions.get(shard.shard, {})
        check("10. the reaper stops it once nothing holds it",
              act.get("action") == "would-stop", f"{act.get('action')}: {act.get('reason')}")
    finally:
        shard_backend.bind_run(None)
        try:
            runstore.release_shards(run_id)
        except Exception:
            pass
        if not a.keep:
            print(f"  stopping {shard.vm}", flush=True)
            backend.stop(shard)
        shard_backend.uninstall()

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} check(s): {FAILED}")
        return 1
    print("every check passed; the cold shard lifecycle works end to end on a real VM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
