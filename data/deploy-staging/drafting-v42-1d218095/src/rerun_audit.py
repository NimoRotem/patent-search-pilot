"""Re-run the M9 relevance / rationale / claim-chart audit on the post-OPS corpus.

Reproduces the ORIGINAL M9 method exactly (same 8 queries, same slugs, same judge rubrics in
src/audit.py, same 28+12 card split for the rationale audit) so the numbers are comparable.
Nothing in the judge or the metric definitions is changed.
"""
from __future__ import annotations
import json, sys, time
sys.path.insert(0, 'src')
from pathlib import Path

import db, audit, webview, enrich_display, goldset
from evaluate import subject_from as evaluate_subject
from config import DATA

REPORTS = DATA / "reports"
OUT = DATA / "eval"

GOLD_SLUGS = ["grabo_gripper_novelty", "schmalz_vacuum_clamp", "probst_kerb_lifter"]
ADHOC = json.loads((OUT / "audit_queries.json").read_text())

AUDIT_QUERIES = [
    ("gold:grabo_novelty", "grabo_gripper_novelty"),
    ("gold:schmalz_clamp", "schmalz_vacuum_clamp"),
    ("gold:probst_kerb", "probst_kerb_lifter"),
    ("ft:handheld_lifter", ADHOC["handheld_lifter"]["slug"]),
    ("ft:robotic_eoat", ADHOC["robotic_eoat"]["slug"]),
    ("ft:suction_pnp", ADHOC["suction_pnp"]["slug"]),
    ("edge:broad_vacuumgripper", ADHOC["broad_vacuumgripper"]["slug"]),
    ("edge:narrow_multifeature", ADHOC["narrow_multifeature"]["slug"]),
]

# rationale audit: same composition as the M9 run (28 grabo + 12 schmalz = 40 cards)
RAT_PLAN = [("grabo_gripper_novelty", 28), ("schmalz_vacuum_clamp", 12)]


def regen_all():
    """Regenerate all 8 audited reports with the current agent on the deepened corpus.

    The 3 gold reports on disk were written 2026-07-18 01:25, i.e. BEFORE the OPS backfill, so
    auditing them would measure stale output. Regenerated here with the same settings
    warm_reports.py uses (max_rounds=2, elements_per_round=3, ground=True).
    """
    from retrieval import Retriever
    from agent import CoverageAgent, AgentConfig
    import goldset as _gs
    R = Retriever()
    A = CoverageAgent(R)

    # --- the 3 gold-anchored reports ---
    gs = _gs.load()
    for e in gs["entries"]:
        if e["id"] not in GOLD_SLUGS:
            continue
        t = time.time()
        try:
            rep = A.run(e["query_text"], subject=evaluate_subject(e), mode=e["mode"],
                        cfg=AgentConfig(mode=e["mode"], max_rounds=2, elements_per_round=3,
                                        ground=True))
            (REPORTS / f"{e['id']}.json").write_text(json.dumps(rep, default=str, indent=1))
            (REPORTS / f"{e['id']}.view.json").unlink(missing_ok=True)
            print(f"  {e['id']:24s} OK {rep['n_families']} fams {time.time()-t:.0f}s", flush=True)
        except Exception as ex:
            import traceback; traceback.print_exc()
            print(f"  {e['id']:24s} ERROR {str(ex)[:150]}", flush=True)

    # --- the 5 free-text reports ---
    for label, d in ADHOC.items():
        slug, q, mode = d["slug"], d["query"], d["mode"]
        t = time.time()
        try:
            rep = A.run(q, subject=None, mode=mode,
                        cfg=AgentConfig(mode=mode, max_rounds=2, elements_per_round=3, ground=True))
            (REPORTS / f"{slug}.json").write_text(json.dumps(rep, default=str, indent=1))
            (REPORTS / f"{slug}.view.json").unlink(missing_ok=True)
            print(f"  {label:24s} OK {rep['n_families']} fams {time.time()-t:.0f}s", flush=True)
        except Exception as ex:
            import traceback; traceback.print_exc()
            print(f"  {label:24s} ERROR {str(ex)[:150]}", flush=True)


def gen_rationales(slug, n):
    """Replicate webapp's /api/ref rationale path for the top-n cards of a report."""
    import webapp
    rep = json.loads((REPORTS / f"{slug}.json").read_text())
    q = rep.get("query", "")
    view = webview.build_view(rep, top_n=max(n, 12))
    cards = view["cards"][:n]
    qv = webapp._query_vec(slug, q)
    made = 0
    for c in cards:
        pub = c["pub"]
        disp = enrich_display.enrich_for_display(pub)
        matched = None
        with db.cursor() as cur:
            cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
            row = cur.fetchone()
            if row:
                matched = webview.match_in_pub(cur, row["id"], qv)
        biblio_txt = f"{pub} {disp.get('title') or ''}. {disp.get('abstract') or ''}"
        webapp._rationale(slug, pub, q, rep.get("elements", []), biblio_txt,
                          (matched or {}).get("text"))
        made += 1
    print(f"  rationales {slug}: {made}", flush=True)
    return [c["pub"] for c in cards]


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"

    if stage in ("all", "regen"):
        print("[1] regenerating all 8 audited reports on the deepened corpus", flush=True)
        regen_all()

    if stage in ("all", "audit"):
        print("[2] relevance audit (precision@10, 8 queries)", flush=True)
        rel = audit.audit_relevance(AUDIT_QUERIES, n=10)
        (REPORTS / "_audit_relevance_POST_OPS.json").write_text(json.dumps(rel, indent=1, default=str))

        print("[3] rationale audit (40 cards, same 28+12 split)", flush=True)
        rows, tally = [], {}
        for slug, n in RAT_PLAN:
            pubs = gen_rationales(slug, n)
            for pub in pubs:
                j = audit.judge_rationale(slug, pub)
                if not j:
                    continue
                j["slug"] = slug
                rows.append(j)
                tally[j["verdict"]] = tally.get(j["verdict"], 0) + 1
        res = {"tally": tally, "n": len(rows), "rows": rows}
        (REPORTS / "_audit_rationale_POST_OPS.json").write_text(json.dumps(res, indent=1, default=str))
        bad = tally.get("overclaims", 0) + tally.get("hallucinates", 0)
        print(f"  rationale tally={tally} n={len(rows)} "
              f"overclaim+hallucinate={bad}/{len(rows)} = {bad/max(1,len(rows)):.1%}", flush=True)

        print("[4] claim-chart cell audit", flush=True)
        cells = audit.audit_chart_cells(["grabo_gripper_novelty", "schmalz_vacuum_clamp"])
        (REPORTS / "_audit_cells_POST_OPS.json").write_text(json.dumps(cells, indent=1, default=str))

    print("AUDIT DONE", flush=True)


if __name__ == "__main__":
    main()
