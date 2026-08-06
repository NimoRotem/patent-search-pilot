"""Generate ONE report with the oracle armed from the environment. Used by oracle_bounds.py.

A separate process per arm on purpose: deep_rank and retrieval hold module-level state and cached
connections, and an injected run must not be able to leave anything behind that a later control
run could pick up. Process isolation is the cheapest guarantee of that.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)


def main():
    #  Imported INSIDE main on purpose. A spawned child re-imports this module, and
    #  rerank_pool._child_rerank documents that the child never imports webapp. Leaving these at
    #  module level would quietly break that invariant and put a second copy of the whole
    #  pipeline into every child.
    import benchmark
    import webapp

    SLUG = os.environ["ORACLE_SLUG"]
    STAGE = os.environ.get("ORACLE_STAGE", "")
    GOLD = [g for g in os.environ.get("ORACLE_GOLD", "").split(",") if g]
    SUBJECT_ID = SLUG.split("-")[1] if "-" in SLUG else ""

    subs = json.load(open("eval/benchmark_subjects.json"))["subjects"]
    sub = next((s for s in subs if SLUG.startswith(f"bench-{s['id']}-")), None)
    if sub is None:
        raise SystemExit(f"cannot resolve a benchmark subject from slug {SLUG!r}")

    query, token = benchmark.ingest(sub["url"])
    R = webapp.REPORTS
    for suf in ("", ".view", ".meta", ".deep", ".detail-preview", ".claim-grid", ".archive",
                ".trace.jsonl"):
        p = R / f"{SLUG}{suf}.json" if not suf.endswith("jsonl") else R / f"{SLUG}{suf}"
        if p.exists():
            p.unlink()
    (R / f"{SLUG}.meta.json").write_text(json.dumps(
        {"query": query, "mode": sub["mode"], "subject": None, "wide": True,
         "doc_token": token, "search_focus": "all_text"}))

    #  The injection plan reaches deep_rank through the report. webapp._generate builds the report, so
    #  the plan is stashed where _attach_disclosures and deep_rank can both see it.
    webapp._ORACLE_PLAN = {"stage": STAGE, "gold": GOLD} if STAGE else None
    webapp._generate(SLUG, query, None, sub["mode"], wide=True, doc_token=token,
                     search_focus="all_text")
    print(f"[oracle-run] {SLUG} stage={STAGE or 'control'} gold={len(GOLD)}")


#  MANDATORY. multiprocessing "spawn" re-imports this module in every child, and the pipeline
#  spawns a reranker child. Without this guard each child re-ran the entire benchmark generation,
#  recursively, until the host ran out of memory and froze. See rerank_pool.in_spawned_child.
if __name__ == "__main__":
    main()
