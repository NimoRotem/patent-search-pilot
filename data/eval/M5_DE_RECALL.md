# Milestone 5 §1 — cross-lingual German recall: diagnosis, fix, before/after

GRABO's real disputes (Schmalz, Probst) are German patents, so DE recall matters. The two DE
cross-lingual gold cases were ~0 at top-100. Diagnosed first, then fixed the dominant cause, then
measured — no vibes.

## Diagnosis (printed, per the spec)

For each DE gold query I printed the per-channel rank of every gold family and what text each gold
family actually has embedded:

- **The DE/EP gold families have almost no embedded text** — 1–5 chunks each, only `[abstract, whole]`,
  **no claims, no descriptions** (the BigQuery EP/WO/DE full-text hole: US claims 90.6% present,
  EP/WO/DE claims ~0%). A dense search has almost nothing to match against for these docs.
- **Native-query dense** surfaced only 2 of 7 grabo_de gold (ranks 135, 409) and 2 of 6 probst gold
  (ranks 86, 814); the rest didn't appear at any rank. **BM25 and CPC contributed nothing.**
- **German↔English query translation did NOT help and even hurt** (grabo_de gold 511→842 when fused
  with an English translation): the corpus is English-dominant, so translating a German query to
  English promotes English distractors above the sparse German gold.

**Dominant cause = (i) missing DE/EP full text**, not (ii) query language. So the fix is to embed
DE text, not to translate the query.

## Fix

- **Enriched a bounded, field-representative pool of claimless DE/EP/WO publications** (the dense
  candidate pool for the vacuum-gripping field, gold *and* distractors, so any recall gain is earned)
  via SerpApi `google_patents_details`, which returns the **native-language (German) claims**.
  Parsed + resolved them (added German dependency handling — "nach einem der voranstehenden
  Ansprüche"), inserted claim rows + chunks, embedded (Vertex, HNSW maintained incrementally).
  **909 DE/EP/WO pubs enriched, 10,975 German claims, 20,525 new embedded chunks.** Bounded by the
  SerpApi 5k/month quota (not all 14,280 DE/EP/WO core).
- Cross-lingual query translation is available (`Retriever.query_translations`, used by the agent)
  but is **off by default in hybrid** because the diagnosis showed it hurts.

## Before / after (DE queries, family recall)

| Query | config | @100 before → after | @500 before → after |
|---|---|--:|--:|
| grabo_de_utility_xling | vector | 0.00 → 0.00 | 0.286 → 0.286 |
| grabo_de_utility_xling | **hybrid** | 0.00 → 0.00 | **0.286 → 0.571** |
| probst_stone_lifter_xling | vector | **0.167** → 0.167 | 0.167 → 0.167 |
| probst_stone_lifter_xling | hybrid | **0.167** → 0.167 | 0.167 → 0.167 |

**What moved:** embedding the enriched German claims **doubled grabo_de hybrid recall@500 (0.286 →
0.571)** — 4 of 7 gold families now surface in the top-500 vs 2 before. The mechanism works: where DE
full text exists, dense finds it. probst_stone was already **> 0 at top-100** (0.167).

## Why grabo_de_utility stays at 0 @100 (documented limitation)

Thoroughly diagnosed, not a retrieval bug:

1. The subject `DE-202019005606-U1` had **zero examiner citations**, so its entire gold set is a
   *hand-curated* Schmalz/Probst competitor list — "relevant competitors" in a business sense, not
   semantic-nearest prior art.
2. Those competitors are genuinely at dense ranks **153–474** (best = family 34201690 at rank 153),
   not top-100 — and the **cross-encoder reranker ranked the closest one *lower* (153→196)**,
   independently confirming it is not a top-100 match.
3. The closest gold (34201690) **cannot be enriched** — Google Patents / SerpApi return no claims or
   full text for it (an old, un-digitized DE family). All 11 remaining DE/EP gold pubs likewise
   returned "no claims on SerpApi."

So no amount of retrieval tuning puts a curated, un-digitized, semantically-distant competitor into
the top-100. The honest levers are corpus/gold definition (citation-derived gold + fuller DE text via
EPO OPS), not the ranker. Enrichment is proven to help wherever DE text is actually available.
