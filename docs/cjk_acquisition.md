# Readable text for the CJK half of the corpus

Measured 2026-08-22 on the live corpus, the live acquisition pool and live provider calls. Every
number below has the query or the call that produced it named beside it. Nothing here is quoted
from a provider's documentation: where a route is described as working or not working, it was
tried.

---

## 1. The answer, first

**Google Patents, fetched from this box's own IP, is the only route that reaches CJK full text at
volume, and it is already the `serp_self` rung of `src/acquire/providers.py`.** Measured on the
live acquisition pool over the three hours to 18:56 UTC on 2026-08-22:

| office | serp_self attempts | hits | hit rate | mean description | mean claims |
|---|--:|--:|--:|--:|--:|
| CN | 9,360 | 9,359 | **99.99%** | 23,049 chars | 5,366 chars |
| KR | 4,657 | 4,569 | 98.1% | 24,022 | 3,182 |
| JP | 393 | 375 | 95.4% | 29,956 | 2,857 |
| TW | 85 | 70 | 82.4% | 34,017 | 4,307 |

All of it in English, machine translated by Google. Sustained throughput with the two workers
that are running now: **5,320, 4,751 and 4,498 hits in the last three full hours**, and 2 refusals
in 17,733 attempts. At 5,000 documents an hour, **898,377 families is about 7.5 days and costs
nothing.** HimmPat's 250 a day is about 3,600 days. The gap is four orders of magnitude and it is
not a tuning problem in either direction.

**The "900,463 families route to HimmPat" figure was an artefact of a label, not a measurement of
the cascade.** `corpus_niche.SOURCE_LADDER` named `himmpat` for any family whose members are all
CN, JP or KR, so `best_source` stamped 62.6% of the acquisition job with it. The cascade in
`src/acquire/providers.py` has never asked HimmPat first: it asks Google Patents four rungs
earlier. In the live run that produced the table above, HimmPat answered **45** publications and
`serp_self` answered **16,392**. The rung is now gone and the ladder routes CJK to
`gpatents_direct` like every other office.

### What this does not fix

Google Patents is one provider, on one IP, with no contract. It is 99.99% today and it can be 0%
tomorrow, and the second-best route for CJK full text is ScrapingBee at 15 credits a page
(about 55,000 pages a month left) fetching **the same pages**. There is no free, non-Google route
to CJK full text. Section 3 is the evidence for that sentence, one provider at a time.

---

## 2. HimmPat is barred from bulk, structurally

`src/realtime_only.py`. It **defaults to deny**: nothing may call a real-time-only provider until
a process declares itself a live search, and the only two callers of `enable()` in the tree are
`src/webapp.py` and `src/runner/worker.py`. Every offline process, the acquisition worker, every
`ops/` script, a cron job, a notebook, the test suite, is refused without having to know the
module exists. This is the mirror of `corpus_guard`, which arms the one process that must not
write; here there is one process that may call, so denying by default is the smaller surface.

Two independent doors:

1. **`sources.himmpat.HimmPat._post`**, the one boundary every billed HimmPat call passes
   through, calls `realtime_only.check("himmpat")` before the cache, the key check and the
   ledger. `HimmPat.enabled()` returns False and `disabled_reason()` says why, so `/api/health`
   and the cascade's startup banner report the bar rather than looking broken.
2. **`acquire.providers.BARRED`**, which `build()` consults before it constructs anything. The
   name is gone from `DEFAULT_ORDER` and from `DEFAULT_CAPS`, and `FULLTEXT_CASCADE=corpus,himmpat`
   raises rather than quietly putting the rung back. Removing a name from a default list only
   changes a default; the env var is the documented way to change the cascade without a deploy.

The live search path is untouched: `sources/fulltext.py` still has its HimmPat rung, and
`sources.bulk()` still fans out to it, from a process that has declared itself.

Tested in `tests/test_cjk_acquisition.py`, defect injected both ways:

