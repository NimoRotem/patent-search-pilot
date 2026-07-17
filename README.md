# Patent Semantic + Agentic Search — Pilot (Vacuum-Gripping field)

Implements `patent-search-pilot-spec.md`: prove **retrieval quality and agent behavior** on one
judge-by-eye technical field (GRABO's vacuum-gripping domain) on cheap, single-box, no-GPU
hardware, evaluation-first — before committing to a worldwide corpus.

## What's built (spec section → code)

| Spec | Implementation |
|---|---|
| §1 Cheap single box, pgvector HNSW | `docker-compose.yml` — Postgres 17 + pgvector 0.8.5, memory-limited. No GPU/Redis/Celery. |
| §2 BigQuery bootstrap + coverage-first + provenance | `bqclient.py`, `coverage_profile.py`, `ingest_bq.py` (one full-text scan → staging), `ingest_pg.py` |
| §2.3 Official-source enrichment (EPO OPS/USPTO) | `enrich.py` (SerpApi/ScrapingBee fallback; OPS drop-in TODO) |
| §3 Normalized, provenance-aware schema | `sql/001_schema.sql` — separate tables, claims-as-rows, kind_code separate, `field_provenance` ledger |
| §4 Chunk EVERY claim, hierarchical, cross-lingual | `patent_text.py`, `chunker.py`, `embed.py` (768 main + 1024/3072 bench) |
| §5 Jurisdiction-neutral date/status engine | `search_modes.py` — novelty (incl. Art.54(3) secret art + priority interval), inventive-step; invalidity/FTO/landscape stubbed |
| §6 Adaptive cascade + RRF + reranker | `retrieval.py` (8 channels, reciprocal-rank fusion, family dedup), `rerank.py` (bge-reranker-v2-m3, CPU) |
| §7 Coverage-ledger agent | `agent.py` — LLM makes queries/synonyms/CPC/translations; deterministic code owns dates/dedup/budget/scoring/stopping; marginal-yield stop |
| §7 Grounded element-by-element report | `report.py` — cites pub# + claim/para coordinate; combination view |
| §8 Frozen gold set + 5-config ablation | `goldset.py` (citation-derived relevance, edges hidden), `evaluate.py` |
| §9 Build order | `run.sh` |

## Pilot corpus (actual)

- **107,795 publications** (25,786 core seed-CPC + 82,009 family/backward-citation expansion), US/EP/WO/DE, all dates.
- **1,819,616 chunk vectors** (571,817 own claims + 467,296 resolved dependent claims + 403,998 description paragraphs + 171,004 figure captions + 107,579 whole + 97,922 abstracts incl. DE originals). Squarely in the spec's 1–3M target.
- Coverage confirmed the spec's hypothesis: US full text strong (claims 90.6%, desc 100%); **EP/WO/DE claims & description 0% in BigQuery** → the hole `enrich.py` targets.

## Run

```bash
docker compose up -d                 # Postgres 17 + pgvector
./run.sh                             # full evaluation-first build (idempotent)
# or individual stages, from src/ with ../.venv/bin/python:
#   coverage_profile.py · goldset.py · ingest_bq.py {core|expanded} · ingest_pg.py all
#   chunker.py · embed.py run · embed.py bench · evaluate.py · evaluate.py bench · report.py <gold_id>
```

Secrets in `.env` (gitignored): OpenAI, ScrapingBee, SerpApi, PG creds. BigQuery uses the GCE
service account.

## Artifacts

- `data/coverage/coverage_report.md` — BigQuery field-presence per jurisdiction (justifies enrichment).
- `data/goldset/goldset.json|md` — 11 frozen searches, relevance = anchors' examiner citations → families + curated competitors; citation edges recorded for hiding.
- `data/eval/eval_report.md` + `eval_results.json` — 5-config ablation metrics.
- `data/eval/dim_benchmark.json` — 768 vs 1024 vs 3072 recall on the gold-relevant subset.
- `data/reports/<id>.md` — grounded, element-by-element prior-art report with combination view.

## Deliberate pilot scope / known limits (documented, not hidden)

- **EP/WO/DE full text**: BigQuery lacks it; `enrich.py` fills it via Google-Patents scrape
  (SerpApi/ScrapingBee) for gold anchors + final candidates. Full-core enrichment awaits EPO OPS
  credentials (one external dependency; `ops_fetch()` TODO in `enrich.py`).
- **Forward citations**: omitted from the expansion (they post-date the subject → irrelevant for
  prior-art recall, and scanning every row's citation array is costly). Backward + family kept.
- **BM25**: Postgres FTS `ts_rank_cd` stands in for true BM25; ParadeDB `pg_search` is a drop-in.
  RRF fuses by rank, so the substitution is sound for the pilot.
- **Embeddings**: synchronous OpenAI API (immediate) rather than the 50%-cheaper Batch API — cost
  is single-digit dollars either way; sync lets the ablations run in-session.

## Next steps (only after the numbers are good — spec §9)

Pick production embedding dimension from `dim_benchmark.json`; then scale to worldwide official
feeds (EPO DOCDB / national bulk / WIPO / USPTO), the production physical architecture, and the
remaining search modes (FTO/invalidity/landscape) — each with its own date+status rules.
