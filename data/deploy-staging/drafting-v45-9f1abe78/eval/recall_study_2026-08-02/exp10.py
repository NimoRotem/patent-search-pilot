"""E10a: is GROUNDED EVIDENCE COUNT a better sort key than a free-form full-text score?
   E10b: does keeping citation/family expansion put DE-3724659-A1 back in the pool?"""
import sys, json, re, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db, embed, llm, deep_analysis, grounding

R = "/home/nimrod_rotem/patent-search-pilot/data/reports/adhoc-584455f78ae2.json"
rep = json.load(open(R))
BRIEF = rep["query"][:rep["query"].find("Drawings (figures analysed")].strip()
FEATS = rep["elements"]

REFS = ["US-11999030-B2", "US-12115659-B1", "US-10625955-B2", "DE-3724659-A1",
        "DE-19646890-A1", "SU-1284930-A1", "SU-627745-A3", "SU-925836-A1",
        "US-5795001-A", "WO-9301026-A1", "US-2026084267-A1"]

SYS = ("You are a patent examiner. For EACH numbered FEATURE of the target invention, say what "
       "this reference discloses. verdict is one of disclosed / partial / absent. When it is not "
       "absent you MUST give a VERBATIM quote copied exactly from the reference text (8-40 "
       'words). Return ONLY JSON {"rows":[{"id":<feature number>,"verdict":"...","quote":"..."}]}'
       ", one row per feature.")

def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()

def chart(pub):
    ft = deep_analysis.full_text(pub)
    body = "\n\n".join(f"[{p['label']}] {p['text']}" for p in ft["passages"])[:180000]
    hay = norm(body)
    out = llm.chat_json(SYS, f"TARGET INVENTION FEATURES:\n" +
                        "\n".join(f"[{i+1}] {f}" for i, f in enumerate(FEATS)) +
                        f"\n\nREFERENCE {pub} — {ft['title']}\nTEXT:\n{body}", max_tokens=2500) or {}
    disc = part = grounded = ungrounded = 0
    for r in (out.get("rows") or []):
        v = str(r.get("verdict") or "").lower()
        if v not in ("disclosed", "partial"):
            continue
        q = norm(r.get("quote") or "")
        ok = len(q.split()) >= 5 and q in hay
        if ok:
            grounded += 1
            disc += v == "disclosed"; part += v == "partial"
        else:
            ungrounded += 1
    return {"pub": pub, "chars": ft["chars"], "grounded": grounded, "disclosed": disc,
            "partial": part, "ungrounded": ungrounded}

print("== E10a: grounded feature evidence per reference (quote must appear verbatim) ==")
t0 = time.time()
with ThreadPoolExecutor(max_workers=6) as ex:
    res = list(ex.map(chart, REFS))
res.sort(key=lambda r: (-r["grounded"], -r["disclosed"]))
print(f"{'pub':20s} {'chars':>8s} {'grounded':>8s} {'disclosed':>9s} {'partial':>7s} {'DROPPED(ungrounded)':>20s}")
for r in res:
    print(f"{r['pub']:20s} {r['chars']:8d} {r['grounded']:8d} {r['disclosed']:9d} "
          f"{r['partial']:7d} {r['ungrounded']:20d}")
print(f"({time.time()-t0:.0f}s for {len(REFS)} references)")

print()
print("== E10b: Stage 1 + citation/family expansion — does DE-3724659-A1 come back? ==")
con = db.connect(); con.autocommit = True; cur = con.cursor()
cur.execute("SET hnsw.ef_search = 400"); cur.execute("SET hnsw.iterative_scan = relaxed_order")
cur.execute("SET hnsw.max_scan_tuples = 40000")
def vec(e): return "[" + ",".join(f"{x:.6f}" for x in e) + "]"
ESSENCE = ("Battery-powered portable vacuum gripper with a rigid base, a deformable peripheral "
           "seal, and a bracing structure limiting seal compression")
queries = [ESSENCE] + FEATS
rrf, best = {}, {}
for q in queries:
    v = vec(embed.embed_query(q[:8000], 768))
    cur.execute("""select c.publication_id, 1-(c.embedding <=> %s::vector) s from chunks c
                   where c.embedding is not null order by c.embedding <=> %s::vector limit 3000""",
                (v, v))
    seen, r = set(), 0
    for row in cur.fetchall():
        pid, sc = row["publication_id"], float(row["s"])
        best[pid] = max(best.get(pid, 0.0), sc)
        if pid not in seen:
            seen.add(pid); rrf[pid] = rrf.get(pid, 0.0) + 1.0/(40+r+1); r += 1
mx = max(rrf.values())
comb = {p: 0.5*(rrf[p]/mx) + 0.5*max(0.0, min(1.0, (best[p]-0.60)/0.30)) for p in rrf}
pool = [p for p, _ in sorted(comb.items(), key=lambda t: -t[1])]
DE = 7775
print(f"   dense-only pool: {len(pool)} pubs;  DE-3724659-A1 at pub-rank "
      f"{pool.index(DE)+1 if DE in comb else None}")
seeds = pool[:40]
inl = "(" + ",".join(["%s"]*len(seeds)) + ")"
cur.execute(f"""with seed as (select id, publication_number, simple_family_id from publications
                              where id in {inl}),
                 fam as (select p.id, 2 w from publications p join seed s
                          on p.simple_family_id=s.simple_family_id where p.simple_family_id is not null),
                 cited as (select p.id, 3 w from citations ci join seed s on ci.src_pub=s.publication_number
                           join publications p on p.publication_number=ci.dst_pub),
                 citing as (select p.id, 1 w from citations ci join seed s on ci.dst_pub=s.publication_number
                            join publications p on p.publication_number=ci.src_pub)
                select id, sum(w) sc from (select * from fam union all select * from cited
                       union all select * from citing) z group by id order by sc desc limit 1000""",
            list(seeds))
cit = [(r["id"], float(r["sc"])) for r in cur.fetchall()]
print(f"   citation/family expansion from the top 40: {len(cit)} pubs; "
      f"DE in it = {any(p == DE for p, _ in cit)}")
for i, (pid, _) in enumerate(cit):
    rrf[pid] = rrf.get(pid, 0.0) + 0.55/(40+i+1)          # the live CHANNEL_WEIGHTS['citation']
    comb[pid] = 0.5*(rrf[pid]/mx) + 0.5*max(0.0, min(1.0, (best.get(pid, 0.0)-0.60)/0.30))
pool2 = [p for p, _ in sorted(comb.items(), key=lambda t: -t[1])]
print(f"   fused pool WITH citation expansion: DE-3724659-A1 at pub-rank "
      f"{pool2.index(DE)+1 if DE in comb else None} of {len(pool2)}")
head = pool2[:1500]
cur.execute("""select id, coalesce(nullif(simple_family_id,''), publication_number) fam
               from publications where id = any(%s)""", (head,))
fm = {r["id"]: r["fam"] for r in cur.fetchall()}
ded, sf = [], set()
for p in head:
    f = fm.get(p)
    if not f or f in sf: continue
    sf.add(f); ded.append(p)
print(f"   after family dedup: DE-3724659-A1 at candidate-rank "
      f"{ded.index(DE)+1 if DE in ded else None} of {len(ded)}  "
      f"(screener would then see it: {'YES' if DE in ded[:400] else 'no'})")
