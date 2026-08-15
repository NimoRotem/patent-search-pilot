# patent-search-pilot — condensed handoff

Written 2026-08-15. Read this before touching anything; it records what was measured, not what was
intended, and several of the obvious "improvements" below have already been tried and reverted.

---

## 1. What this is

A prior-art search engine. A user gives it either a **disclosure** (free text describing an
invention) or a **patent** (PDF upload or a link like `https://patents.google.com/patent/US20260109053A1/en`).
It searches, reads candidate references **in full**, and produces evidence: verbatim quotes with
real coordinates ("claim 7", "paragraph 41") backing every assertion.

### Two search types, two objectives. This distinction is load-bearing.

| | **Type A — disclosure** | **Type B — patent with claims** |
|---|---|---|
| Detected by | no claims in the input | `report["query_document"]["claims"]` non-empty |
| Question | which documents are closest to this invention? | for **every requirement of every claim**, what dated citable document already disclosed it? |
| Unit of work | invention concept ("disclosure") | **limitation** (one requirement inside one claim) |
| Ranking objective | relevance + concept coverage | claim/limitation coverage |
| Stopping rule | marginal new-family yield flattens | **every limitation has evidence**, or is escalated to exhaustion and named |
| Deliverable | element × reference grid | **claim ledger** + (planned) §103 combination table |
| Detection code | `limitations.search_type(report)` | same |

**Why this matters, measured.** On US 2026/0109053 A1, three references a patent attorney actually
filed were retrieved, screened 70–90, and **read in full** — and were ranked 253, 346 and 152 of
498 against a 60-card page, because each grounded 0, 0 and 1 of 28 *invention features*. Each one
is cited for exactly **one requirement** (a sound-damping device in the exhaust path) and resembles
the invention barely at all. Ranking by resemblance is the wrong objective for an attack.

**Why the claim is also the wrong unit.** A claim is a conjunction. A document teaching one of its
three requirements is correctly judged "absent" for the whole claim. Measured: reading 17
references found specifically for what a dependent claim *adds* grounded **1 claim cell in 221**,
while grounding 25 feature cells in the same reading. The reader was not wrong; the question was.

---

## 2. Where it runs

| | |
|---|---|
| Repo | `~/patent-search-pilot` on **instance-3** (10.128.0.5). Not on builder. |
| Service | supervisor unit **`patent-results`**, gunicorn, 1 worker × 16 threads, `timeout=1800s`, port **8631** |
| Front door | <https://rotem.ai/patents> |
| Restart | `sudo supervisorctl restart patent-results` |
| Logs | `data/patent-results.out.log` (stage lines), `data/patent-results.err.log` (tracebacks) — **not** `/var/log/supervisor/patent-search.*`, which belongs to a different, stale app |
| Python | `.venv/bin/python` (3.9). System pip is broken; venv pip works. |
| Tests | `PYTHONPATH=src .venv/bin/python -m pytest tests -q` → **1022 pass**, ~8 min |
| Corpus DB | Postgres 17 + pgvector 0.8.5 in Docker, port **5433**, ~5M publications, ~25M chunk vectors |
| Reports | `data/reports/<slug>.json` (report), `.view.json` (view cache), `.deep.json` (the reading), `.meta.json` |
| Git | branch `feat/claim-centric-search`, **PR #21** → base `pilot-build` (NOT `main`; `pilot-build` is the trunk) |

Getting a shell from builder: `gcloud compute ssh instance-3 --zone us-central1-b --tunnel-through-iap --command "sudo -u nimrod_rotem bash -lc '<cmd>'"`. Plain `ssh` has no key; `gcloud` lands you as `sa_NNN` with no home, hence the `sudo -u`.

**App A** (`~/patents-app` on builder, `:8630`, `rotem.ai/patents-engine`, supervisor `patents`) is a
separate federated engine. This app calls it over HTTP (`src/federation.py`, `src/external.py`) for
`/api/search` and `/api/bulk_search`. It is a dependency, not part of this repo.

---

## 3. Data layers

### 3a. Local Postgres — a warm cache of what we have read
Tables: `publications`, `claims`, `chunks` (kind = `claim_own` / `claim_resolved` / `paragraph` /
`abstract` / `whole`, with `embedding`), `classifications`, `citations`, `figures`.
~5M publications, seeded from **eight vacuum-gripping CPC branches** (`config.SEED_CPC`).

**This was the retrieval universe and that was the single biggest defect in the system.** ~84% of
it is abstract-only.

