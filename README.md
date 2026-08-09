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

### Phase two — the drafting studio (`/drafts`)

Finding the art is half of the job. The second half writes the application.

- **Start from anything.** A description of the invention in the inventor's own words, or a draft
  they already have (pasted or uploaded, split on its headings to begin with). A prior-art search
  is OPTIONAL: references can come from a finished report, from uploads, from a publication number
  typed in mid-conversation, or not at all — in which case the draft says so rather than pretending.
- **A conversation, not a form.** The user asks for changes, corrects a fact, uploads another
  reference, or asks a question the agent answers without touching the draft. Every exchange is a
  durable, leased TURN, so a restart mid-run loses the run and not the project.
- **Claude Code is the drafting agent.** The draft is a tree of files (`draft/01-title.md` …
  `09-abstract.md`, `numerals.md`, one file per drawing) that the agent edits in place, which is
  what lets it narrow claim 1 and then go back and widen the description to support it. Each run is
  pinned: `--safe-mode`, an explicit `--tools` allow-list, a private `CLAUDE_CONFIG_DIR`, and a
  workspace deliberately outside `$HOME` so no CLAUDE.md from the host leaks in. The only command it
  may run is `tools/patent_lookup.py`, which reads a publication out of the local corpus.
- **The reasoning is the product.** Every iteration returns a structured answer: a summary, the
  actual decisions (which feature carries claim 1 clear of which reference, what was deliberately
  left out), what changed file by file, how the draft steps around the art, and what the agent needs
  from the inventor. All of it is shown in the conversation.
- **A separate reviewer runs after every iteration**, automatically, in a NEW session so it has not
  heard the drafter's argument. Two halves that are never merged: mechanical checks decided in code
  (numerals against the text and the drawings, claim numbering and dependency, antecedent basis,
  citation resolution against the corpus, abstract word cap, figure cross-references) and the
  reviewer's judgements, each carrying the sentence it is about. A finding without a quote is
  dropped. Only a check code can PROVE can fail a draft; heuristics can only advise.
- **Filing.** `draft_uspto` separates blockers (unresolved drafting notes, a citation that resolves
  to nothing, no named inventor) from formalities from what only a person can do (the oath, entity
  status, formal drawings under 37 CFR 1.84, the IDS, the fee, the attorney). The package carries
  the specification in 37 CFR 1.77 order with numbered paragraphs, the ADS field values, the claim
  counts that set the fee, and the cited-art listing. **No fee amount is printed** — those change by
  rulemaking and a number baked in here would be quietly wrong within a year.

Modules: `draft_agent` (the CLI bridge), `draft_workspace` (the file tree), `draft_studio` (turns,
prompt, persistence), `draft_studio_service` (routes + worker), `draft_qa` (checks + reviewer),
`draft_cite` (citation resolution), `draft_uspto` (readiness + package). Schema: `sql/006_draft_agent.sql`.

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

## EPO OPS — zero-step unlock for EP/WO/DE full text

`src/ops.py` fully implements the EPO OPS v3.2 client (OAuth2 client-credentials, description +
claims + drawings + INPADOC legal → the schema, with provenance). It runs in **mock/dry-run mode
until credentials exist**, so the parser is provable now:

```bash
cd src && ../.venv/bin/python ops.py --dry-run        # parse a sample OPS response (no creds)
cd src && ../.venv/bin/python test_ops.py             # parser + schema-mapping unit test
```

**The moment `OPS_CONSUMER_KEY` + `OPS_CONSUMER_SECRET` are in `.env`, one command backfills the hole:**

```bash
cd src && ../.venv/bin/python ops.py --backfill-core   # 13,400 claimless DE/EP/WO core pubs → live OPS
```

This is the highest-leverage recall fix (see `REACHABILITY.md`): 68% of in-corpus gold families have
no embedded claims today; OPS fills them. It's the only external blocker.

## Performance & operations (measured on the 8 GB box, 6 GB HNSW)

