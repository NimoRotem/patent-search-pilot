"""Coverage-ledger agentic controller (spec §7).

The LLM generates queries, synonyms/terminology, CPC guesses and translations, and explains
evidence. DETERMINISTIC code owns dates, filtering, dedup, budget, scoring and stopping. The
ledger tracks claim elements, synonyms, CPC branches, languages, citation branches and — the
stopping signal — NEW relevant families produced per query. Stop when marginal yield of new
families is consistently low across channels, capped by budget (not loop count).
"""
from __future__ import annotations
from config import EMBED_DIM  # noqa: E402
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import os
import time
import traceback
import embed, llm, query_set
import retrieval
from retrieval import Retriever
from search_modes import Mode, CombinationBuilder, ElementMapping, Basis, classify_basis, usable_for
from config import SEED_CPC, SEED_CPC_TITLES

FIELD = "vacuum gripping / suction lifting devices"

#  How many families each retrieval pass hands to the coverage ledger. A whole-invention (seed)
#  pass is the ranking backbone and now also feeds a wide LLM screen (deep_rank), so it contributes
#  far more than the historic flat 200; an element pass is attribution, not ranking, and stays
#  cheap. MEASURED (RECALL_STUDY_2026-08-02.md): the reference the searcher named sat at fusion
#  rank 244 of 2,402 and only 25 were ever analysed, so depth of the ranked list is what the new
#  screen consumes.
SEED_TOPK = int(os.environ.get("AGENT_SEED_TOPK", "800"))
ELEMENT_TOPK = int(os.environ.get("AGENT_ELEMENT_TOPK", "200"))
#  A NOVELTY CLUSTER GETS MORE THAN A WHOLE-INVENTION QUERY DOES. A cluster asks for the 2 to 4
#  requirements the claim actually turns on, so a document deep in its list is a document that has
#  most of the combination, which is worth more to a novelty attack than the 800th document that
#  merely looks like the product. This is the "largest retrieval budget" the priority order is for.
CLUSTER_TOPK = int(os.environ.get("AGENT_CLUSTER_TOPK", str(SEED_TOPK + SEED_TOPK // 2)))


def _default_search_workers():
    """How many independent ANN passes run at once. AGENT_SEARCH_WORKERS=1 is the serial rollback.

    The old cap of 2 was sized for "two simultaneous UI searches on an 8-core database". The
    durable lanes changed that arithmetic: one worker per lane runs one search at a time, so the
    bound is the database's I/O overlap, and the passes are I/O-bound waits on an index bigger
    than RAM , the same shape that made three embedding shards 3.0x one stream on this DB."""
    try:
        requested = int(os.environ.get("AGENT_SEARCH_WORKERS", "4"))
    except (TypeError, ValueError):
        requested = 4
    return max(1, min(requested, 8))


class CoverageLedger:
    def __init__(self, elements):
        self.elements = elements
        self.evidence = {e: [] for e in elements}      # element -> [dict(pub,family,coord,kind,score,basis)]
        self.synonyms = {e: set() for e in elements}
        self.cpc_branches = set()
        self.languages = set()
        self.citation_branches = set()
        self.families_seen = set()
        self.query_set = []                             # the short queries the search actually ran
        self.novelty = {"units": [], "clusters": []}    # structured limitations + their clusters
        self.family_score = {}                          # family -> best fused score (any search)
        self.family_seed = {}                           # family -> fused score from the whole-query seed search
        self.family_elem = {}                           # family -> best fused score from an element search
        self.family_hits = {}                           # family -> # element sub-searches that surfaced it
        self.family_pid = {}                            # family -> representative publication_id
        self.round_new = []                             # new families per round
        self.channel_families = {}                      # channel -> set(families) (unique contribution)

    def add_evidence(self, element, hit):
        cur = self.evidence.setdefault(element, [])
        if not any(h["family"] == hit["family"] for h in cur):
            cur.append(hit)
            cur.sort(key=lambda h: h["score"], reverse=True)

    def register_families(self, scored_families, channel=None, bucket="element"):
        """scored_families: list of (family_key, pid, fused_score). `bucket` = 'seed' (the whole-
        query search — the vector-equivalent backbone) or 'element' (a per-element sub-search).
        We keep the seed ranking as the primary signal so the agent can't score BELOW plain
        vector at any k, then promote finds that are central (many element searches) or reached
        via citation — the agent's unique lift ranked ABOVE plain retrieval (spec §4)."""
        fams = [f for f, _, _ in scored_families]
        new = [f for f in fams if f not in self.families_seen]
        self.families_seen.update(fams)
        for f, pid, s in scored_families:
            if s > self.family_score.get(f, -1):
                self.family_score[f] = s
                self.family_pid[f] = pid
            if bucket == "seed":
                self.family_seed[f] = max(self.family_seed.get(f, 0.0), s)
            else:
                self.family_elem[f] = max(self.family_elem.get(f, 0.0), s)
                self.family_hits[f] = self.family_hits.get(f, 0) + 1
        if channel is not None:
            self.channel_families.setdefault(channel, set()).update(fams)
        return len(new)

    def final_score(self, f):
        """Seed (whole-query) score is the backbone; promote central / citation-reached finds.
        Guarantees the seed's strong hits stay at the top (agentic >= vector at every k) while
        letting a high-confidence unique find break into the head."""
        import math
        seed = self.family_seed.get(f, 0.0)
        elem = self.family_elem.get(f, 0.0)
        hits = self.family_hits.get(f, 0)
        cited = f in self.channel_families.get("citation", ())
        promotable = hits >= 3 or cited
        promote = 0.5 * elem if promotable else 0.0
        return seed + promote + 0.05 * max(seed, elem) * math.log1p(hits)

    def ranked_families(self):
        return [f for f in sorted(self.family_score, key=self.final_score, reverse=True)]

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
    ground: bool = True          # per-evidence coordinate grounding (costly); off for the ablation
    # "claim_agentic" BOOSTS claim text: it adds claim-restricted semantic + lexical channels on
    # top of the normal all-text dense channel. It used to REPLACE the all-text channel, which made
    # description paragraphs unsearchable (see retrieval.presets and RECALL_STUDY_2026-08-02.md).
    search_config: str = "agentic"
    # Bounded independent ANN passes; two UI jobs => at most four. Environment override permits
    # an operational serial fallback without reverting or changing ranking behavior.
    search_workers: int = field(default_factory=_default_search_workers)
    #  The cross-encoder head is the last ~60 s of a run. The find phase does not buy it; the
    #  ledger and grid phases do, and reorder the head their own way afterwards anyway.
    final_rerank: bool = True
    #  Find mode: fewer, narrower passes. Measured 2026-08-23: at 6-way concurrency each pass is
    #  27-74 s because the database is I/O-saturated, so the pass count is the wall clock.
    find_mode: bool = False
    #  Element passes probe dense + claim_dense at every depth: a full nine-channel element pass
    #  is 30-65 s under load against 3-15 s trimmed, and the element passes exist for per-element
    #  ATTRIBUTION, which the dense channels carry. Seed, query-set and round passes keep every
    #  channel. False restores the historical shape for an A/B.
    trim_element_channels: bool = True
    #  The uploaded patent's own claims, when the search started from a document. Each independent
    #  claim becomes its own query vector in the query set (query_set.build): a claim is the right
    #  query for a claim-level match even though, measured, it is a poor query on its own.
    input_claims: list = field(default_factory=list)
    #  The subject's OWN specification, for the claim construction. A patentee is his own
    #  lexicographer and his definition of a term is a construction of the claim, so the sentence
    #  that says what "170 to 190 degrees" means is usually one paragraph from the claim and
    #  nowhere in it. Empty is fine: the geometry rule still runs, the lexicography does not.
    input_spec: str = ""


class CoverageAgent:
    def __init__(self, retriever: Retriever = None):
        self.r = retriever or Retriever()

    # ---- LLM language tasks (with robust fallbacks) --------------------------------------
    def decompose(self, text, subject=None):
        """The technical elements, WITH the relationships between them left intact.

        THE INSTRUCTION THIS REPLACES said "break an invention into the DISTINCT technical
        elements". Audited on report adhoc-f1410b74df48 that is exactly what it did, and the two
        least conventional ideas in claim 1 did not survive it. The claim requires a first seal
        portion that is HARDER OR LESS COMPRESSIBLE THAN the second, and requires the two to
        cooperate so the first is PRESSED THROUGH an opening and REDUCES THE GAP AT A STEP in the
        object surface. What came out was "a vacuum seal element with a flexible, stretchable
        first portion exposed through openings" and "a compressible and deformable second portion
        supporting the first portion": three components, no relative property, no mechanism. A
        component list is a list of things ten thousand patents also have. Novelty lives in the
        relationship, so the relationship has to survive the decomposition.
        """
        sys = ("You are a patent prior-art search analyst preparing the searches for one "
               "invention. Return JSON {\"elements\":[short phrases]} with 5-12 phrases.\n\n"
               "Extract the technical elements AND, separately, the technically distinctive "
               "RELATIONSHIPS between them. Do NOT reduce a relationship into independent "
               "component descriptions. Preserve relative properties, spatial relationships, "
               "causal mechanisms and functional results whenever those relationships may be what "
               "distinguishes this invention from conventional ones.\n\n"
               "So, alongside the parts, emit phrases of these shapes wherever the text states "
               "them:\n"
               "  RELATIVE PROPERTY   'an outer seal layer harder and less compressible than the "
               "layer beneath it', never 'a seal layer'.\n"
               "  SPATIAL RELATION    'seal sections exposed through openings around the rim of "
               "the base', never 'a base with openings'.\n"
               "  CAUSAL MECHANISM    'the seal pressed out through an opening to bridge a step "
               "in the workpiece surface', never 'a seal'.\n"
               "  FUNCTIONAL RESULT   the effect the text says the arrangement must produce.\n\n"
               "Rank them: the distinctive relationships first, the unusual mechanisms next, the "
               "conventional supporting parts last. A battery, an alarm, a switch, a handle, a "
               "controller or a manual backup is in an enormous number of patents; emit at most "
               "two such phrases and put them at the end.\n\n"
               "Write every phrase in the words ANOTHER patent would use, not in this document's "
               "own drafting: no 'at least one', no 'said', no reference numerals, no private "
               "reference frame ('the second side of the base element' -> 'on the underside of "
               "the base'). These phrases are matched against documents written decades earlier "
               "in other languages, so a phrase only this applicant would write is a phrase "
               "nothing will match.")
        out = llm.chat_json(sys, f"Field: {FIELD}\n\nInvention text:\n{text[:4000]}") or {}
        raw = out.get("elements") or []                      # key may be present-but-null
        els = [e.strip() for e in raw if isinstance(e, str) and e.strip()]
        return els[:12] or ["vacuum gripper apparatus"]

    def plan(self, element, ledger):
        tried = sorted(ledger.synonyms.get(element, set()))
        sys = ("You expand a patent search for ONE element. Return JSON with keys: "
               '"queries" (2-4 natural-language search strings), "synonyms" (terms/phrases), '
               '"phrases" (exact multiword phrases to match), "cpc" (CPC symbols like B66C1/0225 '
               'if relevant), "assignees" (company names if the element implies a known maker), '
               '"de" (a German translation of the element for cross-lingual recall).\n\n'
               "KEEP THE RELATIONSHIP. If the element states a relative property ('harder than'), "
               "a spatial relation ('exposed through openings in') or a causal mechanism ('pressed "
               "through the opening so the gap at a step is reduced), every query you write must "
               "still contain it. Dropping it back to the component leaves a query that returns "
               "the whole field: 'seal support structure' is every seal ever made, while 'seal "
               "layer harder than the layer under it' is a small set and it is the right one.")
        usr = (f"Field: {FIELD}\nElement: {element}\nAlready-tried synonyms: {tried}\n"
               f"Seed CPC context: { {k: SEED_CPC_TITLES[k] for k in SEED_CPC} }")
        out = llm.chat_json(sys, usr) or {}                  # never trust the LLM to return a dict
        ledger.synonyms.setdefault(element, set()).update(out.get("synonyms") or [])
        return out

    # ---- one search + ledger update ------------------------------------------------------
    def _fetch_search(self, query, subject, mode, element=None, cpc=None, phrases=None,
                      assignees=None, alt_vecs=None, cfg="agentic", retriever=None,
                      wide=False, topk=None):
        """Execute one retrieval pass without mutating the shared coverage ledger.

        Keeping retrieval and ledger application separate lets independent element queries use
        separate PostgreSQL connections concurrently. Results are still applied in the original
        deterministic query order, so ranking, hit counts and stopping behavior stay unchanged.
        """
        r = retriever or self.r
        if cfg == "agentic":
            cfg = getattr(self, "_search_config", "agentic")
        # sub-searches fuse by RRF only; a single cross-encoder rerank runs at report time
        # (spec §6: rerank the final cascade, not every sub-query — and it bounds CPU cost)
        #  `topk` is how many families this pass contributes to the ledger. It used to be 200
        #  for every pass, which put a hard floor under how deep the wide screen (deep_rank) can
        #  ever see; a whole-invention pass now contributes SEED_TOPK.
        res = r.search(query, subject=subject, mode=mode, config=cfg, cpc_hints=cpc,
                       phrases=phrases, assignee_hints=assignees, alt_query_vecs=alt_vecs,
                       do_rerank=False, wide=wide,
                       topk=topk or (SEED_TOPK if wide else ELEMENT_TOPK))
        scored = [(fk, pid, float(sc)) for fk, pid, sc, _ in res.family_ranked]
        channel_families = {
            ch: [r.family_key(p) for p in pids[:100]]
            for ch, pids in res.channel_hits.items()
        }
        # map the strongest reranked families to this element as evidence
        evidence = []
        if element:
            qv = embed.embed_query(query[:2000], EMBED_DIM) if getattr(self, "_ground", True) else None
            for fk, pid, score, prov in res.family_ranked[:5]:
                if qv is not None:                       # full coordinate grounding (report path)
                    g = self._ground_vec(pid, qv, subject, retriever=r)
                    ev = {"family": fk, "pub": g["pub"], "coord": g["coord"], "kind": g["kind"],
                          "basis": g["basis"], "score": float(score), "channels": list(prov.keys())}
                else:                                    # light evidence (ablation): family + score
                    ev = {"family": fk, "pub": None, "coord": None, "kind": None,
                          "basis": "n/a", "score": float(score), "channels": list(prov.keys())}
                evidence.append(ev)
        return {
            "result": res,
            "scored": scored,
            "channel_families": channel_families,
            "cpc": list(cpc or []),
            "element": element,
            "evidence": evidence,
        }

    @staticmethod
    def _apply_search(fetched, ledger, is_seed=False):
        """Apply a completed retrieval pass to the ledger in deterministic query order."""
        n_new = ledger.register_families(
            fetched["scored"], bucket=("seed" if is_seed else "element"))
        for ch, families in fetched["channel_families"].items():
            ledger.channel_families.setdefault(ch, set()).update(families)
        ledger.cpc_branches.update(fetched["cpc"])
        if fetched["element"]:
            for ev in fetched["evidence"]:
                ledger.add_evidence(fetched["element"], ev)
        return fetched["result"], n_new

    def _run_search(self, query, subject, mode, ledger, element=None, cpc=None, phrases=None,
                    assignees=None, alt_vecs=None, cfg="agentic", is_seed=False, wide=False,
                    topk=None):
        fetched = self._fetch_search(
            query, subject, mode, element=element, cpc=cpc, phrases=phrases,
            assignees=assignees, alt_vecs=alt_vecs, cfg=cfg, wide=wide, topk=topk)
        res, n_new = self._apply_search(fetched, ledger, is_seed=is_seed)
        return res, n_new

    def _ground_vec(self, pid, qv, subject, retriever=None):
        """Best chunk of a publication vs a PRE-COMPUTED query vector -> grounded citation."""
        r = retriever or self.r
        vs = "[" + ",".join(f"{x:.6f}" for x in qv) + "]"
        with r.conn.cursor() as c:
            c.execute("SELECT p.publication_number, p.publication_date, p.filing_date, "
                      "p.earliest_priority_date, ch.kind, ch.coord "
                      "FROM chunks ch JOIN publications p ON p.id=ch.publication_id "
                      "WHERE ch.publication_id=%s AND ch.embedding IS NOT NULL "
                      "ORDER BY ch.embedding <=> %s LIMIT 1", (pid, vs))
            row = c.fetchone()
        if not row:
            return {"pub": None, "coord": None, "kind": None, "basis": "n/a"}
        basis = "n/a"
        if subject:
            b = classify_basis(dict(publication_date=row["publication_date"],
                                    earliest_priority_date=row["earliest_priority_date"],
                                    filing_date=row["filing_date"]), subject)
            basis = b.value
        return {"pub": row["publication_number"], "coord": row["coord"], "kind": row["kind"], "basis": basis}

    # ---- main loop -----------------------------------------------------------------------
    def run(self, query_text, subject=None, mode="novelty", cfg: AgentConfig = None, on_event=None):
        """Run one agent job with an isolated per-search LLM budget and usage record."""
        with llm.usage_session():
            return self._run(query_text, subject=subject, mode=mode, cfg=cfg,
                             on_event=on_event)

    def _run(self, query_text, subject=None, mode="novelty", cfg: AgentConfig = None,
             on_event=None):
        """`on_event(stage, data)` (optional) streams progress to the UI: 'elements' (decomposed),
        'partial' (a fast, un-reranked snapshot after the seed search — the first cards the user
        sees, in ~a few seconds), and 'round' (per refinement round). It must never raise."""
        cfg = cfg or AgentConfig(mode=mode)
        self._ground = cfg.ground
        self._search_config = cfg.search_config
        m = Mode(mode)

        def emit(stage, data):
            if on_event:
                try:
                    on_event(stage, data)
                except Exception:
                    pass

        #  Everything that gets EMBEDDED uses the de-figured text. `query_text` itself is kept
        #  intact for the saved report and the page, because the searcher wrote it and the figure
        #  description is real information for a reader; it is only a poison for a query vector.
        #  MEASURED (RECALL_STUDY_2026-08-02.md): deleting the folded-in figure prose moved a named
        #  reference from dense rank #528 to #35, with nothing else changed.
        rank_text = query_set.retrieval_text(query_text) or query_text
        self._rank_text = rank_text
        #  THE FIRST SEED PASS DOES NOT NEED THE DECOMPOSITION, so the two run together: the seed
        #  searches the whole-invention text while the LLM splits it into elements. On the
        #  measured find run the decomposition was ~40 s spent in front of a search that never
        #  reads the elements. Fetch-only here; the ledger does not exist yet, so the result is
        #  applied after it does.
        _split_seed = self._search_config == "claim_agentic"
        _seed_pool = _seed_future = None
        _fork = getattr(getattr(self, "r", None), "fork", None)
        if callable(_fork) and int(cfg.search_workers) > 1:
            def _seed_fetch():
                worker = _fork()
                t0 = time.monotonic()
                try:
                    kw = {"cfg": ["claim_dense"]} if _split_seed else {}
                    return (self._fetch_search(rank_text, subject, m, retriever=worker,
                                               element=None, wide=True, **kw),
                            round(time.monotonic() - t0, 1))
                finally:
                    close = getattr(worker, "close", None)
                    if callable(close):
                        close()

            _seed_pool = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="patent-agent-seed")
            _seed_future = _seed_pool.submit(_seed_fetch)
        elements = self.decompose(rank_text, subject)
        ledger = CoverageLedger(elements)
        ledger.languages.add("en")
        emit("elements", {"n": len(elements), "elements": elements})

        # Every retrieval pass can take several seconds against the growing ANN index. Emit a real
        # counter after each one so the browser does not sit on a single sentence for minutes while
        # the seed-element and agentic-round searches advance. `search_max` is an honest upper
        # bound: early stopping or an LLM returning fewer than three queries can finish sooner.
        search_done = 0
        split_claim_seed = self._search_config == "claim_agentic"
        #  Honest upper bound: seed pass(es) + the query set + per-element seed passes + rounds.
        search_max = ((2 if split_claim_seed else 1)
                      + query_set.max_clusters()             # the novelty combinations, first
                      + 6 + query_set.MAX_CLAIMS + min(8, len(elements))
                      + query_set.max_limitations() * 3      # construction and alternative each
                      + cfg.max_rounds * cfg.elements_per_round * 3)

        def searched(progress_stage, query, *args, progress_round=None, **kwargs):
            nonlocal search_done
            started = time.monotonic()
            result = self._run_search(query, *args, **kwargs)
            search_done += 1
            detail = {
                "search_done": search_done,
                "search_max": search_max,
                "search_seconds": round(time.monotonic() - started, 1),
                "families": len(ledger.families_seen),
            }
            print(f"[agent] pass {search_done}/{search_max} {detail['search_seconds']}s "
                  f"serial fam={detail['families']}", flush=True)
            if progress_round is not None:
                detail["round"] = progress_round
            emit(progress_stage, detail)
            return result

        def searched_batch(progress_stage, specs, progress_round=None, yield_gate=False):
            """Run independent passes concurrently, then apply them in their original order.

            A psycopg connection cannot be shared by concurrent queries. Production Retriever
            supplies ``fork()`` so each worker owns one connection while sharing only the large,
            read-only family map. Test doubles and adapters without ``fork`` keep the historical
            serial behavior. Two workers is deliberate: the app allows two simultaneous searches
            and the database has eight CPUs, so the worst case remains bounded at four ANN passes.
            """
            nonlocal search_done
            specs = list(specs)
            if not specs:
                return []
            fork = getattr(getattr(self, "r", None), "fork", None)
            workers = min(max(1, int(cfg.search_workers)), len(specs))
            if workers == 1 or not callable(fork):
                return [searched(
                    progress_stage, spec["query"], subject, m, ledger,
                    progress_round=progress_round,
                    **{k: v for k, v in spec.items() if k != "query"})
                    for spec in specs]

            def fetch(spec):
                worker = fork()
                started = time.monotonic()
                try:
                    kwargs = {k: v for k, v in spec.items()
                              if k not in ("query", "is_seed")}
                    result = self._fetch_search(
                        spec["query"], subject, m, retriever=worker, **kwargs)
                    return result, round(time.monotonic() - started, 1)
                finally:
                    close = getattr(worker, "close", None)
                    if callable(close):
                        close()

            #  THE YIELD GATE. MEASURED on run FT-D (871 s, 5,005 families): passes 25 to 30
            #  took 23.5 s, 23.6 s, 23.5 s, 65.0 s, 64.5 s and 64.4 s, and moved the family count
            #  from 4,912 to 5,005. That is 60 to 90 s of wall clock on a six-worker pool for 93
            #  new families out of 5,005, i.e. 1.9%. The tail of a pass loop is where the slow
            #  queries live, because the slow queries are the long ones.
            #
            #  It gates by CANCELLING the futures that have not started rather than by submitting
            #  in waves: waves would make every pass wait for the slowest member of its wave, which
            #  costs more throughput than the gate saves. At most `workers` passes are already
            #  running when it fires, and their results are discarded.
            #
            #  Only the caller asks for it, and only the refinement rounds do. The whole-invention
            #  seed passes and the per-element attribution passes always run in full: they are what
            #  the ranking backbone and the coverage ledger are built from, and a ledger with an
            #  element nothing was ever searched for reads as "no prior art" rather than "not
            #  looked for".
            gate_pct = 0.0
            gate_win = 3
            if yield_gate:
                try:
                    import search_settings as _ss
                    gate_pct = float(_ss.get("pass_yield_min_pct"))
                    gate_win = int(_ss.get("pass_yield_window"))
                except Exception:
                    gate_pct = 0.0
            lean = 0

            out = []
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="patent-agent-search") as ex:
                futures = [ex.submit(fetch, spec) for spec in specs]
                # Consume in input order. All futures are already running/queued, so this keeps
                # ledger tie behavior deterministic without forfeiting retrieval concurrency.
                for spec, future in zip(specs, futures):
                    if gate_pct > 0 and lean >= gate_win:
                        stopped = sum(1 for f in futures if f.cancel())
                        print(f"[agent] yield gate: {gate_win} passes under {gate_pct}% new "
                              f"families; {stopped} of {len(specs)} passes cancelled "
                              f"at fam={len(ledger.families_seen)}", flush=True)
                        emit("pass_gate", {"cancelled": stopped, "of": len(specs),
                                           "window": gate_win, "min_pct": gate_pct,
                                           "families": len(ledger.families_seen)})
                        break
                    fetched, seconds = future.result()
                    before_fam = len(ledger.families_seen)
                    result = self._apply_search(
                        fetched, ledger, is_seed=bool(spec.get("is_seed")))
                    if gate_pct > 0:
                        total = max(1, len(ledger.families_seen))
                        gained = len(ledger.families_seen) - before_fam
                        lean = lean + 1 if (100.0 * gained / total) < gate_pct else 0
                    search_done += 1
                    print(f"[agent] pass {search_done}/{search_max} {seconds}s "
                          f"batched({workers}w) wide={bool(spec.get('wide'))} "
                          f"fam={len(ledger.families_seen)}", flush=True)
                    detail = {
                        "search_done": search_done,
                        "search_max": search_max,
                        "search_seconds": seconds,
                        "families": len(ledger.families_seen),
                    }
                    if progress_round is not None:
                        detail["round"] = progress_round
                    emit(progress_stage, detail)
                    out.append(result)
            return out

        # Seed round: expose the vector-equivalent backbone before slower lexical/graph expansion.
        # In claims mode the previous combined pass waited for claim_bm25 before emitting anything;
        # a production 25M-chunk query measured 331.8 s.  Claim-dense already yields the useful
        # semantic head, so stream it first and continue the precise lexical/CPC/citation channels
        # as the next counted pass.  The all-text path keeps its established single-pass ranking.
        if _seed_future is not None:
            fetched, _seed_seconds = _seed_future.result()
            _seed_pool.shutdown(wait=False)
            self._apply_search(fetched, ledger, is_seed=True)
            search_done += 1
            emit("search_progress", {"search_done": search_done, "search_max": search_max,
                                     "search_seconds": _seed_seconds,
                                     "families": len(ledger.families_seen)})
            emit("partial", {"report": self.report(
                query_text, subject, m, ledger, rounds=0, rerank=False)})
            if split_claim_seed:
                searched("search_progress", rank_text, subject, m, ledger, element=None,
                         is_seed=True, wide=True,
                         cfg=["dense", "brief_dense", "claim_bm25", "cpc", "citation", "qbe"])
        elif split_claim_seed:
            searched("search_progress", rank_text, subject, m, ledger,
                     element=None, is_seed=True, cfg=["claim_dense"], wide=True)
            emit("partial", {"report": self.report(
                query_text, subject, m, ledger, rounds=0, rerank=False)})
            searched("search_progress", rank_text, subject, m, ledger, element=None,
                     is_seed=True, wide=True,
                     cfg=["dense", "brief_dense", "claim_bm25", "cpc", "citation", "qbe"])
        else:
            searched("search_progress", rank_text, subject, m, ledger,
                     element=None, is_seed=True, wide=True)
            emit("partial", {"report": self.report(
                query_text, subject, m, ledger, rounds=0, rerank=False)})

        #  THE QUERY SET. One long brief is one blurred vector; a set of short, individually
        #  coherent whole-invention queries each retrieves its own neighbourhood and fusion
        #  recovers the intersection. MEASURED: the live brief put a named reference at dense rank
        #  #528, a 30-word essence sentence put it at #2, and fusing 14 short queries put it at
        #  #37 while finding it in 9 of the 14. These are SEED-bucket passes: they describe the
        #  whole invention, so they belong in the ranking backbone, not in element attribution.
        #  THE STRUCTURED READING OF THE CLAIMS, once, and shared. Every requirement with its
        #  components, the relationship between them, the result that relationship must produce,
        #  its embodiment branches, how generic it is and how much of the novelty it carries; then
        #  the 2-to-4 combinations built from those. The query builder spends the retrieval budget
        #  from it, the external planner asks the outside world about it before it asks about
        #  analogies, and the report carries it so a reader can see what was searched and why.
        analysis = {"units": [], "clusters": []}
        if cfg.input_claims:
            try:
                analysis = query_set.novelty_analysis(
                    list(cfg.input_claims), spec_text=(cfg.input_spec or ""))
            except Exception:
                traceback.print_exc()
        ledger.novelty = analysis
        if analysis.get("units"):
            emit("novelty_units", {"n": len(analysis["units"]),
                                   "n_clusters": len(analysis.get("clusters") or []),
                                   "source": analysis.get("source")})
        try:
            specs = query_set.build(query_text, elements=elements,
                                    claims=(cfg.input_claims or []),
                                    spec_text=(cfg.input_spec or ""), analysis=analysis)
        except Exception:
            traceback.print_exc()
            specs = []
        extra = [s for s in query_set.seed_specs(specs)
                 if s.kind != "brief" and s.text.strip() != rank_text.strip()]
        if cfg.find_mode:
            #  Five whole-invention queries carry the seed bucket for a find; the full set , and
            #  the wide scan profile , belong to the phases that read what they retrieve. `specs`
            #  is priority-ordered, so the five are the novelty clusters and the essence, not
            #  whichever five happened to be built first.
            extra = extra[:5]
        if extra:
            emit("query_set", {"n": len(extra),
                               "queries": [s.as_dict() for s in extra]})
            ledger.query_set = [s.as_dict() for s in extra]
            _find_cfg = ["dense", "claim_dense"] if cfg.find_mode else None
            searched_batch("seed_progress", [
                dict({"query": s.text, "is_seed": True, "wide": not cfg.find_mode,
                      #  THE LARGEST SHARE GOES TO THE COMBINATIONS. A cluster asks for 2 to 4
                      #  requirements at once, which is what a document has to disclose to be
                      #  prior art against the claim rather than evidence about one part of it,
                      #  so it contributes more depth to the ledger than a single-vector
                      #  whole-invention query does.
                      "topk": (CLUSTER_TOPK if (s.kind == "cluster" and not cfg.find_mode)
                               else None)},
                     **({"cfg": _find_cfg} if _find_cfg else {})) for s in extra
            ])
        # attribute seed hits to elements too (cap the per-element seed searches for runtime)
        _find_cfg = (["dense", "claim_dense"]
                     if (cfg.find_mode or cfg.trim_element_channels) else None)
        #  WHEN THE CLAIMS ARE PRESENT THE ELEMENTS ARE THE WEAKER LIST. The limitation and
        #  cluster passes above are verbatim cuts of the legal requirement and the combinations
        #  they form; the element list is a model's paraphrase of the same device. Paying for
        #  eight of each is paying twice, and the second payment is the one that buys "a battery
        #  housed in a handle" its own retrieval pass. `qs_elements_with_claims` is the knob.
        n_element_passes = 6 if cfg.find_mode else 8
        if cfg.input_claims:
            try:
                import search_settings as _ss
                n_element_passes = min(n_element_passes, int(_ss.get("qs_elements_with_claims")))
            except Exception:
                pass
        searched_batch("seed_progress", [
            dict({"query": el, "element": el},
                 **({"cfg": _find_cfg} if _find_cfg else {}))
            for el in elements[:n_element_passes]
        ])

        #  ONE PASS PER CLAIM LIMITATION. A claim is a conjunction, and the art that discloses its
        #  third requirement resembles the invention barely at all: measured, every reference an
        #  attorney filed against US 2026/0109053 A1 was cited for exactly ONE requirement, and a
        #  query built from the whole invention ranked them nowhere. Measured the other way on the
        #  v2 corpus, one brief-shaped query put 1 of 6 known-relevant references in the top 25 and
        #  three limitation-shaped queries put 3 of 6 in the union of their top 30.
        #
        #  `element=None`, deliberately. These are candidate-producing passes, not attribution: the
        #  coverage ledger was constructed from the element list, and handing it a limitation as
        #  though it were an element would put a row in it that nothing else in the pipeline knows
        #  about. The per-limitation verdicts come later, from the ledger's own reading.
        #  Constructions ride with the limitations: they are the same question asked in the
        #  words the art actually uses, and on the case this was built for they are the ONLY
        #  form that reaches the art at all.
        #  `specs` is priority-ordered, so this list is already core-first: relative properties and
        #  mechanisms before conventional supporting parts, and the generic ones capped by
        #  `qs_max_generic_limitations` before they ever get here. An ALTERNATIVE is one embodiment
        #  branch of a requirement that states several, searched apart from its siblings because a
        #  layered composite and a fluid-filled bladder sit in different prior art.
        lim_specs = [s for s in specs if s.kind in ("limitation", "alternative", "construction")]
        if cfg.find_mode:
            #  The find tier buys reach, not depth: six requirements on the cheap dense channels is
            #  seconds, and it is where the measured recall gain sits.
            lim_specs = lim_specs[:6]
        if lim_specs:
            emit("limitation_queries", {"n": len(lim_specs),
                                        "queries": [s.as_dict() for s in lim_specs]})
            searched_batch("seed_progress", [
                dict({"query": s.text, "element": None},
                     **({"cfg": _find_cfg} if _find_cfg else {})) for s in lim_specs])

        ledger.note_round(len(ledger.families_seen))
        emit("seeded", {"families": len(ledger.families_seen)})
        #  A SECOND SNAPSHOT, once the query set and the element passes have run. There used to be
        #  exactly one 'partial' — the first seed pass — and everything the agent found after it
        #  was invisible until the whole run finished and the page reloaded. The open report
        #  streams each snapshot's new references in (webapp.api_cards), so the list grows while
        #  the search works instead of standing still for a quarter of an hour.
        emit("partial", {"report": self.report(
            query_text, subject, m, ledger, rounds=0, rerank=False)})

        rnd = 0
        while not ledger.should_stop(cfg.llm_call_budget - llm.usage()["calls"],
                                     rnd >= cfg.max_rounds):
            rnd += 1
            before = len(ledger.families_seen)
            targets = (ledger.undercovered() or elements)[:cfg.elements_per_round]
            round_specs = []
            for el in targets:
                if llm.usage()["calls"] >= cfg.llm_call_budget:
                    break
                plan = self.plan(el, ledger)
                alt_vecs = None
                de = plan.get("de")
                if de:
                    ledger.languages.add("de")
                    alt_vecs = [embed.embed_query(de, EMBED_DIM)]
                for q in (plan.get("queries") or [el])[:3]:
                    round_specs.append({
                        "query": q,
                        "element": el,
                        "cpc": plan.get("cpc"),
                        "phrases": plan.get("phrases"),
                        "assignees": plan.get("assignees"),
                        "alt_vecs": alt_vecs,
                    })
            searched_batch("round_progress", round_specs, progress_round=rnd, yield_gate=True)
            ledger.note_round(len(ledger.families_seen) - before)
            emit("round", {"round": rnd, "families": len(ledger.families_seen)})
            #  and one per refinement round, for the same reason.
            emit("partial", {"report": self.report(
                query_text, subject, m, ledger, rounds=rnd, rerank=False)})

        emit("reranking", {"families": len(ledger.families_seen)})

        # The cross-encoder head is the last ~60 s of the run and used to be a single opaque call,
        # so the UI froze on one message for roughly half the run. Report through it.
        def _rerank_progress(done, total):
            emit("rerank_progress", {"done": done, "total": total,
                                     "families": len(ledger.families_seen)})

        return self.report(query_text, subject, m, ledger, rounds=rnd,
                           rerank=cfg.final_rerank, on_progress=_rerank_progress)

    # ---- report --------------------------------------------------------------------------
    def _final_rank(self, query_text, ledger, top=None, rerank=True, on_progress=None,
                    return_meta=False):
        """Rank by final_score (seed backbone + centrality/citation promote), then cross-encoder
        rerank the head only (reranking within the head can't change recall@100 — the top-100
        set is fixed). (spec §4 + §6 step 4). `rerank=False` skips the (slow, CPU) cross-encoder —
        used for the fast progressive/partial snapshot that streams to the UI before the full run.

        `top` defaults to retrieval.RERANK_TOP. It used to be hard-coded to 25 in TWO places here
        while retrieval.RERANK_TOP said 50 and carried a comment explaining the raise, so the raise
        never reached the live path and the progress bar reported a depth that was never scored."""
        top = retrieval.RERANK_TOP if top is None else top
        ordered = sorted(ledger.family_score, key=ledger.final_score, reverse=True)
        if not rerank:
            meta = {"attempted": False, "applied": False, "scored": 0,
                    "requested": 0, "model": "BAAI/bge-reranker-v2-m3"}
            return (ordered, meta) if return_meta else ordered
        head = ordered[:top]
        fam = [(fk, ledger.family_pid.get(fk), ledger.final_score(fk), {}) for fk in head]
        outcome = self.r.rerank_families(getattr(self, "_rank_text", query_text), fam,
                                         top=min(retrieval.RERANK_TOP, len(fam)),
                                         on_progress=on_progress, return_meta=return_meta)
        if (return_meta and isinstance(outcome, tuple) and len(outcome) == 2
                and isinstance(outcome[1], dict)):
            reranked, meta = outcome
        else:
            # Compatibility for simple Retriever test doubles and third-party adapters which
            # still implement the historical list-only return contract.
            reranked = outcome
            meta = {"attempted": bool(head), "applied": None, "scored": None,
                    "requested": len(head), "model": "BAAI/bge-reranker-v2-m3"}
        ranked = [fk for fk, _, _, _ in reranked] + ordered[top:]
        return (ranked, meta) if return_meta else ranked

    def report(self, query_text, subject, mode, ledger: CoverageLedger, rounds, rerank=True,
               on_progress=None):
        ranked_families, cross_encoder_rerank = self._final_rank(
            query_text, ledger, rerank=rerank, on_progress=on_progress, return_meta=True)
        # combinational (inventive-step) view: which reference supplies which element
        cb = CombinationBuilder(ledger.elements)
        element_report = {}
        for el, hits in ledger.evidence.items():
            usable = [h for h in hits if (not subject) or h["basis"] not in Basis._value2member_map_
                      or usable_for(Basis(h["basis"]), mode)] or hits
            element_report[el] = usable[:6]
            for h in usable[:3]:
                cb.add(ElementMapping(element=el, publication_number=h["pub"],
                                      basis=Basis(h["basis"]) if h["basis"] in Basis._value2member_map_ else Basis.PUBLIC_PRIOR_ART,
                                      coord=h["coord"] or {}, score=h["score"]))
        combination = cb.combination()
        cov = ledger.element_coverage()
        return {
            # This report is the durable saved record.  Keeping only 200 characters made a long
            # disclosure impossible to recover from History/export even though the search itself
            # used the full text.  Cap only pathological payloads; the UI clamps visually.
            "query": query_text[:20000],
            "subject": subject.number if subject else None,
            "mode": mode.value,
            "search_focus": ("claims" if getattr(self, "_search_config", "agentic")
                             == "claim_agentic" else "all_text"),
            "rounds": rounds,
            "n_families": len(ledger.families_seen),
            "elements": ledger.elements,
            "element_coverage": {e: {"best_score": round(s, 3), "n_evidence": n} for e, (s, n) in cov.items()},
            "element_evidence": element_report,
            "combination_view": combination,
            "channels_used": sorted(ledger.channel_families.keys()),
            "query_set": list(getattr(ledger, "query_set", []) or []),
            #  ON THE REPORT, because "what was searched" was previously answerable only for the
            #  eleven seed queries: the element passes, the round queries and every limitation
            #  query were reconstructible from nothing. This is the structure the budget was spent
            #  from, so a reader can see which requirement carried the novelty and which one was
            #  treated as a conventional part.
            "novelty": dict(getattr(ledger, "novelty", None) or {"units": [], "clusters": []}),
            "languages": sorted(ledger.languages),
            "cpc_branches": sorted(ledger.cpc_branches),
            "llm_usage": llm.usage(),
            "cross_encoder_rerank": cross_encoder_rerank,
            "ranked_families": ranked_families,
            "channel_families": {k: sorted(v) for k, v in ledger.channel_families.items()},
            "round_new_families": ledger.round_new,
        }