### 3b. BigQuery — the actual index (added 2026-08-15)
```
patents-public-data.patents.publications                170,418,479 rows · 3.09 TB
  claims_localized / description_localized / abstract_localized   full text, all languages
  cpc ARRAY<STRUCT<code,inventive,first,tree>>, family_id, assignee_harmonized
  publication_date / filing_date / priority_date are INT64 yyyymmdd
patents-public-data.google_patents_research.publications  170,418,479 rows · 0.51 TB
  embedding_v1  precomputed BERT vector for every patent
  similar       precomputed neighbour list — NO similarity score column; it is
                STRUCT<publication_number, application_number, npl_text, type, category, filing_date>
                and Google's ORDER is the signal, so rank by array offset
  top_terms     distinctive terms per document (vocabulary mining)
  cited_by, cpc, cpc_low, abstract_translated, title_translated
```
Project `nimo-gpt`, SA `nimo-843@nimo-gpt.iam.gserviceaccount.com`. Working sets are written to
dataset `patent_pilot`.

**Cost shape dictates the design.** BigQuery bills for **columns scanned**, not rows matched, so any
query touching `description_localized` scans ~2.5 TB whatever the WHERE says. Never run many
per-question queries against it. Instead materialise a **working set** once — filter by CPC prefix
and date, SELECT the text columns, write to a project table — then query that for pennies.
**Measured: 10 CPC classes + date bound → 2,392,628 publications with full text, 145 seconds,
1,501 GB, $9.38.** Keyed on `sha1(classes|date)` and reused (`worldset.table_for`).

---

## 4. Pipeline, in execution order

```
POST /extract  (webapp.py:1708)
  ingest_input / external → doc {claims[], chunk_vecs, figure_blobs, publication_number, brief}
  → _stash_doc() → doc_token          claims are stashed for BOTH uploads and links

POST /run  (webapp.py:1607) → search_slug() → ensure_report() → _generate()
  parallel fan-out (ThreadPoolExecutor):
    local      agent.CoverageAgent.run()   ANN + BM25 + CPC + citation + QBE over Postgres
    federated  federation.search()          App A, 600s budget
    external   external.run()               PQAI / USPTO / BigQuery-title / SerpApi fan-out
    docchunks  retrieval.search_doc_chunks()
    image      img_search
  _attach_query_document()   ← claims land on the report here (upload OR link)
  _attach_disclosures()      ← the ~28-item checklist, from claims + description

deep_rank.run(report)                              ← THE STAGE THAT DECIDES EVERYTHING
  limitations.search_type()  → TYPE_A | TYPE_B
  limitations.split_claims() → 11 claims → ~27 limitations           (Type B only)
  _candidate_rows()          → top SCREEN_TOP=2500 of the ranked list
  _enrich_missing_text(rows, limit=PRESCREEN_ENRICH_TOP=400)         ← BEFORE the screen
  screen()                   → 0-100 per candidate, interleaved batches of 25
  choose: top CHART_TOP=360 by screen, ≥CHART_MIN_SCREEN=70, cap CHART_TOP_MAX=420,
          + ALWAYS_CHART_RETRIEVAL_HEAD=60, + BLIND_RESCUE (text-less, now full screen depth)
  _enrich_missing_text(chosen)
  deep_analysis.concept_expansions()  → {feature: other words for the same idea}
  deep_analysis.analyse_reference() × chosen, 24 workers
      full text → feature rows (batched 24) + claim/limitation rows (batched CLAIM_BATCH=12)
      every quote: grounding.grounded() + claim_chart._locate() + _refute()
  reread_absent() second concept pass over the top CONCEPT_PASS_TOP=120
  limitations.Ledger(lims).ingest_charts(charts)   → report["ledger"]
  claim_rescue.run(..., ledger=ledger)             ← see §5
  ledger.ingest_charts(charts) again               → final report["ledger"]
  rarity() → score_reference() → coverage_rank.rank() → coverage_rank.guarantee()
  _publish_deep_analysis() → <slug>.deep.json

webview.build_view() → cards, claim_chart (axis="claims"), ledger
templates/report.html → ledger block first (Type B), then the element × reference grid
```

---

## 5. Components

