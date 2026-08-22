#!/usr/bin/env python3
"""pg_prewarm on wake, in two phases, so the first query after a start is not the one that pays
for the whole index. Runs ON the shard as `postgres`, stdlib plus psql, once per boot.

THE PROBLEM. A shard's working set is an HNSW index. Cold, its first dense query walks that index
from disk one random page at a time, which is the 8 to 24 second query the live single box already
demonstrates. Warm, it walks it in memory. The difference is entirely whether somebody paid the
read cost, and the only question is who.

THE TWO PHASES, and why the split is not a detail.

    blocking     a bounded budget (SHARD_PREWARM_BLOCKING_MB, default 6144) of the highest value
                 relations, into shared_buffers. The agent reports `waking` until this returns,
                 so `ensure()` will not hand out a connection to a shard that is about to pay
                 full cold cost.
    background   everything else, in `read` mode into the page cache, after the shard is already
                 declared hot.

If the blocking phase were unbounded the wake would take as long as the whole index takes to
read, which at the prototype disk's 515 MB/s provisioned throughput is about 25 s for a 12.8 GB
index, and SHARD_WAKE_TIMEOUT is 20 s. A wake that always times out wakes nothing. The budget is
the knob that keeps the blocking phase inside the timeout, and `--report` prints what a given
budget costs on this disk so the number is measured and not assumed.

`autoprewarm` is enabled too (bootstrap.sh sets pg_prewarm.autoprewarm=on): a shard stopped
cleanly dumps its buffer list and a background worker restores it at the next start. This script
is what covers the first boot after a load, when no dump exists yet.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

PGPORT = os.environ.get("SHARD_PGPORT", "5432")
PGDATABASE = os.environ.get("SHARD_PGDATABASE", "patents")
STATE_PATH = os.environ.get("SHARD_PREWARM_STATE", "/run/patents-shard/prewarm.json")
BLOCKING_MB = int(os.environ.get("SHARD_PREWARM_BLOCKING_MB", "6144"))
BLOCK_SIZE = 8192

#  Highest value first. The dense channel's cost is the vector index; everything else is lookup.
PRIORITY = ("ix_chunks_hnsw", "chunks", "ix_chunks_tsv", "publications", "classifications")


def psql(sql, timeout=1800.0):
    out = subprocess.run(["psql", "-p", str(PGPORT), "-d", PGDATABASE, "-tAX", "-c", sql],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return out.stdout.strip()


def write_state(**kw):
    kw.setdefault("at", time.time())
    tmp = STATE_PATH + ".tmp"
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(tmp, "w") as fh:
        json.dump(kw, fh)
    os.replace(tmp, STATE_PATH)          # atomic: the agent never reads a half written file


def relations():
    """[(name, bytes)] worth prewarming, priority relations first, then the rest biggest first."""
    rows = psql(
        "SELECT c.relname, pg_relation_size(c.oid) "
        "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = 'public' AND c.relkind IN ('r','i','m') "
        "   AND pg_relation_size(c.oid) > 0 "
        " ORDER BY pg_relation_size(c.oid) DESC")
    found = []
    for line in rows.splitlines():
        if "|" not in line:
            continue
        name, size = line.rsplit("|", 1)
        found.append((name, int(size)))
    rank = {n: i for i, n in enumerate(PRIORITY)}
    return sorted(found, key=lambda r: (rank.get(r[0], len(PRIORITY)), -r[1]))


def prewarm(name, mode, first_block=None, last_block=None):
    span = ""
    if first_block is not None:
        span = f", 'main', {int(first_block)}, {int(last_block)}"
    return int(psql(f"SELECT pg_prewarm('public.{name}', '{mode}'{span})") or 0)


def run(budget_mb=BLOCKING_MB):
    t0 = time.time()
    write_state(state="running", phase="blocking", blocking_mb=budget_mb, bytes=0)
    try:
        rels = relations()
    except Exception as e:
        #  A prewarm that cannot run must not hold the shard in `waking` for ever: an unwarmed
        #  shard is slow, an unreachable one is useless.
        write_state(state="skipped", note=str(e)[:200], seconds=round(time.time() - t0, 2))
        return 1

    budget_blocks = (budget_mb * 1024 * 1024) // BLOCK_SIZE
    warmed_blocks, done_index = 0, 0
    for i, (name, size) in enumerate(rels):
        if warmed_blocks >= budget_blocks:
            break
        blocks = max(0, size // BLOCK_SIZE - 1)
        if blocks <= 0:
            done_index = i + 1
            continue
        take = min(blocks, budget_blocks - warmed_blocks)
        try:
            if take >= blocks:
                warmed_blocks += prewarm(name, "buffer")
                done_index = i + 1
            else:
                warmed_blocks += prewarm(name, "buffer", 0, take - 1)
                done_index = i          # partially done, the background phase finishes it
        except Exception:
            done_index = i + 1          # a relation that will not warm is not a failed wake
        write_state(state="running", phase="blocking", blocking_mb=budget_mb,
                    bytes=warmed_blocks * BLOCK_SIZE, relation=name)

    blocking_ms = int((time.time() - t0) * 1000)
    rest = rels[done_index:]
    write_state(state="background" if rest else "done", phase="background",
                blocking_ms=blocking_ms, blocking_mb=budget_mb,
                bytes=warmed_blocks * BLOCK_SIZE, remaining=len(rest))
    print(f"[prewarm] blocking phase: {warmed_blocks * BLOCK_SIZE / 1e9:.2f} GB in "
          f"{blocking_ms} ms; {len(rest)} relations left to the background phase", flush=True)

    #  From here the shard is HOT. Everything below is opportunistic and must never block a query.
    for name, _size in rest:
        try:
            prewarm(name, "read")
        except Exception:
            pass
    write_state(state="done", phase="background", blocking_ms=blocking_ms,
                blocking_mb=budget_mb, bytes=warmed_blocks * BLOCK_SIZE,
                total_seconds=round(time.time() - t0, 2))
    print(f"[prewarm] done in {time.time() - t0:.1f}s", flush=True)
    return 0


def report():
    """What this shard would have to read, and what that costs at the disk's throughput."""
    rels = relations()
    total = sum(s for _n, s in rels)
    print(f"{'relation':40s} {'GB':>8s}")
    for name, size in rels[:20]:
        print(f"{name:40s} {size / 1e9:8.2f}")
    print(f"{'TOTAL':40s} {total / 1e9:8.2f}")
    for mbs in (140, 515, 1200):
        print(f"  at {mbs:5d} MB/s: {total / (mbs * 1e6):6.1f} s to read the whole shard")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocking-mb", type=int, default=BLOCKING_MB)
    ap.add_argument("--report", action="store_true", help="size the shard, warm nothing")
    a = ap.parse_args()
    sys.exit(report() if a.report else run(a.blocking_mb))
