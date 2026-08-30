import sys, json, re
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import llm, deep_analysis
R = "/home/nimrod_rotem/patent-search-pilot/data/reports/adhoc-584455f78ae2.json"
rep = json.load(open(R)); FEATS = rep["elements"]
SYS = ("You are a patent examiner. For EACH numbered FEATURE say what this reference discloses: "
       "verdict disclosed / partial / absent. When not absent give a VERBATIM quote copied "
       'exactly from the reference text (8-40 words). Return ONLY JSON {"rows":[{"id":<n>,'
       '"verdict":"...","quote":"..."}]}.')
def norm(s): return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()
for pub in ("DE-3724659-A1", "US-10625955-B2"):
    ft = deep_analysis.full_text(pub)
    body = "\n\n".join(f"[{p['label']}] {p['text']}" for p in ft["passages"])[:180000]
    hay = norm(body)
    out = llm.chat_json(SYS, "TARGET INVENTION FEATURES:\n" +
                        "\n".join(f"[{i+1}] {f}" for i, f in enumerate(FEATS)) +
                        f"\n\nREFERENCE {pub} — {ft['title']}\nTEXT:\n{body}", max_tokens=2500) or {}
    print(f"\n=== {pub} ({ft['chars']} chars) ===")
    for r in (out.get("rows") or []):
        v = str(r.get("verdict") or "").lower()
        if v == "absent": continue
        try: i = int(r["id"]) - 1
        except Exception: continue
        q = norm(r.get("quote") or "")
        ok = len(q.split()) >= 5 and q in hay
        print(f"  [{'OK ' if ok else 'DROP'}] {v:9s} {FEATS[i][:56]:56s} | {str(r.get('quote'))[:100]}")
