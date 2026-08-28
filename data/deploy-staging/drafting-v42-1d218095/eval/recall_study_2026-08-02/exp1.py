"""E1: how deep in the dense funnel do the two 'missed' references actually sit?"""
import sys, json, time
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db, embed

REP = "/home/nimrod_rotem/patent-search-pilot/data/reports/adhoc-584455f78ae2.json"
rep = json.load(open(REP))
QUERY = rep["query"]
TARGETS = {"US-10625955-B2": 11188, "DE-3724659-A1": 7775}
CONTROL = {"US-11999030-B2": None, "US-12115659-B1": None, "US-4166648-A": None}

def vec(e): return "[" + ",".join(f"{x:.6f}" for x in e) + "]"

con = db.connect(); con.autocommit = True
cur = con.cursor()

qv = embed.embed_query(QUERY[:8000], 768)
v = vec(qv)

# best chunk cosine per target/control
print("== best-chunk cosine vs the LIVE search query ==")
allpubs = list(TARGETS) + list(CONTROL)
for pub in allpubs:
    cur.execute("""select p.publication_number, c.kind, c.coord, 1-(c.embedding <=> %s::vector) s
                   from chunks c join publications p on p.id=c.publication_id
                   where p.publication_number=%s and c.embedding is not null
                   order by c.embedding <=> %s::vector limit 1""", (v, pub, v))
    r = cur.fetchone()
    print(f"  {pub:20s} best={r['s']:.4f}  kind={r['kind']} coord={r['coord']}" if r else f"  {pub}: none")

# funnel depth: what cosine is at rank K, and are the targets inside?
print()
print("== dense funnel: LIMIT K under the app's ef_search=200 vs a deep ef_search ==")
for ef, K in [(200, 4000), (200, 20000), (4000, 4000), (20000, 20000)]:
    cur.execute("SET hnsw.ef_search = %s" % ef)
    cur.execute("SET hnsw.iterative_scan = relaxed_order")
    cur.execute("SET hnsw.max_scan_tuples = 12000")
    t0 = time.time()
    cur.execute(f"""select c.publication_id, 1-(c.embedding <=> %s::vector) s
                    from chunks c where c.embedding is not null
                    order by c.embedding <=> %s::vector limit {K}""", (v, v))
    rows = cur.fetchall()
    el = time.time() - t0
    pids = [r["publication_id"] for r in rows]
    seen = {}
    for i, p in enumerate(pids):
        seen.setdefault(p, i + 1)
    hits = {pub: seen.get(pid) for pub, pid in TARGETS.items()}
    print(f"  ef_search={ef:<6} LIMIT={K:<6} returned={len(rows):<6} distinct_pubs={len(seen):<6} "
          f"worst_cos={rows[-1]['s']:.4f} {el:.1f}s  targets_at_chunkrank={hits}")