| File | Role |
|---|---|
| `src/webapp.py` (5.2k lines) | Flask routes, job orchestration, `_generate`, view cache |
| `src/agent.py` | `CoverageAgent` — local ANN retrieval, coverage ledger, query-set expansion |
| `src/retrieval.py` | `Retriever` — 9 channels (dense, claim_dense, brief_dense, bm25, claim_bm25, exact, cpc, citation, qbe, biblio, crosslingual), RRF fusion, family dedup, cross-encoder rerank |
| `src/deep_rank.py` | Screen → read → score → order. **The stage that decides the report.** |
| `src/deep_analysis.py` | `analyse_reference` — full-text reading, grounded quotes, `_refute`, `reread_absent(kind="feature"\|"claim")`, `concept_expansions` |
| `src/coverage_rank.py` | Greedy max-coverage ordering + `guarantee()` (claim-slot promotion) |
| **`src/limitations.py`** | **NEW.** Type A/B detection, claim → limitation split, the `Ledger` |
| **`src/limitation_query.py`** | **NEW (step 3).** One faceted query portfolio per limitation, evaluated in ONE BigQuery scan; era-stratified selection; a screen that asks about the requirement rather than the invention |
| **`src/worldset.py`** | **NEW.** BigQuery working set, lexical, `similar_to`, `top_terms`, `classes_of`, `fetch_text`, `ingest` |
| **`src/claim_rescue.py`** | **NEW.** After the main loop: re-ask read refs about uncovered requirements → local no-CPC search → acquire → read → narrow re-ask |
| **`src/claim_acquire.py`** | **NEW.** `by_worldset` (BigQuery + vocabulary loop + similar graph), `by_concept` (App A fan-out), `by_citation` |
| `src/external.py` | App A `/api/bulk_search` fan-out, `materialise()` (writes publications), `family_keys` |
| `src/federation.py` | App A `/api/search` SSE client, 600s budget, partial-result recovery |
| `src/enrich.py` | Full-text recovery chain (OPS → Google → ScrapingBee → SerpApi), `_persist_full_text` |
| `src/bqclient.py` | BigQuery with dry-run cost guards. **`run_guarded` returns `(rows, est_gb, billed_gb)` — a tuple, not rows.** |
| `src/webview.py` | View model: cards, `build_reading_chart(axis=…)`, `build_ledger_view` |
| `templates/report.html` | Ledger block, element × reference grid, cards |
| `eval/attorney_recall.py` | **The KPI.** Scores a report against `eval/attorney_gold.json` |

---

## 6. The measured baseline — the attorney gold set

Ten references filed in a preissuance submission (2026-08-02) against **US 2026/0109053 A1**
(Schmalz, "Grip unit and vacuum handling apparatus"). Frozen in `eval/attorney_gold.json` with the
claim each was mapped against. This is the only gold set here that is an expert's finished work
product rather than a register scrape.

```
PYTHONPATH=src .venv/bin/python eval/attorney_recall.py [<slug> ...]
```

| Run | on page | read | screened | retrieved | in corpus |
|---|---|---|---|---|---|
| `adhoc-42a7b24f36d9` (before this session's fixes) | **1/10** | 4 | 5 | 5 | 7 |
| `adhoc-674f5b499e65` (after fixes 1–9) | **2/10** | 4 | 4 | 6 | 7 |

Per-reference outcome on the later run:
```
GRABO        US-11413727-B2   ON THE PAGE (card 4, via family member US-11999030-B2)
Hukelmann    US-10794526-B2   ON THE PAGE (card 57)  ← promoted by coverage_rank.guarantee
Cho          US-2014008929-A1 read, ranked 102 — off the page
Blatt        US-4453755-A     read, ranked 252 — off the page
Sadler       US-5269665-A     retrieved, outside the 2500 screen window
Quackenbush  US-2966138-A     retrieved, corpus holds 0 claims / 0 paragraphs
Perlmutter   US-5807034-A     in corpus, never retrieved
Crevling / Sato / Bosch       not in the local corpus at all
```
**All except GRABO's B1 spelling are present WITH FULL TEXT in a BigQuery working set** (9/10
verified): Crevling 35k chars, Sato 57k, Bosch 15k, Quackenbush 12,339 chars of description where
the local corpus holds a title.

---

## 7. What changed in this session

1. **A link now counts as a patent.** `_attach_query_document` required `source == "upload"`.
   `report["query_document"]["claims"]` is the *only* place the reading stage looks for the
   subject's claims, so a link search had its claims extracted, stashed, embedded and used for
   retrieval — then never put to a single reference. The claim table was empty and read as "no
   prior art discloses these claims".
2. **The grid charts the claims themselves**, most-disclosed first, 40 columns, both axes sticky.
   Fixed: a row could say "disclosed by 2 of 501 read" and render empty (count and cells came from
   different populations). `df == shown + also` is now asserted.
