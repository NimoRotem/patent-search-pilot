"""Recompute ONLY the agentic config (agent-ranking change) for all gold entries, reusing the
already-measured keyword/vector/hybrid/hybrid_rerank configs from eval_results.json. Much faster
than a full re-run; then re-emit eval_report.md. (Milestone 3 §4 iteration)"""
from __future__ import annotations
import json
import db, goldset
from retrieval import Retriever
from agent import CoverageAgent, AgentConfig
import evaluate as E

R = Retriever()
A = CoverageAgent(R)
corpus = E.corpus_families()
gs = goldset.load()
prev = {r["id"]: r for r in json.load(open(E.OUT / "eval_results.json"))["results"]}

rows = []
for e in gs["entries"]:
    subj = E.subject_from(e)
    mode = e["mode"]
    gold = set(e["gold_families"])
    reach = gold & corpus
    earliest = E.fam_earliest(reach)
    rec = prev.get(e["id"])
    if not rec:
        continue
    try:
        rep = A.run(e["query_text"], subject=subj, mode=mode,
                    cfg=AgentConfig(mode=mode, max_rounds=2, elements_per_round=2, ground=False))
        fams = rep["ranked_families"]
        cf = {ch: set(v) for ch, v in rep["channel_families"].items()}
        m = E.metrics_for(R, fams, gold, reach, cf, earliest)
        m["marginal_yield_per_round"] = rep["round_new_families"]
        m["rounds"] = rep["rounds"]; m["llm_calls"] = rep["llm_usage"]["calls"]
        m["combination_view"] = rep["combination_view"]
        rec["configs"]["agentic"] = m
        base = set().union(*[cf.get(c, set()) for c in ["dense", "cpc"]]) if cf else set()
        rec["citation_only_gold"] = sorted((cf.get("citation", set()) & gold) - base)
        hybrid_fams = set(); # recompute agent-only vs hybrid using stored hybrid contribution
        rec["agent_only_gold"] = sorted((set(fams) & gold) -
            set(rec["configs"].get("hybrid", {}).get("channel_gold_contribution", {}).get("dense", [])))
    except Exception as ex:
        import traceback; traceback.print_exc()
        rec["agentic_error"] = str(ex)[:200]
    rows.append(rec)
    a = rec["configs"].get("agentic", {})
    v = rec["configs"].get("vector", {})
    print(f"  {e['id']:32s} vector@100={v.get('family_recall@100')} agentic@100={a.get('family_recall@100')} "
          f"agentic@1000={a.get('family_recall@1000')}")

E._write(rows, gs)
print("\n[reeval] done — eval_report.md updated")
