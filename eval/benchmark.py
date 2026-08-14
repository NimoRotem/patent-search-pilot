"""The standing prior-art recall benchmark: run every subject, audit every citation list.

WHY MORE THAN ONE SUBJECT
-------------------------
Tuning against a single citation list is how a search engine gets overfitted to one invention.
Two things made that concrete here:

  * run-to-run variance on one subject is +/-2 families, so a "+1" on a single query is noise,
    and several changes were adopted or rejected on differences that size;
  * the two subjects fail for OPPOSITE reasons. EP 3 707 092's cited art is mostly IN the corpus
    and the problem is ranking it. US 2026/0109053's is mostly NOT, and no ranking change can
    produce a document that was never retrieved. A change that trades one for the other looks
    neutral on either subject alone and is obvious across both.

USAGE
    python eval/benchmark.py --run  --tag v11        # generate fresh reports, then audit
    python eval/benchmark.py                         # audit the newest reports, no generation
    python eval/benchmark.py --only schmalz --run --tag v11
    python eval/benchmark.py --compare v10 v11       # two tags side by side

Reports are written as `bench-<subject>-<tag>`. The audit is eval/citation_recall.py, unchanged
and family-level: a citation counts as found if ANY member of its DOCDB simple family is on the
page. Nothing here is specific to a subject; add one to benchmark_subjects.json.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import traceback
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

SUBJECTS = os.path.join(HERE, "benchmark_subjects.json")
RESULTS = os.path.join(HERE, "benchmark_results")


def subjects(only=None):
    d = json.load(open(SUBJECTS))
    subs = d["subjects"]
    if only:
        want = {s.strip() for s in only.split(",") if s.strip()}
        subs = [s for s in subs if s["id"] in want]
        missing = want - {s["id"] for s in subs}
        if missing:
            raise SystemExit(f"unknown subject id(s): {sorted(missing)}")
    return subs


def ingest(url):
    """Put the subject document through the SAME front door a user uses: /extract's link path.

    -> (brief_text, doc_token). Raises on failure; a benchmark that silently measured a degraded
    ingest would be worse than one that stopped.
    """
    import ingest_input
    import webapp
    res = ingest_input.extract_link(url)
    if not res or not res.get("ok"):
        raise RuntimeError(f"link ingest failed for {url}: "
                           f"{(res or {}).get('error', 'no result')}")
    token = webapp._stash_doc(res)
    brief = res.get("brief") or ""
    if not (token and brief.strip()):
        raise RuntimeError(f"link ingest produced no {'token' if not token else 'brief'} "
                           f"for {url}")
    return brief, token


def generate(sub, tag, wide=True):
    """Run one full search and return the slug.

    REUSE_META_FROM_TAG makes this arm reuse another arm's INGESTED SUBJECT verbatim instead of
    re-ingesting.

    WHY. ingest() condenses the patent with an LLM, so it produces different prose every run. The
    external query plan is keyed on that brief and the external replay cache is keyed on the
    resulting queries, so an uncached step at the very TOP of the funnel defeats the caching of
    everything below it. Measured on the first treatment attempt: the same subject produced a
    1,951-character brief in the control and a 2,304-character brief in the treatment, jaccard
    0.41, so every subject missed the cache and fetched a different external world. The corpus was
    then not the only thing that differed between the arms, which is the whole reason the database
    was cloned.

    Reusing the control's brief and document token makes the two arms byte-identical upstream of
    retrieval. This lives in eval/ rather than src/ on purpose: src_tree_hash stays unchanged, so
    the already-completed control arm remains comparable.
    """
    import webapp
    slug = f"bench-{sub['id']}-{tag}"
    reuse = os.environ.get("REUSE_META_FROM_TAG", "").strip()
    if reuse:
        src_meta = webapp.REPORTS / f"bench-{sub['id']}-{reuse}.meta.json"
        if not src_meta.exists():
            raise RuntimeError(f"REUSE_META_FROM_TAG={reuse} but {src_meta.name} does not exist; "
                               f"refusing to silently re-ingest and break the comparison")
        m = json.loads(src_meta.read_text())
        query, token = m.get("query") or "", m.get("doc_token")
        if not query.strip():
            raise RuntimeError(f"{src_meta.name} has no query to reuse")
        if not webapp._load_doc_materials(token):
            raise RuntimeError(f"the stashed document for {src_meta.name} is gone "
                               f"(token {token}); the arms would not share a subject")
        print(f"[reuse] subject taken verbatim from {reuse}: "
              f"{len(query):,} char brief, token {str(token)[:12]}")
    else:
        query, token = ingest(sub["url"])
    R = webapp.REPORTS
    for suf in ("", ".view", ".meta", ".deep", ".detail-preview", ".claim-grid", ".archive"):
        p = R / f"{slug}{suf}.json"
        if p.exists():
            p.unlink()
    (R / f"{slug}.meta.json").write_text(json.dumps(
        {"query": query, "mode": sub["mode"], "subject": None, "wide": wide,
         "doc_token": token, "search_focus": "all_text"}))
    t0 = time.time()
    webapp._generate(slug, query, None, sub["mode"], wide=wide, doc_token=token,
                     search_focus="all_text")
    #  BUILD THE PAGE, because the page is what the benchmark measures.
    #
    #  `_generate` writes the report; the CARDS are built lazily when /report is first opened and
    #  cached to <slug>.view.json. This function deletes that file above and nothing here recreated
    #  it, so citation_recall — which reads exactly that file — saw `{"cards": []}` and scored
    #  every run 0 displayed. v15, abc2 and abt2 all report "0 / 9 families in the RANKED top 0",
    #  and "top 0" is the tell: the harness was reporting a page it never rendered. The headline
    #  number of this benchmark has been structurally zero, while still being a number.
    try:
        rep = json.loads((R / f"{slug}.json").read_text())
        webapp._build_view_cached(slug, rep, regen=True)
    except Exception:
        traceback.print_exc()      # audit will say 0 displayed, and now that means something
    return slug, round(time.time() - t0, 1)


def audit(slug, citations):
    """-> (hit, total, captured stdout). citation_recall prints; we keep it AND the numbers."""
    import citation_recall
    buf = io.StringIO()
    with redirect_stdout(buf):
        hit, total = citation_recall.audit(slug, list(citations))
    return hit, total, buf.getvalue()


def surfaced_from(text):
    """The 'surfaced' count out of citation_recall's own summary line, so there is one source."""
    for line in text.splitlines():
        if "surfaced on the page" in line:
            try:
                return int(line.split(";")[1].strip().split()[0])
            except Exception:
                return None
    return None


