"""5-configuration ablation + metrics (spec §8). Acceptance is NOT 'beats abstract-only' — we
measure recall, channel contribution, element coverage, citation-only / agent-only finds, FP
rate and marginal yield on the FROZEN gold set, with citation edges hidden (the retrieval never
seeds from the subject's own family, so the answer-key citations can't leak).

Also compares 768 vs 1024 vs 3072 embedding dimensions on the gold-relevant subset.
"""
from __future__ import annotations
import json, statistics
from datetime import date, datetime
from pathlib import Path
import db, goldset, embed
from retrieval import Retriever
from agent import CoverageAgent, AgentConfig
from search_modes import Subject, Mode
from config import DATA

KS = [100, 500, 1000]
OUT = DATA / "eval"
OUT.mkdir(parents=True, exist_ok=True)


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


def corpus_families():
    with db.cursor() as c:
        c.execute("SELECT DISTINCT simple_family_id f FROM publications WHERE simple_family_id IS NOT NULL")
        return set(r["f"] for r in c.fetchall())


def subject_from(entry):
    s = entry.get("subject")
    if not s or not s.get("efd"):
        return None
    return Subject(number=s["number"], efd=_d(s["efd"]), filing_date=_d(s.get("filing_date")),
                   publication_date=_d(s.get("publication_date")), jurisdiction=s.get("jurisdiction"))


def fam_earliest(gold):
    if not gold:
        return {}
    with db.cursor() as c:
        c.execute("SELECT simple_family_id f, min(publication_date) d FROM publications "
                  "WHERE simple_family_id = ANY(%s) GROUP BY simple_family_id", (list(gold),))
        return {r["f"]: r["d"] for r in c.fetchall()}


def recall(retrieved, gold, k):
    if not gold:
        return None
    return round(len(set(retrieved[:k]) & gold) / len(gold), 4)


def channels_to_families(R, channel_hits):
    out = {}
    for ch, pids in channel_hits.items():
        out[ch] = set(R.family_key(p) for p in pids)
    return out


def metrics_for(R, retrieved, gold, reach, chan_fams, earliest):
    m = {}
    for k in KS:
        m[f"family_recall@{k}"] = recall(retrieved, gold, k)
        m[f"reachable_recall@{k}"] = recall(retrieved, reach, k) if reach else None
    # earliest relevant publication recovered?
    if earliest:
        ef = min(earliest, key=lambda f: earliest[f] or date.max)
        m["earliest_relevant_family"] = ef
        m["earliest_recovered"] = ef in set(retrieved[:1000])
    # false-positive (non-gold) rate in top-20 — caveat: gold set is incomplete
    top20 = retrieved[:20]
    m["nongold_rate@20"] = round(sum(1 for f in top20 if f not in gold) / max(1, len(top20)), 3)
    # per-channel unique contribution to gold
    if chan_fams:
        m["channel_gold_contribution"] = {ch: sorted(gold & fs) for ch, fs in chan_fams.items() if (gold & fs)}
    return m


