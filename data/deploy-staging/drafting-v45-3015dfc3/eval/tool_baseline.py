"""What does the best tool a searcher already has score on OUR benchmark?

WHY THIS IS THE FIRST NUMBER TO GET
-----------------------------------
The pilot delivers 24 of 266 gold families, 9%. Nobody can say whether that is bad, because there
is no published standard for prior-art recall on an arbitrary subject set and no number to compare
it to. An absolute target picked by feel would drive months of work toward an arbitrary line.

So the target is built from a measurement instead: run the tools a searcher uses today against the
same subjects and the same gold, and read the answer off. Version one of "functional" is parity
with the best of them at lower cost.

ARMS
    gp_similar   Google Patents' own "Similar Documents" for the subject publication. This is the
                 strongest honest anchor: it is Google's own similarity model over the whole
                 corpus, it is what a searcher sees on the page, and it costs one click.
    gp_search    Google Patents keyword search using the subject's title. A deliberate FLOOR for
                 the query-driven route rather than a fair fight: a real searcher iterates queries
                 and this does not, so read it as "the worst a competent searcher would do".
    ours         the pilot's delivered top 50, scored identically on the same families.

WHAT IS DELIBERATELY NOT USED. The same SerpApi response carries `patent_citations` and `cited_by`.
Those ARE the gold set. Reading them would score the answer key and report it as retrieval. Only
`similar_documents` is touched, and the code asserts it.

METRICS, following the product definition rather than convenience:
    recall@50 over X/Y families      the headline. X and Y reported separately from A, because an
                                     A citation is background art and dilutes the signal.
    recall@10                        what a reader actually looks at.
    hit@10 on X                      the product floor: did at least one X-quality family the
                                     attorney did not already have reach the first ten.

    python eval/tool_baseline.py --split dev --arms gp_similar,gp_search,ours
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import db  # noqa: E402
import pubnorm  # noqa: E402
from funnel import gold_by_subject  # noqa: E402

CACHE = os.path.join(ROOT, "data", "baseline")
SERP = "https://serpapi.com/search"
TOPK = 50


def _key():
    k = os.environ.get("SERPAPI_KEY", "")
    if not k:
        raise SystemExit("SERPAPI_KEY not set; fetch it from the advisor at call time")
    return k


def _cached(name, fn):
    """One call per subject per arm, ever. Re-running the harness must not re-spend quota."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{name}.json")
    if os.path.exists(p):
        return json.load(open(p))
    d = fn()
    with open(p, "w") as fh:
        json.dump(d, fh)
    time.sleep(1.0)
    return d


def gp_similar(sub_pub):
    """Google Patents 'Similar Documents'. -> [publication_number] in Google's own order."""
    import requests
    pid = f"patent/{sub_pub.replace('-', '')}/en"

    def call():
        r = requests.get(SERP, params={"engine": "google_patents_details",
                                       "patent_id": pid, "api_key": _key()}, timeout=90)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}

    d = _cached(f"gpd_{sub_pub}", call)
    #  Guard, not a comment: patent_citations IS the gold set and must never enter a baseline.
    assert "patent_citations" not in _USED_FIELDS, "baseline must not read the answer key"
    out = []
    for x in (d.get("similar_documents") or []):
        pn = (x or {}).get("publication_number") or ""
        if pn:
            out.append(pn)
    return out, d


_USED_FIELDS = {"similar_documents"}


def gp_search(title):
    """Google Patents keyword search on the subject's title. -> [publication_number]."""
    import requests

    def call():
        r = requests.get(SERP, params={"engine": "google_patents", "q": title[:280],
                                       "num": 100, "api_key": _key()}, timeout=90)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}

    d = _cached(f"gps_{abs(hash(title)) % (10 ** 12)}", call)
    out = []
    for x in (d.get("organic_results") or []):
        pn = (x or {}).get("publication_number") or (x or {}).get("patent_id") or ""
        pn = pn.replace("patent/", "").replace("/en", "")
        if pn:
            out.append(pn)
    return out, d


def subject_title(sub_pub, sub):
    """The subject's real title, for the keyword arm.

    The benchmark's `name` field is a human LABEL ("WO-2020229522-A1 (Robot gripper, industrial
    robot, handling system, and me)"), a publication number plus a truncated parenthetical. Sent
    to Google Patents it returns nothing at all, and the arm quietly scored 0.0% across 30
    subjects: an empty result set is indistinguishable from a tool that found nothing relevant,
    which would have been reported as a baseline rather than as a broken probe.
    """
    with db.cursor() as cur:
        cur.execute("""SELECT title FROM publications
                        WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g'))
                              = upper(regexp_replace(%s,'[^A-Za-z0-9]','','g'))
                          AND title IS NOT NULL AND title <> '' LIMIT 1""", (sub_pub,))
        r = cur.fetchone()
    if r and r["title"]:
        return " ".join(str(r["title"]).split())
    #  Fall back to the parenthetical in the label, which is the title with the number stripped.
    name = str(sub.get("name") or "")
    if "(" in name and ")" in name:
        inner = name[name.index("(") + 1:name.rindex(")")]
        inner = inner.split(",")[-1].strip() if inner.count(",") > 2 else inner
        return " ".join(inner.split())
    return ""


