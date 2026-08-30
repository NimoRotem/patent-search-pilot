"""E8a: honest prevalence of abstracts that match neither the title nor claim 1 (US only).
   E8b: the docchunks channel as built (weight x cosine) vs rank fusion over the same vectors."""
import sys, re, random, json, time
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db
con = db.connect(); con.autocommit = True; cur = con.cursor()

STOP = set("a an the of and or to for with in on at by is are be as from that this it its which "
           "such said have has having comprising comprises comprise method system device apparatus "
           "assembly unit means portion member element part first second third one two least "
           "including includes include provided plurality wherein thereof therein said configured "
           "surface end side body according present invention disclosed described relates".split())
def toks(s):
    return {w for w in re.findall(r"[a-z]{5,}", (s or "").lower()) if w not in STOP}

cur.execute("""select p.publication_number, p.title, p.abstract,
                      (select c.text from claims c where c.publication_id=p.id
                        order by c.claim_no limit 1) c1
               from publications p
               where p.country='US' and p.abstract is not null and length(p.abstract)>250
                 and p.title is not null and length(p.title)>15
               order by p.id limit 6000""")
rows = [r for r in cur.fetchall() if r["c1"]]
random.seed(11); random.shuffle(rows)
sample = rows[:1500]
bad, tot, ex = 0, 0, []
for r in sample:
    a = toks(r["abstract"])
    ref = toks(r["title"]) | toks(r["c1"])
    if not a or not ref:
        continue
    tot += 1
    if not (a & ref):
        bad += 1
        if len(ex) < 6:
            ex.append((r["publication_number"], r["title"][:44], r["abstract"][:64]))
print("== E8a: US publications whose abstract shares NO content word with title OR claim 1 ==")
print(f"   sample={tot}   suspect={bad}  ({100.0*bad/max(tot,1):.2f}%)")
for e in ex:
    print(f"     {e[0]:20s} {e[1]:44s} | {e[2]}")

print()
print("== E8b: docchunks channel — weight x cosine (as built) vs rank fusion ==")
doc = json.load(open("/home/nimrod_rotem/patent-search-pilot/data/reports/"
                     "doc-7996c8df7ccc4372a0519e21431a8f5c.json"))
vecs, ws = doc["chunk_vecs"], doc["chunk_weights"]
print(f"   {len(vecs)} document chunk vectors; weights: "
      f"{sum(1 for w in ws if w>=1.0)} at 1.0, {sum(1 for w in ws if w<1.0)} below")
T = {"US-10625955-B2": 11188, "DE-3724659-A1": 7775}
def vec(e): return "[" + ",".join(f"{x:.6f}" for x in e) + "]"
cur.execute("SET hnsw.ef_search = 400"); cur.execute("SET hnsw.iterative_scan = relaxed_order")
cur.execute("SET hnsw.max_scan_tuples = 40000")
pooled, per_vec_order, cosmax = {}, [], {}
t0 = time.time()
for v, w in zip(vecs, ws):
    vs = vec(v)
    cur.execute("""select c.publication_id, 1-(c.embedding <=> %s::vector) s from chunks c
                   where c.embedding is not null order by c.embedding <=> %s::vector limit 400""",
                (vs, vs))
    order, seen = [], set()
    for r in cur.fetchall():
        pid, sc = r["publication_id"], float(r["s"])
        if float(w) * sc > pooled.get(pid, -1):
            pooled[pid] = float(w) * sc
        cosmax[pid] = max(cosmax.get(pid, 0.0), sc)
        if pid not in seen:
            seen.add(pid); order.append(pid)
    per_vec_order.append(order)
print(f"   {len(vecs)} ANN passes in {time.time()-t0:.0f}s, pooled pubs={len(pooled)}")

def rank_of(ranked, pid):
    return (ranked.index(pid) + 1) if pid in ranked else None
as_built = [p for p, _ in sorted(pooled.items(), key=lambda t: -t[1])]
by_cos   = [p for p, _ in sorted(cosmax.items(), key=lambda t: -t[1])]
rrf = {}
for order in per_vec_order:
    for i, pid in enumerate(order):
        rrf[pid] = rrf.get(pid, 0.0) + 1.0 / (40 + i + 1)
by_rrf = [p for p, _ in sorted(rrf.items(), key=lambda t: -t[1])]
for name, ranked in [("weight x cosine (as built)", as_built), ("max cosine (unweighted)", by_cos),
                     ("RRF over the 38 vectors", by_rrf)]:
    print(f"   {name:28s} " + "  ".join(f"{p}=#{rank_of(ranked, i)}" for p, i in T.items()))
