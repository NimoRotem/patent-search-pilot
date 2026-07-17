"""Coverage-ledger agentic controller (spec §7).

The LLM generates queries, synonyms/terminology, CPC guesses and translations, and explains
evidence. DETERMINISTIC code owns dates, filtering, dedup, budget, scoring and stopping. The
ledger tracks claim elements, synonyms, CPC branches, languages, citation branches and — the
stopping signal — NEW relevant families produced per query. Stop when marginal yield of new
families is consistently low across channels, capped by budget (not loop count).
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import embed, llm
from retrieval import Retriever
from search_modes import Mode, Subject, CombinationBuilder, ElementMapping, Basis, classify_basis, usable_for
from config import SEED_CPC, SEED_CPC_TITLES

FIELD = "vacuum gripping / suction lifting devices"


class CoverageLedger:
    def __init__(self, elements):
        self.elements = elements
        self.evidence = {e: [] for e in elements}      # element -> [dict(pub,family,coord,kind,score,basis)]
        self.synonyms = {e: set() for e in elements}
        self.cpc_branches = set()
        self.languages = set()
        self.citation_branches = set()
        self.families_seen = set()
        self.family_score = {}                          # family -> best fused score (for ranking)
        self.family_pid = {}                            # family -> representative publication_id
        self.round_new = []                             # new families per round
        self.channel_families = {}                      # channel -> set(families) (unique contribution)

    def add_evidence(self, element, hit):
        cur = self.evidence.setdefault(element, [])
        if not any(h["family"] == hit["family"] for h in cur):
            cur.append(hit)
            cur.sort(key=lambda h: h["score"], reverse=True)

    def register_families(self, scored_families, channel=None):
        """scored_families: list of (family_key, pid, fused_score). Tracks best score+pid."""
        fams = [f for f, _, _ in scored_families]
        new = [f for f in fams if f not in self.families_seen]
        self.families_seen.update(fams)
        for f, pid, s in scored_families:
            if s > self.family_score.get(f, -1):
                self.family_score[f] = s
                self.family_pid[f] = pid
        if channel is not None:
            self.channel_families.setdefault(channel, set()).update(fams)
        return len(new)

    def ranked_families(self):
        return [f for f, _ in sorted(self.family_score.items(), key=lambda t: t[1], reverse=True)]

    def element_coverage(self):
        return {e: (max([h["score"] for h in hs], default=0.0), len(hs))
                for e, hs in self.evidence.items()}

    def undercovered(self, min_score=0.35, min_hits=2):
        cov = self.element_coverage()
        return [e for e, (s, n) in cov.items() if s < min_score or n < min_hits]

    def note_round(self, n_new):
        self.round_new.append(n_new)

    def should_stop(self, budget_calls_left, max_rounds_reached):
        if budget_calls_left <= 0 or max_rounds_reached:
            return True
        # marginal yield low across the last two independent rounds
        if len(self.round_new) >= 2 and sum(self.round_new[-2:]) <= 1:
            return True
        return False


@dataclass
class AgentConfig:
    mode: str = "novelty"
    max_rounds: int = 6
    llm_call_budget: int = 40
    elements_per_round: int = 4
    evidence_per_element: int = 6


class CoverageAgent:
    def __init__(self, retriever: Retriever = None):
        self.r = retriever or Retriever()

    # ---- LLM language tasks (with robust fallbacks) --------------------------------------
    def decompose(self, text, subject=None):
        sys = ("You are a patent prior-art search analyst. Break an invention into the DISTINCT "
               "technical elements a prior-art search must separately cover. Return JSON "
               '{"elements":[short phrases]} with 5-12 concise element phrases.')
        out = llm.chat_json(sys, f"Field: {FIELD}\n\nInvention text:\n{text[:4000]}")
        els = [e.strip() for e in out.get("elements", []) if isinstance(e, str) and e.strip()]
        return els[:12] or ["vacuum gripper apparatus"]

    def plan(self, element, ledger):
        tried = sorted(ledger.synonyms.get(element, set()))
        sys = ("You expand a patent search for ONE element. Return JSON with keys: "
               '"queries" (2-4 natural-language search strings), "synonyms" (terms/phrases), '
               '"phrases" (exact multiword phrases to match), "cpc" (CPC symbols like B66C1/0225 '
               'if relevant), "assignees" (company names if the element implies a known maker), '
               '"de" (a German translation of the element for cross-lingual recall).')
        usr = (f"Field: {FIELD}\nElement: {element}\nAlready-tried synonyms: {tried}\n"
               f"Seed CPC context: { {k: SEED_CPC_TITLES[k] for k in SEED_CPC} }")
        out = llm.chat_json(sys, usr)
        ledger.synonyms.setdefault(element, set()).update(out.get("synonyms", []) or [])
        return out

    # ---- one search + ledger update ------------------------------------------------------
    def _run_search(self, query, subject, mode, ledger, element=None, cpc=None, phrases=None,
                    assignees=None, alt_vecs=None, cfg="agentic"):
        # sub-searches fuse by RRF only; a single cross-encoder rerank runs at report time
        # (spec §6: rerank the final cascade, not every sub-query — and it bounds CPU cost)
        res = self.r.search(query, subject=subject, mode=mode, config=cfg, cpc_hints=cpc,
                            phrases=phrases, assignee_hints=assignees, alt_query_vecs=alt_vecs,
                            do_rerank=False, topk=200)
        scored = [(fk, pid, float(sc)) for fk, pid, sc, _ in res.family_ranked]
        n_new = ledger.register_families(scored)
        for ch, pids in res.channel_hits.items():
            ledger.channel_families.setdefault(ch, set()).update(self.r.family_key(p) for p in pids[:100])
        if cpc:
            ledger.cpc_branches.update(cpc)
        # map the strongest reranked families to this element as grounded evidence
        if element:
            for fk, pid, score, prov in res.family_ranked[:6]:
                g = self._ground(pid, query, subject, mode)
                if g:
                    ledger.add_evidence(element, {"family": fk, "pub": g["pub"], "coord": g["coord"],
                                                  "kind": g["kind"], "score": float(score),
                                                  "basis": g["basis"], "channels": list(prov.keys())})
        return res, n_new

    def _ground(self, pid, query, subject, mode):
        """Best chunk of a publication vs the query -> grounded citation (pub#, coord, basis)."""
        qv = embed.embed_query(query[:2000], 768)
        vs = "[" + ",".join(f"{x:.6f}" for x in qv) + "]"
        with self.r.conn.cursor() as c:
            c.execute("SELECT p.publication_number, p.publication_date, p.filing_date, "
                      "p.earliest_priority_date, ch.kind, ch.coord "
                      "FROM chunks ch JOIN publications p ON p.id=ch.publication_id "
                      "WHERE ch.publication_id=%s AND ch.embedding IS NOT NULL "
                      "ORDER BY ch.embedding <=> %s LIMIT 1", (pid, vs))
            row = c.fetchone()
        if not row:
            return None
        basis = "n/a"
        if subject:
            b = classify_basis(dict(publication_date=row["publication_date"],
                                    earliest_priority_date=row["earliest_priority_date"],
                                    filing_date=row["filing_date"]), subject)
            basis = b.value
        return {"pub": row["publication_number"], "coord": row["coord"], "kind": row["kind"], "basis": basis}

    # ---- main loop -----------------------------------------------------------------------
    def run(self, query_text, subject=None, mode="novelty", cfg: AgentConfig = None):
        cfg = cfg or AgentConfig(mode=mode)
        m = Mode(mode)
        elements = self.decompose(query_text, subject)
        ledger = CoverageLedger(elements)
        ledger.languages.add("en")

        # seed round: broad search on the whole invention
        self._run_search(query_text, subject, m, ledger, element=None)
        # attribute seed hits to elements too
        for el in elements:
            self._run_search(el, subject, m, ledger, element=el)
        ledger.note_round(len(ledger.families_seen))

        rnd = 0
        while not ledger.should_stop(cfg.llm_call_budget - llm.usage()["calls"],
                                     rnd >= cfg.max_rounds):
            rnd += 1
            before = len(ledger.families_seen)
            targets = (ledger.undercovered() or elements)[:cfg.elements_per_round]
            for el in targets:
                if llm.usage()["calls"] >= cfg.llm_call_budget:
                    break
                plan = self.plan(el, ledger)
                alt_vecs = None
                de = plan.get("de")
                if de:
                    ledger.languages.add("de")
                    alt_vecs = [embed.embed_query(de, 768)]
                for q in (plan.get("queries") or [el])[:3]:
                    self._run_search(q, subject, m, ledger, element=el,
                                     cpc=plan.get("cpc"), phrases=plan.get("phrases"),
                                     assignees=plan.get("assignees"), alt_vecs=alt_vecs)
            ledger.note_round(len(ledger.families_seen) - before)

        return self.report(query_text, subject, m, ledger, rounds=rnd)

    # ---- report --------------------------------------------------------------------------
    def _final_rank(self, query_text, ledger, top=60):
        """Single cross-encoder rerank of the top RRF families (spec §6 step 4)."""
        ordered = sorted(ledger.family_score.items(), key=lambda t: t[1], reverse=True)
        head = ordered[:top]
        fam = [(fk, ledger.family_pid.get(fk), sc, {}) for fk, sc in head]
        reranked = self.r.rerank_families(query_text, fam, top=min(60, len(fam)))
        ranked = [fk for fk, _, _, _ in reranked] + [fk for fk, _ in ordered[top:]]
        return ranked

    def report(self, query_text, subject, mode, ledger: CoverageLedger, rounds):
        ranked_families = self._final_rank(query_text, ledger)
        # combinational (inventive-step) view: which reference supplies which element
        cb = CombinationBuilder(ledger.elements)
        element_report = {}
        for el, hits in ledger.evidence.items():
            usable = [h for h in hits if (not subject) or usable_for(Basis(h["basis"]), mode)] or hits
            element_report[el] = usable[:6]
            for h in usable[:3]:
                cb.add(ElementMapping(element=el, publication_number=h["pub"],
                                      basis=Basis(h["basis"]) if h["basis"] in Basis._value2member_map_ else Basis.PUBLIC_PRIOR_ART,
                                      coord=h["coord"] or {}, score=h["score"]))
        combination = cb.combination()
        cov = ledger.element_coverage()
        return {
            "query": query_text[:200],
            "subject": subject.number if subject else None,
            "mode": mode.value,
            "rounds": rounds,
            "n_families": len(ledger.families_seen),
            "elements": ledger.elements,
            "element_coverage": {e: {"best_score": round(s, 3), "n_evidence": n} for e, (s, n) in cov.items()},
            "element_evidence": element_report,
            "combination_view": combination,
            "channels_used": sorted(ledger.channel_families.keys()),
            "languages": sorted(ledger.languages),
            "cpc_branches": sorted(ledger.cpc_branches),
            "llm_usage": llm.usage(),
            "ranked_families": ranked_families,
            "channel_families": {k: sorted(v) for k, v in ledger.channel_families.items()},
            "round_new_families": ledger.round_new,
        }
