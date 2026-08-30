# Pilot Results — Patent Semantic + Agentic Search (Vacuum-Gripping field)

_Run 2026-07-17. Evaluation-first pilot per `patent-search-pilot-spec.md`. All numbers are real,
produced by the code in this repo against a live 107,795-publication / 1,819,616-vector corpus._

## What was built and run (spec §9 build order)

1. Postgres 17 + pgvector 0.8.5 (HNSW), Docker, memory-limited — single cheap box, no GPU.
2. Normalized provenance-aware schema + jurisdiction-neutral novelty/inventive-step **date engine**
   (Art.54(2) public art vs Art.54(3) secret prior art vs priority-interval — verified live in the
   demo report's `public_prior_art` / `secret_prior_art` tags).
3. **BigQuery coverage profile** → confirmed the spec's hypothesis exactly: US full text strong
   (claims 90.6%, desc 100%); **EP/WO/DE claims & description 0% in BigQuery** (the enrichment hole).
4. **Frozen gold set** (built before the index): 11 searches, relevance = each anchor's examiner
   citations resolved to DOCDB families (CLEF-IP method) + curated competitors; citation edges hidden.
5. Ingest: 25,786 seed-CPC core + 82,009 family/backward-citation expansion.
6. Chunk + embed **every claim** (own + parent-resolved), paragraphs, figures, cross-lingual DE.
7. HNSW + FTS; 8-channel adaptive cascade + reciprocal-rank fusion + family dedup + reranker.
8. Coverage-ledger agent (LLM makes queries/synonyms/CPC/translations; deterministic code owns
   dates/dedup/budget/scoring/stopping).
9. **5-config ablation + dimension benchmark** below.

## 5-config ablation — mean family recall (macro-avg, 11 gold searches)

| Config | recall@100 | recall@500 | recall@1000 | earliest-relevant recovered |
|---|--:|--:|--:|--:|
| keyword (BM25) | 0.00 | 0.09 | 0.11 | 0/11 |
| **vector (dense)** | **0.175** | **0.243** | **0.302** | **3/11** |
| hybrid | 0.09 | 0.20 | 0.25 | 1/11 |
| hybrid+reranker | 0.09 | 0.20 | 0.25 | 1/11 |
| agentic | 0.067 | 0.198 | 0.237 | 2/11 |

## Dimension benchmark (768 vs 1024 vs 3072, recall@50 on the gold-relevant subset)

| 768 | 1024 | 3072 |
|--:|--:|--:|
| 0.825 | 0.825 | 0.825 |

**→ 768 dimensions suffice** — no measurable recall gain from 1024 or 3072. At full scale this is the
difference between ~1 TB and ~4 TB of raw vectors (spec appendix), so the pilot's headline decision is:
**embed at 768.**

## What the numbers say (the point of an evaluation-first pilot)

1. **Semantic beats lexical decisively for examiner-citation recall.** Keyword/BM25 recovers ~0% at
   rank 100; dense recovers ~5–43% per query. Examiners cite art that uses *different terminology*
   than the patent — exactly where lexical search fails and embeddings win. This alone justifies the
   vector index.
2. **Naive hybrid fusion HURT here.** RRF-fusing a strong channel (dense) with a weak one (BM25)
   pulled recall@100 below pure dense (0.09 vs 0.175). Lesson for production: **weight/learn the
   fusion** (or gate weak channels) rather than uniform RRF.
3. **The agent's value is unique coverage, not top-k precision.** It surfaced gold families **no other
   config found** — e.g. 5–10 unique examiner-cited families per GRABO query via terminology + CPC
   expansion (see `eval_report.md` "Unique-contribution findings"), and its CPC search auto-expanded
   into the neighbouring **F16J15 seal** classes. But broad expansion lowers precision@100, and this
   run *cost-constrained* the agent (2 rounds, grounding off, BM25 dropped from its hot loop) to fit
   CPU/time — a recall-tuned agent is the obvious next experiment.
4. **Description embeddings were deferred** (search-critical abstract+claims tier embedded: 1.24M of
   1.82M vectors). Descriptions carry a lot of prior-art disclosure, so current recall is a floor.
5. **Absolute recall is modest** because gold = examiner citations (a deliberately hard, narrow target)
   and top-20 is mostly relevant-but-not-in-gold (nongold@20 ≈ 0.99). This is a known property of the
   citations-as-relevance methodology, not a defect.

## Agentic report quality (spec §7) — see `data/reports/grabo_gripper_novelty.md`

For GRABO's own vacuum gripper, the agent decomposed claim 1 into 12 elements and produced a grounded,
element-by-element prior-art map: every reference cited with **publication number + claim/paragraph
coordinate + legal basis** (public vs secret prior art), a **combinational inventive-step view**
(primary reference + secondaries → which element each supplies), and a decreasing marginal-yield
stopping signal `[713, 472, 218, 110, 76, 71, 31]`. The 6 narrowest elements (e.g. "seal protrudes no
greater than its thickness", "bracing structure") returned **zero prior art** — i.e. the search
isolated exactly the granted patent's distinguishing features. That is the behavior a real prior-art
search must have.

## Recommended next steps (only now that numbers exist — spec §9)

- Embed descriptions (finish the 1.82M) and re-measure; expect the biggest recall lift here.
- Replace uniform RRF with weighted/learned fusion; gate BM25 when it underperforms.
- Tune the agent for recall (more rounds/channels, keep grounding) rather than the cost-capped config.
- Add real BM25 (ParadeDB `pg_search`) instead of the ts_rank/count stand-in.
- Provision EPO OPS credentials to enrich the full EP/WO/DE core (only external dependency).
- Ship production at **768-dim**, national-office feeds, HNSW — the pilot's numbers support it.
