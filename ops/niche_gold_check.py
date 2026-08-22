"""Does the frozen boundary contain the art a real attorney and a real examiner actually cited?

    python ops/niche_gold_check.py

THIS RUNS AFTER THE BOUNDARY IS FROZEN, NEVER BEFORE. `ops/niche_boundary.py` does not import a
gold set, for the reason `eval/acquisition_cohort.py` states: a corpus chosen with one eye on the
answer key improves on the benchmark and nowhere else, and the failure is invisible until a holdout
runs. So the overlap with gold is an OUTCOME reported here, not an input, and nothing in this file
may ever be fed back into `config/niche_boundary.json`.

Three frozen sets, none of them written by this project:

  eval/attorney_gold.json   the ten references a patent attorney filed against a Schmalz
                            application. The reach case: four of them are classified in acoustics,
                            exhaust silencers, vacuum cleaners and power tools.
  eval/nguyen_gold.json     the six documents filed under 37 CFR 1.290 against US 2025/0033224 A1.
  eval/benchmark_gold.csv   the X/Y search-report citations of the six standing benchmark subjects.

Reported per reference: held at all, in the niche, and WHICH rule admitted it, so a miss can be
attributed to the CPC boundary, to the closures or to the corpus not holding the document.
"""
from __future__ import annotations

import argparse
import collections
import csv
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import corpus_niche as cn  # noqa: E402

CACHE = os.path.join(ROOT, "data", "niche_cache")
MANIFESTS = os.path.join(ROOT, "data", "manifests")
EVAL = os.path.join(ROOT, "eval")


def gold_publications():
    """{publication_number: [source label]} across the three frozen sets."""
    out = collections.defaultdict(list)
    for name, label in (("attorney_gold.json", "attorney"), ("nguyen_gold.json", "nguyen")):
        path = os.path.join(EVAL, name)
        if not os.path.exists(path):
            continue
        for c in json.load(open(path)).get("citations") or ():
            pub = c.get("corpus_pub") or c.get("pub")
            if pub and not str(pub).startswith("NPL"):
                out[pub].append(label)
    path = os.path.join(EVAL, "benchmark_gold.csv")
    if os.path.exists(path):
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("eligible", "").lower() != "true":
                    continue
                pub = row.get("cited_pub_resolved") or row.get("citation_raw")
                if pub:
                    out[pub].append("benchmark:" + row.get("subject_id", ""))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--manifests", default=MANIFESTS)
    ap.add_argument("--release", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    rel = (os.path.join(args.manifests, args.release) if args.release
           else cn.latest_release_dir(args.manifests))
    gold = gold_publications()
    print(f"gold publications: {len(gold):,} across "
          f"{len({s.split(':')[0] for v in gold.values() for s in v})} sources")

    fam_of = {}
    with gzip.open(os.path.join(args.cache, "publications.csv.gz"), "rt", newline="") as fh:
        for row in csv.reader(fh):
            if row[1] in gold:
                fam_of[row[1]] = row[7] or row[1]
    print(f"held in the corpus at all: {len(fam_of):,} of {len(gold):,}")

    want = set(fam_of.values())
    found = {}
    for _part, rec in cn.read_manifest(rel):
        if rec["family_id"] in want:
            found[rec["family_id"]] = rec
    print(f"gold families in the niche manifest: {len(found):,} of {len(want):,}")

    boundary = cn.Boundary.load(os.path.join(ROOT, "config", "niche_boundary.json"))
    by_rule = collections.Counter()
    rows = []
    for pub, labels in sorted(gold.items()):
        fam = fam_of.get(pub)
        rec = found.get(fam) if fam else None
        if rec is None:
            rule = "NOT IN CORPUS" if fam is None else "held, outside the niche"
        else:
            rule = boundary.tier_of_symbols(rec["cpc"]) or "citation_or_family_closure"
        by_rule[rule] += 1
        rows.append((pub, rule, rec["has_complete_text"] if rec else False, ",".join(labels)))

    print("\nadmitted by:")
    for k, v in by_rule.most_common():
        print(f"  {k:32} {v:6,}  {100.0 * v / len(gold):5.1f}%")

    complete = sum(1 for _p, _r, c, _l in rows if c)
    print(f"\ngold families with complete text: {complete:,} of {len(gold):,} "
          f"({100.0 * complete / max(len(gold), 1):.1f}%)")

    if args.verbose:
        print(f"\n{'publication':22} {'rule':32} {'text':5} source")
        for pub, rule, comp, labels in rows:
            print(f"{pub:22} {rule:32} {'full' if comp else '':5} {labels}")
    else:
        print("\nthe two expert sets, in full:")
        for pub, rule, comp, labels in rows:
            if labels.startswith(("attorney", "nguyen")):
                print(f"  {pub:22} {rule:32} {'full text' if comp else 'thin':10} {labels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
