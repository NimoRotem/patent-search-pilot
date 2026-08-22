# The lexical backend interface

Owner of the contract: workstream B (retrieval). Implementor of the Tantivy side: workstream C.
The executable copy is `src/retrieval/lexical.py`; if the two disagree, the code is right and this
file is a bug.

## Why there is an interface at all

Sparse retrieval today is PostgreSQL `to_tsvector('english', ...)` over the whole corpus, ranked by
raw match count. Measured on the standing benchmark: one `solo:bm25` pass takes 20.45 s on
`ep3707092` and 175.55 s on `schmalz`, and in both cases it returns **zero** of the gold families at
any depth. CJK is 39.9% of the corpus and the `english` configuration has no segmenter for it, so
that share is not degraded, it is dead. Workstream C replaces the implementation. Nothing else in
retrieval should have to change when it does.

## The contract

```python
class LexicalBackend:
    name: str

    def available(self) -> bool: ...

    def search(self, query, *, fields=(), filters=None, limit=1000,
               operator="or", max_terms=None, rank="density") -> list[LexicalHit]: ...

    def publications(self, query, *, fields=(), filters=None, limit=1000,
                     operator="or", max_terms=None, rank="density") -> list[tuple]: ...
```

### Arguments

| Name | Type | Meaning |
|---|---|---|
| `query` | `str` | Raw query text. Never pre-tokenised. May be a whole paragraph of a patent. |
| `fields` | `tuple[str]` | Chunk kinds to search. `()` means the backend's default field set. |
| `filters` | `LexicalFilters` | What is allowed back. See below. |
| `limit` | `int` | Maximum rows. `search` counts chunks, `publications` counts publications. |
| `operator` | `"or"` \| `"and"` | How the query terms combine. |
| `max_terms` | `int` \| `None` | Truncate the query to its N most specific terms. `None` = backend default. |
| `rank` | `"density"` \| `"match_count"` | A HINT. A backend with real BM25 should ignore it and return BM25. |

`fields` are the values of `chunks.kind`:
`abstract`, `claim_own`, `claim_resolved`, `whole`, `paragraph`, `title`, `figure_caption`.
A Tantivy document is one chunk and its field is its kind.

### `LexicalFilters`

```python
kinds: tuple = ()                     # include only these chunk kinds
exclude_kinds: tuple = ()             # never these
published_before: date | None         # publications.publication_date < this
published_after: date | None          # publications.publication_date > this
effective_filing_before: date | None  # COALESCE(earliest_priority_date, filing_date) < this
exclude_family_of: str | None         # publication_number whose simple family is excluded
mode: str | None                      # 'novelty' | 'inventive_step'
sql: tuple = ("", ())                 # POSTGRES ONLY: (fragment, params) against alias `p`
```

Build it with `LexicalFilters.from_subject_mode(subject, mode)`. Never build the date window by
hand: `search_modes.citable_where` is the only implementation of the citability rules and a second
one is a legal defect, not a performance optimisation.

`sql` is an escape hatch for the Postgres backend only, because the novelty window is a
jurisdiction-sensitive disjunction (EPC Art. 54(2) public art OR Art. 54(3) secret art, with an
optional same-jurisdiction restriction). **A Tantivy backend ignores `sql` and implements the
structural fields.** If it can only express one half of the novelty disjunction, implement
`published_before` and drop the secret-art half: under-returning is a recall loss, over-returning
is a document presented as prior art that is not.

### `LexicalHit`

```python
publication_id: int | str    # local bigint, or "fed:<PUBNUM>" for an external row
chunk_id: int | None         # None when the backend cannot name the chunk
score: float                 # the backend's own relevance
field: str                   # the chunk kind that matched
snippet: str                 # matched text, for display and for evidence quotation
```

`score` is **not** comparable across backends or across channels. Fusion consumes the ORDER only
(`fusion.rrf`). Results must be returned best-first; that ordering is the entire product of this
call.

`snippet` is not decoration. Every positive grid cell in a report needs an exact quotation with a
location, so a backend that can return the matched text saves a round trip per cell.

`publications()` returns `[(publication_id, score)]` best-first, one row per publication. The
default implementation folds `search()`; override it if your engine can aggregate internally,
because pulling every matching chunk back to fold in Python is the expensive shape.

## Registering a backend

```python
import retrieval
retrieval.lexical.register_backend(lambda retriever: TantivyBackend(...))
```

`retrieval.lexical.backend(retriever)` resolves it: the registered backend if
`available()` is true, `PostgresLexicalBackend` otherwise. It never returns `None` and never
raises, so a Tantivy index that is still building degrades to today's behaviour rather than to a
failed search.

## What the channels ask for

| Channel | fields | operator | max_terms | rank | limit |
|---|---|---|---|---|---|
| `bm25` | `()` (backend default; Postgres excludes `paragraph`) | `or` | 8 | `match_count` | `PUB_CAP` / `SEED_PUB_CAP` |
| `claim_bm25` | `("claim_own", "claim_resolved")` | `and` | 4 | `density` | `PUB_CAP` / `SEED_PUB_CAP` |

The `or` on the broad channel is not an oversight. Requiring all terms of a long
query-by-example returns nothing: measured, 18 terms AND-ed returned 0 publications and 4 terms
AND-ed returned 4. `claim_bm25` can require AND because it is a precision channel sitting beside a
broad one.

The 8-term cap on the broad channel is a measured cost control, not a quality choice: 18 terms took
33.6 s and 8 took 8.5 s, both filling the same 1,000-publication cap. **A backend that is fast
enough should be given more terms**, which is why `max_terms` is an argument and not a constant.

## The phrase channel is NOT behind this interface, and probably should be

`channel_exact` runs `phraseto_tsquery` directly rather than through a `LexicalBackend`, because
the interface has no phrase operator: `operator` is `"or"` or `"and"`, and adjacency is a third
thing. Adding it is workstream C's call, and here is the number that should decide it.

MEASURED 2026-08-22 on the live corpus, EP 3 707 092, four phrases a model produced for that
subject, each run alone on a cold cache:

```
'air extraction means'    0.33 s    12 families
'vacuum seal element'     2.80 s     4 families
'rigid base element'      3.27 s     5 families
'contact surface'        97.26 s   300 families
```

One generic two-word phrase was 94% of the channel's 103 s. The cost is the aggregation over the
whole match set, which the 1,200-row limit then truncates, so a phrase matching tens of thousands
of chunks pays for a ranking that is thrown away. `retrieval.exact.PHRASE_MAX_CHUNKS` now declines
such a phrase after a bounded probe (`EXACT_PHRASE_MAX_CHUNKS`, default 20,000), which took the
four-phrase channel from 9.17 s warm to 2.96 s and, on that subject, ALSO recovered a gold family
at rank 500 that the generic phrase's 300 families had displaced.

**A Tantivy phrase query does not have this shape**, so the guard is a `to_tsvector` cost control
and not a quality rule. If C exposes phrases, it should be a `LexicalBackend` operator
(`operator="phrase"` is the obvious spelling), and the threshold should be raised or turned off for
that backend rather than inherited from this one.

## What "available" means

`available()` gating exists because an empty result set and a genuine miss are indistinguishable to
everything downstream, and a miss is scored as a recall failure. A backend that is mid-build, out
of disk, or serving a partial index MUST return `False` rather than an empty list.
