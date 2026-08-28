"""E9: end-to-end PROTOTYPE of the proposed pipeline, measured on this exact search.

  Stage 0  many short queries instead of one long brief (no figure prose)
  Stage 1  wide candidate generation, fused by RRF + best-single-query score
  Stage 2  cheap LLM screen over the top 400 (abstract used only when it agrees with the doc)
  Stage 3  full-text judgement of the top 50 -> the PRIMARY sort key
"""
import sys, json, re, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db, embed, llm, deep_analysis

R = "/home/nimrod_rotem/patent-search-pilot/data/reports/adhoc-584455f78ae2.json"
rep = json.load(open(R))
BRIEF = rep["query"][:rep["query"].find("Drawings (figures analysed")].strip()
claims = rep["query_document"]["claims"]
TARGETS = ("US-10625955-B2", "DE-3724659-A1")
t_start = time.time()

# ---- Stage 0 -------------------------------------------------------------------------------
ESS_SYS = ("Write SHORT prior-art search queries for this invention. Return ONLY JSON "
           '{"essence":"<=35 words naming the device, how it is powered and its key structural '
           'feature>","alts":["<=12 words, alternative vocabulary a searcher/examiner would use",'
           '... 5 of them]}. Use the words that appear in patents, not marketing words.')
qs = llm.chat_json(ESS_SYS, BRIEF, max_tokens=600) or {}
queries = []
if qs.get("essence"):
    queries.append(("essence", qs["essence"]))
for i, a in enumerate(qs.get("alts") or []):
    queries.append((f"alt{i}", a))
for i, e in enumerate(rep["elements"]):
    queries.append((f"el{i}", e))
for c in claims:
    if c.get("independent"):
        queries.append((f"claim{c['claim_no']}", c["text"]))
print("== Stage 0: query set ==")
for n, q in queries:
    print(f"   {n:9s} {q[:110]}")

con = db.connect(); con.autocommit = True; cur = con.cursor()
cur.execute("SET hnsw.ef_search = 400"); cur.execute("SET hnsw.iterative_scan = relaxed_order")
cur.execute("SET hnsw.max_scan_tuples = 40000")
def vec(e): return "[" + ",".join(f"{x:.6f}" for x in e) + "]"

# ---- Stage 1 -------------------------------------------------------------------------------
rrf, best, found_in = {}, {}, {}
t0 = time.time()
for n, q in queries:
    v = vec(embed.embed_query(q[:8000], 768))
    cur.execute("""select c.publication_id, 1-(c.embedding <=> %s::vector) s from chunks c
                   where c.embedding is not null order by c.embedding <=> %s::vector limit 3000""",
                (v, v))
    seen, r = set(), 0
    for row in cur.fetchall():
        pid, sc = row["publication_id"], float(row["s"])
        best[pid] = max(best.get(pid, 0.0), sc)
        if pid not in seen:
            seen.add(pid)
            rrf[pid] = rrf.get(pid, 0.0) + 1.0 / (40 + r + 1)
            found_in[pid] = found_in.get(pid, 0) + 1
            r += 1
print(f"== Stage 1: {len(queries)} passes in {time.time()-t0:.0f}s -> {len(rrf)} publications ==")
# combine: normalised RRF (agreement) + normalised best cosine (a single strong hit still counts)
mx_rrf = max(rrf.values()); lo, hi = 0.60, 0.90
comb = {p: 0.5 * (rrf[p] / mx_rrf) + 0.5 * max(0.0, min(1.0, (best[p] - lo) / (hi - lo)))
        for p in rrf}
pool = [p for p, _ in sorted(comb.items(), key=lambda t: -t[1])]
# family dedup over the head only
head = pool[:1200]
cur.execute("""select id, publication_number, title, abstract,
                      coalesce(nullif(simple_family_id,''), publication_number) fam
               from publications where id = any(%s)""", (head,))
info = {r["id"]: r for r in cur.fetchall()}
dedup, seenfam = [], set()
for p in head:
    r = info.get(p)
    if not r or r["fam"] in seenfam:
        continue
    seenfam.add(r["fam"]); dedup.append(p)
CAND = dedup[:400]
pos = {info[p]["publication_number"]: i + 1 for i, p in enumerate(CAND)}
print(f"   family-deduped candidate pool = {len(CAND)}   targets after Stage 1: " +
      ", ".join(f"{t}=#{pos.get(t)}" for t in TARGETS))

# ---- Stage 2 -------------------------------------------------------------------------------
STOP = set("a an the of and or to for with in on at by is are be as from that this it its which "
           "such said have has having comprising comprises method system device apparatus".split())
