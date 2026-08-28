"""E3: CPC scope, multi-query fusion, and the docchunks weighting."""
import sys, json, time
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db, embed
from config import SEED_CPC

REP = "/home/nimrod_rotem/patent-search-pilot/data/reports/adhoc-584455f78ae2.json"
rep = json.load(open(REP)); QUERY = rep["query"]
inv_only = QUERY[:QUERY.find("Drawings (figures analysed")].strip()
ESSENCE = ("handheld battery powered electric vacuum suction lifter with an elastic annular "
           "sealing ring, an electric pump that continuously evacuates the chamber, and a handle")
T = {"US-10625955-B2": 11188, "DE-3724659-A1": 7775}
def vec(e): return "[" + ",".join(f"{x:.6f}" for x in e) + "]"
con = db.connect(); con.autocommit = True; cur = con.cursor()
cur.execute("SET hnsw.ef_search = 1000"); cur.execute("SET hnsw.iterative_scan = relaxed_order")
cur.execute("SET hnsw.max_scan_tuples = 40000")

print("== CPC of the two targets ==")
for pub, pid in T.items():
    cur.execute("select symbol from classifications where publication_id=%s order by symbol", (pid,))
    print(" ", pub, [r["symbol"] for r in cur.fetchall()][:14])
print()
print("== corpus size inside the 8 seed CPC branches ==")
like = " OR ".join(["cl.symbol LIKE %s"] * len(SEED_CPC))
cur.execute(f"select count(distinct cl.publication_id) n from classifications cl where {like}",
            [h + "%" for h in SEED_CPC])
print("  pubs in seed CPC:", cur.fetchone()["n"])
print()

print("== dense search RESTRICTED to the 8 seed CPC branches (a field-scoped deep funnel) ==")
for name, text in [("live_query", QUERY), ("brief_only", inv_only), ("short_essence", ESSENCE)]:
    v = vec(embed.embed_query(text[:8000], 768))
    t0 = time.time()
    cur.execute(f"""select c.publication_id, 1-(c.embedding <=> %s::vector) s
                    from chunks c
                    where c.embedding is not null and exists (
                      select 1 from classifications cl where cl.publication_id=c.publication_id
                      and ({like}))
                    order by c.embedding <=> %s::vector limit 4000""",
                [v] + [h + "%" for h in SEED_CPC] + [v])
    rows = cur.fetchall(); order, seen = [], set()
    for r in rows:
        if r["publication_id"] not in seen:
            seen.add(r["publication_id"]); order.append(r["publication_id"])
    ranks = {p: (order.index(i) + 1 if i in seen else None) for p, i in T.items()}
    print(f"  {name:14s} pubs={len(order):5d} cut={rows[-1]['s']:.3f} {time.time()-t0:5.1f}s  {ranks}")
print()

print("== RRF fusion of MANY short queries vs one long one ==")
els = rep["elements"]
qs = [("essence", ESSENCE), ("brief", inv_only)] + [("el%d" % i, e) for i, e in enumerate(els)]
chans = {}
for name, text in qs:
    v = vec(embed.embed_query(text[:8000], 768))
    cur.execute("""select c.publication_id, 1-(c.embedding <=> %s::vector) s from chunks c
                   where c.embedding is not null order by c.embedding <=> %s::vector limit 4000""",
                (v, v))
    order, seen = [], set()
    for r in cur.fetchall():
        if r["publication_id"] not in seen:
            seen.add(r["publication_id"]); order.append(r["publication_id"])
    chans[name] = order
K = 40
fused = {}
for name, order in chans.items():
    for rank, pid in enumerate(order):
        fused[pid] = fused.get(pid, 0.0) + 1.0 / (K + rank + 1)
ranked = [p for p, _ in sorted(fused.items(), key=lambda t: t[1], reverse=True)]
print("  fused pool size:", len(ranked))
for pub, pid in T.items():
    print(f"  {pub:18s} fused pub-rank = {ranked.index(pid)+1 if pid in fused else None}"
          f"   (in {sum(1 for o in chans.values() if pid in o)} of {len(chans)} sub-queries)")
