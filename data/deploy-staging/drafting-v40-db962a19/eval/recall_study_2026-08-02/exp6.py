"""E6: how much TEXT does a screener need? + is the batch-of-25 screen itself lossy?"""
import sys, json, time
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db, llm, deep_analysis

REP = "/home/nimrod_rotem/patent-search-pilot/data/reports/adhoc-584455f78ae2.json"
rep = json.load(open(REP))
BRIEF = rep["query"][:rep["query"].find("Drawings (figures analysed")].strip()
T = ["US-10625955-B2", "DE-3724659-A1"]

SYS = ("You are a patent prior-art examiner SCORING one candidate reference against a target "
       "invention. Judge ONLY on technical substance in the text given. Integer SCORE 0-100: "
       "90-100 same invention; 70-89 most core elements; 40-69 related field; 1-39 same broad "
       'area; 0 unrelated. Return ONLY JSON {"score":<int>,"why":"<one sentence>"}.')

con = db.connect(); con.autocommit = True; cur = con.cursor()
print("== E6a: what the cheap screener actually SAW for each target ==")
for pub in T:
    cur.execute("select id, title, abstract from publications where publication_number=%s", (pub,))
    r = cur.fetchone()
    cur.execute("select text from claims where publication_id=%s order by claim_no limit 1", (r["id"],))
    c1 = (cur.fetchone() or {}).get("text") or ""
    seen = ((r["abstract"] or "") + "  " + c1)[:900]
    print(f"\n  --- {pub} ({r['title']}) — the 900 chars the screener saw:")
    print("      " + seen.replace("\n", " ")[:880])

print()
print("== E6b: score vs TEXT BUDGET (single-candidate calls, 3 repeats each) ==")
for pub in T:
    cur.execute("select id, title, abstract from publications where publication_number=%s", (pub,))
    r = cur.fetchone(); pid = r["id"]
    cur.execute("select text from claims where publication_id=%s order by claim_no limit 1", (pid,))
    c1 = (cur.fetchone() or {}).get("text") or ""
    cur.execute("select claim_no, text from claims where publication_id=%s order by claim_no", (pid,))
    allcl = "\n".join(f"claim {x['claim_no']}: {x['text']}" for x in cur.fetchall())
    ft = deep_analysis.full_text(pub)
    body = "\n\n".join(f"[{p['label']}] {p['text']}" for p in ft["passages"])[:180000]
    variants = [("abstract+claim1 (900c)", ((r["abstract"] or "") + "  " + c1)[:900]),
                ("all claims", allcl[:60000]),
                ("full text", body)]
    for name, text in variants:
        got = []
        for _ in range(3):
            out = llm.chat_json(SYS, f"TARGET INVENTION:\n{BRIEF}\n\nCANDIDATE {pub} — "
                                     f"{r['title']}\nTEXT ({len(text)} chars):\n{text}",
                                max_tokens=400) or {}
            got.append(out.get("score"))
        print(f"  {pub:18s} {name:24s} chars={len(text):7d}  scores={got}")

print()
print("== E6c: field-scoped DEEP funnel — how deep can we go inside the 81,890-pub field? ==")
from config import SEED_CPC
import embed
ESSENCE = ("handheld battery powered electric vacuum suction lifter with an elastic annular "
           "sealing ring, an electric pump that continuously evacuates the chamber, and a handle")
like = " OR ".join(["cl.symbol LIKE %s"] * len(SEED_CPC))
def vec(e): return "[" + ",".join(f"{x:.6f}" for x in e) + "]"
v = vec(embed.embed_query(rep["query"][:8000], 768))
cur.execute("SET hnsw.ef_search = 1000"); cur.execute("SET hnsw.iterative_scan = relaxed_order")
for K, mst in [(4000, 40000), (20000, 200000), (60000, 600000)]:
    cur.execute("SET hnsw.max_scan_tuples = %d" % mst)
    t0 = time.time()
    cur.execute(f"""select c.publication_id, 1-(c.embedding <=> %s::vector) s from chunks c
                    where c.embedding is not null and exists (select 1 from classifications cl
                      where cl.publication_id=c.publication_id and ({like}))
                    order by c.embedding <=> %s::vector limit {K}""",
                [v] + [h + "%" for h in SEED_CPC] + [v])
    rows = cur.fetchall(); order, seen = [], set()
    for x in rows:
        if x["publication_id"] not in seen:
            seen.add(x["publication_id"]); order.append(x["publication_id"])
    tg = {}
    for pub in T:
        cur2 = con.cursor(); cur2.execute("select id from publications where publication_number=%s", (pub,))
        pid = cur2.fetchone()["id"]
        tg[pub] = (order.index(pid) + 1) if pid in seen else None
    print(f"  LIMIT={K:<6} rows={len(rows):<6} pubs={len(order):<6} cut={rows[-1]['s']:.3f} "
          f"{time.time()-t0:6.1f}s  targets={tg}")
