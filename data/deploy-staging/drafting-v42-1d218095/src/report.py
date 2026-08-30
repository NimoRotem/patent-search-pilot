"""Render the coverage-ledger agent's output as a grounded, element-by-element prior-art
report (spec §7): every assertion cites publication number + claim/paragraph coordinate; a
combinational (inventive-step) view maps reference -> element; final set flagged for
drawings/legal-status enrichment.
"""
from __future__ import annotations
import json, sys
from datetime import date
from pathlib import Path
import db, goldset
from retrieval import Retriever
from agent import CoverageAgent, AgentConfig
from evaluate import subject_from
from config import DATA

OUT = DATA / "reports"
OUT.mkdir(parents=True, exist_ok=True)


def _coord(c):
    if not c:
        return ""
    if isinstance(c, str):
        try: c = json.loads(c)
        except Exception: return c
    for k in ("claim_no", "para_no", "figure_no"):
        if c.get(k) is not None:
            return f"{k.replace('_no','')} {c[k]}"
    return ""


def render(rep: dict) -> str:
    L = [f"# Prior-Art Search Report — {rep['mode'].replace('_',' ').title()} mode",
         f"_Generated {date.today()} · subject: {rep.get('subject') or 'natural-language query'}_",
         "", f"> {rep['query']}", "",
         f"**Families surfaced:** {rep['n_families']} · **rounds:** {rep['rounds']} · "
         f"**channels:** {', '.join(rep['channels_used'])} · **languages:** {', '.join(rep['languages'])} · "
         f"**LLM calls:** {rep['llm_usage']['calls']}", ""]
    # element-by-element
    L += ["## Element-by-element prior art", ""]
    cov = rep["element_coverage"]
    for el in rep["elements"]:
        c = cov.get(el, {})
        L.append(f"### {el}  \n_best score {c.get('best_score')}, {c.get('n_evidence',0)} references_")
        for h in rep["element_evidence"].get(el, [])[:5]:
            coord = _coord(h.get("coord"))
            L.append(f"- **{h['pub']}** ({h['basis']}) — {h['kind']}{(' · ' + coord) if coord else ''} "
                     f"· score {round(h['score'],3)} · via {', '.join(h.get('channels',[]))}")
        L.append("")
    # inventive-step combination view
    cv = rep["combination_view"]
    L += ["## Combinational view (which reference supplies which element)", "",
          f"- **Primary reference:** `{cv.get('primary')}` covers: {', '.join(cv.get('covers',[])) or '—'}"]
    for s in cv.get("secondaries", []):
        L.append(f"- **Secondary:** `{s['ref']}` supplies: {', '.join(s['supplies'])}")
    if cv.get("uncovered_elements"):
        L.append(f"- **Not found in corpus:** {', '.join(cv['uncovered_elements'])}")
    # coverage ledger + enrichment note
    L += ["", "## Coverage ledger", "",
          f"- CPC branches searched: {', '.join(rep['cpc_branches']) or '—'}",
          f"- Marginal new-family yield per round: {rep.get('round_new_families')}",
          "", "## Final set — flagged for enrichment (spec §6 step 8)",
          "_The top families would be enriched via EPO OPS / USPTO ODP for drawings, facsimile PDF "
          "and verified legal status before use as evidence._", ""]
    for f in rep["ranked_families"][:15]:
        L.append(f"- family `{f}`")
    return "\n".join(L) + "\n"


def run_for(entry_id=None, query=None, mode="novelty"):
    R = Retriever(); A = CoverageAgent(R)
    subj = None
    if entry_id:
        gs = goldset.load()
        e = next(x for x in gs["entries"] if x["id"] == entry_id)
        query, mode, subj = e["query_text"], e["mode"], subject_from(e)
        name = entry_id
    else:
        name = "adhoc"
    rep = A.run(query, subject=subj, mode=mode, cfg=AgentConfig(mode=mode))
    (OUT / f"{name}.json").write_text(json.dumps(rep, indent=2, default=str))
    md = render(rep)
    (OUT / f"{name}.md").write_text(md)
    print(md)
    print(f"[report] wrote {OUT/f'{name}.md'}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_for(entry_id=sys.argv[1])
    else:
        run_for(entry_id="grabo_gripper_novelty")