def run(tag, only=None, do_generate=True, wide=True):
    os.makedirs(RESULTS, exist_ok=True)
    out = {"tag": tag, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "subjects": {}}
    for sub in subjects(only):
        sid = sub["id"]
        print(f"\n{'=' * 78}\n{sid}: {sub['name']}\n{'=' * 78}", flush=True)
        slug = f"bench-{sid}-{tag}"
        secs = None
        if do_generate:
            try:
                slug, secs = generate(sub, tag, wide=wide)
                print(f"[bench] generated {slug} in {secs}s", flush=True)
            except Exception as e:
                traceback.print_exc()
                out["subjects"][sid] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
                continue
        try:
            hit, total, text = audit(slug, sub["citations"])
        except Exception as e:
            traceback.print_exc()
            out["subjects"][sid] = {"error": f"audit failed: {str(e)[:300]}", "slug": slug}
            continue
        print(text)
        out["subjects"][sid] = {
            "slug": slug, "seconds": secs, "displayed": hit, "in_corpus": total,
            "surfaced": surfaced_from(text), "cited": len(sub["citations"]),
            "detail": text,
        }
    _headline(out)
    path = os.path.join(RESULTS, f"{tag}.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nwritten: {path}")
    return out


def _headline(out):
    print(f"\n{'=' * 78}\nBENCHMARK {out['tag']}\n{'=' * 78}")
    print(f"{'subject':14s} {'displayed':>12s} {'surfaced':>10s} {'cited':>6s} {'secs':>7s}")
    td = ti = ts = tc = 0
    for sid, r in out["subjects"].items():
        if r.get("error"):
            print(f"{sid:14s}  ERROR: {r['error'][:60]}")
            continue
        d, i, s, c = (r["displayed"], r["in_corpus"], r["surfaced"] or 0, r["cited"])
        td, ti, ts, tc = td + d, ti + i, ts + s, tc + c
        print(f"{sid:14s} {f'{d}/{i} in corpus':>12s} {f'{s}/{i}':>10s} {c:>6d} "
              f"{(r['seconds'] or 0):>7.0f}")
    print(f"{'TOTAL':14s} {f'{td}/{ti}':>12s} {f'{ts}/{ti}':>10s} {tc:>6d}")
    print("\ndisplayed = cited families in the ranked top 50. surfaced = ranked list plus the "
          "not-readable section.\nin corpus = cited families the corpus holds AT ALL, which is "
          "itself a number a change can move.")


def compare(tags):
    rows = {}
    for t in tags:
        p = os.path.join(RESULTS, f"{t}.json")
        if not os.path.exists(p):
            raise SystemExit(f"no results for tag {t} ({p})")
        rows[t] = json.load(open(p))
    ids = sorted({sid for r in rows.values() for sid in r["subjects"]})
    w = max(12, *(len(t) for t in tags))
    print(f"{'subject':14s} " + " ".join(f"{t:>{w}s}" for t in tags))
    for sid in ids:
        cells = []
        for t in tags:
            r = rows[t]["subjects"].get(sid) or {}
            cells.append("ERROR" if r.get("error") else
                         f"{r.get('displayed', '-')}/{r.get('in_corpus', '-')}"
                         f" ({r.get('surfaced', '-')})")
        print(f"{sid:14s} " + " ".join(f"{c:>{w}s}" for c in cells))
    print("\ncell = displayed/in-corpus (surfaced)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="dev", help="names the reports and the results file")
    ap.add_argument("--run", action="store_true", help="generate fresh reports before auditing")
    ap.add_argument("--only", default=None, help="comma-separated subject ids")
    ap.add_argument("--no-wide", action="store_true", help="local corpus only, no external APIs")
    ap.add_argument("--compare", nargs=2, metavar=("TAG_A", "TAG_B"))
    args = ap.parse_args()
    if args.compare:
        compare(args.compare)
        return
    run(args.tag, only=args.only, do_generate=args.run, wide=not args.no_wide)


if __name__ == "__main__":
    main()
