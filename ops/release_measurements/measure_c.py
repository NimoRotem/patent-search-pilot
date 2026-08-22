"""Family home-domain distribution, from a bounded page sample of the live corpus.

Sampling publications and then expanding to whole families over-samples large families in
proportion to their size, which is exactly right for estimating MASS (chunks and publications
live in publications). Family COUNTS from this sample are biased upward for large families and
are reported only as a ratio.
"""
import os, sys, json, time
sys.path.insert(0, "/home/nimrod_rotem/v3/F-release/src")
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

ROOT = "/home/nimrod_rotem/v3/F-release"
load_dotenv(os.path.join(ROOT, ".env"))
from corpus import assign            # noqa: E402
from retrieval.shard_router import UNCLASSIFIED  # noqa: E402

dsn = (f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} dbname={os.environ['PGDATABASE']} "
       f"user={os.environ['PGUSER']} password={os.environ['PGPASSWORD']}")
conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
cur = conn.cursor()
cur.execute("SET statement_timeout = '300s'")
PCT = float(os.environ.get("SAMPLE_PCT", "0.3"))


def batched(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


t0 = time.time()
cur.execute("EXPLAIN SELECT id, simple_family_id, publication_number FROM publications "
            "WHERE simple_family_id = ANY(%s)", (["X"],))
print("EXPLAIN family expand:", " | ".join(r["QUERY PLAN"] for r in cur.fetchall()))
cur.execute("EXPLAIN SELECT publication_id, count(*) n FROM chunks WHERE publication_id = ANY(%s) "
            "GROUP BY 1", ([1],))
print("EXPLAIN chunk counts:", " | ".join(r["QUERY PLAN"] for r in cur.fetchall()))

cur.execute(f"SELECT id, simple_family_id, publication_number FROM publications "
            f"TABLESAMPLE SYSTEM ({PCT})")
seed = cur.fetchall()
fams = sorted({assign.family_key(r["simple_family_id"], r["publication_number"]) for r in seed})
print(f"seed pubs={len(seed):,} distinct families={len(fams):,}  ({time.time()-t0:.1f}s)")

pubs = {}
for chunk in batched(fams, 5000):
    cur.execute("SELECT id, simple_family_id, publication_number FROM publications "
                "WHERE simple_family_id = ANY(%s) OR publication_number = ANY(%s)",
                (chunk, chunk))
    for r in cur.fetchall():
        pubs[r["id"]] = assign.family_key(r["simple_family_id"], r["publication_number"])
print(f"expanded pubs={len(pubs):,}  ({time.time()-t0:.1f}s)")

pids = sorted(pubs)
syms = {}
for chunk in batched(pids, 5000):
    cur.execute("SELECT publication_id p, symbol, is_first FROM classifications "
                "WHERE publication_id = ANY(%s) AND symbol IS NOT NULL", (chunk,))
    for r in cur.fetchall():
        e = syms.setdefault(r["p"], {"symbols": [], "first_symbols": []})
        e["symbols"].append(r["symbol"])
        if r["is_first"]:
            e["first_symbols"].append(r["symbol"])
print(f"classified pubs={len(syms):,}  ({time.time()-t0:.1f}s)")

nchunks = {}
for chunk in batched(pids, 3000):
    cur.execute("SELECT publication_id p, count(*) n FROM chunks WHERE publication_id = ANY(%s) "
                "GROUP BY 1", (chunk,))
    for r in cur.fetchall():
        nchunks[r["p"]] = int(r["n"])
print(f"pubs with chunks={len(nchunks):,} total chunks={sum(nchunks.values()):,}  "
      f"({time.time()-t0:.1f}s)")

by_fam = {}
for pid, fk in pubs.items():
    by_fam.setdefault(fk, []).append(pid)

mass_chunks, mass_pubs, fam_count = {}, {}, {}
cross, mentions, reachable = 0, 0, 0
for fk, members in by_fam.items():
    rows = [syms.get(p, {"symbols": [], "first_symbols": []}) for p in members]
    home, weights = assign.home_domain(rows)
    sec = assign.secondary_domains(home, weights)
    c = sum(nchunks.get(p, 0) for p in members)
    mass_chunks[home] = mass_chunks.get(home, 0) + c
    mass_pubs[home] = mass_pubs.get(home, 0) + len(members)
    fam_count[home] = fam_count.get(home, 0) + 1
    if sec:
        cross += 1
    mentions += len(weights)
    reachable += 1

tot_c = sum(mass_chunks.values()) or 1
tot_p = sum(mass_pubs.values()) or 1
print(f"\nfamilies={len(by_fam):,} cross-domain families={cross:,} "
      f"({100*cross/max(1,len(by_fam)):.1f}%)  mention/family={mentions/max(1,len(by_fam)):.2f}")
print(f"UNCLASSIFIED share: pubs {100*mass_pubs.get(UNCLASSIFIED,0)/tot_p:.2f}%  "
      f"chunks {100*mass_chunks.get(UNCLASSIFIED,0)/tot_c:.2f}%  "
      f"families {100*fam_count.get(UNCLASSIFIED,0)/max(1,len(by_fam)):.2f}%")
print(f"distinct home domains={len(mass_chunks)}")
print("\ntop home domains by chunk mass:")
for d, c in sorted(mass_chunks.items(), key=lambda kv: -kv[1])[:25]:
    print(f"  {d:14s} chunks {c:>9,} {100*c/tot_c:5.2f}%   pubs {mass_pubs.get(d,0):>7,} "
          f"{100*mass_pubs.get(d,0)/tot_p:5.2f}%   fams {fam_count.get(d,0):>7,}")

json.dump({"pct": PCT, "families": len(by_fam), "cross_domain_families": cross,
           "mass_chunks": mass_chunks, "mass_pubs": mass_pubs, "fam_count": fam_count,
           "sample_pubs": len(pubs), "sample_chunks": sum(nchunks.values())},
          open(os.path.join(ROOT, "data/logs/home_domain_sample.json"), "w"))
conn.close()
print(f"\ntotal {time.time()-t0:.1f}s")