def toks(s): return {w for w in re.findall(r"[a-z]{5,}", (s or "").lower()) if w not in STOP}
rowsx = []
for p in CAND:
    r = info[p]
    cur.execute("select text from claims where publication_id=%s order by claim_no limit 1", (p,))
    c1 = (cur.fetchone() or {}).get("text") or ""
    # an abstract that shares no word with the title or claim 1 is not this patent's abstract
    ab = r["abstract"] or ""
    if ab and not (toks(ab) & (toks(r["title"]) | toks(c1))):
        ab = ""
    v = vec(embed.embed_query(BRIEF[:8000], 768))
    rowsx.append({"pub": r["publication_number"], "title": r["title"] or "",
                  "text": (ab + "  " + c1)[:1100]})
SCR = ("You are a patent examiner SCREENING candidates against a target invention. For EACH "
       "numbered candidate give an integer 0-100 relevance score (90+ same invention, 70-89 most "
       "core elements, 40-69 related field, 1-39 same broad area, 0 unrelated). Judge on "
       'technical substance, never the title alone. Return ONLY JSON {"results":[{"id":<n>,'
       '"score":<int>}]}, every id exactly once.')
screen, B = {}, 25
t0 = time.time()
def screen_batch(i):
    b = rowsx[i:i + B]
    out = llm.chat_json(SCR, f"TARGET INVENTION:\n{BRIEF}\n\nCANDIDATES:\n" +
                        "\n".join(f"[{j+1}] {c['title']}\n  {c['text']}" for j, c in enumerate(b)),
                        max_tokens=1600) or {}
    got = {}
    for x in (out.get("results") or []):
        try: j = int(x["id"]) - 1
        except Exception: continue
        if 0 <= j < len(b): got[b[j]["pub"]] = int(x.get("score") or 0)
    return got
with ThreadPoolExecutor(max_workers=6) as ex:
    for g in ex.map(screen_batch, range(0, len(rowsx), B)):
        screen.update(g)
sorted_screen = sorted(screen.items(), key=lambda t: -t[1])
srank = {p: i + 1 for i, (p, _) in enumerate(sorted_screen)}
print(f"== Stage 2: screened {len(screen)} in {time.time()-t0:.0f}s "
      f"({(len(rowsx)+B-1)//B} calls) ==   targets: " +
      ", ".join(f"{t}=#{srank.get(t)}(score {screen.get(t)})" for t in TARGETS))

# ---- Stage 3 -------------------------------------------------------------------------------
TOP = [p for p, _ in sorted_screen[:50]]
for t in TARGETS:
    if t not in TOP and t in screen:
        TOP.append(t)          # keep both targets measurable even if the screen dropped one
FT = ("You are a patent prior-art examiner SCORING one candidate against a target invention, "
      "having read it IN FULL. Integer SCORE 0-100 (90+ same invention, 70-89 most core "
      'elements, 40-69 related field, 1-39 same broad area, 0 unrelated). Return ONLY JSON '
      '{"score":<int>,"why":"<one sentence naming the specific disclosed feature>"}.')
def judge(pub):
    ft = deep_analysis.full_text(pub)
    body = "\n\n".join(f"[{p['label']}] {p['text']}" for p in ft["passages"])[:180000]
    out = llm.chat_json(FT, f"TARGET INVENTION:\n{BRIEF}\n\nCANDIDATE {pub} — {ft['title']}\n"
                            f"FULL TEXT:\n{body}", max_tokens=400) or {}
    return pub, out.get("score"), (out.get("why") or "")[:150], ft["chars"]
t0 = time.time()
with ThreadPoolExecutor(max_workers=8) as ex:
    judged = list(ex.map(judge, TOP))
print(f"== Stage 3: full-text judged {len(judged)} refs in {time.time()-t0:.0f}s "
      f"({sum(j[3] for j in judged):,} chars) ==")
final = sorted(judged, key=lambda j: (-(j[1] or 0), srank.get(j[0], 999)))
print()
print(f"{'#':>3} {'score':>5} {'pub':20s} {'screen':>6} {'stage1':>6}  why")
for i, (pub, sc, why, ch) in enumerate(final[:25], 1):
    mark = "  <<<" if pub in TARGETS else ""
    print(f"{i:3d} {str(sc):>5s} {pub:20s} {str(screen.get(pub)):>6s} {str(pos.get(pub)):>6s}  {why[:74]}{mark}")
print()
for t in TARGETS:
    p = next((i for i, j in enumerate(final, 1) if j[0] == t), None)
    print(f"  {t:18s} FINAL rank {p} of {len(final)}   (live report: "
          f"{'card 11, score 45' if t.startswith('US-10') else 'rank 244 of 2402, never shown'})")
print(f"\nwall clock: {time.time()-t_start:.0f}s")
