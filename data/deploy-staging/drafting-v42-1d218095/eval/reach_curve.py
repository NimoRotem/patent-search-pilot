"""Is prior-art search a RANKING problem or a QUERY-GENERATION problem?

THEORY
------
Coverage of the answer space is a function of the number of GENUINELY DISTINCT query
formulations, not of the depth of any one of them.

The evidence for stating it: 99.3% of the art examiners cite in this field is already in this
corpus, and ten of twelve cited documents measured on EP 3 707 092 sit beyond the 50,000 nearest
chunks to the brief's vector, at cosine 0.57-0.71. No practical widening of one query reaches
them. But a document unreachable from ONE vector may sit at rank 20 from a differently conceived
one, because it is a different point in the space that is being asked about.

The pipeline already runs ~13 whole-invention formulations (essence, five alternatives, the brief,
the claims) plus 8 element queries. They are all paraphrases of the SAME document written by the
same model from the same source, so they land in much the same neighbourhood. Meanwhile
external.plan() generates 6-9 product-neutral, PROBLEM-shaped queries in other fields' vocabulary
-- and sends them only to the external APIs. The local corpus, which holds 99.3% of the answer,
never sees them.

WHAT THIS MEASURES
------------------
Cumulative REACH of the cited families -- does the document appear in the channel's output at all
-- as formulations are added, arm by arm:

    A  the brief alone                                  (what one query buys)
    B  + the existing query set: essence, alts, claims  (paraphrase diversity)
    C  + element queries                                (part-level diversity)
    D  + the problem-shaped aspects, run LOCALLY        (conceptual diversity)
    E  + claim-level lexical on distinctive claim terms (a non-embedding signal)

Reach, not final rank, on purpose: reach is the necessary condition. If diversity moves it, the
reranker over the enlarged pool becomes the precision tool and there is something to rank. If
diversity does NOT move it, the art is unreachable by query formulation and the theory is wrong.

    python eval/reach_curve.py --subject ep3707092
    python eval/reach_curve.py --subject all --arms ABCDE
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import db          # noqa: E402
import embed       # noqa: E402
import external    # noqa: E402
import query_set   # noqa: E402
import webapp      # noqa: E402
import citation_recall as CR  # noqa: E402


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def gold_families(cur, citations):
    """{family_key: publication} for the cited documents this corpus holds."""
    res = CR.resolve(cur, citations)
    out = {}
    for p, r in res.items():
        if r:
            out.setdefault(r["fam"], r["pub"])
    return out


def fam_of(cur, pids):
    if not pids:
        return {}
    cur.execute("""SELECT id, COALESCE(NULLIF(simple_family_id,''), publication_number) fam
                   FROM publications WHERE id = ANY(%s)""", (list(pids),))
    return {r["id"]: r["fam"] for r in cur.fetchall()}


def dense_pass(r, text, subject, mode):
    """One dense retrieval pass at the wide seed profile -> [publication_id]."""
    qv = embed.embed_query(text[:8000])
    return [pid for pid, _s in r.channel_dense(qv, subject=subject, mode=mode)]


def brief_dense_pass(r, text, subject, mode):
    qv = embed.embed_query(text[:8000])
    return [pid for pid, _s in r.channel_brief_dense(qv, subject=subject, mode=mode)]


def claim_lexical_pass(r, text, subject, mode):
    """Claim-level lexical: distinctive terms from the invention, matched inside CLAIM text only.

    Orthogonal to embeddings on purpose. Examiner citations often share a distinctive claim TERM
    (venturi, bellows, ejector, silencer) with the subject while being semantically distant enough
    that no vector search reaches them.
    """
    return [pid for pid, _s in r.channel_claim_bm25(text, subject=subject, mode=mode)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default="ep3707092")
    ap.add_argument("--arms", default="ABCDE")
    ap.add_argument("--tag", default="v13", help="which finished report to take the brief from")
    args = ap.parse_args()

    subs = json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]
    if args.subject != "all":
        subs = [s for s in subs if s["id"] == args.subject]
    if not subs:
        raise SystemExit(f"unknown subject {args.subject}")

    r = webapp.retriever()
    r.scan_profile(wide=True)
    con = db.connect()
    con.autocommit = True
    cur = con.cursor()

    grand = {}
    for sub in subs:
        rep_path = os.path.join(ROOT, "data", "reports", f"bench-{sub['id']}-{args.tag}.json")
        if not os.path.exists(rep_path):
            print(f"{sub['id']}: no report {rep_path}, skipped")
            continue
        rep = json.load(open(rep_path))
        brief = query_set.retrieval_text(rep.get("query") or "")
        subject = external.subject_from_doc(
            (rep.get("query_document") or {}).get("publication_number") or "")
        gold = gold_families(cur, sub["citations"])
        print(f"\n{'=' * 78}\n{sub['id']}: {len(gold)} cited families in corpus\n{'=' * 78}")

        #  ---- build the formulations per arm ----------------------------------------------
        specs = query_set.build(brief, claims=(rep.get("query_document") or {}).get("claims") or [])
        whole = [s.text for s in query_set.seed_specs(specs)]
        elements = [s.text for s in specs if s.kind == "element"]
        try:
            plan = external.plan(specs, brief=brief)
            aspects = [a["blurb"] for a in plan["aspects"] if a.get("blurb")]
            aspects += [a["problem"] for a in plan["aspects"] if a.get("problem")]
        except Exception:
            aspects = []

        arms = {
            "A": ("brief alone", [("dense", brief)]),
            "B": ("+ query set (essence, alts, claims)",
                  [("dense", t) for t in whole if t.strip() != brief.strip()]),
            "C": ("+ element queries", [("dense", t) for t in elements]),
            "D": ("+ problem-shaped aspects, run LOCALLY",
                  [("dense", t) for t in aspects] + [("brief_dense", t) for t in aspects]),
            "E": ("+ claim-level lexical", [("claim_bm25", brief)]
                  + [("claim_bm25", t) for t in elements[:6]]),
        }
        runner = {"dense": dense_pass, "brief_dense": brief_dense_pass,
                  "claim_lexical": claim_lexical_pass, "claim_bm25": claim_lexical_pass}

        seen_pids, found, rows = set(), set(), []
        n_queries = 0
        for key in args.arms:
            label, passes = arms[key]
            t0 = time.time()
            for kind, text in passes:
                if not text or len(text.strip()) < 8:
                    continue
                n_queries += 1
                try:
                    seen_pids.update(runner[kind](r, text, subject, None))
                except Exception as e:
                    print(f"   ({kind} failed: {type(e).__name__} {str(e)[:60]})")
            fams = set(fam_of(cur, seen_pids).values())
            found = {f for f in gold if f in fams}
            rows.append((key, label, n_queries, len(seen_pids), len(found), time.time() - t0))
            print(f"  {key}  {label:42s} q={n_queries:3d}  pool={len(seen_pids):7,}  "
                  f"REACHED {len(found):2d}/{len(gold)}  ({time.time() - t0:.0f}s)")
        grand[sub["id"]] = (len(gold), rows)
        missing = [gold[f] for f in gold if f not in found]
        if missing:
            print(f"  still unreached: {', '.join(missing[:8])}"
                  + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""))

    if len(grand) > 1:
        print(f"\n{'=' * 78}\nCUMULATIVE REACH ACROSS SUBJECTS\n{'=' * 78}")
        print(f"{'arm':4s} {'formulations':>13s} {'pool':>10s} {'reached':>12s}")
        for i, key in enumerate(args.arms):
            tot_g = sum(g for g, _ in grand.values())
            tot_r = sum(rws[i][4] for _, rws in grand.values())
            tot_q = sum(rws[i][2] for _, rws in grand.values())
            tot_p = sum(rws[i][3] for _, rws in grand.values())
            print(f"{key:4s} {tot_q:>13d} {tot_p:>10,} {f'{tot_r}/{tot_g}':>12s}")


if __name__ == "__main__":
    main()
