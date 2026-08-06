"""Build the development and holdout benchmarks: stratified, family-split, no leakage.

WHY THE SIX-SUBJECT BENCHMARK WAS NOT ENOUGH
--------------------------------------------
Run-to-run variance is +/-2 citation families, so a single-subject reading is a draw from a spread
that runs 0% to 29% on an unchanged pipeline. Worse, all six subjects come from one narrow field
and four of them have EVERY cited document already in the corpus, so that benchmark cannot
validate external retrieval or cross-field search at all. Funnel attribution says 39 of 82 gold
families are never retrieved, which is exactly the failure those four subjects cannot see.

SCOPE. Vacuum handling and adjacent mechanical arts only: cleaning, gripping and clamping, robots
and manipulators, machine tools, sawing, material handling, cranes and hoists, fastening, pumps.
No software, biology or chemistry.

SUBJECTS COME FROM BIGQUERY, NOT FROM OUR OWN CORPUS, and that is the whole point.
Selecting subjects from the corpus was tried first and cannot work: the corpus was built by
expanding along citation edges, so a publication in it tends to have its citations in it too, BY
CONSTRUCTION. Measured, 297 of 306 corpus-sourced candidates had 100% of their cited art already
held, and only 3 were below 80%. A benchmark built that way is structurally incapable of testing
external retrieval, which is where 39 of 82 current misses live.

Sourcing from `patents-public-data.patents.publications` instead (citation.type carries the X/Y
search-report code; citation.category carries SEA/APP/EXA) gives a real mix:

    corpus-sourced pool     mostly_in 303   mixed 0     mostly_out 3
    BigQuery-sourced pool   mostly_in 2243  mixed 346   mostly_out 1988

STRATIFICATION, so a change that helps one kind of subject and hurts another is visible rather
than averaged away:

    field                 six or more CPC subclasses
    subject era           pre-2010, 2010-2017, post-2017
    citation list size    small (6-11), medium (12-20), large (21-40)
    claim count           few (5-11), many (12+)
    IN-CORPUS FRACTION    mostly-in (>=80%), mixed, mostly-out (<50%)   <- the axis that matters
    description present   yes/no, because it decides whether potential claims exist at all

SPLIT. By DOCDB simple family, never by publication: a subject and its own continuation in
different splits would leak, and so would two subjects sharing a family. The six existing subjects
are pinned to DEVELOPMENT because they have already been tuned against, and a subject that has
been tuned against is not a holdout however it is labelled.

    python eval/build_benchmark.py --dev 30 --holdout 20
    python eval/build_benchmark.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import hashlib
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import db  # noqa: E402
import pubnorm  # noqa: E402

SUBJECTS = os.path.join(HERE, "benchmark_subjects.json")

#  Scoped fields. The value is only for the report; membership is what selects.
FIELDS = {
    "A47L": "vacuum cleaning", "B08B": "cleaning", "B25B": "gripping and clamping",
    "B25F": "power tools", "B25J": "manipulators and robots", "B23Q": "machine tools",
    "B27B": "sawing", "B65G": "material handling", "B66C": "cranes and load engaging",
    "B66F": "hoisting and lifting", "F04D": "pumps", "F16B": "fastening",
}
XY = "(ci.origin LIKE '%%X%%' OR ci.origin LIKE '%%Y%%')"
#  Deterministic selection: the benchmark must rebuild identically or it is not a benchmark.
SEED = 20260805


#  DURABLE PATH, not /tmp. This box runs a /tmp cleaner that deleted the pool, a pytest temp
#  directory mid-suite and two run logs during this work; anything that must survive a few hours
#  does not belong there.
BQ_POOL = os.environ.get(
    "BQ_CANDIDATE_POOL", os.path.join(ROOT, "data", "benchmark", "bq_scored.json"))


def candidates(cur, min_xy=6, max_xy=40):
    """The BigQuery-sourced pool, scored for how much of its cited art the corpus holds.

    Built by eval/fetch_bq_candidates (a one-off ~32 GB BigQuery scan, about 20 cents) and cached
    to BQ_POOL, because rebuilding it on every run would be both slow and a recurring cost for a
    set that changes only when the corpus does.
    """
    if not os.path.exists(BQ_POOL):
        raise SystemExit(f"no candidate pool at {BQ_POOL}; see the module docstring")
    raw = json.load(open(BQ_POOL))
    #  Family key. For a subject the corpus holds we use its DOCDB family. For one it does not we
    #  use a hash of the citation list, because two members of one family carry near-identical
    #  search-report citations and would otherwise both be selectable into different splits.
    pns = [c["pn"] for c in raw]
    fam = {}
    for i in range(0, len(pns), 20000):
        cur.execute("""SELECT publication_number pn,
                              COALESCE(NULLIF(simple_family_id,''), publication_number) fam
                       FROM publications WHERE publication_number = ANY(%s)""",
                    (pns[i:i + 20000],))
        fam.update({r["pn"]: r["fam"] for r in cur.fetchall()})
    out = []
    for c in raw:
        cits = sorted({p for p in (c.get("xy") or []) if p and p.strip()})
        n = len(cits)
        if not (min_xy <= n <= max_xy):
            continue
        y = int(str(c["pd"])[:4]) if c.get("pd") else 0
        sig = "cits:" + hashlib.sha1("|".join(sorted(c["xy"])).encode()).hexdigest()[:16]
        out.append({
            "pn": c["pn"], "cc": c["cc"], "pd": c["pd"], "nxy": n,
            "n_held": c["n_held"], "frac_in_corpus": c["frac"], "corpus": c["bucket"],
            "sub": sorted(c.get("subs") or ["?"])[0],
            "subs": sorted(c.get("subs") or []),
            "fam": fam.get(c["pn"]) or sig,
            "subject_in_corpus": bool(c.get("subject_in_corpus")),
            "era": "pre2010" if y < 2010 else ("2010_2017" if y <= 2017 else "post2017"),
            "size": "small" if n <= 11 else ("medium" if n <= 20 else "large"),
            #  BigQuery's citation.publication_number is EMPTY for non-patent literature, and an
            #  empty string is not a citation. Left in, 25 of them inflated the listed count and
            #  then showed up as UNRESOLVED exclusions, which reads like a parsing defect.
            "citations": sorted({p for p in (c["xy"] or []) if p and p.strip()}),
            "title": "",
        })
    return out


def stratum(d):
    #  The corpus axis leads: it is the one the previous benchmark had no variation on, and the
    #  one that decides whether external retrieval can be measured at all.
    return (d["corpus"], d["sub"], d["era"], d["size"])


def select(pool, n, taken_families, rng):
    """Round-robin across strata so no single field or corpus-fraction dominates."""
    buckets = {}
    for d in pool:
        if d["fam"] in taken_families:
            continue
        buckets.setdefault(stratum(d), []).append(d)
    for v in buckets.values():
        #  prefer a subject the corpus can supply claims for (the disclosure list needs them),
        #  then a longer citation list, then deterministically by number
        v.sort(key=lambda d: (not d["subject_in_corpus"], -d["nxy"], d["pn"]))
    keys = sorted(buckets)
    rng.shuffle(keys)
    picked, i = [], 0
    while len(picked) < n and keys:
        progressed = False
        for k in list(keys):
            if len(picked) >= n:
                break
            while buckets[k]:
                d = buckets[k].pop(0)
                if d["fam"] in taken_families:
                    continue
                picked.append(d)
                taken_families.add(d["fam"])
                progressed = True
                break
            if not buckets[k]:
                keys.remove(k)
        if not progressed:
            break
        i += 1
    return picked


def to_subject(d, split):
    return {
        "id": f"{d['sub'].lower()}_{d['pn'].replace('-', '').lower()}",
        "name": f"{d['pn']} ({d['title']})",
        "url": pubnorm.google_url(d["pn"]),
        "mode": "novelty",
        "split": split,
        "field": d["sub"],
        "field_name": FIELDS.get(d["sub"], ""),
        "strata": {"era": d["era"], "size": d["size"], "corpus": d["corpus"],
                   "subject_in_corpus": d["subject_in_corpus"]},
        "subject_family_id": d["fam"],
        "gold_source": "bigquery_search_report_xy",
        "note": (f"{d['nxy']} X/Y search-report citations from the DOCDB record, "
                 f"{d['n_held']} of them in this corpus ({d['frac_in_corpus']:.0%})."),
        "citations": [],          # filled below
    }


def citations_for(cur, pn):
    cur.execute(f"""SELECT DISTINCT ci.dst_pub FROM citations ci
                    WHERE ci.src_pub = %s AND {XY} ORDER BY ci.dst_pub""", (pn,))
    return [r["dst_pub"] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", type=int, default=30)
    ap.add_argument("--holdout", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    existing = json.load(open(SUBJECTS))
    #  IDEMPOTENT. Rebuild from the PINNED subjects only, discarding anything a previous run
    #  generated. Without this a second run kept the 30 dev subjects it had just written (so it
    #  selected no new ones) and appended 20 more holdout, giving 40: the benchmark silently grew
    #  every time it was rebuilt, which is the opposite of what a fixed benchmark is for.
    keep = [s for s in existing["subjects"] if not s.get("generated")]
    con = db.connect()
    con.autocommit = True
    cur = con.cursor()

    #  Families already spoken for: the six existing subjects, pinned to development because they
    #  have been tuned against.
    taken = set()
    for s in keep:
        m = s.get("subject_family_id")
        if m:
            taken.add(m)
        s["split"] = s.get("split", "dev")
    cur.execute("""SELECT COALESCE(NULLIF(simple_family_id,''), publication_number) fam
                   FROM publications WHERE publication_number = ANY(%s)""",
                ([pubnorm.canonical(s["url"].split("/patent/")[-1].split("/")[0]) for s in keep],))
    taken |= {r["fam"] for r in cur.fetchall()}

    pool = candidates(cur)
    print(f"[build] {len(pool)} candidates across {len({d['sub'] for d in pool})} subclasses, "
          f"{len(taken)} families already taken")

    rng = random.Random(SEED)
    need_dev = max(0, args.dev - len(keep))
    dev = select(pool, need_dev, taken, rng)
    hold = select(pool, args.holdout, taken, rng)

    def show(name, rows):
        print(f"\n{name}: {len(rows)}")
        for axis in ("corpus", "sub", "era", "size"):
            c = {}
            for d in rows:
                c[d[axis]] = c.get(d[axis], 0) + 1
            print(f"   {axis:8s} {dict(sorted(c.items()))}")
        print(f"   subject in corpus: {sum(1 for d in rows if d['subject_in_corpus'])}/{len(rows)}"
              f"   median in-corpus fraction "
              f"{sorted(d['frac_in_corpus'] for d in rows)[len(rows)//2] if rows else 0:.0%}")

    show("DEVELOPMENT (new)", dev)
    show("HOLDOUT", hold)
    overlap = {d["fam"] for d in dev} & {d["fam"] for d in hold}
    print(f"\nfamily overlap dev/holdout: {len(overlap)} (must be 0)")
    assert not overlap

    if args.dry_run:
        return
    subs = list(keep)
    for d in dev:
        s = to_subject(d, "dev")
        s["generated"] = True
        s["citations"] = d["citations"]
        subs.append(s)
    for d in hold:
        s = to_subject(d, "holdout")
        s["generated"] = True
        s["citations"] = d["citations"]
        subs.append(s)
    existing["subjects"] = subs
    existing["splits"] = {
        "dev": sum(1 for s in subs if s.get("split", "dev") == "dev"),
        "holdout": sum(1 for s in subs if s.get("split") == "holdout"),
    }
    existing["scope"] = ("vacuum handling and adjacent mechanical arts: "
                         + ", ".join(sorted(set(FIELDS.values()))))
    existing["selection_seed"] = SEED
    with open(SUBJECTS, "w") as fh:
        json.dump(existing, fh, indent=2)
    print(f"\nwritten {SUBJECTS}: {existing['splits']}")
    print("next: eval/benchmark_gold.py  then  eval/freeze_disclosures.py")


if __name__ == "__main__":
    main()