3. **`claim_rescue`** — after the main loop, go back for claims/limitations with no art.
   Measured: the narrow re-ask (`second_look` → `reread_absent(kind="claim")`) took a report from
   1 grounded claim cell in 221 to 8, moving claim 6 from 0 matches to 3.
4. **Claims entered the ranking objective.** `score_reference` built `covered` from feature rows
   only; claims now enter `covered` and the coverage idf, earn lead credit, and
   `coverage_rank.guarantee()` promotes the best discloser of any claim no visible card discloses.
   *Live: `promoted 2 references into the top 60 as the only art found for a claim: US-10794526-B2 (claim 9)`.*
5. **Text before the screen.** Enrichment was circular: no text → low screen → not chosen → never
   fetched. *Live: `pre-screen text recovery: 388 fetched, 137 candidates the screener can now read`.*
   `BLIND_RESCUE` 400 → full screen depth.
6. **`worldset`** — BigQuery as the index. Working set + lexical + `similar` graph + `top_terms`
   vocabulary loop + `ingest` that writes **text or nothing**.
7. **Federation** was discarding 1,500 collected hits on a 360s budget against 384s of real work.
   Budget → 600s; interim shortlists returned on deadline. *Live: completed in 481s.*
8. **`limitations`** — Type A/B split, claim → limitation parse, the `Ledger` as the stopping rule,
   anticipation computed (one document covering every limitation of a claim = §102 kill).
   *Live: `11 claims -> 27 limitations (27 split by the model, 0 structurally)`.*
9. **Ledger rendered** as the report's primary block for Type B.
10. **`eval/attorney_recall.py` + `eval/attorney_gold.json`** — the KPI.

Commits on `feat/claim-centric-search`: `f80d9972`, `e3353f04`, `3847c179`, `1fe627cb`, `f26a6a56`,
`dd21f802`, `a68d7f7f`, `335bacf9`.

---

## 8. Known limitations — read before "improving" anything

**Already tried and REVERTED. Do not retry without new evidence:**
- Widening `SCREEN_TOP` 2,500 → 5,000 **lowered** top-50 recall, three runs in a row. Every
  downstream cut is a fixed size, so a wider pool is more competition at each of them.
- Narrowing the CPC channel to the subject's own symbols: 0 of 12 cited references. Examiner
  citations do not sit in the subject's subgroups.
- Rerank-gating in `fuse.final_score`: recall@20 0.018 → 0.000.
- Charting 504 references instead of 344 did not improve order; it made the 50-card cut harder.
- **Google's precomputed similarity graph does not reach this gold set.** `google_patents_research`
  holds exactly 25 neighbours per publication. Seeded on Hukelmann, Blatt and Cho — the three the
  reading trusts most — the only gold document among all 75 neighbours is the seed's own family
  member. It is a resemblance signal and these references are precisely the ones that do not
  resemble. Keep it as a cheap extra bucket; do not build on it.
- **The citation graph is nearly as empty here.** Of the 45 possible pairs among the ten
  references, the forward-citation table contains **one** edge (Hukelmann → Cho); the ten cite 160
  distinct documents between them and almost none of each other. An examiner's own route does not
  connect this art.
- **Widening `coverage_rank.guarantee`'s bar does not rescue a read-but-buried reference.**
  Measured offline against `adhoc-3c99d840e5b3` (618 references, 27 limitations, 60-card window)
  in six arms — need 1 or 2 disclosers on the page, counting any verdict or only `disclosed`,
  capped at 12, 20 or unlimited. Blatt moved 314 → 314/317 and Cho 121 → 122/126 in every one.
  `guarantee` promotes the BEST discloser of a short item, and for every limitation Blatt and Cho
  ground there is a stronger one. Their cells are all `partial`; the fix is better evidence or a
  better reading, not a bigger promotion rule.

**Open defects:**
- **Vocabulary drift (unverified mitigation).** `claim_acquire.by_worldset` round 2 learned
  "exhaust, chamber, silencer, engine" from F01N and returned diesel silencers, aircraft
  attenuators and model-aeroplane mufflers — the automotive muffler field swallowed the query.
  Mitigated with a generic-term blocklist (`_GENERIC`) and domain anchors (`_anchor_terms`).
  **Neither has been re-tested.** Proper fix: score terms by log-odds against the class background,
  which needs a background model that does not exist yet.
