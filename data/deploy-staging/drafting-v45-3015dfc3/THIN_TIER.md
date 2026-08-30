# The thin tier: worldwide reach for the price of a screen

Phase 3 of the rebuild. Status 2026-08-18: **data layer built, embed run staged, index pending.**

## What exists now

- `nimo-gpt.patent_pilot.thin_tier_v1` — 170,418,479 publications, worldwide, all offices, all
  dates: publication_number, country, kind, dates, family_id, title, abstract (128.7M non-null),
  CPC. 100.9 GB of text. Build cost $1.52 (244 GB scan of `patents-public-data`), rebuildable any
  time with the same `CREATE OR REPLACE` (clustered by country_code).

## Why it exists

Both gold sets failed first on REACH (6 of 10 Schmalz refs sit outside the seeded CPC branches;
"in corpus, never retrieved" is the funnel's floor). The local thick corpus (~5M pubs) can never
be the universe; worldsets ($9.38 per class-set) are the escape hatch per search. The thin tier is
the permanent fix: every publication on earth findable at screen quality, so the only question
left for retrieval is ranking, and full text is fetched on demand for candidates that matter.

## The plan (in order)

1. **Embed** title+abstract for all 170M rows with a multilingual open model on the Spot GPU box
   (`sec-ai-workstation`, g4-standard-48, ~$2-4/h spot — coordinate with its sec-ai/qwen workload
   before occupying the GPU). Candidate model: BAAI/bge-m3 (multilingual — 24% of rows are
   CN/JP/KR and their abstracts are often local-language). Throughput at large batch ≈ 5-15k
   texts/s → 4-10 h ≈ **$20-60 total**, vs ~$60k+ to embed the same rows on a paid API. Quality
   bar is deliberately low: this tier only nominates candidates for the screen; the gemini 768-d
   space stays the evidence/ranking space.
2. **Quantize + load**: sign-binarize to `bit(dim)` (1024-d → 128 B/row ≈ 22 GB) into table
   `thin_pubs` on patents-pilot-db, **IVFFlat hamming index** (not HNSW: no per-node graph
   overhead at 170M rows; IVF recall is enough for a nominator). Keep fp16 vectors in GCS parquet
   for a later rerank stage if measured necessary.
3. **Query path**: embed the query set with the same model (CPU is fine at query volume),
   `ORDER BY embedding <~> query` (hamming) per limitation, feed survivors into the existing
   screen. New retrieval channel `thin_dense`, OFF by default until measured on both gold sets —
   the acceptance test is the funnel's "in corpus"/"retrieved" stages reaching 10/10 and 5/5.
4. **RAM budget**: thick tier fp16 HNSW (~47 GB after `/srv/patents/halfvec_build.sh` and the
   fp32 index drop) + thin IVF (~22 GB) ≈ 69 GB on a 62 GB box — acceptable for an IVF nominator
   (list scans are sequential); measure before resizing anything.
5. **Freshness**: `patents-public-data` lags ~3 months. Weekly delta = re-run the thin_tier_v1
   CREATE (it is cheap) + an EPO OPS / USPTO bulk top-up for the gap window, folded into
   `ops/refresh_corpus.sh`.

## What NOT to do (measured elsewhere in this repo)

- Do not use Google's own `embedding_v1` (64-d): its nearest neighbours do not contain examiner
  citations (recall 0 at top-200 even seeded with the subject's own vector).
- Do not make the thin tier the ranking space or mix its scores into fusion untested — W_SEM>0
  measured worse twice in App A's fusion; the tier NOMINATES, the screen decides.
