"""Where does examiner-cited art actually live, and how much of it does this corpus index?

WHY
---
14 of the 37 citations in the first benchmark were not in the corpus at all, so no ranking change
could reach them. The obvious response is "index more", and the obvious way to get that wrong is
to guess which classifications to add. This measures it instead.

METHOD
------
Take every publication in the indexed field that has search-report citations (category SEA / EXA /
ISR -- NOT the applicant's IDS, see collect_subject.py), and look at where the CITED documents are
classified. That is a direct sample of what an examiner reaches for when examining art in this
field, which is exactly the population the corpus needs to cover.

Reports, per CPC subclass:
    cited        how many distinct cited publications carry it
    held         how many of those this corpus holds
    coverage     held/cited
    in seed      whether SEED_CPC already covers it

A subclass with a high `cited` count and low coverage is a real gap. One with a high count and
high coverage is already served. The output is an expansion list ordered by how much art it would
actually add, not by intuition.

    python eval/cpc_gap.py                 # the whole indexed field
    python eval/cpc_gap.py --sample 4000   # faster, sampled
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import db  # noqa: E402
from config import SEED_CPC  # noqa: E402

EXAMINER = ("(ci.category LIKE '%%SEA%%' OR ci.category LIKE '%%EXA%%' "
            "OR ci.category LIKE '%%ISR%%')")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="cap the number of citing publications (0 = all)")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    like = " OR ".join(["cl.symbol LIKE %s"] * len(SEED_CPC))
    pats = [h + "%" for h in SEED_CPC]
    limit = f"LIMIT {args.sample}" if args.sample else ""

    sql = f"""
    WITH field AS (
      SELECT DISTINCT p.publication_number pn
      FROM classifications cl JOIN publications p ON p.id = cl.publication_id
      WHERE {like}
      {limit}
    ),
    cited AS (
      SELECT DISTINCT ci.dst_pub
      FROM citations ci JOIN field f ON f.pn = ci.src_pub
      WHERE {EXAMINER}
    ),
    resolved AS (
      SELECT c.dst_pub, p.id pid
      FROM cited c LEFT JOIN publications p ON p.publication_number = c.dst_pub
    )
    SELECT substring(cl.symbol from 1 for 4) AS subclass,
           count(DISTINCT r.dst_pub) AS held_here
    FROM resolved r JOIN classifications cl ON cl.publication_id = r.pid
    WHERE r.pid IS NOT NULL
    GROUP BY 1 ORDER BY 2 DESC"""

    with db.cursor() as cur:
        print("[cpc-gap] sampling the field and its examiner citations (a few minutes)...")
        cur.execute(sql, pats)
        held = {r["subclass"]: r["held_here"] for r in cur.fetchall()}

        cur.execute(f"""
        WITH field AS (
          SELECT DISTINCT p.publication_number pn
          FROM classifications cl JOIN publications p ON p.id = cl.publication_id
          WHERE {like} {limit})
        SELECT count(DISTINCT ci.dst_pub) n,
               count(DISTINCT ci.dst_pub) FILTER (
                 WHERE EXISTS (SELECT 1 FROM publications p
                               WHERE p.publication_number = ci.dst_pub)) held
        FROM citations ci JOIN field f ON f.pn = ci.src_pub
        WHERE {EXAMINER}""", pats)
        tot = cur.fetchone()

    seed = {s[:4] for s in SEED_CPC}
    print(f"\nexaminer-cited documents reachable from the indexed field: {tot['n']:,}")
    print(f"of those, held in this corpus: {tot['held']:,} "
          f"({100.0 * tot['held'] / max(tot['n'], 1):.1f}%)\n")
    print(f"{'subclass':9s} {'held here':>10s} {'in seed?':>9s}")
    print("-" * 32)
    for sub, n in sorted(held.items(), key=lambda kv: -kv[1])[:args.top]:
        print(f"{sub:9s} {n:>10,} {('yes' if sub in seed else 'NO'):>9s}")
    outside = [(s, n) for s, n in held.items() if s not in seed]
    outside.sort(key=lambda kv: -kv[1])
    print(f"\n{len(outside)} subclasses OUTSIDE the seed set carry cited art we already hold "
          f"(via family/citation expansion). The biggest:")
    for s, n in outside[:12]:
        print(f"    {s}  {n:,}")
    print("\nA subclass high on this list is where examiners in this field actually look. "
          "SEED_CPC covers:", ", ".join(sorted(seed)))


if __name__ == "__main__":
    main()