- **`by_concept` (App A route) ingests text-less rows.** Live: 300 publications acquired, 0 of 40
  readable, and those 40 were 40 of the 46 candidates the rescue then "read" — the whole rescue
  read six documents. It is now a FALLBACK, taken only when the world routes returned fewer than
  `RESCUE_CONCEPT_BELOW` candidates, because it is the only route that does not need BigQuery.
- **`claim_rescue.second_look` on RESCUED references now grounds nothing.** Live on
  `adhoc-3c99d840e5b3`: `narrow re-ask of the 60 rescued references: 0 claim cells grounded across
  0 of them`, while the same pass over references the search had ALREADY read recovered 14 cells
  across 13 of 150. The likely reason is that it has been fixed upstream: the pass exists because
  a whole claim is a conjunction and a document found for the part it adds fails the whole-claim
  test, and the reader is now asked about LIMITATIONS, so the first read of a rescued reference
  already asks the narrow question. One run is not enough to delete a pass that was measured to
  take a report from 1 grounded claim cell to 8 — but check it before trusting it, and check it
  before spending ~60 model calls on it.
- **Blatt and Cho are read and still off the page.** `guarantee()` only promotes a claim's *only*
  discloser; where something else already covers that claim they stay buried.
- **`eval_baseline.json` is stale** — its recall@100_in 0.1158 sets a 0.0926 floor while plain HEAD
  scores 0.048, so `eval_guard.py` was already red before any of this work. Re-baseline before
  trusting it.
- **The ledger is Type B only.** Type A has no ledger and falls back to the grid (intentional).
- **`analyse_reference` reads limitations but nothing searches per-limitation yet** (step 3).
- **No §103 combination engine** (step 5). The report cannot say "A + B together kill claim 1".
- Cost/time: a Type B search is now ~40–60 minutes. `ACQUIRE_ENABLED=0` and
  `DEEP_RANK_RESCUE_CLAIMS=0` disable the expensive tails.

---

## 9. Next steps

### Step 3 — query portfolio per limitation — BUILT (`src/limitation_query.py`)
See §13 for what was measured. In short: a limitation is a THING, in a PLACE, in a KIND OF
APPARATUS, so a query for one is a conjunction of three disjunctions with proximity between the
first two. `must_any`/`must_all` cannot express that. Per-limitation querying moved one reference
from rank 8,810 under an invention-wide query to 135 under the query for the requirement it was
actually cited for, and era-stratified selection is what makes 1960s art reachable at all.

Still open inside step 3:
- the eight semantic REGISTERS are asked for in one prompt (`_FACET_SYS` lists automotive,
  acoustics, power-tool, 1960s-US and translated forms) rather than run as eight separate queries.
  Whether separate queries beat one prompt is unmeasured.
- assignee and forward/backward citation channels are not built. §8 says why citation looks
  unpromising *for this subject*; that is one gold set, not a law.

