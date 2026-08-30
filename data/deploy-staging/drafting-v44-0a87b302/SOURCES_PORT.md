# SOURCES_PORT — App A's federated-search adapters, in-process

Phase 2b of the rebuild. App A (`patents-app`, FastAPI + asyncio, rotem.ai/patents
on builder :8630) carried the hard-won adapter stack the pilot reached over HTTP
(`external.bulk()` → `POST /api/bulk_search`). This branch ports that stack into
the pilot as a self-contained package, `src/sources/`, behind a sync facade, so
App B can fan out to the external patent sources in one process with no network
hop and no second service to keep alive. **Only files were added** — nothing
existing was edited, so the branch merges without conflict.

## What was ported

From App A's `adapters/` + `fulltext.py` + `docstore.py`, comments (the recorded
measurements) included:

| `src/sources/` file | From | Kept behaviors |
|---|---|---|
| `serpapi_gpatents.py` | `adapters/serpapi_gpatents.py` | 429 backoff, **quota-exhausted latch** (900s cooldown, fails fast without HTTP), 100 results/credit page sizing, HTML-unescape, legal-status/state mapping, details/citations/family |
| `bigquery_gpatents.py` | `adapters/bigquery_gpatents.py` | `nimo-gpt.patents_cache.pubs` via ADC, **2 GB `maximum_bytes_billed` guard**, CPC-hint-required, word-boundary regex title match, position-weighted rank + FARM_FINGERPRINT determinism, `similar_pull` |
| `pqai.py` | `adapters/pqai.py` | **mediator route** (serialized, 1.2s + jitter spacing, 5-min block cooldown, 1,000/24h ledger `~/.patents/pqai_mediator_calls.json`), monthly token-quota latch (`pqai_search_quota.json`, auto-clears on the 1st), 429 circuit breaker, non-quota-counted detail endpoints |
| `epo_ops.py` | `adapters/epo_ops.py` | OAuth2 client-credentials with token reuse (registry singletons — the whole point), exchange-documents parser + plain-shape fallback + loud shape-drift error, **fulltext for EP/WO only** (verified: others 404), OPS `{"$": ...}` tree flattener, INPADOC family |
| `uspto.py` | `adapters/uspto.py` | ODP `X-API-KEY`, 2-term title AND (the recall sweet spot), legacy-PatentsView-syntax detector (loud, recovering), prosecution events detail |
| `openalex.py` | `adapters/openalex.py` | NPL channel, daily-budget 429 latch keyed on UTC date, null-tolerant parsing |
| `gpatents_direct.py` | `adapters/gpatents_direct.py` | **0.7s pacer + concurrency 2 + 900s cooldown latch** (Google 503-blocks the whole IP after ~80 fast requests — measured 2026-08-14), one itemprop parser for every jurisdiction (English translations), section split, citations, `available()`/`block_reason()` |
| `himmpat.py` | `adapters/himmpat.py` | **envelope `{code,message,data}` where HTTP 200 ≠ success** (`CODE_MEANING` map, per-code block hours), bare-key auth, per-unit spend ledger `~/.patents/himmpat_usage.json` (rolling 24h + lifetime, charged in vendor units, hydrate bills PER ID), postfix expression builder, mergeBy=HFA, ST.36 markup stripper, truncation warnings |
| `ipaustralia.py` | `adapters/ipaustralia.py` | OAuth2 gateway, strict `{searchType, query}` contract, array-shaped fields, no fake DOCDB family ids |
| `web_patent_fallback.py` | `adapters/web_patent_fallback.py` | ScrapingBee → Firecrawl → Tavily serial ladder, retry-with-backoff on retryable statuses, patents.google.com URL validation |
| `fulltext.py` | `fulltext.py` | The acquisition ladder: docstore → PQAI (US) → EPO OPS (EP/WO) → gpatents_direct → ScrapingBee → HimmPat (CN/JP/KR/TW) → SerpApi. Caps: `PATENTS_SERP_FULLTEXT_CAP=25`, `PATENTS_SB_FULLTEXT_CAP=900`, `PATENTS_GOOGLE_FULLTEXT_CAP=40`, `PATENTS_HIMMPAT_FULLTEXT_CAP=10`. Everything acquired is persisted, so the ladder gets shorter every run |
| `docstore.py` | `docstore.py` | zlib text compression, **merge-never-overwrite** (`_better_text`: longer wins; English beats a non-English original at ≥50% length), list dedup-merge, prefer-new scalars — re-backed onto Postgres (below) |
| `base.py` | `adapters/base.py` | `Adapter` interface, shared async `TTLCache`, `Budget`, `cached_json` |
| `schema.py` | `schema.py` (subset) | `SubQuery`/`Candidate`, `normalize_pub`/`canonical_pub` (US pre-grant zero repair), `iso_date`, office URL builders |
| `registry.py` | `adapters/__init__.py` | process-wide adapter singletons (OPS token reuse depends on this), `reset_adapters()` |

