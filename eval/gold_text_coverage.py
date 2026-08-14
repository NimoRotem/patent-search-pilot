"""How much TEXT do we actually hold for the references we are graded on?

WHY THIS IS THE FIRST QUESTION, NOT A DETAIL
--------------------------------------------
The funnel says 44% of gold references die at NOT_RETRIEVED and reads as a search problem. Oracle
injection was meant to settle whether fixing retrieval would pay, by handing each stage the gold it
never received. It could not: measured on two subjects, `before_read` put 8 of 12 gold into the
read set on one and 4 of 11 on the other, the SAME four as the control. Not because the injection
failed, it is stamped and verified, but because 7 of that subject's 11 cited references are not in
the database at all. A family id can be spliced into a ranking. A document that does not exist
cannot be screened, read, charted or delivered.

So the ladder was only ever measuring the in-corpus subset, and every ranking experiment run so far
was re-ordering a pool in which most of the right answers are a title and an abstract.

This tool reports the denominator that makes those experiments interpretable. Buckets are chosen to
match what the pipeline can do with a document, not to look tidy:

    readable, 3k+     enough description to ground a disclosure against
    stub, under 3k    title and abstract. It embeds, it ranks, it grounds almost nothing
    in DB, no text    a row exists and chunking never ran
    absent            we never fetched it

    python eval/gold_text_coverage.py            # dev split
    python eval/gold_text_coverage.py --split all --min-chars 3000

Writes data/logs/gold_text_held.csv, one row per gold reference, for joining against the funnel.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import db  # noqa: E402
from funnel import gold_by_subject  # noqa: E402

#  Below this a "document" is a title and an abstract. Measured: the gold references the pipeline
#  read and grounded evidence against ran 1,862 to 46,571 characters, and every one it held only a
#  stub of was never read at all.
MIN_READABLE = 3000


def chars_held(pub: str) -> int:
    """Characters of chunk text held for a publication. -1 when the row does not exist."""
    with db.cursor() as cur:
        cur.execute("""SELECT (SELECT coalesce(sum(length(ch.text)),0) FROM chunks ch
                                 WHERE ch.publication_id=p.id) chars
                       FROM publications p
                       WHERE upper(regexp_replace(p.publication_number,'[^A-Za-z0-9]','','g'))
                             = upper(regexp_replace(%s,'[^A-Za-z0-9]','','g')) LIMIT 1""", (pub,))
        r = cur.fetchone()
    return int(r["chars"]) if r else -1


def bucket(ch: int, floor: int) -> str:
    if ch < 0:
        return "absent from the DB"
    if ch == 0:
        return "in DB, NO text"
    return "stub, under %dk chars" % (floor // 1000) if ch < floor else "readable, %dk+" % (floor // 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=("all", "dev", "holdout"))
    ap.add_argument("--min-chars", type=int, default=MIN_READABLE)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "logs", "gold_text_held.csv"))
    args = ap.parse_args()
    floor = args.min_chars

    subs = {s["id"]: s for s in
            json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]}
    gold = gold_by_subject()
    counts, by_stratum, rows = collections.Counter(), collections.defaultdict(collections.Counter), []

    for sid, cits in sorted(gold.items()):
        sub = subs.get(sid) or {}
        if args.split != "all" and sub.get("split", "dev") != args.split:
            continue
        strat = (sub.get("strata") or {}).get("corpus") or "pinned"
        for c in cits:
            pub = (c["cited_pub_resolved"] or c["citation_raw"] or "").strip()
            ch = chars_held(pub)
            b = bucket(ch, floor)
            counts[b] += 1
            by_stratum[strat][b] += 1
            rows.append({"subject_id": sid, "corpus_stratum": strat, "cited_pub": pub,
                         "in_corpus": c["in_corpus"], "chars_held": ch, "bucket": b})

    order = ["readable, %dk+" % (floor // 1000), "stub, under %dk chars" % (floor // 1000),
             "in DB, NO text", "absent from the DB"]
    tot = sum(counts.values()) or 1
    print(f"{tot} eligible gold references ({args.split} split), by text actually held\n")
    for b in order:
        print("  %-24s %4d  %5.1f%%" % (b, counts[b], 100 * counts[b] / tot))

    print("\nby corpus stratum:")
    print("  %-12s %13s %13s %13s %13s" % ("", "readable", "stub", "no text", "absent"))
    for st in ("mostly_in", "mixed", "mostly_out", "pinned"):
        c = by_stratum.get(st)
        if not c:
            continue
        n = sum(c.values()) or 1
        print("  %-12s" % st + "".join(" %6d %5.0f%%" % (c[b], 100 * c[b] / n) for b in order))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["subject_id"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwritten {args.out}")
    print("A reference we hold no text for cannot be delivered by ANY amount of ranking work.")


if __name__ == "__main__":
    main()
