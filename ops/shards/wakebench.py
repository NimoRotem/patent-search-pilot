#!/usr/bin/env python3
"""Measure cold -> hot for one shard, milestone by milestone. The number the 20 s
SHARD_WAKE_TIMEOUT has to live with, and the reason the architecture is what it is.

    ./wakebench.py domain_03 3            three stop/start cycles, printing every milestone
    ./wakebench.py domain_03 1 --json     one cycle, machine readable

WHAT IS TIMED, and why each milestone is separate. `ensure()` returns `hot` only when the shard
itself says hot, so the wake budget is spent in four places and only a split reading says which
one to attack:

    start_api_returned   the Compute API accepted the start call
    instance_RUNNING     GCE says the VM is up. It is NOT serving; the kernel is still booting
    agent_answering      :8639 answers, which means the network stack and systemd are up
    postgres_accepting   the agent reports postgres.accepting, the first moment a query could run
    HOT                  postgres accepts AND shard_status is `ready` AND blocking prewarm returned

A run is stopped from whatever state it is in first, then waited to TERMINATED, so every cycle
starts from the same place: a genuinely cold VM, not a suspended one and not a warm page cache.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from retrieval import shard_backend                                        # noqa: E402


def wait_status(backend, shard, want, limit=600.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        info = backend.gce.instance(shard.vm, shard.zone, max_age=0.0)
        if ((info or {}).get("status") or "").upper() == want:
            return time.time() - t0
        time.sleep(3)
    raise SystemExit(f"{shard.vm} never reached {want} in {limit:.0f}s")


def one_cycle(backend, shard, limit=600.0):
    backend.stop(shard)
    wait_status(backend, shard, "TERMINATED", limit)

    marks = {}
    t0 = time.time()
    backend.gce.start(shard.vm, shard.zone)
    marks["start_api_returned"] = round(time.time() - t0, 1)
    while time.time() - t0 < limit:
        info = backend.gce.instance(shard.vm, shard.zone, max_age=0.0) or {}
        status = (info.get("status") or "").upper()
        if status == "RUNNING":
            marks.setdefault("instance_RUNNING", round(time.time() - t0, 1))
            ip = info.get("ip")
            health = backend.probe(ip, backend.agent_port, timeout=1.5) if ip else None
            if health:
                marks.setdefault("agent_answering", round(time.time() - t0, 1))
                marks.setdefault("agent_first_state", health.get("state"))
                if (health.get("postgres") or {}).get("accepting"):
                    marks.setdefault("postgres_accepting", round(time.time() - t0, 1))
                if health.get("state") == "hot" and health.get("available"):
                    marks["HOT"] = round(time.time() - t0, 1)
                    marks["prewarm_blocking_ms"] = (health.get("prewarm") or {}).get("blocking_ms")
                    return marks
        time.sleep(0.5)
    marks["HOT"] = None
    return marks


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("shard", nargs="?", default="domain_03")
    ap.add_argument("runs", nargs="?", type=int, default=1)
    ap.add_argument("--limit", type=float, default=600.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    backend = shard_backend.ShardBackend()
    shard = backend.shard_for(a.shard)
    if shard is None:
        raise SystemExit(f"no shard for {a.shard!r}")

    results = []
    for run in range(1, a.runs + 1):
        if not a.json:
            print(f"--- run {run}/{a.runs}: stopping {shard.vm} and waiting for TERMINATED",
                  flush=True)
        marks = one_cycle(backend, shard, a.limit)
        results.append(marks)
        if not a.json:
            for k in ("start_api_returned", "instance_RUNNING", "agent_answering",
                      "agent_first_state", "postgres_accepting", "HOT", "prewarm_blocking_ms"):
                if k in marks:
                    print(f"    {k:22s} {marks[k]}", flush=True)

    hot = [m["HOT"] for m in results if m.get("HOT") is not None]
    if a.json:
        print(json.dumps({"shard": shard.shard, "vm": shard.vm, "runs": results}, indent=2))
    else:
        print()
        print(f"cold -> hot seconds, {len(hot)}/{len(results)} runs reached hot: {hot}")
        if hot:
            print(f"min {min(hot):.1f}  max {max(hot):.1f}  "
                  f"mean {sum(hot) / len(hot):.1f}   against SHARD_WAKE_TIMEOUT="
                  f"{shard_backend.WAKE_TIMEOUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