**Skipped, deliberately:** `lens` (dead trial key — 401 on every search),
`kipris` / `euipo` (never approved), `gpatents_scrape` (superseded by
`gpatents_direct`), PQAI `/search/103` obviousness combinations (App A pipeline
feature, no pilot consumer), App A's LLM planner/round loop (the pilot has its
own planner in `external.plan()`), and the docstore `sections`/`images` tables
(the pilot has its own chunker; images/pdf/family/citations ride in `meta`).
The SerpApi search page-error swallow (only `SerpApiQuotaExhausted` propagates
from a page fetch; a generic page error yields an empty page) is App A's code
as written and was kept as-is.

## The facade (`import sources`)

App A is asyncio; the pilot is threads. The adapters stay async internally —
their pacing, latches and semaphores are asyncio constructs whose measured
behavior we did not want to re-derive — and the package owns **one long-lived
background event loop** in a daemon thread. Facade calls submit coroutines to
it and block on the result; the adapters' asyncio primitives are created lazily
on that loop (Python 3.9's `asyncio.Lock()` binds a loop at construction, so
import-time primitives would die "attached to a different loop").

```python
import sources

# fan-out — same query dicts external.plan() already emits
cands = sources.search([{"source": "serpapi_gpatents", "q": '"vacuum lifter"',
                         "cpc": [], "element": "gripper", "why": "head term",
                         "date_from": "2010-01-01", "date_to": "2020-01-01"}])
# -> list of candidate dicts, exactly /api/bulk_search's row shape:
#    {pub_number, source, source_rank, title, abstract, snippet, assignee,
#     date, priority_date, kind, cpc, url, family_id, query_i, element}

env = sources.bulk(queries, timeout=75)
# -> the full envelope App A returned: {ok, candidates, stats, errors, skipped,
#    warnings, elapsed, n_queries, n_candidates, budget_used}

texts = sources.fetch_fulltext(["US9876543B2", "EP1234567A1"])
# -> {pub: {claims, description, abstract, title, source}} + "_summary"
#    (per-source tally, paid_usd, scrapingbee credits, warnings)

status = sources.health()
# -> per-source {enabled, search_available, note, reason} + himmpat ledger,
#    pqai mediator/quota notes, gpatents_direct cooldown, docstore stats
```

Fail-soft throughout: a dead source contributes an error entry in the envelope,
never an exception through the facade; a cap or latch that binds is **named**
in `skipped`/`warnings`, never silent.

**Budgets** (fresh per call, App A's per-run numbers): 12/source
(`PATENTS_PER_SOURCE_CAP`), SerpApi 40 (`PATENTS_SERPAPI_CAP`), HimmPat 3
(`HIMMPAT_QUERIES_PER_RUN`), PQAI 6 (`PATENTS_PQAI_CAP`). Facade knobs:
`PATENTS_FANOUT_TIMEOUT` (45s/source), `PATENTS_BULK_TIMEOUT` (75s),
`PATENTS_MAX_BULK_QUERIES` (80), `PATENTS_BULK_CONCURRENCY` (2).

## Docstore on Postgres

`sources_docstore` (also `sql/008_sources_docstore.sql`; auto-created on first
use): `publication_number text primary key, title text, abstract text,
claims_z bytea, description_z bytea, meta jsonb, updated_at timestamptz`.
Uses the pilot's `src/db.py` cursor helper (dict rows, `PG*` env / `.env`).
`meta` carries chars/langs/sources plus biblio lists; the merge runs in one
row-locked transaction. If Postgres is unreachable the ladder degrades to
"no cache" and says so in warnings instead of failing the fetch.

**Quota ledgers stay in App A's exact files** (`~/.patents/himmpat_usage.json`,
`~/.patents/pqai_mediator_calls.json`, `~/.patents/pqai_search_quota.json`), so
while both apps run on one host they spend against ONE shared per-host budget.

