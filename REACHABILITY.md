# Corpus reachability — where the recall ceiling actually is, and what expansion would cost

Decision-support for whether to fund a bigger ingest. Measured on the frozen 11-query gold set
(79 distinct gold families). **TL;DR: the ceiling is text depth, not corpus size — the free EPO OPS
backfill is the highest-leverage fix; a paid corpus expansion has limited, quantified headroom.**

## Finding 1 — corpus coverage is already high (86%), not the bottleneck

| | families | share |
|---|--:|--:|
| distinct gold families | 79 | 100% |
| **in the 107k corpus (reachable)** | **68** | **86%** |
| missing from corpus | 11 | 14% |

Per-query coverage is 84–100% for every query. So the eval's low `reachable@100 ≈ 0.18` is **not**
"82% of gold is missing" — 86% of gold is present. It is a **retrieval-ranking + text-depth ceiling**:
of the 68 in-corpus gold families, only ~18% currently rank inside the top-100.

## Finding 2 — the real ceiling is text depth (the EP/WO/DE full-text hole)

Of the 68 in-corpus gold families, **46 (68%) have no embedded claims** — only a title + abstract —
because BigQuery lacks EP/WO/DE full text (and some thin US docs):

| jurisdiction | in-corpus gold families | thin (no claims) |
|---|--:|--:|
| US | 40 | 15 |
| DE | 29 | 20 |
| EP | 8 | 6 |
| WO | 7 | 5 |

A thin-text family has almost nothing for dense retrieval to match, so it ranks far below top-100
(exactly the DE diagnosis in `M5_DE_RECALL.md`, where enriching DE claims doubled grabo_de recall@500).

**→ The #1 lever to lift `reachable@100` toward 0.5 is filling text depth, i.e. the EPO OPS backfill
(`ops.py --backfill-core`, 13,400 claimless DE/EP/WO core pubs). It is essentially free (OPS
4 GB/week), needs no new BigQuery ingest, and directly deepens 31 of the 46 thin gold families.**
This is a genuine retrieval improvement, not teaching-to-the-test — it adds the *real* text of docs
already in the corpus.

## Finding 3 — the 11 missing gold families (what a corpus expansion buys)

Resolved via BigQuery — they sit **outside the US/EP/WO/DE seed jurisdictions**:

| jurisdiction | missing gold | era | note |
|---|--:|---|---|
| FR | 4 | 1953–1965 | old French vacuum-lifting / handling art |
| CN | 1 | 2017 | one suction-cup family |
| (unresolved) | 6 | — | very old / non-CPC-classified docs |

So corpus expansion could recover **at most 14%** of gold, and the recoverable part is dominated by
**old French patents**.

## Costed expansion options

Current corpus = 25,786 seed-CPC pubs (US/EP/WO/DE). Worldwide seed-CPC adds **54,522** more pubs:
CN 28,938 · KR 9,067 · JP 4,569 · GB 1,359 · FR 1,313. BigQuery full-text extraction ≈ **$10–20 per
scan** (the seed slice is ~1.5 TB billed; §2 dry-run = 16 GB for the count query).

| Goal | Action | +pubs | BigQuery $ | Recovers |
|---|---|--:|--:|---|
| **reachable@100 → ~0.5** | **EPO OPS backfill (text depth), no ingest** | 0 | **$0** | most in-corpus thin gold |
| absolute recall +~5% | + FR + GB field expansion (classic prior-art jurisdictions) | ~2,700 (+ families/citations ~10k) | ~$15–25 | ~4 of 11 missing (the FR art) |
| reachable@100 → ~0.8 | OPS backfill **and** FR/GB expansion **and** re-tuned reranking on the deeper text | ~10k | ~$25 | text depth + FR art |
| (not recommended) full worldwide | + CN/KR/JP seed-CPC | +42k | ~$40–60 | 1 missing gold (CN) — poor ROI |

## Honest expansion vs teaching-to-the-test

- **Honest field-coverage expansion** = ingest more *vacuum-gripping + neighbouring-CPC* art in
  under-covered jurisdictions (FR/GB for the old classic art). This is a real capability gain that
  helps *future, unseen* searches, and it's what the seed-CPC/family/citation crawl already does —
  just widened to FR/GB.
- **Teaching-to-the-test** = ingesting the exact 11 missing gold publications by number. **Do NOT do
  this** — it inflates the eval without improving real search. The gold set exists to *measure*
  coverage, not to be back-filled into the corpus.

## Recommendation

1. **Run the EPO OPS backfill first (free, biggest lever).** It deepens the text of docs already in
   the corpus and should move `reachable@100` from ~0.18 toward ~0.4–0.5 with zero ingest cost. This
   is gated only on OPS credentials — `ops.py` is implemented and ready (`--dry-run` proven).
2. **Then, optionally, a small FR + GB field expansion (~$15–25 BigQuery)** to pick up the old
   classic prior art that the missing gold is concentrated in. Modest, honest, bounded.
3. **Skip the full worldwide ingest (CN/KR/JP, +42k pubs, ~$40–60).** It recovers one missing gold
   family — poor ROI for the pilot; revisit only if a real dispute needs Asian art.

Net: the pilot's recall ceiling is mostly a *text-depth* problem with a *free* fix (OPS), not a
corpus-size problem needing a big paid ingest.