def evaluate(entries=None, run_agentic=True):
    R = Retriever()
    A = CoverageAgent(R) if run_agentic else None
    corpus = corpus_families()
    gs = goldset.load()
    rows = []
    for e in (entries or gs["entries"]):
        subj = subject_from(e)
        mode = e["mode"]
        gold = set(e["gold_families"])
        reach = gold & corpus
        earliest = fam_earliest(reach)
        rec = {"id": e["id"], "category": e["category"], "mode": mode,
               "n_gold": len(gold), "n_reachable": len(reach), "configs": {}}
        chan_by_cfg = {}
        for cfg in ["keyword", "vector", "hybrid", "hybrid_rerank"]:
            res = R.search(e["query_text"], subject=subj, mode=mode, config=cfg, topk=1000)
            fams = [fk for fk, _, _, _ in res.family_ranked]
            cf = channels_to_families(R, res.channel_hits)
            chan_by_cfg[cfg] = (set(fams), cf)
            rec["configs"][cfg] = metrics_for(R, fams, gold, reach, cf, earliest)
        if run_agentic:
            try:
                rep = A.run(e["query_text"], subject=subj, mode=mode,
                            cfg=AgentConfig(mode=mode, max_rounds=2, elements_per_round=2, ground=False))
                fams = rep["ranked_families"]
                cf = {ch: set(v) for ch, v in rep["channel_families"].items()}
                chan_by_cfg["agentic"] = (set(fams), cf)
                m = metrics_for(R, fams, gold, reach, cf, earliest)
                m["claim_element_coverage"] = rep["element_coverage"]
                m["marginal_yield_per_round"] = rep["round_new_families"]
                m["rounds"] = rep["rounds"]
                m["llm_calls"] = rep["llm_usage"]["calls"]
                m["combination_view"] = rep["combination_view"]
                rec["configs"]["agentic"] = m
                # references found ONLY via citations (agentic channels)
                base = set().union(*[cf.get(c, set()) for c in ["dense", "bm25", "cpc", "exact"]]) if cf else set()
                rec["citation_only_gold"] = sorted((cf.get("citation", set()) & gold) - base)
                # references found ONLY via the agent vs plain hybrid
                hybrid_fams = chan_by_cfg["hybrid"][0]
                rec["agent_only_gold"] = sorted((set(fams) & gold) - (hybrid_fams & gold))
            except Exception as ex:   # keep the non-agentic ablation even if the agent trips
                import traceback; traceback.print_exc()
                rec["agentic_error"] = str(ex)[:200]
        rows.append(rec)
        print(f"  [{e['id']}] gold={len(gold)} reach={len(reach)} "
              + " ".join(f"{c}:R@100={rec['configs'][c].get('family_recall@100')}"
                         for c in rec["configs"]))
        _write(rows, gs)   # incremental: a partial/interrupted run still yields a report
    _write(rows, gs)
    return rows


def _agg(rows, cfg, key):
    vals = [r["configs"][cfg][key] for r in rows if cfg in r["configs"]
            and r["configs"][cfg].get(key) is not None]
    return round(statistics.mean(vals), 4) if vals else None


def _write(rows, gs):
    (OUT / "eval_results.json").write_text(json.dumps({"generated": date.today().isoformat(),
                                                        "results": rows}, indent=2, default=str))
    cfgs = ["keyword", "vector", "hybrid", "hybrid_rerank", "agentic"]
    md = ["# Pilot Evaluation — 5-config ablation (spec §8)",
          f"_Generated {date.today()} · {len(rows)} frozen gold searches · corpus 107,795 pubs / 1.82M vectors_",
          "",
          "## Mean family recall@k (macro-avg over gold searches)", "",
          "| Config | recall@100 | recall@500 | recall@1000 | reachable@100 | nongold@20 |",
          "|---|--:|--:|--:|--:|--:|"]
    for c in cfgs:
        md.append(f"| {c} | {_agg(rows,c,'family_recall@100')} | {_agg(rows,c,'family_recall@500')} "
                  f"| {_agg(rows,c,'family_recall@1000')} | {_agg(rows,c,'reachable_recall@100')} "
                  f"| {_agg(rows,c,'nongold_rate@20')} |")
    # earliest recovered
    md += ["", "## Earliest-relevant-publication recovered (count yes / total)", ""]
    for c in cfgs:
        ys = sum(1 for r in rows if r["configs"].get(c, {}).get("earliest_recovered"))
        tot = sum(1 for r in rows if "earliest_recovered" in r["configs"].get(c, {}))
        md.append(f"- {c}: {ys}/{tot}")
    # agent-only / citation-only
    md += ["", "## Unique-contribution findings", ""]
    for r in rows:
        if r.get("agent_only_gold") or r.get("citation_only_gold"):
            md.append(f"- `{r['id']}`: agent-only gold families={r.get('agent_only_gold')} · "
                      f"citation-only={r.get('citation_only_gold')}")
    # per-query table
    md += ["", "## Per-query family recall@100", "",
           "| query | cat | mode | gold | reach | keyword | vector | hybrid | +rerank | agentic |",
           "|---|---|---|--:|--:|--:|--:|--:|--:|--:|"]
    for r in rows:
        g = lambda c: r["configs"].get(c, {}).get("family_recall@100")
        md.append(f"| `{r['id']}` | {r['category']} | {r['mode']} | {r['n_gold']} | {r['n_reachable']} "
                  f"| {g('keyword')} | {g('vector')} | {g('hybrid')} | {g('hybrid_rerank')} | {g('agentic')} |")
    (OUT / "eval_report.md").write_text("\n".join(md) + "\n")
    print("\n[eval] wrote", OUT / "eval_report.md")