def families_for(pubs):
    """{publication -> family key}. Local corpus first, then BigQuery for the rest.

    Scoring in publication space instead of family space would count a US sibling of a cited DE
    document as a miss, which is exactly the dedup the pipeline itself does.
    """
    out, unknown = {}, []
    for p in pubs:
        c = pubnorm.canonical(p) or p
        with db.cursor() as cur:
            cur.execute("""SELECT coalesce(nullif(simple_family_id,''), publication_number) f
                             FROM publications
                            WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g'))
                                  = upper(regexp_replace(%s,'[^A-Za-z0-9]','','g')) LIMIT 1""",
                        (c,))
            r = cur.fetchone()
        if r:
            out[p] = str(r["f"])
        else:
            unknown.append((p, c))
    if unknown:
        try:
            from google.cloud import bigquery
            bq = bigquery.Client(project="nimo-gpt")
            q = """SELECT publication_number, CAST(family_id AS STRING) fid
                   FROM `patents-public-data.patents.publications`
                   WHERE publication_number IN UNNEST(@pubs)"""
            cfg = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("pubs", "STRING", [c for _p, c in unknown])])
            got = {r["publication_number"]: r["fid"] for r in bq.query(q, job_config=cfg).result()}
            for p, c in unknown:
                out[p] = got.get(c) or c
        except Exception as e:
            print(f"  [bq family lookup failed: {type(e).__name__}] falling back to publication id")
            for p, c in unknown:
                out[p] = c
    return out


def code_class(code):
    c = (code or "").upper()
    if "X" in c:
        return "X"
    if "Y" in c:
        return "Y"
    return "A"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    ap.add_argument("--arms", default="gp_similar,gp_search,ours")
    ap.add_argument("--tag", default="v15")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    subs = {s["id"]: s for s in
            json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]}
    gold = gold_by_subject()

    rows = []
    for sid in sorted(gold):
        sub = subs.get(sid) or {}
        if sub.get("split", "dev") != args.split:
            continue
        cits = gold[sid]
        subject_pub = ""
        for c in cits:
            subject_pub = c.get("subject_pub") or subject_pub
        if not subject_pub:
            continue
        gold_fams = {}
        for c in cits:
            gold_fams.setdefault(str(c["gold_family_id"]), code_class(c["citation_code"]))
        own_family = {str(c.get("subject_family_id") or "") for c in cits} - {""}

        for arm in arms:
            try:
                if arm == "gp_similar":
                    pubs, _raw = gp_similar(subject_pub)
                elif arm == "gp_search":
                    title = subject_title(subject_pub, sub)
                    if not title:
                        print(f"  [{sid}/gp_search] no title available, skipped")
                        continue
                    pubs, _raw = gp_search(title)
                elif arm == "ours":
                    vp = os.path.join(ROOT, "data", "reports",
                                      f"bench-{sid}-{args.tag}.view.json")
                    if not os.path.exists(vp):
                        continue
                    pubs = [c.get("pub") for c in (json.load(open(vp)).get("cards") or [])
                            if c.get("pub")]
                else:
                    continue
            except Exception as e:
                print(f"  [{sid}/{arm}] FAILED {type(e).__name__}: {e}")
                continue

            fam_of = families_for(pubs[:TOPK * 2])
            seen, ranked = set(), []
            for p in pubs:
                f = str(fam_of.get(p) or p)
                if f in own_family or f in seen:
                    continue
                seen.add(f)
                ranked.append(f)
            rows.append({"subject_id": sid, "arm": arm, "n_returned": len(ranked),
                         "gold_total": len(gold_fams),
                         "gold_xy": sum(1 for v in gold_fams.values() if v in ("X", "Y")),
                         **_score(ranked, gold_fams)})
            r = rows[-1]
            print(f"{sid:<24s} {arm:<11s} returned {r['n_returned']:>3d}  "
                  f"XY@50 {r['hit_xy_50']:>2d}/{r['gold_xy']:<3d}  "
                  f"XY@10 {r['hit_xy_10']:>2d}  X@10 {'yes' if r['x_in_top10'] else 'no'}")
        if args.limit and len({r["subject_id"] for r in rows}) >= args.limit:
            break

    out = os.path.join(ROOT, "data", "logs", "tool_baseline.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'=' * 78}\nTOOL BASELINE, {args.split} split\n{'=' * 78}")
    print(f"{'arm':<12s} {'subjects':>9s} {'XY recall@50':>14s} {'XY recall@10':>14s} "
          f"{'>=1 X in top 10':>16s}")
    for arm in arms:
        sel = [r for r in rows if r["arm"] == arm]
        if not sel:
            continue
        gx = sum(r["gold_xy"] for r in sel) or 1
        print(f"{arm:<12s} {len(sel):>9d} "
              f"{sum(r['hit_xy_50'] for r in sel) / gx:>13.1%} "
              f"{sum(r['hit_xy_10'] for r in sel) / gx:>13.1%} "
              f"{sum(1 for r in sel if r['x_in_top10']) / len(sel):>15.0%}")
    print(f"\nwritten {out}")
    print("gp_search uses the subject title only and is a FLOOR for the query route, not a fair "
          "fight. gp_similar is the anchor.")


def _score(ranked, gold_fams):
    def hits(k, kinds):
        return sum(1 for f in ranked[:k] if gold_fams.get(f) in kinds)
    return {"hit_all_50": hits(50, ("X", "Y", "A")), "hit_xy_50": hits(50, ("X", "Y")),
            "hit_xy_10": hits(10, ("X", "Y")), "hit_x_10": hits(10, ("X",)),
            "x_in_top10": hits(10, ("X",)) > 0}


if __name__ == "__main__":
    main()
