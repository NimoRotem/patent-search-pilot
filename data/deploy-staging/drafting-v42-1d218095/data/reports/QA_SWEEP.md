# Adversarial QA sweep (Milestone 8)

I tried hard to break every surface. Below is what I actually attacked, the **6 real defects** found,
each fix + its regression test, and the decisions I made deliberately. Suite went **33 → 82 tests**,
all green in ~9 s.

## Surfaces attacked (10)

| # | Surface | Attacks tried | Result |
|---|---|---|---|
| 1 | Legal dating/basis engine | every boundary: pub/filing/priority day-before/of/after; missing dates; prio==filing; priority interval; own-family member | **1 bug** (below) — now exhaustively tested |
| 2 | Retrieval — degenerate queries | empty · one-word · 5000-word · German · Chinese · pure punctuation · SQL-injection string · a patent number · whitespace-only | **1 bug** (empty crashed); the rest were already safe |
| 3 | Agent — malformed LLM | LLM returns `{}`, `None`, `{"elements":null}`, `{"elements":[]}`, non-string elements, garbage keys | **2 bugs** (decompose + plan crashed) |
| 4 | Agent — stop condition | high-yield-forever, budget=0, max-rounds | terminates (round + budget caps) — no infinite loop |
| 5 | Export | 0 / 1 / 25 refs; a thin DE ref with no drawings/PDF/claims; **non-ASCII assignees (HÖRAUF, BÖHM)** | all render valid PDF+DOCX; umlauts preserved in the PDF text |
| 6 | Free-text generation lock | 8 concurrent generations of the SAME new query; a generation that errors midway | **1 bug** (check-then-act race → double-run); error path sets status, no hang |
| 7 | Reranker | empty passage list; empty query; None/empty passages; a `compute_score` that raises "Already borrowed" | **1 bug** (empty crashed; not fail-soft) |
| 8 | Path traversal — `/figures`, `/pdf` | `../../../../etc/passwd`, `%2e%2e%2f…`, `..%2f…`, `....//…` | already blocked by Flask; **added defense-in-depth + tests** — no file leak |
| 9 | API robustness | malformed slugs, 5000-pub export, injection strings, compare with 1/10 pubs, missing report, huge params | all clean (404/400/200) — **no 500s** |
| 10 | Data quality (what we SHOW) | 5 random cards across US/DE vs the DB; all claim-chart cells vs `element_evidence` | consistent — no mislabelled evidence, no off-by-one/wrong-pub |

## Defects found + fixed (6), each with a regression test

1. **Dating: same-day-as-priority publication was wrongly `secret_prior_art`.** A reference
   published *exactly on* the subject's effective filing date (with earlier filing) returned
   secret-prior-art (novelty-only). **Decision (documented in `search_modes.classify_basis`):**
   prior art must be "made available *before* the effective date" — a same-day publication is not
   "before", so it is **NOT prior art**. Secret (Art 54(3)) art now requires publication *strictly
   after* the EFD (filed strictly before, published strictly after). This is the conservative
   choice — the tool never *over*-flags prior art (which would mislead a lawyer); the reference is
   still retrieved, only its basis label changes. *Test:* `test_dates_boundaries.py` (13 boundary
   cases incl. day-before/of/after each date, missing dates, prio==filing, own family).

2. **Retrieval: an empty query crashed** (the embedding API rejects empty input → `RetryError`).
   *Fix:* `Retriever.search` short-circuits empty/whitespace queries to an empty result;
   `embed.embed_query` substitutes a neutral token as a backstop. *Test:*
   `test_hardening.py::test_empty_query_returns_no_results_not_crash` + `test_embed_query_handles_empty`.

3. **Agent: `decompose` crashed on `{"elements": null}`** (`.get("elements", [])` returns `None`
   when the key is present-but-null → iterating `None`). *Fix:* `out.get("elements") or []`. *Test:*
   `test_hardening.py::test_decompose_survives_malformed_llm` (6 malformed responses).

4. **Agent: `plan` crashed when the LLM returned `None`** (`.get` on `None`). *Fix:*
   `llm.chat_json(...) or {}`. *Test:* `test_plan_survives_malformed_llm`.

5. **Free-text: check-then-act race let two concurrent same-query requests both start a
   generation** (double-run, wasted compute). *Fix:* claim the slug atomically inside `_JOB_LOCK`
   before starting the thread. *Test:* `test_generation_lock_prevents_double_run` (8 concurrent → 1).

6. **Reranker: crashed on an empty passage list (`IndexError`) and was not fail-soft.** *Fix:*
   `rerank.rerank` returns `[]` for no passages and falls back to identity order on ANY failure
   (reranking only re-orders the head, so it must never crash a report generation — this also
   neutralises the "Already borrowed" tokenizer contention error). *Test:*
   `test_rerank_empty_and_failure_are_safe` + `test_rerank_families_empty_is_safe`.

**Security hardening (defense-in-depth, no live bug):** path traversal on `/figures` and `/pdf` was
already blocked by Flask's `safe_join`, but I added explicit `_safe_pub` / filename validation on the
routes AND a `_pubkey` guard at the `enrich_display` data layer (so no caller — web or not — can ever
name a cache file / figure dir with a traversal string). *Tests:* `test_security.py` (traversal
blocked on both routes, `_pubkey` rejects 8 unsafe keys, normal serving still works).

## Decided acceptable-by-design (with reasoning)

- **BM25 ~3 s per query.** Off the live hot path (cached loads + the agentic config never run it);
  only the offline eval uses it. Not a user-facing latency. (Documented in README perf section.)
- **`whitespace-only` / `matches-nothing` queries return results, not an error.** The embedder maps
  any non-empty text to *some* vector, so a nonsense query returns the nearest (low-relevance) art
  rather than an empty page — acceptable; the scores make the weak match obvious. Only truly empty
  queries are short-circuited.
- **Reranker "Already borrowed" under contention** is now non-fatal (identity fallback) rather than
  prevented outright, because generation is already serialized by `_GEN_LOCK`; the fallback is the
  belt to the lock's suspenders.
- **Invalidity/FTO/landscape modes remain stubbed** (raise `NotImplementedError`) per the original
  spec — not a bug.

## Result

`./run_tests.sh` → **82 passed** (from 33) in ~9 s · `./regression.sh` → all live E2E green. Every
fix has a regression test. No corpus re-index, no spend (holding for the EPO unblock).