# --- dimension benchmark: 768 vs 1024 vs 3072 (spec §7/§8) --------------------------------
def bench_targets():
    gs = goldset.load()
    fams = set()
    for e in gs["entries"]:
        fams |= set(e["gold_families"])
        if e.get("anchor_family"):
            fams.add(e["anchor_family"])
    with db.cursor() as c:
        c.execute("SELECT id FROM publications WHERE simple_family_id = ANY(%s)", (list(fams),))
        return [r["id"] for r in c.fetchall()]


def bench_dims():
    """Compare recall of 768 (main) vs 1024/3072 (bench) on the gold-relevant subset."""
    import numpy as np
    gs = goldset.load()
    conn = db.connect()
    def search_bench(table, qvec, k=50):
        vs = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
        with conn.cursor() as c:
            c.execute(f"SELECT p.simple_family_id f, min(b.embedding <=> %s::vector) d "
                      f"FROM {table} b JOIN chunks ch ON ch.id=b.chunk_id "
                      f"JOIN publications p ON p.id=ch.publication_id "
                      f"WHERE p.simple_family_id IS NOT NULL GROUP BY p.simple_family_id ORDER BY d LIMIT %s", (vs, k))
            return [r["f"] for r in c.fetchall()]
    def search_main(qvec, pubset, k=50):
        vs = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
        with conn.cursor() as c:
            c.execute("SELECT p.simple_family_id f, min(ch.embedding <=> %s::vector) d "
                      "FROM chunks ch JOIN publications p ON p.id=ch.publication_id "
                      "WHERE ch.kind IN ('abstract','claim_own','claim_resolved') AND ch.embedding IS NOT NULL "
                      "AND ch.publication_id = ANY(%s) AND p.simple_family_id IS NOT NULL "
                      "GROUP BY p.simple_family_id ORDER BY d LIMIT %s", (vs, pubset, k))
            return [r["f"] for r in c.fetchall()]
    pubset = bench_targets()
    out = {768: [], 1024: [], 3072: []}
    for e in gs["entries"]:
        gold = set(e["gold_families"])
        if not gold:
            continue
        for dim, table in [(1024, "bench_emb_1024"), (3072, "bench_emb_3072")]:
            qv = embed.embed_query(e["query_text"][:8000], dim)
            got = set(search_bench(table, qv, 50))
            out[dim].append(len(got & gold) / len(gold))
        qv768 = embed.embed_query(e["query_text"][:8000], 768)
        got = set(search_main(qv768, pubset, 50))
        out[768].append(len(got & gold) / len(gold))
    summary = {d: round(statistics.mean(v), 4) for d, v in out.items() if v}
    (OUT / "dim_benchmark.json").write_text(json.dumps(summary, indent=2))
    print("[bench] recall@50 on gold-relevant subset by dim:", summary)
    return summary


if __name__ == "__main__":
    import sys
    if "bench" in sys.argv:
        bench_dims()
    else:
        evaluate(run_agentic="--no-agentic" not in sys.argv)
