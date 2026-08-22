"""Adaptive multi-channel retrieval cascade + RRF + family dedup + rerank (spec §6).

This was one 855-line `src/retrieval.py`. It is now a package, one module per channel, and this
file is the compatibility surface: EVERY public name the old module exported is re-exported here
unchanged, so `import retrieval` and `from retrieval import X` keep working for all seventeen
callers in `src/`, `eval/` and `tests/`. The split is behaviour preserving on its own commit.

    orchestrator.py   the Retriever, the presets and the pass that runs the channels
    dense.py          hot claim vectors, hot description vectors, hot abstracts, QBE's raw pass
    lexical.py        the BM25 channel and the backend seam workstream C implements
    exact.py          phrase and proximity
    cpc.py            classification, and the subject's own symbols
    citations.py      citation graph + DOCDB family expansion
    qbe.py            query by example
    global_search.py  SEAM: the 170M catalog + external APIs (workstream E)
    shard_router.py   SEAM: which domain shards to wake, and the routing distribution
    shard_manager.py  SEAM: shard lifecycle and connections (workstream E)
    family.py         family identity, family collapse, federated ids
    legal.py          the citability window pushed into every channel's SQL
    fusion.py         weighted RRF, the OOD display filter, the cross-encoder head
    base.py           connections (per thread), funnel widths, chunk -> publication aggregation

The private names `_vec`, `_date_clause` and `_rerank_progressive` are re-exported too: they are
imported by `chunk_budget.py`, `sanity_index.py` and the tests, so they are private by convention
only and the split must not break them.
"""
from __future__ import annotations

#  Modules the tests monkeypatch through the package (`retrieval.db`, `retrieval.embed`,
#  `retrieval.rr`). Keep them importable from here.
import db                                            # noqa: F401
import embed                                         # noqa: F401
import rerank as rr                                  # noqa: F401

from . import citations, cpc, dense, exact, family, fusion, global_search, legal, lexical
from . import orchestrator, qbe, shard_manager, shard_router      # noqa: F401
from .base import (CHUNK_FETCH, EF_SEARCH, MAX_SCAN_TUPLES, PUB_CAP, SEED_CHUNK_FETCH,
                   SEED_EF_SEARCH, SEED_MAX_SCAN_TUPLES, SEED_PUB_CAP, RetrieverBase, _vec)
from .cpc import CPC_HINT_MAX
from .dense import search_doc_chunks
from .fusion import (CHANNEL_WEIGHTS, DENSE_FLOOR, OOD_LOCAL_RELEVANCY_FLOOR, RERANK_CHUNK,
                     RERANK_TOP, RRF_K, _cget, _rerank_progressive, deprioritize_ood_local,
                     is_local_noise, rrf)
from .legal import _date_clause
from .lexical import (BM25_TERMS, LexicalBackend, LexicalFilters, LexicalHit,
                      PostgresLexicalBackend)
from .orchestrator import PRESETS, Result, Retriever

__all__ = [
    "Retriever", "Result", "PRESETS", "search_doc_chunks",
    "CHANNEL_WEIGHTS", "RRF_K", "DENSE_FLOOR", "RERANK_TOP", "RERANK_CHUNK",
    "CHUNK_FETCH", "PUB_CAP", "SEED_CHUNK_FETCH", "SEED_PUB_CAP",
    "EF_SEARCH", "SEED_EF_SEARCH", "MAX_SCAN_TUPLES", "SEED_MAX_SCAN_TUPLES",
    "BM25_TERMS", "CPC_HINT_MAX", "OOD_LOCAL_RELEVANCY_FLOOR",
    "is_local_noise", "deprioritize_ood_local", "rrf", "RetrieverBase",
    "LexicalBackend", "LexicalFilters", "LexicalHit", "PostgresLexicalBackend",
    "global_search", "shard_router", "shard_manager",
    "_vec", "_date_clause", "_rerank_progressive", "_cget",
    "db", "embed", "rr",
]