* `test_a_bulk_process_cannot_reach_the_himmpat_http_boundary` asserts no HTTP call is made.
* `test_defect_injection_without_the_guard_the_same_bulk_call_goes_through` stubs
  `realtime_only.check` to a no-op and asserts the identical call now reaches the transport. If
  that test ever passes with the guard in place, the guard is not on the call path.
* `test_a_live_search_process_may_still_call_himmpat` is the other half of the rule.
* `test_build_refuses_a_himmpat_rung_even_when_an_operator_asks_for_one` covers the env var.

---

## 3. Every other route, with the call that settled it

### 3.1 BigQuery `patents-public-data`: no CJK full text exists there. At all.

Censused over the whole table, `bq query` 2026-08-22, 1.8 GB scanned:

| office | `description_localized` entries | `claims_localized` entries | `abstract_localized` entries | publications |
|---|--:|--:|--:|--:|
| US | **21,993,541** | **18,760,680** | 24,138,560 | 22,002,426 |
| CN | 0 | 0 | 109,458,729 | 54,743,394 |
| JP | 0 | 0 | 31,336,526 | 28,175,668 |
| KR | 0 | 0 | 10,977,481 | 8,027,935 |
| TW | 0 | 0 | 2,874,249 | 2,595,882 |
| EP | 0 | 0 | 13,309,783 | 9,106,299 |
| WO | 0 | 0 | 21,043,516 | 6,143,867 |
| DE | 0 | 0 | 5,775,822 | 8,250,246 |

Full text in that dataset is a **United States** column. This is not a regression that reading an
older release would undo: the same census against `patents.publications_201710` returns NULL
description for all 25,216,799 JP, 16,098,407 CN and 4,454,457 KR rows in it.
`google_patents_research.publications` carries `title`, `abstract`, `top_terms`, `cited_by` and
`embedding_v1`, and no description or claims column at all.

What BigQuery **does** hold for CJK is Google's English abstract, and it holds it almost
everywhere:

| office | English abstracts | of publications | share |
|---|--:|--:|--:|
| CN | 54,729,311 | 54,743,394 | **100.0%** |
| JP | 12,770,474 | 28,175,668 | 45.3% |
| KR | 3,227,226 | 8,027,935 | 40.2% |
| TW | 1,682,074 | 2,595,882 | 64.8% |

So BigQuery is a **reachability** route, not a full-text route, and it is implemented as the
`bq_cjk` rung. See section 5.

### 3.2 EPO OPS: refuses CJK full text by office, and says so

Live, 2026-08-22, with `OPS_CONSUMER_KEY` from `.env`:

| request | result |
|---|---|
| `CN.101234567.A/description` | **404 `CLIENT.InvalidCountryCode`** |
| `CN.101234567.A/claims` | **404 `CLIENT.InvalidCountryCode`** |
| `JP.2005312821.A/description` | 404 `CLIENT.InvalidCountryCode` |
| `KR20100012345/description` | 404 `CLIENT.InvalidCountryCode` |
| `CN.101234567.A/abstract` | **200**, 2,813 bytes, English |
| `CN.101234567.A/biblio` | 200, 4,408 bytes |
| `EP1000000/description` | 200, 11,187 bytes |

`InvalidCountryCode` is a refusal by **office**, not a missing document, which is stronger than
the 404s recorded on 2026-08-14: no CN, JP or KR publication will ever answer, so there is no
point retrying with a different number form.

The real quota, read off the `X-Throttling-Control` header rather than from the documentation:
idle is `retrieval=green:200, other=green:1000, search=green:30, inpadoc=green:60, images=green:200`
per minute, degrading to `retrieval=green:100, search=green:15` under load. 200 retrievals a
minute is 288,000 a day, so the 4 GB/week tier is the binding limit, not the rate. All of that
capacity is only usable for EP and WO, which is what the `epo_ops` rung already does.

### 3.3 MAREC is obtainable after all, and it does not help

