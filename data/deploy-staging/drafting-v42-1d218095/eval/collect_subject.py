"""Build a benchmark subject from a publication's REAL search-report citation list.

WHY THIS EXISTS
---------------
Two subjects and 37 citations cannot tell a real gain from noise: run-to-run variance on this
pipeline is +/-2 families, so a "+1" is unattributable, and changes have already been adopted and
rejected on differences that size. More subjects is the cheapest thing that makes tuning
trustworthy.

WHAT COUNTS AS GOLD, and what does not
--------------------------------------
`citations.category` records WHO supplied each citation and `citations.origin` carries the
search-report relevance code. They are not interchangeable:

    APP   the applicant's information disclosure statement. One US patent in this corpus carries
          5,771 of these against 11 from the search report. An IDS is a dump of everything the
          applicant knew about; scoring a search engine against it measures the wrong thing and
          is unreachable by construction.
    SEA / EXA / ISR   the search report, the examiner, the international search report.
    origin X   particularly relevant on its own      <- these two threaten novelty and
    origin Y   relevant in combination                  inventive step. This is the gold.
    origin A   background/context.

So the default gold is the X/Y-coded citations. `--all-examiner` widens it to the whole search
report, which typically means 50-100 documents per subject and dilutes a top-50 metric with
background art the searcher was never expected to rank first.

THE LEAK THIS REFUSES TO CREATE
-------------------------------
`retrieval.channel_citation_family` expands the backward citations of whatever it retrieves. If
the subject is in the corpus and is retrieved by its own text, its own citation list -- the answer
key -- is expanded into the candidate pool. Measured on EP 3 707 092 before this was fixed: the
subject's own family came back at rank 1 of its own results and all six of its in-corpus backward
citations were in the ranked list.

That is now closed in the pipeline (webapp._generate recovers a Subject from the ingested
document, and retrieval._date_clause excludes its family from every channel), so an in-corpus
subject is safe. This still reports which side of that line a subject falls on.

    python eval/collect_subject.py WO-2012144120-A1 --id suction_chuck
    python eval/collect_subject.py EP-2386771-A3 --id suction_display --all-examiner
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import db  # noqa: E402
import pubnorm  # noqa: E402

XY = "(ci.origin LIKE '%%X%%' OR ci.origin LIKE '%%Y%%')"
EXAMINER = ("(ci.category LIKE '%%SEA%%' OR ci.category LIKE '%%EXA%%' "
            "OR ci.category LIKE '%%ISR%%')")


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def subject_row(cur, pub):
    cur.execute(
        """SELECT publication_number pn, title, country, publication_date pd,
                  COALESCE(NULLIF(simple_family_id,''), publication_number) fam
           FROM publications
           WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g')) = ANY(%s)
           LIMIT 1""",
        ([norm(v) for v in (pubnorm.variants(pub) or [pub])],))
    return cur.fetchone()


def gold(cur, pub, pred):
    """[(dst_pub, origin, category)] for the citations matching `pred`, self-family removed."""
    cur.execute(f"""
        SELECT DISTINCT ci.dst_pub, ci.origin, ci.category,
               COALESCE(NULLIF(q.simple_family_id,''), q.publication_number) fam
        FROM citations ci
        LEFT JOIN publications q ON q.publication_number = ci.dst_pub
        WHERE ci.src_pub = %s AND {pred}
        ORDER BY ci.dst_pub""", (pub,))
    return [dict(r) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pub")
    ap.add_argument("--id", required=True)
    ap.add_argument("--all-examiner", action="store_true",
                    help="widen the gold from X/Y to the whole search report")
    ap.add_argument("--min-citations", type=int, default=8)
    args = ap.parse_args()

    with db.cursor() as cur:
        s = subject_row(cur, args.pub)
        if not s:
            raise SystemExit(f"{args.pub} is not in this corpus; "
                             f"collect its citation list by hand instead")
        rows = gold(cur, s["pn"], EXAMINER if args.all_examiner else XY)
        rows = [r for r in rows if r["fam"] != s["fam"]]
        held = [r for r in rows if r["fam"]]
        fams = {r["fam"] or r["dst_pub"] for r in rows}

    print(f"{s['pn']}  {str(s['title'])[:64]}")
    print(f"  gold: {len(rows)} citations / {len(fams)} families "
          f"({'whole search report' if args.all_examiner else 'X/Y coded only'})")
    print(f"  {len(held)} are in this corpus (RANKING test), "
          f"{len(rows) - len(held)} are not (REACH test)")
    codes = {}
    for r in rows:
        codes[r["origin"]] = codes.get(r["origin"], 0) + 1
    print(f"  relevance codes: {codes}")
    if len(fams) < args.min_citations:
        raise SystemExit(f"  only {len(fams)} families, below --min-citations "
                         f"{args.min_citations}")

    block = {
        "id": args.id,
        "name": f"{s['pn']} ({str(s['title'])[:56]})",
        "url": pubnorm.google_url(s["pn"]),
        "mode": "novelty",
        "note": (f"{len(rows)} "
                 f"{'search-report' if args.all_examiner else 'X/Y-coded search-report'} "
                 f"citations / {len(fams)} families, pulled from the corpus citations table. "
                 f"{len(held)} in corpus, {len(rows) - len(held)} not. Subject is in the corpus, "
                 f"so its family is excluded from retrieval by the recovered Subject."),
        "citations": [r["dst_pub"] for r in rows],
    }
    print("\n" + json.dumps(block, indent=2))


if __name__ == "__main__":
    main()
