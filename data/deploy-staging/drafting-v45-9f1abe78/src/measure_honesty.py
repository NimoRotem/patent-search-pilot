"""Before/after measurement for the rationale-grounding and claim-chart-accuracy fixes.

METHOD (deliberately conservative, and comparable to the existing audit):
  * Same reports, same slugs, same 28+12 card split, same judge rubrics in src/audit.py as the
    run that produced _audit_rationale_POST_OPS.json (26.3%) and _audit_cells_POST_OPS.json (58%).
  * NO report regeneration. Re-running the searches would change the candidate set and destroy
    comparability (and cost real API budget). Only the rationale/chart layers are recomputed.
  * Writes to *_NEWFIX.json — never overwrites the git-tracked audit artefacts.

Rationale is measured in three variants so the harness bug and the real regression are separated:
  A  old rationale, old judge text          -> reproduces the 26.3% baseline
  B  old rationale, generator's actual text -> isolates the generator/judge desync alone
  C  new rationale, generator's actual text -> the fix

Claim chart is measured as: of the cells that STILL RENDER AS COVERAGE after the change, what
fraction does the independent examiner judge reject? That is the number that matters, because a
cell demoted to weak/unrelated/uncertain no longer makes a coverage claim to the reader.
"""
from __future__ import annotations
import json, sys, shutil, time
sys.path.insert(0, 'src')
from pathlib import Path

import db, audit, webview, enrich_display, llm, webapp, claim_chart
from config import DATA

REPORTS = DATA / "reports"
RATDIR = DATA / "rationale"
RAT_PLAN = [("grabo_gripper_novelty", 28), ("schmalz_vacuum_clamp", 12)]
CELL_SLUGS = ["grabo_gripper_novelty", "schmalz_vacuum_clamp"]


def gen_inputs(slug, n):
    """Exactly webapp's /api/ref rationale inputs, as rerun_audit.gen_rationales builds them."""
    rep = json.loads((REPORTS / f"{slug}.json").read_text())
    q = rep.get("query", "")
    view = webview.build_view(rep, top_n=max(n, 12))
    cards = view["cards"][:n]
    qv = webapp._query_vec(slug, q)
    out = []
    for c in cards:
        pub = c["pub"]
        disp = enrich_display.enrich_for_display(pub)
        matched = None
        with db.cursor() as cur:
            cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
            row = cur.fetchone()
            if row:
                matched = webview.match_in_pub(cur, row["id"], qv)
        biblio = f"{pub} {disp.get('title') or ''}. {disp.get('abstract') or ''}"
        out.append({"pub": pub, "query": q, "elements": rep.get("elements", []),
                    "biblio": biblio, "matched": (matched or {}).get("text")})
    return out


def judge_with_text(slug, pub, shown):
    """audit.judge_rationale, but grading against a caller-supplied reference text."""
    c = RATDIR / f"{slug}__{pub}.json"
    if not c.exists():
        return None
    rat = json.loads(c.read_text())
    why = (rat.get("why") or "").strip()
    if not why:
        return None
    usr = (f"REFERENCE {pub} ACTUAL TEXT:\n{shown}\n\n"
           f"AI RATIONALE:\nwhy: {why}\nreads on: {json.dumps(rat.get('reads_on') or [])}")
    out = llm.chat_json(audit.RAT_SYS, usr, max_tokens=200) or {}
    v = (out.get("verdict") or "vague").lower()
    if v not in ("accurate", "overclaims", "hallucinates", "vague"):
        v = "vague"
    return {"verdict": v, "reason": out.get("reason", "")[:140], "pub": pub, "slug": slug,
            "why": why[:200], "reads_on": rat.get("reads_on") or []}


def tally(rows):
    t = {}
    for r in rows:
        t[r["verdict"]] = t.get(r["verdict"], 0) + 1
    n = len(rows)
    bad = t.get("overclaims", 0) + t.get("hallucinates", 0)
    return {"tally": t, "n": n, "bad": bad, "rate": round(bad / n, 4) if n else None}


