"""E4: does judging on REAL TEXT (not a 900-char snippet) change the verdict?
    E5: a cheap wide screener over the top 300 ranked families."""
import sys, json, time
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db, llm, deep_analysis, webview

REP = "/home/nimrod_rotem/patent-search-pilot/data/reports/adhoc-584455f78ae2"
rep = json.load(open(REP + ".json"))
view = json.load(open(REP + ".view.json"))
BRIEF = rep["query"][:rep["query"].find("Drawings (figures analysed")].strip()

SCORE_SYS = (
    "You are a patent prior-art examiner SCORING one candidate reference against a target "
    "invention. Judge ONLY on technical substance in the reference text you are given. "
    "Give an integer SCORE 0-100: 90-100 = discloses essentially the same invention; "
    "70-89 = strongly relevant, discloses most core elements; 40-69 = related field, some "
    "overlapping features; 1-39 = same broad area, different problem/solution; 0 = unrelated. "
    'Return ONLY JSON {"score":<int>,"why":"<one sentence>"}.')

print("== E4: same reference, snippet-judged (LIVE) vs full-text-judged ==")
live = {c["pub"]: (c["relevancy_score"], c["relevancy_opinion"]) for c in view["cards"]}
for pub in ["US-10625955-B2", "DE-3724659-A1", "US-11999030-B2", "US-12115659-B1"]:
    ft = deep_analysis.full_text(pub)
    body = "\n\n".join(f"[{p['label']}] {p['text']}" for p in ft["passages"])[:180000]
    out = llm.chat_json(SCORE_SYS,
                        f"TARGET INVENTION:\n{BRIEF}\n\nCANDIDATE {pub} — {ft['title']}\n"
                        f"FULL TEXT ({ft['chars']} chars, {ft['n_claims']} claims, "
                        f"{ft['n_paragraphs']} paragraphs):\n{body}", max_tokens=500) or {}
    lv = live.get(pub)
    print(f"  {pub:18s} live(snippet)={str(lv[0]) if lv else 'not shown':>10s}   "
          f"full-text={str(out.get('score')):>4s}   chars={ft['chars']}")
    print(f"      full-text why: {str(out.get('why'))[:230]}")

print()
print("== E5: cheap LLM screener over the top 300 ranked families (title+abstract+claim 1) ==")
ranked = rep["ranked_families"][:300]
conn = db.connect(); conn.autocommit = True; cur = conn.cursor()
reps = webview.resolve_family_reps(cur, ranked)
cands = []
for fam in ranked:
    r = reps.get(fam)
    if not r:
        continue
    pid = r["id"]
    cur.execute("select abstract from publications where id=%s", (pid,))
    ab = (cur.fetchone() or {}).get("abstract") or ""
    cur.execute("select text from claims where publication_id=%s order by claim_no limit 1", (pid,))
    c1 = (cur.fetchone() or {}).get("text") or ""
    cands.append({"pub": r["publication_number"], "title": r.get("title") or "",
                  "text": (ab + "  " + c1)[:900]})
print(f"  resolved {len(cands)} of {len(ranked)} families")

SCR_SYS = ("You are a patent examiner SCREENING candidate references against a target invention. "
           "For EACH numbered candidate give an integer 0-100 relevance score (same scale: 90+ "
           "essentially the same invention, 70-89 most core elements, 40-69 related field, "
           "1-39 same broad area, 0 unrelated). Judge on technical substance, never the title "
           'alone. Return ONLY JSON {"results":[{"id":<n>,"score":<int>}]}, every id once.')
scores, B = {}, 25
t0 = time.time()
for i in range(0, len(cands), B):
    batch = cands[i:i + B]
    lines = [f"[{j+1}] {c['title']}\n  {c['text']}" for j, c in enumerate(batch)]
    out = llm.chat_json(SCR_SYS, f"TARGET INVENTION:\n{BRIEF}\n\nCANDIDATES:\n" +
                        "\n".join(lines), max_tokens=1500) or {}
    for r in (out.get("results") or []):
        try:
            j = int(r["id"]) - 1
        except Exception:
            continue
        if 0 <= j < len(batch):
            scores[batch[j]["pub"]] = int(r.get("score") or 0)
print(f"  screened {len(scores)} candidates in {time.time()-t0:.0f}s "
      f"({(len(cands)+B-1)//B} LLM calls)")
order = sorted(scores.items(), key=lambda t: -t[1])
print("  top 20 by screener:")
for k, (pub, sc) in enumerate(order[:20], 1):
    print(f"    {k:3d}. {sc:3d}  {pub}")
for pub in ("US-10625955-B2", "DE-3724659-A1"):
    pos = next((k for k, (p, _) in enumerate(order, 1) if p == pub), None)
    print(f"  {pub:18s} screener score={scores.get(pub)}  -> screener rank {pos} of {len(order)}"
          f"   (fusion rank was {[reps.get(f, {}).get('publication_number') for f in ranked].index(pub)+1 if pub in [reps.get(f, {}).get('publication_number') for f in ranked] else '-'})")
