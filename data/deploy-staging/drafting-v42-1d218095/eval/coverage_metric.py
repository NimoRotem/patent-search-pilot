"""THE primary metric: weighted disclosure coverage@K, against a frozen list and fixed weights.

    coverage@K = sum(weight of disclosures supported by >= 1 qualifying reference in the top K)
                 ---------------------------------------------------------------------------
                                sum(weight of all evaluated disclosures)

Read the denominator from the FROZEN list (eval/disclosures_frozen/), never from the report. A
report carries whatever disclosure list that run happened to generate and whatever weights that
run's candidate set implied; scoring a run against its own artefacts measures nothing, because a
change to retrieval moves the denominator underneath the numerator.

`qualifying` is deliberately strict and reported alongside the number. Until the evidence-cell
audit (plan section 6) establishes positive-evidence precision, a "disclosed" verdict is a model
output, not a fact, so the metric is reported at two strictnesses and both move together or the
result is not trusted:

    verdict_only   any grounded row with verdict disclosed or partial
    verified       the same, but requiring the grounding gate to have passed and the refuter not
                   to have overturned it

Reported broken out by disclosure kind, because they are not the same claim on the reader:
independent-claim limitations decide validity, potential claims are contingency, and averaging
them into one number hides which one moved.

    python eval/coverage_metric.py --tag v13
    python eval/coverage_metric.py --tag v14 --subject suction_chuck --k 10,20,50
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import freeze_disclosures as FZ  # noqa: E402

#  Verdict strengths that count as support. `uncertain` is a disclosure an independent refuter
#  declined to confirm, so it does NOT count as covering a disclosure however it is ranked.
SUPPORTING = {"disclosed", "partial"}
STRICT_GROUNDING = {"verified", "ok", "grounded", "pass", "passed", True}

KINDS = ["independent_limitation", "combination", "dependent_limitation", "potential_claim"]


def _norm_text(s):
    return " ".join(str(s or "").split()).lower()


def support_map(report, strict=False):
    """{disclosure text (normalised) -> [publications supporting it]}, in ranked order."""
    dr = report.get("deep_rank") or {}
    by, order = dr.get("by_pub") or {}, dr.get("order") or []
    out = {}
    for pub in order:
        for c in ((by.get(pub) or {}).get("covered") or []):
            if c.get("verdict") not in SUPPORTING:
                continue
            if strict:
                g = c.get("grounding")
                if g is not None and g not in STRICT_GROUNDING:
                    continue
                if c.get("refuted") is True:
                    continue
            out.setdefault(_norm_text(c.get("item")), []).append(pub)
    return out


def coverage(report, frozen, ks=(10, 20, 50), strict=False):
    """-> {k: {"overall": frac, "by_kind": {...}, "unanswered": [...]}} plus the denominator."""
    dr = report.get("deep_rank") or {}
    order = dr.get("order") or []
    pos = {p: i + 1 for i, p in enumerate(order)}
    sup = support_map(report, strict=strict)
    items = frozen["disclosures"]
    total = sum(d["weight"] for d in items) or 1.0

    out = {"denominator_weight": round(total, 3), "n_disclosures": len(items),
            "disclosure_list_version": frozen.get("disclosure_list_version"),
            "content_hash": frozen.get("content_hash"), "strict": strict, "at": {}}
    for k in ks:
        got, by_kind, unanswered = 0.0, {}, []
        kw_total = {}
        for d in items:
            w = d["weight"]
            kw_total[d["kind"]] = kw_total.get(d["kind"], 0.0) + w
            pubs = sup.get(_norm_text(d["text"]), [])
            best = min([pos[p] for p in pubs if p in pos] or [10 ** 9])
            if best <= k:
                got += w
                by_kind[d["kind"]] = by_kind.get(d["kind"], 0.0) + w
            elif k == max(ks):
                unanswered.append({"text": d["text"], "kind": d["kind"], "weight": w,
                                   "supported_at_rank": (best if best < 10 ** 9 else None)})
        out["at"][k] = {
            "overall": round(got / total, 4),
            "by_kind": {kk: round(by_kind.get(kk, 0.0) / (kw_total.get(kk) or 1.0), 4)
                        for kk in KINDS if kk in kw_total},
            "n_unanswered": len([d for d in items
                                 if min([pos[p] for p in sup.get(_norm_text(d["text"]), [])
                                         if p in pos] or [10 ** 9]) > k]),
        }
        if unanswered:
            out["unanswered"] = unanswered
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--subject", default="all")
    ap.add_argument("--k", default="10,20,50")
    args = ap.parse_args()
    ks = tuple(int(x) for x in args.k.split(","))
    subs = json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]
    if args.subject != "all":
        subs = [s for s in subs if s["id"] == args.subject]

    #  ONE BUDGET OR NO AVERAGE. The denominator is only comparable across subjects if every
    #  frozen list was extracted under the same limits. Measured: raising the output budget from
    #  6,000 to 24,000 tokens took two subjects from 0 disclosures to 195, against a median of 39
    #  for lists frozen under the old budget. Averaging a 195-item denominator against a 39-item
    #  one produces a number that looks like coverage and is really a mixture of two rulers, and
    #  nothing about the output would show it. Refuse instead of reporting it.
    _budgets = {}
    for sub in subs:
        fz = FZ.load(sub["id"])
        if fz and fz.get("disclosures"):
            _budgets.setdefault(
                json.dumps(fz.get("extraction_budget") or {}, sort_keys=True), []
            ).append(sub["id"])
    if len(_budgets) > 1 and args.subject == "all":
        print("REFUSING TO AVERAGE: the frozen lists were extracted under different budgets, so "
              "their denominators are not comparable.")
        for b, ids in sorted(_budgets.items(), key=lambda kv: -len(kv[1])):
            print(f"  {len(ids):>3d} subject(s) at {b}")
            print(f"      e.g. {', '.join(ids[:6])}")
        print("Re-freeze every subject under one budget "
              "(python eval/freeze_disclosures.py --force) and run this again.")
        raise SystemExit(2)

    tot = {k: [0.0, 0.0] for k in ks}
    print(f"{'subject':16s} {'n':>4s} " + " ".join(f"{'@' + str(k):>7s}" for k in ks)
          + f" {'ind@50':>7s} {'crit unans':>11s}")
    for sub in subs:
        path = os.path.join(ROOT, "data", "reports", f"bench-{sub['id']}-{args.tag}.json")
        frozen = FZ.load(sub["id"])
        if not os.path.exists(path) or not frozen:
            print(f"{sub['id']:16s} {'-':>4s}  "
                  f"{'no report' if not os.path.exists(path) else 'NOT FROZEN'}")
            continue
        rep = json.load(open(path))
        c = coverage(rep, frozen, ks=ks)
        ind = c["at"][max(ks)]["by_kind"].get("independent_limitation")
        crit = sum(1 for u in c.get("unanswered", [])
                   if u["kind"] in ("independent_limitation", "combination"))
        print(f"{sub['id']:16s} {c['n_disclosures']:>4d} "
              + " ".join(f"{c['at'][k]['overall']:>7.1%}" for k in ks)
              + f" {(ind if ind is not None else 0):>7.1%} {crit:>11d}")
        for k in ks:
            tot[k][0] += c["at"][k]["overall"] * c["denominator_weight"]
            tot[k][1] += c["denominator_weight"]
    print(f"\n{'WEIGHTED TOTAL':16s} {'':>4s} "
          + " ".join(f"{(tot[k][0] / (tot[k][1] or 1)):>7.1%}" for k in ks))
    print("\ndenominator is the FROZEN disclosure list with weights fixed by kind; "
          "candidate-derived rarity is used for ranking only.")


if __name__ == "__main__":
    main()
