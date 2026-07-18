# Patent Semantic + Agentic Prior-Art Search — Pilot (Vacuum-Gripping field)

A working prior-art search tool for the vacuum-gripping / suction-lifting field (GRABO's domain),
built to be judged by eye on real data before committing to a worldwide corpus. It retrieves,
displays, and **exports litigation-grade** prior-art reports.

**Live:** http://127.0.0.1:8631 (Flask, supervisor `patent-results`, localhost/no-auth per pilot spec).

---

## What it does

- **Describe an invention in plain language** (or pick a frozen gold example) → an agent decomposes
  it into technical elements, searches an 8-channel retrieval cascade, and returns ranked prior art.
- **Element × Reference claim chart** — a heat-map of which reference discloses which invention
  element, with the claim/paragraph/figure coordinate and the prior-art basis (public vs secret).
- **Reference cards** — biblio, CPC, legal status, prior-art basis, **real patent drawings**
  (lightbox), **PDF facsimile**, and section tabs (abstract / claims / description / figures /
  citations) with the **matched claim/paragraph highlighted** and query terms marked. Plus a
  one-line AI "why relevant" rationale and a **citation graph** (backward / forward / similar /
  more-like-this).
- **Triage** — flag each reference Relevant / Maybe / Not + attorney notes (persisted), filter to
  relevant-only.
- **Export** — select references → a clean **PDF and editable DOCX** prior-art report: cover,
  executive summary, claim chart, per-reference biblio + embedded drawing + quoted matched passage +
  rationale, inventive-step combination analysis, and a full-ranked appendix.
- **Side-by-side compare** of 2–3 references for a combination argument.

---

## Architecture

```
Free-text invention  ─►  CoverageAgent (LLM decomposes → elements; deterministic code owns
                          dates / dedup / budget / scoring / stopping)
                              │  per element + whole query
                              ▼
      8-channel retrieval cascade  ──►  weighted RRF fusion  ──►  family dedup  ──►  cross-encoder rerank
      (dense · BM25 · CPC · exact/phrase · citation+family graph · query-by-example ·
       cross-lingual · bibliographic)
                              │
                              ▼
      jurisdiction-neutral date/status engine (novelty incl. secret art · inventive step)
                              │
                              ▼
      grounded element-by-element report  ──►  web UI  ──►  PDF / DOCX export
```

- **Corpus:** 107,795 publications (US/EP/WO/DE, all dates) — 25,786 seed-CPC core + 82,009 family &
  backward-citation expansion — as **1.84M embedded passages** (every claim own + parent-resolved,
  description paragraphs, figure captions, abstracts incl. German originals + enriched DE/EP/WO claims).
- **Store:** PostgreSQL 17 + `pgvector` HNSW (6 GB index) in Docker. Normalized, provenance-aware
  schema (claims-as-rows, kind_code separate, `field_provenance` ledger).
- **Fusion (the key quality lever):** weighted reciprocal-rank fusion — dense-dominant per-channel
  weights + a dense-hit floor so broad/noisy channels (CPC, BM25) can't demote a strong semantic hit
  below the top-k. RRF_K=40.
- **Embeddings + agent LLM:** Vertex AI `gemini-embedding-001` (768-dim, multilingual) +
  `gemini-2.5-flash` via the GCE service account (no key). Reranker: `bge-reranker-v2-m3` on CPU.
- **Enrichment:** SerpApi `google_patents_details` fills the EP/WO/DE full-text hole (BigQuery has
  ~0% EP/WO/DE claims/descriptions) and provides drawings + PDF + legal status. Downloaded locally so
  images never hot-link/break; graceful "facsimile not digitized" for old patents.

Source layout (`src/`): `search_modes` (date engine) · `bqclient`/`ingest_bq`/`ingest_pg` (BigQuery
bootstrap) · `patent_text` (claim split + dependency resolve, EN+DE) · `chunker` · `embed` ·
`retrieval` (cascade + weighted RRF + rerank) · `agent` (coverage-ledger controller) · `webview`
(report→view model) · `enrich`/`enrich_display`/`enrich_de_batch` · `export_data`/`export_pdf`/
`export_docx` · `webapp` (Flask) · `evaluate` (5-config ablation).

---

## Evaluation (frozen 11-query gold set — no vibes)

Relevance = each anchor's examiner/search-report citations resolved to DOCDB families (CLEF-IP
method) + curated competitors; citation edges hidden at retrieval time. Mean **family recall@100**,
macro-averaged, across the retrieval-quality milestones:

