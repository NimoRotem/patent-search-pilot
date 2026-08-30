"""Re-derive the niche boundary from evidence, and diff it against the checked-in config.

    python ops/niche_boundary.py                    # the evidence table and the rule's answer
    python ops/niche_boundary.py --sweep            # what other thresholds would have admitted
    python ops/niche_boundary.py --emit proposed.json

THE RULE, in one sentence: a CPC main group is in the niche when the field's own evidence points at
it at least `min_density` densely and at least `min_support` times.

THE TWO EVIDENCE SIGNALS, and why neither is the gold set:

  E1  co-classification. Of the 80,308 publications carrying a `config.SEED_CPC` symbol, how many
      also carry this group. That is what this field's own documents are about.
  E2  examiner reach. Of the documents cited from those 80,308 by a SEARCH REPORT (category SEA,
      EXA or ISR, never the applicant's IDS), how many carry this group. That is what an examiner
      in this field reaches for.

`eval/*gold*.json` is never read here, deliberately. A boundary chosen with one eye on the answer
key produces a corpus that scores well on the benchmark and nowhere else, and the failure is
invisible until a holdout runs. The overlap with gold is measured AFTER this file is frozen, by
`ops/niche_gold_check.py`, and reported as an outcome.

DENSITY AND NOT COUNT. Ranking by raw evidence count admits H10P72 and B65H3 and also G06F3 and
B01D46, because a big group is cited often for the same reason it is big. Dividing by the group's
world size asks the question that matters for acquisition: of the documents this branch contains,
what share does this field actually touch. MEASURED 2026-08-22, the seed's own parent main groups
score 0.076 to 1.222 on that measure and the best group outside the six core subclasses scores
0.069, which is why the core is a subclass list and everything else has to earn its place.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import corpus_niche as cn  # noqa: E402
from config import SEED_CPC  # noqa: E402

CACHE = os.path.join(ROOT, "data", "niche_cache")
CONFIG = os.path.join(ROOT, "config", "niche_boundary.json")
EXAMINER = ("SEA", "EXA", "ISR")


def _symbol_sets(cache):
    cur, syms = None, []
    with gzip.open(os.path.join(cache, "classifications.sorted.tsv.gz"), "rt") as fh:
        for line in fh:
            pid, scheme, sym = line.rstrip("\n").split("\t")
            if scheme != "CPC":
                continue
            if pid != cur:
                if cur is not None:
                    yield cur, syms
                cur, syms = pid, []
            s = cn.normalise_symbol(sym)
            if s:
                syms.append(s)
    if cur is not None:
        yield cur, syms


def evidence(cache):
    """{group: {"seed": n, "cited": n}} plus the two population sizes."""
    seed_pids = set()
    seed_groups = collections.Counter()
    pid_groups = {}
    for pid, syms in _symbol_sets(cache):
        groups = {cn.main_group_of(s) for s in syms}
        groups.discard("")
        pid_groups[pid] = groups
        if any(s.startswith(tuple(SEED_CPC)) for s in syms):
            seed_pids.add(pid)
            for g in groups:
                seed_groups[g] += 1
    print(f"E1: {len(seed_pids):,} SEED_CPC publications", file=sys.stderr)

    num_of = {}
    import csv
    with gzip.open(os.path.join(cache, "publications.csv.gz"), "rt", newline="") as fh:
        for row in csv.reader(fh):
            num_of[row[0]] = row[1]
    seed_nums = {num_of[p] for p in seed_pids if p in num_of}
    pid_of = {v: k for k, v in num_of.items()}

    cited_nums = set()
    with gzip.open(os.path.join(cache, "citations.csv.gz"), "rt", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 4 or not row[2]:
                continue
            if any(k in row[2].upper() for k in EXAMINER) and row[0] in seed_nums:
                cited_nums.add(row[1])
    cited_pids = {pid_of[n] for n in cited_nums if n in pid_of}
    cited_groups = collections.Counter()
    n_classified = 0
    for pid in cited_pids:
        gs = pid_groups.get(pid)
        if not gs:
            continue
        n_classified += 1
        for g in gs:
            cited_groups[g] += 1
    print(f"E2: {len(cited_nums):,} examiner-cited documents, {len(cited_pids):,} held locally, "
          f"{n_classified:,} of those classified "
          f"({100.0 * (len(cited_pids) - n_classified) / max(len(cited_pids), 1):.1f}% carry no CPC)",
          file=sys.stderr)

    out = {}
    for g in set(seed_groups) | set(cited_groups):
        out[g] = {"seed": seed_groups.get(g, 0), "cited": cited_groups.get(g, 0)}
    return out, {"seed_publications": len(seed_pids), "cited_documents": len(cited_nums),
                 "cited_held": len(cited_pids), "cited_classified": n_classified}


def rank(ev, world, core, min_support, min_density):
    rows = []
    for g, e in ev.items():
        w = world.get(g, 0)
        if not w or cn.is_indexing_code(g):
            continue
        support = e["seed"] + e["cited"]
        rows.append({"group": g, "seed": e["seed"], "cited": e["cited"], "support": support,
                     "world": w, "density": support / w, "in_core": g[:4] in core,
                     "admitted": support >= min_support and support / w >= min_density})
    rows.sort(key=lambda r: -r["density"])
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--emit", default=None)
    args = ap.parse_args(argv)

    boundary = cn.Boundary.load(args.config)
    world_path = os.path.join(args.cache, "world_cpc_l5.json")
    if not os.path.exists(world_path):
        raise SystemExit(f"missing {world_path}: run ops/niche_world.py --run first")
    world = {r["l5"]: r["pubs"] for r in json.load(open(world_path))}
    wfam = {r["l5"]: r["families"] for r in json.load(open(world_path))}

    ev, pops = evidence(args.cache)
    rows = rank(ev, world, boundary.core_subclasses, boundary.min_support, boundary.min_density)

    print(f"populations: {json.dumps(pops)}")
    anchors = sorted({cn.main_group_of(s) for s in SEED_CPC})
    print("\nthe seed's own parent main groups, which are by definition this field:")
    for g in anchors:
        r = next((x for x in rows if x["group"] == g), None)
        if r:
            print(f"  {g:10} density={r['density']:.4f} support={r['support']:,}")

    print(f"\nranked by evidence density (support >= {boundary.min_support}, "
          f"bar {boundary.min_density}):")
    print(f"{'group':10} {'dens':>7} {'support':>8} {'seed':>7} {'cited':>7} {'world':>9} {'core':>5}")
    for r in rows[:args.top]:
        if r["support"] < boundary.min_support:
            continue
        mark = "*" if r["admitted"] else " "
        print(f"{mark}{r['group']:9} {r['density']:7.4f} {r['support']:8,} {r['seed']:7,} "
              f"{r['cited']:7,} {r['world']:9,} {'core' if r['in_core'] else '':>5}")

    admitted = sorted(r["group"] for r in rows if r["admitted"] and not r["in_core"])
    have = sorted(boundary.adjacent_groups)
    print(f"\nrule admits {len(admitted)} adjacent groups; config carries {len(have)}")
    if admitted != have:
        print("  only in the rule:  ", sorted(set(admitted) - set(have)))
        print("  only in the config:", sorted(set(have) - set(admitted)))
    else:
        print("  identical")

    if args.sweep:
        print("\nthreshold sweep (adjacent groups only):")
        print(f"{'min_density':>12} {'groups':>7} {'world pubs':>12} {'world families':>15}")
        for d in (0.10, 0.07, 0.05, 0.04, 0.03, 0.02, 0.015, 0.01):
            sel = [r for r in rows
                   if not r["in_core"] and r["support"] >= boundary.min_support and r["density"] >= d]
            print(f"{d:12.3f} {len(sel):7,} {sum(r['world'] for r in sel):12,} "
                  f"{sum(wfam.get(r['group'], 0) for r in sel):15,}")

    if args.emit:
        spec = json.load(open(args.config))
        spec["adjacent_groups"] = admitted
        spec["derived"]["adjacent_evidence"] = {
            r["group"]: {"seed": r["seed"], "cited": r["cited"], "world": r["world"],
                         "density": round(r["density"], 4)}
            for r in rows if r["admitted"] and not r["in_core"]}
        with open(args.emit, "w") as fh:
            json.dump(spec, fh, indent=1)
        print(f"\nwrote {args.emit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