**`docs/v3_resume.md` says MAREC is unobtainable because the Information Retrieval Facility no
longer operates. That is wrong: Google hosts it.** `patents-public-data.marec.publications`,
**19,101,548 rows, 547 GB of ST.36 XML**, readable with the service account we already have:

| office | rows |
|---|--:|
| JP | **8,133,947** |
| US | 5,673,935 |
| EP | 3,508,686 |
| WO | 1,784,980 |

8.13M JP publications is by a distance the largest JP holding available to us for free, so it was
worth parsing before believing. 320 records sampled through `bq head` (free) and parsed with this
repo's own `acquire.providers.parse_st36`:

| office | sampled | with description >= 800 chars | with claims >= 200 chars | with an abstract |
|---|--:|--:|--:|--:|
| JP | 173 | **0** | **0** | 173 |
| US | 80 | 79 | 79 | 80 |
| WO | 40 | 27 | 27 | 39 |
| EP | 27 | 15 | 15 | 15 |

**The MAREC JP portion is Patent Abstracts of Japan: an English abstract, a title and
bibliography, and no body.** Its US, EP and WO portions do carry full text, and those three
offices are already served free by `pqai` and `epo_ops`. Each record also carries an IRF licence
notice in the XML restricting redistribution. Correct the resume doc; do not build on it for CJK.

### 3.4 The offices' own channels: none is a route we can take today

Every one of these was called live from this box on 2026-08-22.

| route | result | verdict |
|---|---|---|
| CNIPA `epub.cnipa.gov.cn` | TLS handshake timeout from `us-central1` | unreachable from this fleet |
| CNIPA `pss-system.cponline.cnipa.gov.cn` | HTTP **412** | anti-automation gate, account required |
| WIPO PATENTSCOPE search | 200 | reachable, but the detail page is keyed by an internal `docId`, not a publication number, and a direct publication-number `detail.jsf` reset the connection |
| WIPO PATENTSCOPE OAI-PMH | **404** | no harvest endpoint |
| Espacenet web search | **403** | blocked |
| KIPRIS Plus REST | 200 with an empty XML envelope on a bogus key | free, but needs a registered Korean service key we do not have |
| JPO `ip-data.jpo.go.jp` | 200 | the JPO's own API portal; registration required, application in Japanese |
| J-PlatPat | 200 | a web UI with no public API |
| Lens.org API | **401** | paid, commercial licence |

PATENTSCOPE is the closest of these to usable and it is still a JSF scrape behind a session and a
`docId` indirection, against a WIPO service, for content Google already gives us in English. None
of these is a volume route without an account application, and none of them would be free of a
scraping question afterwards.

### 3.5 There is no English original to fall back on

The obvious cheap idea is to stop translating and find the family member that was filed in
English. Measured, and it does not work.

World DOCDB, from `patents-public-data.patents.publications` grouped by `family_id`
(2.4 GB scanned): **68,760,229 simple families have a CJK member, and only 6,086,224 of them
(8.85%) have a US, EP, WO, GB, AU or CA member.** 62,534,532 (90.9%) are CJK-only worldwide.

Inside our own niche it is worse. 3,902 niche families that this corpus holds only CJK members
for, checked against the world family in BigQuery: **98 (2.51%) have any non-CJK member anywhere,
94 (2.41%) have an English-office member.**

**So for 97.5% of the CJK-only niche, a machine translation is the only English text that will
ever exist.** That is the fact the translation decision in section 6 has to be built on.

---

## 4. What is actually missing, and it is not what the brief assumed

Two premises in the brief are measurably wrong, and correcting them changes what to build.

### 4.1 "CJK is 39.9% of the corpus, so that share is dead in lexical search"

39.9% is the share of **publications by office**. It is not the share of indexed **text**.
Sampled 1% of `chunks` with `TABLESAMPLE SYSTEM (1)`, 277,419 rows: **6,152 (2.22%) contain any
CJK character and 5,781 (2.08%) are CJK-dominant.** Scaled to 27.62M chunks that is about 574,000
chunks, not 11 million.