| Config | M1 (initial) | M3 (weighted RRF + paragraphs + agent ranking) |
|---|--:|--:|
| keyword (BM25) | 0.00 | 0.00 |
| vector (dense) | 0.175 | 0.170 |
| hybrid | 0.090 | **0.170** (fusion fix: no longer demotes dense) |
| hybrid + reranker | 0.090 | 0.170 |
| **agentic** | 0.067 | **0.181 — best config at every k** |

The thesis holds: the **agentic** config (element decomposition + citation/family expansion, with a
seed-primary ranking) is the best config — it surfaces gold families no single-shot config finds.
`data/eval/eval_report.md` has the full per-query table; `data/eval/M3_BEFORE_AFTER.md` the deltas.

---

## Run

```bash
docker compose up -d                       # Postgres 17 + pgvector
./.venv/bin/pip install -r requirements.txt requirements-rerank.txt
./run.sh                                    # evaluation-first build from BigQuery (idempotent)
# serve the UI:
supervisorctl start patent-results          # or: cd src && ../.venv/bin/python webapp.py 8631
```

Secrets in `.env` (gitignored): Vertex uses the GCE service account; SerpApi + ScrapingBee keys for
enrichment; Postgres creds. Open http://127.0.0.1:8631.

Per-stage (from `src/`, `../.venv/bin/python`): `coverage_profile.py` · `goldset.py` ·
`ingest_bq.py {core|expanded}` · `ingest_pg.py all` · `chunker.py` · `embed.py run` ·
`evaluate.py` · `report.py <gold_id>` · `warm_reports.py` (cache all gold).

---

## Known limits (honest)

- **Corpus reachability ≈ 0.18** — many gold references (examiner citations) sit *outside* the 107k
  seed subset, so even perfect retrieval caps near reachable@100 ≈ 0.18. A targeted corpus expansion
  (more CPC/family neighbours) would lift this; quantifying the cost/benefit is the next step.
- **Cross-lingual German recall** — the EP/WO/DE full-text hole is the dominant cause (BigQuery has
  ~0% EP/WO/DE claims). Embedding enriched DE claims **doubled** grabo_de recall@500 (0.29→0.57).
  One DE case (`grabo_de_utility_xling`) stays at 0 @100 because its subject had **zero examiner
  citations**, so its gold is a hand-curated competitor set that isn't a semantic top-100 match
  (confirmed by the cross-encoder) and whose closest member has no digitized full text anywhere.
  Cross-lingual query translation was tested and does **not** help (the corpus is English-dominant;
  translating promotes English distractors). See `data/eval/M5_DE_RECALL.md`.
- **EP full text via EPO OPS is pending** — the only external blocker. `enrich.py` has a drop-in
  `ops_fetch()` TODO; today EP/WO/DE text is filled via SerpApi/Google-Patents (bounded by a 5k/mo
  quota, so DE enrichment covers a field-representative candidate pool, not all 14k DE/EP/WO core).
- **Reranker on CPU** — rerank depth is capped at 25 (a GPU would allow the spec's ~300).

## Data & artifacts

`data/coverage/` (BigQuery coverage) · `data/goldset/` (frozen gold) · `data/eval/` (ablations +
before/after) · `data/reports/` (cached gold reports) · `data/reports/exports/` (PDF/DOCX) ·
`data/reports/screenshots/` · `data/enriched/` + `data/figures/` + `data/pdfs/` (enrichment cache).
