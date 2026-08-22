#!/usr/bin/env python3
"""Regenerate the `domains` column of ops/shards/shards.tsv from workstream F's partition.

WHY A GENERATOR AND NOT A HAND WRITTEN TABLE. The eight way split of every CPC subclass is
workstream F's, computed once in `ops/build_release.py` and written to `data/logs/plan.json`.
There must not be a second partition: a subclass that F loaded onto domain_05 and that this table
routes to domain_02 is a shard that is woken, queried and answers nothing, which downstream is
indistinguishable from a genuine miss. So the table is DERIVED, and this is the derivation.

    ./plan_to_table.py --plan ~/v3/F-release/data/logs/plan.json          # rewrite shards.tsv
    ./plan_to_table.py --plan .../plan.json --check                       # exit 1 if it drifted

The shard identity columns (shard, vm, zone) are NOT derived. They are the fleet, they are in the
file already, and a rebalance by F must not silently rename a VM.

MEASURED 2026-08-22 on the plan as it stands: 602 subclasses, disjoint, total, `unclassified` in
domain_08. The per-domain chunk masses are 142,148 for seven shards and 142,147 for the eighth,
which cannot arise from packing atomic subclasses, so the SIZING in that file is synthetic. The
ASSIGNMENT is what is used here and the sizing is not used at all.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "shards.tsv")
#  shard_router.UNCLASSIFIED. Named here rather than imported so this script runs with no venv.
UNCLASSIFIED = "unclassified"


def read_table(path=TABLE):
    """-> (header lines, [[shard, vm, zone, domains], ...]) preserving comments."""
    head, rows = [], []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#"):
                head.append(line)
                continue
            parts = line.split("\t")
            rows.append([p.strip() for p in parts] + [""] * (4 - len(parts)))
    return head, rows


def domains_from_plan(plan_path):
    """-> {shard_key: [subclass, ...]} from F's plan.json, validated."""
    with open(plan_path) as fh:
        plan = json.load(fh)
    shards = plan.get("plan", {}).get("shards") or {}
    if not shards:
        raise SystemExit(f"{plan_path} has no plan.shards")
    seen, out = {}, {}
    for key in sorted(shards):
        doms = [str(d).strip() for d in shards[key] if str(d).strip()]
        for d in doms:
            if d in seen:
                raise SystemExit(f"{d} is in both {seen[d]} and {key}; the partition is not disjoint")
            seen[d] = key
        out[key] = sorted(doms)
    if UNCLASSIFIED not in seen:
        raise SystemExit(f"the plan assigns no shard to {UNCLASSIFIED!r}, which route() always emits")
    return out, seen[UNCLASSIFIED]


def render(head, rows, domains, catch_all):
    lines = list(head)
    for shard, vm, zone, _old in rows:
        doms = list(domains.get(shard, []))
        if shard == catch_all:
            #  The catch-all owns every symbol the plan has never seen, and it is the shard that
            #  holds `unclassified` because a symbol we cannot place and a document with no symbol
            #  are the same problem and belong on the same box.
            doms = ["*"] + [d for d in doms if d != "*"]
        lines.append("\t".join([shard, vm, zone, ",".join(doms)]))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, help="workstream F's data/logs/plan.json")
    ap.add_argument("--table", default=TABLE)
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    a = ap.parse_args()

    head, rows = read_table(a.table)
    domains, catch_all = domains_from_plan(a.plan)
    keys = {r[0] for r in rows}
    missing = set(domains) - keys
    if missing:
        raise SystemExit(f"the plan names shards the table does not: {sorted(missing)}")

    text = render(head, rows, domains, catch_all)
    with open(a.table) as fh:
        current = fh.read()
    if a.check:
        if current != text:
            print("shards.tsv has DRIFTED from the plan", file=sys.stderr)
            return 1
        print(f"shards.tsv matches the plan: {sum(len(v) for v in domains.values())} subclasses "
              f"over {len(domains)} shards, catch-all {catch_all}")
        return 0
    with open(a.table + ".tmp", "w") as fh:
        fh.write(text)
    os.replace(a.table + ".tmp", a.table)
    print(f"wrote {a.table}: {sum(len(v) for v in domains.values())} subclasses over "
          f"{len(domains)} shards, catch-all {catch_all}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