The reason is that the Chinese text already in the corpus is **already English**. Sampled 3% of
`publications`: 49,350 CN rows carry `abstract_lang='zh'` and **exactly 1 of them contains a CJK
character.** The corpus was built from BigQuery, which supplied Google's English abstract and the
source language label, and the label has been lying ever since. CN is not dead in lexical search;
it is searchable and mislabelled.

CJK-character share of chunks, by office, 1% publication sample joined through `ix_chunks_pub`:

| office | chunks sampled | containing CJK | share |
|---|--:|--:|--:|
| CN | 34,842 | 240 | **0.69%** |
| TW | 1,060 | 162 | 15.3% |
| JP | 8,440 | 1,588 | 18.8% |
| KR | 6,020 | 2,640 | **43.9%** |

Korean is the CJK problem in this corpus, not Chinese.

### 4.2 What IS missing is claims and description

Same 1% sample, chunks per office by kind:

| office | abstract chunks | claim chunks | ratio |
|---|--:|--:|--:|
| US | 9,473 | 159,687 | 16.9 : 1 |
| CN | 16,700 | 1,064 | **0.06 : 1** |

CN has more abstract chunks than the US and one sixtieth of the claim chunks. The corpus knows
what 795,448 Chinese families are **about**; it cannot quote a single line of most of them. That
is the 5.7% readable figure, and it is a full-text acquisition problem, which is what section 1
answers.

### 4.3 `to_tsvector('english')` on CJK, verified

Run live against the production database:

```
to_tsvector('english', '一种真空吸盘装置及其控制方法,包括吸盘本体和真空发生器。')
  -> '一种真空吸盘装置及其控制方法':1 '包括吸盘本体和真空发生器':2

to_tsvector('english', '真空吸着装置およびその制御方法')
  -> '真空吸着装置およびその制御方法':1

to_tsvector('english', '진공 흡착 패드 및 그 제어 방법')
  -> '그':5 '및':4 '방법':7 '제어':6 '진공':1 '패드':3 '흡착':2
```

Chinese and Japanese collapse to **one lexeme per punctuation-delimited run**: a whole sentence
becomes a single token that no query term can ever match. Korean is space-delimited so it does
tokenise, but with an English stemmer and no Korean morphology, so particles stay glued to nouns
and recall is partial rather than zero. **Chinese and Japanese source text is dead in the lexical
channel; Korean is degraded.** That is the Tantivy case, on 2.08% of chunks rather than 39.9%.

---

## 5. What was implemented: the `bq_cjk` rung

`acquire.providers.BigQueryCjkProvider`, rung 1 of the cascade, immediately after `corpus` and
ahead of every paid rung.

**What it serves.** English title and abstract for CN / JP / KR / TW, from
`nimo-gpt.patents_cache.cjk_text`, a clustered copy of the `patents-public-data` CJK slice built
by `ops/bq_cjk_cache.py`. **72,194,695 rows, 83.5 GB**: CN 54,729,311, JP 12,770,459,
KR 3,226,463, TW 1,468,462.

**What it can never serve.** Claims or description, because as section 3.1 shows there are none
to serve. `test_bq_cjk_can_never_claim_full_text` asserts `complete()` is False even for a
4,000-character abstract, so the cascade always falls through to the rung that does have full
text and an abstract can never be promoted to a document.

**Why a cache table.** A single publication-number lookup against
`patents-public-data.patents.publications` dry-runs at **228 GB**, $1.43 to read one abstract,
because the table is neither partitioned nor clustered on the number. Clustered by the normalised
number, the same lookup prunes to the blocks that can hold the keys. Measured on the settled
table:

