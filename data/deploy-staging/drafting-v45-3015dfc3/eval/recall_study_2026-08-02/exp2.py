"""E2: does the query FORMULATION explain the miss? Reproduce the real channels per variant."""
import sys, json, time
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db, embed

REP = "/home/nimrod_rotem/patent-search-pilot/data/reports/adhoc-584455f78ae2.json"
rep = json.load(open(REP))
QUERY = rep["query"]
qd = rep["query_document"]
claims = qd["claims"]
c1 = next(c["text"] for c in claims if c["claim_no"] == 1)
inv_only = QUERY[:QUERY.find("Drawings (figures analysed")].strip()

TARG = {"US-10625955-B2": 11188, "DE-3724659-A1": 7775}

VARIANTS = [
    ("live_query(brief+figures)", QUERY),
    ("brief_only(no figure text)", inv_only),
    ("claim1_verbatim", c1),
    ("short_essence", "handheld battery powered electric vacuum suction lifter with an elastic "
                      "annular sealing ring, an electric pump that continuously evacuates the "
                      "chamber, and a handle"),
]
for el in rep["elements"][:12]:
    VARIANTS.append(("element: " + el[:46], el))

def vec(e): return "[" + ",".join(f"{x:.6f}" for x in e) + "]"

con = db.connect(); con.autocommit = True
cur = con.cursor()
cur.execute("SET hnsw.ef_search = 200")
cur.execute("SET hnsw.iterative_scan = relaxed_order")
cur.execute("SET hnsw.max_scan_tuples = 12000")

CH = {
  "dense(all chunks)": "where c.embedding is not null",
  "claim_dense(claims only)": "where c.embedding is not null and c.kind in ('claim_own','claim_resolved')",
}

print(f"{'variant':46s} {'channel':26s} {'pubs':>5s} {'cut':>6s} | " +
      " | ".join(f"{p:>22s}" for p in TARG))
for name, text in VARIANTS:
    qv = embed.embed_query(text[:8000], 768); v = vec(qv)
    # best chunk cosine of each target under THIS query, per channel scope
    for chname, where in CH.items():
        kindfilter = "" if "all" in chname else "and c.kind in ('claim_own','claim_resolved')"
        best = {}
        for pub, pid in TARG.items():
            cur.execute(f"""select 1-(c.embedding <=> %s::vector) s from chunks c
                            where c.publication_id=%s and c.embedding is not null {kindfilter}
                            order by c.embedding <=> %s::vector limit 1""", (v, pid, v))
            r = cur.fetchone(); best[pub] = r["s"] if r else None
        cur.execute(f"""select c.publication_id, 1-(c.embedding <=> %s::vector) s from chunks c
                        {where} order by c.embedding <=> %s::vector limit 4000""", (v, v))
        rows = cur.fetchall()
        order, seen = [], set()
        for r in rows:
            if r["publication_id"] not in seen:
                seen.add(r["publication_id"]); order.append(r["publication_id"])
        cut = rows[-1]["s"] if rows else 0
        cells = []
        for pub, pid in TARG.items():
            rank = (order.index(pid) + 1) if pid in seen else None
            cells.append(f"{(best[pub] or 0):.3f} @pub#{rank if rank else '-':>5}")
        print(f"{name:46.46s} {chname:26s} {len(order):5d} {cut:6.3f} | " + " | ".join(f"{c:>22s}" for c in cells))
