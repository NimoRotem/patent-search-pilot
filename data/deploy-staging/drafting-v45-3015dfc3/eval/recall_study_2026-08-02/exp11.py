"""E11: the recommended change, applied to the LIVE ranked list (retrieval untouched).
   screen the top 300 -> chart the top 60 on full text -> rank by GROUNDED, IDF-WEIGHTED evidence.
"""
import sys, json, re, math, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db, llm, deep_analysis, webview, grounding

R = "/home/nimrod_rotem/patent-search-pilot/data/reports/adhoc-584455f78ae2"
rep = json.load(open(R + ".json")); view = json.load(open(R + ".view.json"))
BRIEF = rep["query"][:rep["query"].find("Drawings (figures analysed")].strip()
FEATS = rep["elements"]
TARGETS = ("US-10625955-B2", "DE-3724659-A1")
live_rank = {c["pub"]: c["rank"] for c in view["cards"]}
live_score = {c["pub"]: c["relevancy_score"] for c in view["cards"]}

con = db.connect(); con.autocommit = True; cur = con.cursor()
ranked = rep["ranked_families"][:300]
reps = webview.resolve_family_reps(cur, ranked)
STOP = set("a an the of and or to for with in on at by is are be as from that this it its which "
           "such said have has having comprising comprises method system device apparatus".split())
def toks(s): return {w for w in re.findall(r"[a-z]{5,}", (s or "").lower()) if w not in STOP}

cands = []
for i, fam in enumerate(ranked):
    r = reps.get(fam)
    if not r: continue
    pid = r["id"]
    cur.execute("select abstract from publications where id=%s", (pid,))
    ab = (cur.fetchone() or {}).get("abstract") or ""
    cur.execute("select text from claims where publication_id=%s order by claim_no limit 2", (pid,))
    cl = " ".join(x["text"] for x in cur.fetchall())
    # guard: an abstract sharing no word with the title or the claims is not this patent's abstract
    if ab and not (toks(ab) & (toks(r.get("title")) | toks(cl))):
        ab = "[abstract unavailable]"
    cands.append({"pub": r["publication_number"], "title": r.get("title") or "", "fusion": i + 1,
                  "text": (ab + "  " + cl)[:1400]})
print(f"== candidates from the LIVE ranked list: {len(cands)} ==")

SCR = ("You are a patent examiner SCREENING candidates against a target invention. For EACH "
       "numbered candidate give an integer 0-100 relevance score (90+ same invention, 70-89 most "
       "core elements, 40-69 related field, 1-39 same broad area, 0 unrelated). Judge on the "
       'technical substance shown, never the title alone. Return ONLY JSON {"results":'
       '[{"id":<n>,"score":<int>}]}, every id exactly once.')
B = 25
def sb(i):
    b = cands[i:i+B]
    out = llm.chat_json(SCR, f"TARGET INVENTION:\n{BRIEF}\n\nCANDIDATES:\n" +
                        "\n".join(f"[{j+1}] {c['title']}\n  {c['text']}" for j, c in enumerate(b)),
                        max_tokens=1600) or {}
    g = {}
    for x in (out.get("results") or []):
        try: j = int(x["id"]) - 1
        except Exception: continue
        if 0 <= j < len(b): g[b[j]["pub"]] = int(x.get("score") or 0)
    return g
t0 = time.time(); screen = {}
with ThreadPoolExecutor(max_workers=6) as ex:
    for g in ex.map(sb, range(0, len(cands), B)): screen.update(g)
srt = sorted(screen.items(), key=lambda t: -t[1])
srank = {p: i+1 for i, (p, _) in enumerate(srt)}
print(f"== screen: {len(screen)} in {time.time()-t0:.0f}s ==  " +
      "  ".join(f"{t}: #{srank.get(t)} score {screen.get(t)}" for t in TARGETS))

SYS = ("You are a patent examiner. For EACH numbered FEATURE of the target invention say what "
       "this reference discloses: verdict disclosed / partial / absent. When not absent you MUST "
       "give a VERBATIM quote copied exactly from the reference text (8-40 words). Return ONLY "
       'JSON {"rows":[{"id":<feature number>,"verdict":"...","quote":"..."}]}, one row per feature.')
def norm(s): return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()
TOP = [p for p, _ in srt[:60]]
for t in TARGETS:
    if t in screen and t not in TOP: TOP.append(t)
def chart(pub):
    ft = deep_analysis.full_text(pub)
    if ft["chars"] == 0:
        return {"pub": pub, "chars": 0, "feats": set(), "ungrounded": 0, "no_text": True}
    body = "\n\n".join(f"[{p['label']}] {p['text']}" for p in ft["passages"])[:180000]
    out = llm.chat_json(SYS, "TARGET INVENTION FEATURES:\n" +
                        "\n".join(f"[{i+1}] {f}" for i, f in enumerate(FEATS)) +
                        f"\n\nREFERENCE {pub} — {ft['title']}\nTEXT:\n{body}", max_tokens=2500) or {}
    good, bad = set(), 0
    for r in (out.get("rows") or []):
        if str(r.get("verdict") or "").lower() not in ("disclosed", "partial"): continue
        q = norm(r.get("quote") or "")
        try: fid = int(r.get("id")) - 1
        except Exception: continue
        if 0 <= fid < len(FEATS) and grounding.grounded(r.get("quote") or "", body): good.add(fid)
        else: bad += 1
    return {"pub": pub, "chars": ft["chars"], "feats": good, "ungrounded": bad, "no_text": False}
t0 = time.time()
with ThreadPoolExecutor(max_workers=8) as ex:
    charts = list(ex.map(chart, TOP))
print(f"== full-text charted {len(charts)} refs in {time.time()-t0:.0f}s "
      f"({sum(c['chars'] for c in charts):,} chars; {sum(1 for c in charts if c['no_text'])} had NO text) ==")

N = len(charts)
df = [sum(1 for c in charts if i in c["feats"]) or 1 for i in range(len(FEATS))]
print("\n== feature rarity across the charted set (this is what makes a hit distinctive) ==")
for i, f in enumerate(FEATS):
    print(f"   df={df[i]:3d}/{N}  idf={math.log(N/df[i]):.2f}  {f[:66]}")
def score(c): return sum(math.log(N / df[i]) for i in c["feats"])
final = sorted(charts, key=lambda c: (-score(c), -len(c["feats"])))
print()
print(f"{'#':>3} {'idf':>5} {'n':>2} {'pub':20s} {'live':>5} {'livescore':>9} {'screen':>6} {'fusion':>6}")
fus = {c["pub"]: c["fusion"] for c in cands}
for i, c in enumerate(final[:25], 1):
    mark = "   <<<" if c["pub"] in TARGETS else ""
    print(f"{i:3d} {score(c):5.1f} {len(c['feats']):2d} {c['pub']:20s} "
          f"{str(live_rank.get(c['pub'], '-')):>5s} {str(live_score.get(c['pub'], '-')):>9s} "
          f"{str(screen.get(c['pub'])):>6s} {str(fus.get(c['pub'])):>6s}{mark}")
print()
for t in TARGETS:
    p = next((i for i, c in enumerate(final, 1) if c["pub"] == t), None)
    print(f"  {t:18s} -> rank {p} of {len(final)}   (live: card {live_rank.get(t, 'NOT SHOWN')}, "
          f"fusion {fus.get(t)}, screen #{srank.get(t)})")
