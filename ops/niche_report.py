"""The completeness statement: how much of the niche exists, how much we hold, how much we can read.

    python ops/niche_report.py                       # newest release
    python ops/niche_report.py --release niche-2026-08-22 --markdown

Reads the manifest and the extraction cache only. The one number it cannot get locally is the size
of the niche in the world, which comes from `patents-public-data` and is cached in
`data/niche_cache/world_universe.json` by `ops/niche_world.py`.

WHAT EACH NUMBER MEANS, because three of them are easy to misread:

* "families in the niche" at the boundary is a WORLD count from BigQuery. It is what a complete
  corpus would contain at this boundary, and it is the denominator for coverage.
* "held" counts families with at least one publication ROW here. A row is not a document: MEASURED
  on this release, most of them are a title and an abstract.
* the jurisdiction breakdown counts a family once per office it was filed in, so the column sums
  to more than the total. The publication breakdown does not. Both are printed because collapsing
  them into one number is how a corpus gets described as more complete than it is.
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

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config", "niche_boundary.json")
CACHE = os.path.join(ROOT, "data", "niche_cache")
MANIFESTS = os.path.join(ROOT, "data", "manifests")


def pub_index(cache):
    """publication_number -> (country, decade). Decade from the publication date, falling back to
    the filing date, then the earliest priority date."""
    out = {}
    countries = {}
    with gzip.open(os.path.join(cache, "publications.csv.gz"), "rt", newline="") as fh:
        for row in csv.reader(fh):
            num, country = row[1], row[3]
            date = row[4] or row[5] or row[6]
            dec = (int(date[:4]) // 10) * 10 if len(date) >= 4 and date[:4].isdigit() else 0
            out[num] = (countries.setdefault(country, country), dec)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--manifests", default=MANIFESTS)
    ap.add_argument("--release", default=None)
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)

    rel = (os.path.join(args.manifests, args.release) if args.release
           else cn.latest_release_dir(args.manifests))
    idx = cn.read_index(rel)
    summary = json.load(open(os.path.join(rel, "summary.json")))
    pubs = pub_index(args.cache)

    world_path = os.path.join(args.cache, "world_universe.json")
    world = json.load(open(world_path)) if os.path.exists(world_path) else []
    wtot = next((r for r in world if r.get("kind") == "total"), None)
    wcountry = {r["bucket"]: r["families"] for r in world if r.get("kind") == "country"}
    wdecade = {str(r["bucket"]): r["families"] for r in world if r.get("kind") == "decade"}

    boundary = cn.Boundary.load(CONFIG)
    tot = collections.Counter()
    by_country = collections.defaultdict(collections.Counter)
    by_decade = collections.defaultdict(collections.Counter)
    by_source = collections.Counter()
    missing = collections.Counter()

    for _part, rec in cn.read_manifest(rel):
        #  Only the CPC part of the niche is comparable with the world count: the world query asks
        #  patents-public-data for the boundary, and the citation closure reaches families that
        #  boundary does not name. Mixing them makes "held" exceed "exists", which it did.
        in_cpc = boundary.tier_of_symbols(rec["cpc"]) is not None
        tot["families"] += 1
        tot["cpc_families"] += int(in_cpc)
        tot["publications"] += len(rec["publications"])
        tot["unclassified"] += int(not rec["cpc"])
        complete = rec["has_complete_text"]
        tot["has_claims"] += int(rec["has_claims"])
        tot["has_description"] += int(rec["has_description"])
        tot["complete"] += int(complete)
        by_source[rec["best_source"] or "none_needed"] += 1
        for f in rec["missing_fields"]:
            missing[f] += 1
        cc = set()
        dec = None
        for p in rec["publications"]:
            c, d = pubs.get(p, ("", 0))
            if c:
                cc.add(c)
            if d and (dec is None or d < dec):
                dec = d
            by_country[c]["publications"] += 1
        for c in cc:
            by_country[c]["families"] += 1
            by_country[c]["complete"] += int(complete)
            by_country[c]["cpc_families"] += int(in_cpc)
        d0 = dec or 0
        by_decade[d0]["families"] += 1
        by_decade[d0]["complete"] += int(complete)
        by_decade[d0]["cpc_families"] += int(in_cpc)
        by_decade[d0]["publications"] += len(rec["publications"])

    out = {
        "release_id": idx["release_id"],
        "state": idx["state"],
        "boundary_sha256": idx["boundary_sha256"],
        "world": {"families": wtot["families"] if wtot else None,
                  "publications": wtot["pubs"] if wtot else None},
        "enumeration": summary,
        "totals": dict(tot),
        "best_source": dict(by_source),
        "missing_fields": dict(missing),
        "by_country": {k: dict(v) for k, v in by_country.items()},
        "by_decade": {str(k): dict(v) for k, v in by_decade.items()},
        "world_by_country": dict(wcountry),
        "world_by_decade": dict(wdecade),
    }
    path = os.path.join(rel, "completeness.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"wrote {path}")

    if args.markdown:
        _markdown(out)
    return 0


def _markdown(out):
    t = out["totals"]
    e = out["enumeration"]
    w = out["world"]
    print("\n### The niche, in one table\n")
    print("| | families | publications | method |")
    print("|---|--:|--:|---|")
    print(f"| in the niche at this boundary, worldwide | {w['families']:,} | "
          f"{w['publications']:,} | BigQuery patents-public-data, distinct over the boundary |")
    print(f"| this corpus holds, from the CPC boundary | {e['families_from_cpc']:,} | "
          f"{e['publications_after_family_closure']:,} | classifications, family closed |")
    print(f"| this corpus holds, whole niche | {t['families']:,} | {t['publications']:,} | "
          f"manifest |")
    print(f"| holds claim text | {t['has_claims']:,} | | claims >= {cn.MIN_CLAIMS_CHARS} chars |")
    print(f"| holds description text | {t['has_description']:,} | | "
          f"paragraphs >= {cn.MIN_DESC_CHARS} chars |")
    print(f"| holds COMPLETE text | {t['complete']:,} | | both, in one publication |")
    print(f"| no classification at all | {t['unclassified']:,} | | cpc == [] |")
    print(f"| reachable only from an external source | {e['citation_reach_external_only']:,} | | "
          f"X/Y cited, no local row |")

    print("\n### By jurisdiction (a family is counted once per office it was filed in)\n")
    print("| office | in niche | from CPC boundary | world at CPC boundary | held % | "
          "complete text | complete % |")
    print("|---|--:|--:|--:|--:|--:|--:|")
    rows = sorted(out["by_country"].items(), key=lambda kv: -kv[1].get("families", 0))[:18]
    for c, v in rows:
        f = v.get("families", 0)
        cf = v.get("cpc_families", 0)
        comp = v.get("complete", 0)
        wf = out["world_by_country"].get(c, 0)
        print(f"| {c or '(none)'} | {f:,} | {cf:,} | {wf:,} | "
              f"{100.0*cf/wf if wf else 0:.1f}% | {comp:,} | {100.0*comp/f if f else 0:.1f}% |")

    print("\n### By decade (family dated by its earliest held publication)\n")
    print("| decade | in niche | from CPC boundary | world at CPC boundary | held % | "
          "complete text | complete % |")
    print("|---|--:|--:|--:|--:|--:|--:|")
    for d in sorted(out["by_decade"], key=lambda x: int(x)):
        v = out["by_decade"][d]
        f = v.get("families", 0)
        cf = v.get("cpc_families", 0)
        comp = v.get("complete", 0)
        wf = out["world_by_decade"].get(d, 0)
        label = "(no date)" if d == "0" else f"{d}s"
        print(f"| {label} | {f:,} | {cf:,} | {wf:,} | {100.0*cf/wf if wf else 0:.1f}% | "
              f"{comp:,} | {100.0*comp/f if f else 0:.1f}% |")

    print("\n### Where the missing text has to come from\n")
    print("| best_source | families |")
    print("|---|--:|")
    for k, v in sorted(out["best_source"].items(), key=lambda kv: -kv[1]):
        print(f"| {k} | {v:,} |")


if __name__ == "__main__":
    raise SystemExit(main())