### Step 4 — multi-lens readers
Replace one reader/one checklist with five parallel readers over the same text:
**anticipation** (does this ONE document teach ALL limitations of claim N? → §102),
**limitation** (which requirements, quoted), **combination scout** (what pairs with what we have),
**vocabulary miner** (feeds step 3's loop), **date/status checker** (citable under this mode?).
Hook: `deep_analysis.analyse_reference` already batches; add prompt variants + fan-out.

### Step 5 — the §103 combination engine
Set-cover over reference **pairs and triples** to find the smallest set that between them covers
every limitation of a claim, plus a motivation-to-combine rationale. This is the actual invalidity
product and nothing today produces it. Hook: `Ledger.evidence` is already keyed by limitation.

### Smaller, high value
- Re-run `adhoc-385f6b114f20` (in flight at handoff) and score it; it is the first end-to-end Type B
  search with limitations + ledger and has never been measured.
- Verify the vocabulary anchors actually stop the drift.
- Delete or fix the text-less `by_concept` path.
- Escalation ladder: per-limitation rounds (working set → other classes/eras/jurisdictions → NPL /
  catalogues / standards → analogy search → name what was tried), stopping per limitation.

---

## 10. KPIs

**Primary (the product):**
| KPI | How | Baseline | Target |
|---|---|---|---|
| Attorney citations **on the page** | `eval/attorney_recall.py` | **2/10** | ≥7/10 |
| Limitations **covered** (≥2 grounded disclosures) | `report["ledger"]["summary"]["counts"]` | unmeasured | ≥90% |
| Claims **anticipated** (one doc covers all limitations) | `ledger_summary["anticipated"]` | unmeasured | report honestly; 0 is a valid finding |
| Ledger **`done`** | `ledger_summary["done"]` | unmeasured | true, or every uncovered limitation named with what was tried |

**Diagnostic funnel** (these say *what to fix*, not whether it is good):
in corpus → retrieved → screened → read in full → on the page. All five printed by
`attorney_recall.py`. A drop between two adjacent stages localises the defect exactly.

**Guardrails (must not regress):**
- `pytest tests -q` → 1022 pass
- grid invariant `row.df == cells shown + row.n_also`
- no ungrounded quote ever renders as coverage (`grounding == "verified"` gate)
- BigQuery spend per search < ~$25 (dry-run guards in `bqclient`)
- wall clock < 90 min

---

## 11. How to test

```bash
cd ~/patent-search-pilot

# unit + integration (8 min)
PYTHONPATH=src .venv/bin/python -m pytest tests -q            # expect 1022 passed

# the KPI against a finished report
PYTHONPATH=src .venv/bin/python eval/attorney_recall.py <slug>

# a live Type B search end to end
curl -s -X POST http://127.0.0.1:8631/extract -F "url=US20260109053A1" -o /tmp/ex.json
python3 -c "import json,io; d=json.load(open('/tmp/ex.json')); io.open('/tmp/b.txt','w').write(d['brief']); print(d['doc_token'])"
curl -s -X POST http://127.0.0.1:8631/run --data-urlencode "query@/tmp/b.txt" \
     --data-urlencode "doc_token=<token>" -d mode=novelty -d search_focus=all_text -D -

# watch it (40-60 min)
grep -E "\[limitations\]|\[ledger\]|\[rescue\]|\[acquire\]|\[worldset\]|pre-screen|promoted" \
     data/patent-results.out.log | tail -20
```

**Stage lines that prove each component is alive:**
```
[limitations] 11 claims -> 27 limitations (27 split by the model, 0 structurally)
[ledger] 27 limitations across 11 claims: N covered, N partial, N with nothing — ANTICIPATED: ...
[ledger] after the rescue: N covered, N partial, 0 with nothing; done=True
[deep_rank] pre-screen text recovery: 388 fetched, 137 candidates the screener can now read
[deep_rank] promoted 5 references into the top 60 as the only art found for a claim: ...
[worldset] built …ws_<hash>: 2392628 publications across 10 classes (1501 GB, $9.38, 145s)
[limq] the art already trusted is classified in F16L, B25J, ... — added to the working set
[limq] <lim id>: N in the working set, N taken across 4 eras
[limq] 6 limitation portfolios in one scan (15 GB) -> N candidates across 6 limitations
[limq] <lim id>: screened N/N, N at or above 45, N to read
[limq] N distinct families to read across N limitations (budget 150 reached; ...)
[acquire] ingest capped at 200: took 100 of 300 for claim 6[a], 100 of 200 for ~similar
[rescue] N limitations to search for — N with no evidence at all, N with less than 2 disclosures
[rescue] narrow re-ask of the N rescued references: N claim cells grounded
```

**Scoring the query layer WITHOUT a 50-minute pipeline run.** The whole of §13 was measured by
generating the portfolio SQL and running it against the working set directly, for about $0.21 a
time. `build_sql` is pure — it needs no database, no LLM and no service:

```python
import limitation_query as LQ
sql, slugs = LQ.build_sql(plan, "nimo-gpt.patent_pilot.ws_36208e56133485ea", date_max="20241018")
# wrap it and ask only where the gold landed:
#   WITH portfolio AS (<sql>) SELECT ... FROM portfolio WHERE pub IN (<the ten>)
```
Iterate the facets there, not in the pipeline. A run only settles what the ledger and the reader do
with what the query found.

**A finished report is regenerated by deleting `<slug>.view.json` and re-fetching
`/report/<slug>`** (~50s; runs the listwise rerank). The report `.json` is authoritative; the
`.view.json` is a cache and can be stale after a code change.

---

## 12. Gotchas that have cost real time

- `bqclient.run_guarded` returns **`(rows, est_gb, billed_gb)`**. Iterating it yields the row list
  as a single "row".
- `google_patents_research.similar` has **no similarity score**. Rank by array offset. It also
  holds only **25** neighbours per publication, which is why it is a bucket and not a channel.
- **A mid-pattern `(?s)` is legal in RE2 and a hard error in Python's `re`.** A proximity pattern
  that worked perfectly in BigQuery raised in every local test that touched it. `[^.]` already
  spans newlines, so the flag was never needed.
- **`ensure_dataset` gives `patent_pilot` a 3-day default table expiration** while
  `worldset.TTL_DAYS` says 30 and `_exists` believes it. Both live working sets were due to vanish
  three days after they were built while the code logged "reusing" — so from day 4 every search
  silently paid $9.38 for a rebuild. `worldset.build` now sets the expiry on the table explicitly.
- **A working set is keyed on `sha1(classes|date)`**, so a class list that comes from a model is a
  new $9.38 table every search. `claim_acquire.by_limitation` seeds it from the subclasses the
  references the reading already trusts actually carry, which is both stabler and not a guess.
  Widening the class list is nearly free: the build scans ~1,500 GB of text columns whatever the
  WHERE says.
- **The gunicorn worker imports `claim_acquire` and `worldset` lazily, inside the rescue.** A
  `git pull` during a running search therefore changes the code that search is about to execute,
  while `deep_rank` and `claim_rescue` (imported at boot) stay on the old version. That happened
  on `adhoc-3c99d840e5b3` and made it a mixed build. Restart between a deploy and a measurement,
  or the run measures neither version.
- `enrich._persist_full_text` returns `{"ok": false, "reason": "not_in_corpus"}` for any
  publication with no `publications` row. Materialise the row *first*.
- **Runtime-reassigning a module constant does not change a default argument.**
  `orphans(limit=MAX_CLAIMS)` binds at def time, so a probe that sets `claim_rescue.MAX_CLAIMS = 2`
  after import silently runs at production width. Env tuning is fine (read at import).
- `report["ranked_families"]` is **rewritten by `deep_rank`** at the end of a run — it is the FINAL
  order, not the retrieval order. Retrieval order is `deep_rank.candidates` / `candidate_families`.
- `pgrep -f` / `pkill -f` match the shell running your own command. Kill by explicit PID.
- Google Patents direct **503-blocks the whole egress IP** after ~80 requests, search and documents
  alike. ScrapingBee classes `patents.google.com` as Google: **15 credits/page**, `custom_google=True`
  required. Prefer BigQuery for all of this.
- Never edit `src/webapp.py` by prepending imports above `from __future__ import annotations` —
  SyntaxError takes the front door down.
- Backups: `*.bak-*` is gitignored; several exist beside changed files.

---

## 13. What the query layer was measured to do (2026-08-15, second session)

Everything below was measured against **`nimo-gpt.patent_pilot.ws_36208e56133485ea`** — 2,392,628
publications, 42.2 GB, every one with full text — which holds **all ten** references the attorney
filed, including the three the local corpus has never had. Quackenbush is in it with 12,339
characters of description where the corpus holds a title. So the retrieval universe is solved; the
open question was only ever whether the queries find them inside it.

### 13a. The query shape

| | hits | of the ten |
|---|---|---|
| bag of keywords, OR-ed — what `worldset.lexical(must_any=…)` does | 111,545 | 10, at no usable rank |
| three facets, literal forms only | 8,331 | 6 |
| three facets **with proximity** | 14,373 | **9** |
| …plus requiring the two facets in one sentence | 14,373 → same 9, 4k fewer hits | 9 |
| …plus requiring the hand-tool facet | 6,044 | 8 (loses Blatt) |
| …plus requiring it in the CLAIMS | 3,567 | 6 |

The tenth is GRABO's own patent, which never uses the word "exhaust" anywhere in 84,872 characters
— a real property of the document, not a query defect — and the main search finds it anyway.

**Proximity is not a refinement.** Sato's patent is titled "Sound absorption structure for air flow
path" and the damping words in its body are `sound | noise | sounds | absorbed | absorb`, never
adjacent. Bosch's are `damping | noise | dampened | noises`. Neither matches `sound[- ]?absorb`;
both match "sound within a sentence of absorb". Two of the ten are reachable only this way.

### 13b. One query per limitation, not one per invention

Rank of each reference in the query for the limitation it was actually cited for, era-stratified:

```
Sadler        55      Bosch    135      Blatt    200      Quackenbush 219      Hukelmann 334
Perlmutter  1,045     Crevling 1,796    GRABO  2,703      Sato      3,765      Cho       5,152
```

Bosch is the demonstration. Under a single query built for the sound-damping requirement it ranks
**8,810**; under the query for the requirement it was cited for — a hollow handle conducting
cooling air — it ranks **135**. Cho likewise: cited for a suction plate forming a vacuum chamber
with an embedded pump, and buried by any query about damping.

### 13b-2. THE RESULT, and the five configurations it took to get there

Scored the same way every time — the model's own facets, the live caps, the working set above,
counting a reference as found when the portfolio returns it inside `per_era=350`:

| configuration | of the ten |
|---|---|
| the model writes paraphrases (`"through grip portion"`, `"sound damping device"`) | **0** |
| terms repaired to strings a document contains | 4 — Cho, GRABO, Hukelmann, Sadler |
| the same code, a second sample of the facets | 3 — Blatt, Quackenbush, Sadler |
| two samples merged, readings POOLED under the limitation | 2 — Quackenbush, Sadler |
| two samples merged, a quota per READING | **8** — Blatt, Cho, Crevling, GRABO, Hukelmann, Perlmutter, Quackenbush, Sadler |

Missing at 8: Sato and Bosch. Both are reachable — a hand-written reading anchored on the exhaust
found Bosch at era-rank 135 — so this is the facets, not the machinery.

Four things are load-bearing there and each was measured, not reasoned:

1. **A term has to be a string a document contains.** Asked for "surface forms" the model returned
   `"through grip portion"`, `"in first housing portion"`, `"sound damping device"`. No patent
   contains any of them; a real document says "the exhaust air is guided through the hollow grip
   portion". `_terms` now strips leading prepositions and articles, drops a meaningless head noun,
   and keeps the first two words. **0 of 10 → 4.**
2. **One sample of the facets is a coin flip.** Same code, same limitations, two samples: one
   found Cho, GRABO, Hukelmann, Sadler; the next Blatt, Quackenbush, Sadler. Three to five either
   way and a *different* three to five. This is the largest single source of variance in the whole
   portfolio, larger than any cap or threshold.
3. **Merge the samples, but do NOT pool their readings.** OR-ing the readings and taking the best
   score returned **2 of 10 — worse than either sample alone**, because the union admits every
   document any reading reaches into the SAME per-era quota, so the pool grows faster than the
   signal and dilutes the ranking the quota is spending.
4. **A reading is a distinct query and needs its own quota**, exactly as a limitation does. With
   that, Quackenbush lands at era-rank **2**, Sadler at 8, Perlmutter at 8. **2 of 10 → 8.**

The general shape, and it has now bitten in four different places in this pipeline: *combining
independent evidence by union or averaging destroys it; combining it by giving each source its own
quota preserves it.*

### 13c. Era is its own competition

Quackenbush (1960, and the attorney's most comprehensive match) is **1,971 of 14,373** overall and
**219 of 1,403** among documents of its own era. Old art is short, written in another century's
vocabulary, and has fewer of everything to match on; it loses every relevance contest it is
entered in. Ranking inside era buckets and taking a quota from each is what makes it reachable.

### 13d. Three caps, each of which returned nothing while looking like it worked

This is the recurring failure mode in this pipeline and it is worth naming as a class: **a cap that
binds is a silent truncation, and two caps on one list means the tighter one silently wins.**

1. `by_worldset` ingested `list(found)[:200]` from a dict filled claim by claim, 300 hits per
   query. It ingested the first claim's first query and discarded every later claim, both
   vocabulary rounds and the entire similarity graph. Live, after the fix:
   `ingest capped at 200: took 100 of 300 for claim 6[a], 100 of 200 for ~similar` — the second
   hundred had never once been reached.
2. `rank_era <= 350 AND rank_q <= 800` in the portfolio SQL. Blatt is era-rank 200 / limitation-rank
   1,397, Quackenbush 219 / 2,429, Hukelmann 334 / 1,024 — all three inside the era quota, all
   three cut by the global one. 2 of 10 instead of 5.
3. `LIMQ_MAX_TOTAL` sized for the reader rather than the screener re-imposed the same cap one layer
   up: at 2,400 over six limitations each gets 400, and a reference at era-rank 200 sits at
   round-robin position ~800 because four era buckets interleave.

Every cap in `limitation_query` is now per limitation and per era, taken round robin, and logs what
it dropped.

### 13e. The cost shape is why the portfolio is affordable

BigQuery bills for columns scanned, so **thirty queries cost what one costs**: the portfolio is a
single scan with one boolean per facet, a pivot, and a window function per limitation. About $0.21
against the 42 GB working set, against ~$0.21 *per query* for the old one-query-at-a-time shape.
That is what makes it reasonable to size the pool for a screener (2,480 candidates in 33 seconds)
rather than for the reader, and to screen against the requirement before spending an ingest or a
read on anything.