| Path | latency | note |
|---|--:|---|
| gold report page load (warm, cached view) | **p50 10 ms · p95 15 ms** | the normal case |
| gold report page load (cold, rebuild view) | ~1.2 s | first load after a re-run; then cached |
| per-card enrichment `/api/ref` (cached) | ~40–160 ms | drawings + sections + rationale |
| vector search (core dense) | **p50 288 ms · p95 400 ms** | |
| hybrid search | ~4 s | **BM25-bound; eval-only** — the live UI never runs it |
| fresh free-text search | first cards ≤60 s; final target ≤300 s | backgrounded + polled; cached after |
| supervisor restart → `/healthz` 200 | < 7 s | OS page-cache keeps the index warm (first load ~50 ms) |

**Memory:** Postgres capped at 6 g (page-caches the 6 GB HNSW); the webapp base is ~165 MB and the
CPU reranker (~2 GB) is **lazy-loaded** only when a report is generated. ~10 GB stays available; a
16-request concurrent read/export burst added ~55 MB.

**Safe concurrency:** report **reads / API / export are cheap and highly concurrent** (read-only,
served from cache). Report generations overlap; cross-encoder calls alone queue through one
dedicated child process, keeping one model copy in RAM and avoiding tokenizer contention. BM25's
cost is off the hot path (the live UI uses cached loads + the agentic config, neither runs BM25).

## QA checklist (release-candidate regression — `./regression.sh`)

Three layers of automated checks:
- **`./run_tests.sh`** — hermetic pytest unit/integration tests (no paid APIs): retrieval
  fusion (locks in the weighted-RRF + dense-floor fix — it fails if you break the dense weight),
  family dedup, date/basis engine, agent ranking + stop-condition, valid PDF/DOCX export, every API
  endpoint + edge case, and the OPS parser.
- **`./regression.sh`** — live cached-report and endpoint checks against the running app.
- **`ops/ui_e2e_acceptance.py`** — submits a fresh search through `/run`, times progressive and
  final results, requires agent stages and grounded cards, verifies real cross-encoder plus
  listwise reranking, and (with `--wide`) requires several external providers to contribute.

Example from the authenticated service loopback:

```bash
python3 ops/ui_e2e_acceptance.py \
  --query "robotic end-of-arm vacuum gripper for handling sheets or panels with an array of suction cups and independent vacuum zones" \
  --mode inventive_step --wide --require-source USPTO --require-source Lens \
  --expect-any-family 34201690 --expect-any-family 63449883 \
  --expect-any-family 70050062 \
  --output /tmp/patent-ui-acceptance.json
```

Repeat `--expect-family` when every listed family must rank in the top 25, or repeat
`--expect-any-family` with a gold set when at least one relevant family must rank there.

`./regression.sh` all green = shippable. Covers:

- [x] `/healthz` 200
- [x] all 11 gold reports load with a claim chart + ≥10 reference cards
- [x] US reference enrichment: real drawings + working PDF facsimile + sections
- [x] citation graph · more-like-this · side-by-side compare · print view
- [x] triage flags persist across reload
- [x] export **PDF + DOCX** for a gold report **and** a free-text report
- [x] no 500s: empty query→302, bad slug→404, junk ref/graph→200 graceful, empty export→400, missing pdf→404
- [x] EPO OPS parser dry-run/unit test passes (no creds)
- [x] supervisor autostart + auto-restart + survives reboot

## Eval table (frozen 11-query gold set, mean family recall)

| Config | recall@100 | recall@500 | recall@1000 |
|---|--:|--:|--:|
| keyword (BM25) | 0.015 | 0.106 | 0.136 |
| vector (dense) | 0.170 | 0.260 | 0.283 |
| hybrid (weighted RRF) | 0.170 | 0.275 | 0.311 |
| hybrid + reranker | 0.170 | 0.275 | 0.311 |
| **agentic** | **0.185** | 0.266 | **0.319** |

Agentic is the best config at recall@100 and @1000. `reachable@100 ≈ 0.19` (of in-corpus gold);
the ceiling is text depth, not corpus size — see `REACHABILITY.md`.

## Data & artifacts

`data/coverage/` (BigQuery coverage) · `data/goldset/` (frozen gold) · `data/eval/` (ablations +
before/after) · `data/reports/` (cached gold reports) · `data/reports/exports/` (PDF/DOCX) ·
`data/reports/screenshots/` · `data/enriched/` + `data/figures/` + `data/pdfs/` (enrichment cache).
