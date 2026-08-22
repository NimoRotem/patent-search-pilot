#!/usr/bin/env python3
"""Stop every shard nobody is using. Cron this on the controller, once a minute.

The whole cost argument for cold shards is this script running. A c4-highmem-16 left up is
$1.04/hour, $761/month; the same VM stopped is its disk and nothing else. The lease
(`shard_leases`, sql/009_durable_runs.sql) is what says a shard is wanted, the search's own
heartbeat thread refreshes it, and this turns the absence of one into a stopped VM fifteen minutes
later, unless a query is actually running on it.

    ./idle_reaper.py --dry-run          say what it would do
    ./idle_reaper.py                    do it
    ./idle_reaper.py --idle-minutes 5   a shorter window, for a test

Install (the controller is whichever box runs the search worker):
    * * * * * cd /path/to/repo && PYTHONPATH=src .venv/bin/python ops/shards/idle_reaper.py \\
              >> /var/log/patents-shard-reaper.log 2>&1
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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--idle-minutes", type=float, default=shard_backend.IDLE_MINUTES)
    ap.add_argument("--hard-idle-minutes", type=float, default=shard_backend.HARD_IDLE_MINUTES)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    backend = shard_backend.ShardBackend()
    actions = backend.reap(idle_minutes=a.idle_minutes,
                           hard_idle_minutes=a.hard_idle_minutes, dry_run=a.dry_run)
    if a.json:
        print(json.dumps(actions))
    else:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if not actions:
            print(f"{stamp} no shard is running")
        for act in actions:
            print(f"{stamp} {act['shard']:16s} {act['action']:10s} {act['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