def main():
    res = {}
    inputs = {slug: gen_inputs(slug, n) for slug, n in RAT_PLAN}

    # ---------- A: baseline reproduction (old rationales, old judge text) ----------
    print("[A] baseline: old rationales, judge rebuilds its own text", flush=True)
    rows_a = []
    for slug, _ in RAT_PLAN:
        for it in inputs[slug]:
            j = audit.judge_rationale(slug, it["pub"])   # caches have no _source_text -> old path
            if j:
                j["slug"] = slug
                rows_a.append(j)
    res["A_baseline"] = tally(rows_a)
    print("   ", res["A_baseline"], flush=True)

    # ---------- B: desync isolated (old rationales, generator's real text) ----------
    print("[B] desync isolated: old rationales, judged on the generator's actual input", flush=True)
    rows_b = []
    for slug, _ in RAT_PLAN:
        for it in inputs[slug]:
            shown = f"{it['biblio']} {it['matched'] or ''}".strip()[:4000]
            j = judge_with_text(slug, it["pub"], shown)
            if j:
                rows_b.append(j)
    res["B_desync_fixed"] = tally(rows_b)
    print("   ", res["B_desync_fixed"], flush=True)

    # ---------- C: the fix (new rationales, generator's real text) ----------
    print("[C] fix: regenerate rationales with span+bigram grounding and the why-verifier", flush=True)
    bak = DATA / f"rationale.BEFORE_FIX"
    if not bak.exists():
        shutil.copytree(RATDIR, bak)
        print(f"    backed up old rationale caches -> {bak}", flush=True)
    made = 0
    for slug, _ in RAT_PLAN:
        for it in inputs[slug]:
            (RATDIR / f"{slug}__{it['pub']}.json").unlink(missing_ok=True)
            webapp._rationale(slug, it["pub"], it["query"], it["elements"],
                              it["biblio"], it["matched"])
            made += 1
    print(f"    regenerated {made} rationales", flush=True)
    rows_c = []
    for slug, _ in RAT_PLAN:
        for it in inputs[slug]:
            j = audit.judge_rationale(slug, it["pub"])   # now finds _source_text
            if j:
                j["slug"] = slug
                rows_c.append(j)
    res["C_fixed"] = tally(rows_c)
    print("   ", res["C_fixed"], flush=True)

    # how many were rewritten / stripped by the why-verifier
    states = {}
    for slug, _ in RAT_PLAN:
        for it in inputs[slug]:
            p = RATDIR / f"{slug}__{it['pub']}.json"
            if p.exists():
                st = json.loads(p.read_text()).get("why_grounding", "?")
                states[st] = states.get(st, 0) + 1
    res["why_verifier_states"] = states
    print("    why-verifier:", states, flush=True)

    # ---------- claim chart ----------
    print("[D] claim chart: verify the matrix, then judge what still renders as coverage", flush=True)
    chart_res = []
    for slug in CELL_SLUGS:
        rep = json.loads((REPORTS / f"{slug}.json").read_text())
        chart = webview.build_claim_chart(rep)
        claim_chart.verify_matrix(chart, rep)
        ev = rep.get("element_evidence", {})
        before, after = [], []
        for row in chart["rows"]:
            el = row["element"]
            for cell in row["cells"]:
                if not cell.get("covered"):
                    continue
                raw = None
                for h in ev.get(el, []):
                    if h.get("pub") == cell["pub"] and isinstance(h.get("coord"), dict):
                        raw = h["coord"]; break
                if not raw:
                    continue
                j = audit.judge_cell(el, cell["pub"], raw)     # SAME judge as the 58% baseline
                j["element"] = el; j["verify"] = cell.get("verify")
                before.append(j)
                if cell.get("verify") == "discloses":
                    after.append(j)
        def fp(rows):
            n = len(rows)
            bad = sum(1 for r in rows if r["verdict"] in ("weak", "unrelated"))
            return {"n": n, "false_positives": bad, "rate": round(bad / n, 4) if n else None}
        chart_res.append({"slug": slug, "before": fp(before), "after_shown_as_coverage": fp(after),
                          "verification": chart.get("verification"),
                          "cells": [{"element": c["element"][:50], "pub": c["pub"],
                                     "judge": c["verdict"], "verify": c["verify"]} for c in before]})
        print(f"    {slug}: before={fp(before)} after={fp(after)}", flush=True)
    allb = [c for r in chart_res for c in r["cells"]]
    tot_b = {"n": len(allb),
             "false_positives": sum(1 for c in allb if c["judge"] in ("weak", "unrelated"))}
    tot_b["rate"] = round(tot_b["false_positives"] / tot_b["n"], 4) if tot_b["n"] else None
    shown = [c for c in allb if c["verify"] == "discloses"]
    tot_a = {"n": len(shown),
             "false_positives": sum(1 for c in shown if c["judge"] in ("weak", "unrelated"))}
    tot_a["rate"] = round(tot_a["false_positives"] / tot_a["n"], 4) if tot_a["n"] else None
    res["chart"] = {"per_slug": chart_res, "TOTAL_before": tot_b,
                    "TOTAL_after_shown_as_coverage": tot_a}
    print("    TOTAL before:", tot_b, "\n    TOTAL after :", tot_a, flush=True)

    (REPORTS / "_MEASURE_NEWFIX.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "chart"}, indent=1, default=str))
    print("WROTE data/reports/_MEASURE_NEWFIX.json")


if __name__ == "__main__":
    main()
