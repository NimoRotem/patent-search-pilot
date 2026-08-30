"""Before/after measurement for the 'why relevant' full-text fix (BUG 2).

METHOD — deliberately the same shape as measure_honesty.py so the numbers are comparable:
  * Same reports, same slugs, same 28+12 = 40 card split, same judge rubric (audit.RAT_SYS).
  * NO report regeneration: re-running the searches would change the candidate set, destroy
    comparability, and spend real SerpApi budget. Only the rationale layer is recomputed.
  * BOTH arms are judged the same way (audit.judge_rationale, which grades against the
    generator's own `_source_text`). That holds the generator/judge desync fixed in both arms,
    so the delta isolates ONE variable: what reference text the generator was shown.

  BEFORE = title + abstract + the single best-matching chunk   (the shipped behaviour)
  AFTER  = title + abstract + independent claims + the N nearest claim/description passages

Writes data/reports/_MEASURE_CLAIMSFIX.json — a NEW filename, so no git-tracked audit
artefact (RELIABILITY.json, _MEASURE_NEWFIX.json, *_POST_OPS.json) is overwritten.

CAVEAT, repeated from RELIABILITY.json: the generator, the `why` verifier and the auditing
judge are all gemini-2.5-flash. They share blind spots, so this measures self-consistency
against the shown text, not ground truth.
"""
from __future__ import annotations
import json, sys, shutil
sys.path.insert(0, 'src')
from pathlib import Path

import db, audit, webview, enrich_display, webapp
from config import DATA

REPORTS = DATA / "reports"
RATDIR = DATA / "rationale"
RAT_PLAN = [("grabo_gripper_novelty", 28), ("schmalz_vacuum_clamp", 12)]
OUT = REPORTS / "_MEASURE_CLAIMSFIX.json"


def gen_inputs(slug, n):
    rep = json.loads((REPORTS / f"{slug}.json").read_text())
    q = rep.get("query", "")
    view = webview.build_view(rep, top_n=max(n, 12))
    cards = view["cards"][:n]
    qv = webapp._query_vec(slug, q)
    out = []
    for c in cards:
        pub = c["pub"]
        disp = enrich_display.enrich_for_display(pub)
        matched, passages = None, []
        with db.cursor() as cur:
            cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
            row = cur.fetchone()
            if row:
                secs = webview.sections(cur, row["id"])
                matched = webview.match_in_pub(cur, row["id"], qv)
                passages = webapp.ref_passages(cur, row["id"], qv, secs)
        biblio = f"{pub} {disp.get('title') or ''}. {disp.get('abstract') or ''}"
        out.append({"pub": pub, "query": q, "elements": rep.get("elements", []),
                    "biblio": biblio, "matched": (matched or {}).get("text"),
                    "passages": passages})
    return out


def tally(rows):
    t = {}
    for r in rows:
        t[r["verdict"]] = t.get(r["verdict"], 0) + 1
    n = len(rows)
    bad = t.get("overclaims", 0) + t.get("hallucinates", 0)
    return {"tally": t, "n": n, "bad": bad, "rate": round(bad / n, 4) if n else None}


def run_arm(label, inputs, use_passages):
    """Wipe the 40 rationale caches, regenerate them under one arm, judge them."""
    rows, meta = [], []
    for slug, _ in RAT_PLAN:
        for it in inputs[slug]:
            (RATDIR / f"{slug}__{it['pub']}.json").unlink(missing_ok=True)
    for slug, _ in RAT_PLAN:
        for it in inputs[slug]:
            if use_passages:
                r = webapp._rationale(slug, it["pub"], it["query"], it["elements"],
                                      it["biblio"], passages=it["passages"])
            else:
                r = webapp._rationale(slug, it["pub"], it["query"], it["elements"],
                                      it["biblio"], it["matched"])
            meta.append({"pub": it["pub"], "slug": slug,
                         "basis": r.get("text_basis"), "n_passages": r.get("n_passages"),
                         "citations": r.get("citations") or [],
                         "source_chars": len(r.get("_source_text") or ""),
                         "why": (r.get("why") or "")[:400],
                         "reads_on": r.get("reads_on") or []})
    for slug, _ in RAT_PLAN:
        for it in inputs[slug]:
            j = audit.judge_rationale(slug, it["pub"])
            if j:
                j["slug"] = slug
                rows.append(j)
    t = tally(rows)
    print(f"[{label}] {t}", flush=True)
    return t, rows, meta


def main():
    backup = DATA / "rationale.BEFORE_CLAIMSFIX"
    if RATDIR.exists() and not backup.exists():
        shutil.copytree(RATDIR, backup)
        print(f"backed up rationale caches -> {backup}", flush=True)
    RATDIR.mkdir(parents=True, exist_ok=True)

    inputs = {slug: gen_inputs(slug, n) for slug, n in RAT_PLAN}
    res = {"method": __doc__.strip().splitlines()[0]}

    t_b, rows_b, meta_b = run_arm("BEFORE title+abstract+1 chunk", inputs, use_passages=False)
    t_a, rows_a, meta_a = run_arm("AFTER  claims+description passages", inputs, use_passages=True)

    basis_before = {}
    basis_after = {}
    for m in meta_b:
        basis_before[m["basis"]] = basis_before.get(m["basis"], 0) + 1
    for m in meta_a:
        basis_after[m["basis"]] = basis_after.get(m["basis"], 0) + 1

    res["BEFORE"] = t_b
    res["AFTER"] = t_a
    res["delta_rate"] = (round(t_a["rate"] - t_b["rate"], 4)
                         if t_b["rate"] is not None and t_a["rate"] is not None else None)
    res["text_basis_before"] = basis_before
    res["text_basis_after"] = basis_after
    res["avg_source_chars"] = {
        "before": round(sum(m["source_chars"] for m in meta_b) / max(len(meta_b), 1)),
        "after": round(sum(m["source_chars"] for m in meta_a) / max(len(meta_a), 1))}
    res["cited_claim_or_body_after"] = sum(
        1 for m in meta_a if any((c.get("label") or "").startswith(("claim", "paragraph"))
                                 for c in m["citations"]))
    res["rows_before"] = rows_b
    res["rows_after"] = rows_a
    res["meta_after"] = meta_a
    res["meta_before"] = meta_b
    res["caveat"] = ("generator, why-verifier and judge are all gemini-2.5-flash; they share "
                     "blind spots, so this is self-consistency against the shown text, not "
                     "ground truth")
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print("wrote", OUT, flush=True)
    print(json.dumps({k: res[k] for k in
                      ("BEFORE", "AFTER", "delta_rate", "text_basis_before",
                       "text_basis_after", "avg_source_chars",
                       "cited_claim_or_body_after")}, indent=1), flush=True)


if __name__ == "__main__":
    main()
