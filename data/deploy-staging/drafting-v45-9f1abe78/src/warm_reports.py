"""Regenerate + cache all 11 gold reports with the current retrieval/agent (Milestone 3 §1/§5).
Reports are cached to data/reports/<id>.json (ground=True so the claim chart has evidence pubs);
stale view caches are removed so the webapp rebuilds them. Idempotent; prints per-report status."""
from __future__ import annotations
import json, time, sys
from pathlib import Path
import goldset
from retrieval import Retriever
from agent import CoverageAgent, AgentConfig
from evaluate import subject_from
from config import DATA

REPORTS = DATA / "reports"
R = Retriever()
A = CoverageAgent(R)
gs = goldset.load()

only = sys.argv[1:] or None
for e in gs["entries"]:
    if only and e["id"] not in only:
        continue
    t = time.time()
    subj = subject_from(e)
    try:
        rep = A.run(e["query_text"], subject=subj, mode=e["mode"],
                    cfg=AgentConfig(mode=e["mode"], max_rounds=2, elements_per_round=3, ground=True))
        (REPORTS / f"{e['id']}.json").write_text(json.dumps(rep, default=str, indent=1))
        (REPORTS / f"{e['id']}.view.json").unlink(missing_ok=True)
        print(f"  {e['id']:32s} OK  {rep['n_families']} fams, {len(rep['ranked_families'])} ranked, "
              f"{rep['rounds']} rounds, {time.time()-t:.0f}s", flush=True)
    except Exception as ex:
        import traceback; traceback.print_exc()
        print(f"  {e['id']:32s} ERROR {str(ex)[:120]}", flush=True)
print("warm done", flush=True)
