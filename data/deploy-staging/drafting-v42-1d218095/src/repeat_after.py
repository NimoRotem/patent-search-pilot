"""Repeat the AFTER arm N times to size the run-to-run noise of the 40-card overclaim metric.

The point: BEFORE and AFTER differed by 2 cards (1 vs 3 bad out of 40). If re-running the
SAME arm moves by a comparable amount, the harness cannot resolve this change and any claimed
win or loss would be noise.
"""
from __future__ import annotations
import json, sys
sys.path.insert(0, 'src')
import audit, webapp
from measure_claimsfix import gen_inputs, tally, RAT_PLAN, RATDIR, REPORTS

REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def main():
    inputs = {slug: gen_inputs(slug, n) for slug, n in RAT_PLAN}
    runs = []
    for i in range(REPS):
        for slug, _ in RAT_PLAN:
            for it in inputs[slug]:
                (RATDIR / f"{slug}__{it['pub']}.json").unlink(missing_ok=True)
        n_reads = []
        for slug, _ in RAT_PLAN:
            for it in inputs[slug]:
                r = webapp._rationale(slug, it["pub"], it["query"], it["elements"],
                                      it["biblio"], passages=it["passages"])
                n_reads.append(len(r.get("reads_on") or []))
        rows = []
        for slug, _ in RAT_PLAN:
            for it in inputs[slug]:
                j = audit.judge_rationale(slug, it["pub"])
                if j:
                    j["slug"] = slug
                    rows.append(j)
        t = tally(rows)
        t["avg_reads_on"] = round(sum(n_reads) / max(len(n_reads), 1), 2)
        runs.append(t)
        print(f"[AFTER repeat {i+1}] {t}", flush=True)
    rates = [r["rate"] for r in runs if r["rate"] is not None]
    out = {"runs": runs, "rates": rates,
           "spread": round(max(rates) - min(rates), 4) if rates else None}
    (REPORTS / "_MEASURE_CLAIMSFIX_REPEAT.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1), flush=True)


if __name__ == "__main__":
    main()