| lookup | billed | per publication |
|---|--:|--:|
| 24 keys from the pool | 12.6 MB (near BigQuery's 10 MB per-query floor) | **$0.0000033** |
| 4,000 random pending CJK keys | 864 MB | $0.0000014 |
| 5,000 keys | 1,016 MB | $0.0000013 |

**The first queries after the build are not the cost, and reading them as the cost is how a good
provider gets rejected.** Immediately after the `CREATE TABLE AS`, the identical 6-key lookup
billed **334 MB**; once the table had settled it billed **10.5 MB**, a 32-fold difference.
Splitting a mixed batch into one query per office is also worse, not better: four single-office
queries bill 4 x 10.5 MB against one mixed query's 10.5 MB, because the floor is per query.

That floor is why the provider has a `prefetch()` seam. `Worker.prefetch_batch` warms every rung
once per leased batch, so 24 publications cost one query minimum instead of 24.
`test_the_worker_warms_every_rung_once_per_leased_batch` is defect injected against removing that
call. The ledger charges in **megabytes**, because megabytes are what BigQuery bills, and the
recorded figure is the real bytes the batch query billed divided over the keys it carried.

**Hit rate against the real work list.** A random 4,000 of the pending CJK rows in
`fulltext_fetch_task`, drawn with `ORDER BY md5(publication_number)` rather than a bare `LIMIT`,
because a `LIMIT` on this pool returns one physically clustered block and reads about 20 points
low:

| office | in the sample | found in the cache | share |
|---|--:|--:|--:|
| CN | 2,857 | 2,857 | **100.0%** |
| JP | 664 | 542 | 81.6% |
| KR | 433 | 299 | 69.1% |
| TW | 46 | 29 | 63.0% |
| **all** | **4,000** | **3,727** | **93.2%** |

The 6.8% shortfall is age, not format: that tail is pre-2000 and Google has no English abstract
for it either.

**Cost.** The build scanned 231 GB, $1.44 at $6.25/TB and free inside BigQuery's 1 TB monthly
allowance. The table is 83.5 GB, about **$1.67 a month** of active storage. At 24 keys a leased
batch the entire 898,377-family CJK job is **$2.94**; at 4,000-key batches it is $1.21. The
default ledger cap is 2,000,000 MB a month, $12.50. No new account: the GCE service account
already used by `sources/bigquery_gpatents.py` reads it.

### 5.1 A partial answer is now kept instead of discarded

`Worker.cascade_for` used to return `None` whenever nothing cleared the completeness floor, so
everything the cascade had fetched was thrown away and the row went to `missing` holding
literally nothing. Measured on the live pool: **1,341 publications**. It now returns the largest
incomplete result and `Worker.handle` stores it (GCS, `sources_docstore`, `corpus_ingest_queue`,
and nothing else) while still recording the row as `missing`, because no full text was found and
the next pass should still try. For a Chinese publication that Google has no page for, the
`bq_cjk` title and abstract is the whole of what will ever be readable, and before this it was
being deleted on the way past.

---

## 6. The translation decision

**Index the English machine translation as the retrieval text, keep the source text as the
evidentiary text, and label every quotation with which one it came from.** The three reasons are
measurements, not preferences.

### 6.1 The dense channel does not need the translation. Measured.

`gemini-embedding-001` at 768 dimensions, the corpus's own vector space. 60 corpus abstracts
still in CJK script were embedded as `RETRIEVAL_QUERY`, against a pool of **1,060** English
documents: each query's own simple-family sibling's English abstract, plus 1,000 English abstracts
drawn from `B25J`, so the distractors are in the niche rather than random art.

> **recall@1 = 59/60 (98.3%), recall@5 = 60/60, recall@10 = 60/60, median rank 1, mean
> self-cosine 0.830.** Worst rank across the 60 was 3.

Embedding CJK source text retrieves its own English family member. The multilingual model earns
its keep and the dense channel needs no translation to work across the language boundary.

### 6.2 The lexical channel cannot use the source text at all

Section 4.3. Chinese and Japanese collapse to one lexeme per sentence in
`to_tsvector('english')`, and Tantivy without a CJK tokenizer will do the same thing. So whatever
the lexical index holds for a Chinese document has to be English, or that document is absent from
half the retrieval system. Since **97.5% of CJK-only niche families have no non-CJK member
anywhere in the world** (section 3.5), that English cannot be an original. It is a machine
translation or it is nothing.

### 6.3 A quotation from a translation is a different evidentiary object

This is the constraint that stops "index the translation" from being the whole answer. A positive
grid cell cites an exact quotation with a location. A quotation from Google's machine translation
of `CN102145486A` is not a quotation from `CN102145486A`; it is evidence about what the document
probably says, and an examiner or an opponent can contest the translation in a way they cannot
contest the original.

So the record has to carry both, and the acquisition path already can:

* `FetchResult.claims_lang` / `desc_lang` record the language the text is in. `serp_self` returns
  `en` for a Chinese document because the page it fetched was `/en`, which is honest.
* `bq_cjk` sets `meta["text_is_machine_translation"] = True` and
  `meta["source_language"]` to the office's own language, so a reader of `sources_docstore` can
  always tell.
* The corpus's `abstract_lang` column is **not** to be trusted for this: it says `zh` on 54,729,311
  Chinese abstracts that are in English (section 4.1). Anything deciding what a quotation is
  should read the docstore record's provenance, not that column.

### 6.4 The three things this implies, for the workstreams that own them

1. **Retrieval (G) and the lexical build (F).** Index the English text. It is what the corpus
   already holds for CN, it is what `serp_self` fetches, and it is the only thing Tantivy can
   tokenise. Do not spend a CJK tokenizer on 2.08% of chunks before the acquisition has run: after
   it, the CJK share of chunks will be smaller still, because the text arriving is English.
2. **The report renderer.** A quotation whose docstore record carries
   `text_is_machine_translation` must be rendered as a translation, with the source language and
   the publication number, and ideally beside the source-language span. This is a UI change nobody
   owns yet and it is the one real cost of the decision above.
3. **`abstract_lang` is wrong and should be repaired, not worked around.** It claims `zh` for
   English text on essentially the whole Chinese corpus. Whoever next writes an ingest pass should
   set it from the text, not from the office.

---

## 7. Summary table: routes to CJK full text, priced

| route | reaches CJK full text | rate | cost | account | verdict |
|---|---|---|---|---|---|
| **Google Patents, own IP (`serp_self`)** | **yes, 99.99% CN** | **~5,000/hour measured** | **free** | none | **the answer** |
| ScrapingBee (`scrapingbee`) | yes, same pages | 8 in flight | 15 credits/page, ~55,000 pages left | have | the insurance policy, and it is thin |
| SerpApi (`serpapi`) | yes, same pages | 6,000/hour | $0.0092/doc, **$8,265** for 898,377 | have | last resort; the plan is 30,000/month, so 30 months |
| HimmPat | yes | **250 units/day** | trial key | have | **barred from bulk**. 3,600 days |
| BigQuery (`bq_cjk`) | **no, abstracts only** (93.2% of the work list) | unlimited | $0.0000033/doc, $2.94 for the job | have | implemented, for reachability |
| EPO OPS | **no**, `InvalidCountryCode` | 200/min | free | have | EP/WO only, already wired |
| MAREC on BigQuery | **no**, JP is abstracts | unlimited | ~$3 one-off | have | corrects the resume doc, helps nothing |
| CNIPA | unknown | unreachable | unknown | no | blocked from this fleet |
| PATENTSCOPE | partial | scrape | free | no | no per-publication API |
| KIPRIS Plus | unknown | unknown | free | **no** | needs a Korean service key |
| JPO JPP | unknown | unknown | free | **no** | needs a Japanese registration |
| Lens.org | unknown | unknown | paid | **no** | 401 |

**Everything in the first three rows fetches the same Google Patents page.** The corpus's CJK
full text has exactly one upstream, and the cheapest way to reach it is also the fastest.