## Credentials (env only — this repo is public)

`SERPAPI_KEY` (alias `SERPAPI_API_KEY`), `SCRAPINGBEE_API_KEY`,
`FIRECRAWL_API_KEY`, `TAVILY_API_KEY`, `PQAI_TOKEN` (optional `PQAI_BASE_URL`,
defaults to the official `https://api.projectpq.ai`), `EPO_OPS_KEY` /
`EPO_OPS_SECRET` (aliases `OPS_CONSUMER_KEY` / `OPS_CONSUMER_SECRET` — App B's
`.env` spelling), `USPTO_ODP_KEY` (alias `ODP_API_KEY`), `HIMMPAT_API_KEY`,
`IPA_CLIENT_ID` / `IPA_CLIENT_SECRET`. BigQuery uses ADC (`GCP_PROJECT`,
default `nimo-gpt`). The names match App A's supervisor conf (verified against
the live :8630 process), so the same keys work unchanged on either side.

Dependency note: internals use `httpx` async, exactly like App A. `httpx` is
not in `requirements.txt` directly but is already in the pilot venv as a hard
dependency of `openai==1.59.6`; nothing else new is required
(google-cloud-bigquery and psycopg are already pilot dependencies).

## What REMAINS to rewire (deliberately not done on this branch — zero edits)

1. **`src/external.py`** — `bulk()` (line ~305) currently POSTs to App A
   (`FEDERATION_INTERNAL_URL` → `http://10.128.0.13:8630/api/bulk_search`).
   Replace the HTTP round trip with `sources.bulk(queries[:MAX_QUERIES],
   timeout)` — the envelope is contract-identical (`candidates` rows carry the
   same keys, `stats`/`errors`/`skipped` the same shapes), so `best_records()`,
   `materialise()` and the replay cache keying need no change. Keep the replay
   record/replay wrapping around the call. `probe_sources()` (line ~275) then
   probes through the same in-process path; drop `lens` from its source list
   (not ported — dead).
2. **`src/enrich.py`** — `fetch_best_full_text()` / `fetch_details()` currently
   go Mongo → SerpApi directly (plus `fulltext_recovery.fetch_google_full_text`).
   Insert `sources.fetch_fulltext([pubnum])` as the acquisition rung between the
   Mongo corpus and the raw SerpApi call: it already runs docstore → PQAI → OPS →
   Google direct → ScrapingBee → HimmPat → SerpApi with the per-run caps, and
   persists what it buys. Its SerpApi spend guard is per-run (`SERP_FULLTEXT_CAP`);
   enrich's account-level `searches_left()` gate should stay wrapped around it.
3. **Webapp health panel** — `src/webapp.py` has no per-source panel today
   (`/api/health` is app-level). Add a route that returns `sources.health()`
   and surface it in ops; `external.probe_sources()` remains the live-fire
   check ("a health check that does not execute a query cannot detect a 401ing
   source").
4. **Ops hygiene once rewired** — App A's `/api/bulk_search` on :8630 keeps
   serving the other three dashboards until they migrate; nothing to
   decommission yet. When the pilot's supervisor env gains the source keys,
   copy the names above from App A's conf (values live in the advisor).

## Tests

`tests/test_sources_port.py` — hermetic: the facade's HTTP client factory is
replaced by a handler-driven fake; no network, no keys, no paid API. The
module overrides the suite conftest's `no_paid_apis` fixture (which imports
embed/enrich/llm — heavyweight modules this package never touches) and
isolates the `~/.patents` ledgers into tmp files per test. Covered: facade +
candidate row shape, fail-soft error envelopes, budget caps binding (and being
named), HimmPat HTTP-200-business-error / 201-empty / hydrate-unit-charging /
envelope drift, gpatents_direct pacing + 503 cooldown latch, SerpApi quota
latch, env aliases, health shape, ladder short-circuit on docstore hit, ladder
order (OPS before the costlier rungs) + persistence.

The three docstore tests use the **real pilot Postgres** like the rest of the
suite, with `ZZTESTSRC*` rows cleaned up after. They auto-skip where the DB is
unreachable; `SOURCES_TEST_PG=1` forces them (fail loudly), `SOURCES_TEST_PG=0`
forces skip. Status at branch push: **20 passed** with the DB reachable
(builder → 10.128.0.53:5433), 17 passed + 3 skipped without it.
