"""The dense (pgvector) channels: hot claim vectors, hot description vectors, hot abstracts.

Three separate pools rather than one, because the pools have different populations and a single
top-K over all chunk kinds is systematically biased against short documents (see
`channel_brief_dense`). Cold-shard dense retrieval is the same SQL against a different host and
belongs to workstream E; it enters through `retrieval.shard_router`, not here.
"""
from __future__ import annotations
from config import EMBED_DIM  # noqa: E402

import re

import db
import embed

from .base import _vec
from .legal import _date_clause, as_mode


class DenseMixin:
    """Every channel whose candidate set comes from an ANN scan of `chunks.embedding`."""

    def channel_dense(self, qvec, subject=None, mode=None):
        dc, dp = _date_clause(subject, mode)
        sql = (f"SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score "
               f"FROM chunks c JOIN publications p ON p.id=c.publication_id "
               f"WHERE c.embedding IS NOT NULL {dc} "
               f"ORDER BY c.embedding <=> %s::vector LIMIT %s")
        v = _vec(qvec)
        return self._families_from_chunks(sql, [v, *dp, v, self._fetch()], self._cap())

    def channel_claim_dense(self, qvec, subject=None, mode=None):
        """Semantic search restricted to patent claims (the claim-search product mode)."""
        dc, dp = _date_clause(subject, mode)
        sql = (f"SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score "
               f"FROM chunks c JOIN publications p ON p.id=c.publication_id "
               f"WHERE c.embedding IS NOT NULL AND c.kind IN ('claim_own','claim_resolved') {dc} "
               f"ORDER BY c.embedding <=> %s::vector LIMIT %s")
        v = _vec(qvec)
        return self._families_from_chunks(sql, [v, *dp, v, self._fetch()], self._cap())

    def channel_brief_dense(self, qvec, subject=None, mode=None):
        """Semantic search restricted to the ABSTRACT and WHOLE-document chunks.

        MEASURED STRUCTURAL BIAS this exists to correct: the general dense channel takes the best
        chunk per publication out of a global top-K. A long modern patent contributes a hundred
        paragraph chunks and gets a hundred chances to be in that top-K; a document whose entire
        text in this corpus is a forty-word abstract gets one. 84% of the corpus is abstract-only,
        and the documents an examiner actually cites skew old, foreign and thin, so the bias runs
        directly against the art that matters.

        Same query, same K=9,000 chunks, measured: the all-kinds pool yields 2,330 distinct
        publications and the abstract/whole pool yields 6,109. NINE publications from a real
        examiner citation list that the all-kinds channel never returns at all appear in the
        abstract/whole pool, several inside its first 3,000.

        Weighted BELOW dense and claim_dense on purpose: a match against a whole-document summary
        is weaker evidence than a match against a specific passage. It is there to give short
        documents a pool they can compete in, not to outrank a real passage match.
        """
        dc, dp = _date_clause(subject, mode)
        sql = (f"SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score "
               f"FROM chunks c JOIN publications p ON p.id=c.publication_id "
               f"WHERE c.embedding IS NOT NULL AND c.kind IN ('abstract','whole') {dc} "
               f"ORDER BY c.embedding <=> %s::vector LIMIT %s")
        v = _vec(qvec)
        return self._families_from_chunks(sql, [v, *dp, v, self._fetch()], self._cap())

    def channel_dense_raw(self, vecstr, subject=None, mode=None, limit=None):
        limit = self._fetch() if limit is None else limit
        dc, dp = _date_clause(subject, mode)
        sql = (f"SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score FROM chunks c "
               f"JOIN publications p ON p.id=c.publication_id WHERE c.embedding IS NOT NULL {dc} "
               f"ORDER BY c.embedding <=> %s::vector LIMIT %s")
        return self._families_from_chunks(sql, [vecstr, *dp, vecstr, limit], self._cap())

    def channel_crosslingual(self, alt_query_vecs, subject=None, mode=None):
        """Dense search from translated/alternate-language query embeddings (agent-supplied)."""
        agg = {}
        for v in alt_query_vecs:
            for pid, sc in self.channel_dense_raw(_vec(v), subject, mode, limit=800):
                agg[pid] = max(agg.get(pid, 0), sc)
        pooled = sorted(agg.items(), key=lambda t: t[1], reverse=True)
        return self.collapse_pairs(pooled, self._cap())

    def query_translations(self, query):
        """Translate the query to the OTHER language (DE<->EN) and embed, cached per query.
        Makes cross-lingual first-class in every config (spec M5 §1): a German-side match on the
        enriched DE/EP/WO claims can then land via the crosslingual channel even for an English
        query, and vice-versa."""
        if not hasattr(self, "_xl_cache"):
            self._xl_cache = {}
        key = query[:400]
        if key in self._xl_cache:
            return self._xl_cache[key]
        import llm
        # detect source language crudely; translate to the other
        is_de = bool(re.search(r"\b(und|der|die|das|eine|einer|mit|zum|Vakuum|Greifer|Unterdruck)\b", query))
        tgt = "English" if is_de else "German"
        out = llm.chat_json(
            f'Translate this patent search text to concise {tgt}. Keep technical terms. '
            'Return JSON {"t":"..."}', query[:1500]) or {}
        vecs = []
        t = (out.get("t") or "").strip()
        if t:
            vecs.append(embed.embed_query(t[:8000], EMBED_DIM))
        self._xl_cache[key] = vecs
        return vecs


# ---- document-chunk multi-vector search (parallel channel 'docchunks', spec item 3b) --------
def search_doc_chunks(chunk_vecs, weights=None, fam_map=None, subject=None, mode=None,
                      per_limit=400, topk=200):
    """Pooled multi-vector dense retrieval over the query DOCUMENT's OWN chunks.

    A dropped file / patent link is chunked (ingest_input) and each strong chunk is embedded at
    768d like the corpus itself. Instead of collapsing the document to one brief vector, EVERY
    chunk vector retrieves its own dense neighbours here; results are pooled per publication
    keeping the best WEIGHTED cosine (weights bias independent-claim / abstract / whole chunks
    up), then deduped by family. This is the 'docchunks' channel that fans out in parallel with
    the local text channels, the federated APIs and image search.

    Runs in its OWN psycopg connection so it is safe to call from a worker thread CONCURRENTLY
    with the shared singleton Retriever (which owns a different connection). `fam_map` is that
    Retriever's read-only pid->family_key map (`retriever()._fam`); reading it across threads is
    safe because it is not mutated during a search. Returns [(family_key, publication_id, score)]
    best-first (score = best weighted cosine; comparable to the dense channel's cosine).
    """
    vecs = [v for v in (chunk_vecs or []) if v]
    if not vecs:
        return []
    weights = list(weights or [1.0] * len(vecs))
    if len(weights) < len(vecs):
        weights += [1.0] * (len(vecs) - len(weights))
    mode = as_mode(mode)
    dc, dp = _date_clause(subject, mode)
    sql = (f"SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score FROM chunks c "
           f"JOIN publications p ON p.id=c.publication_id WHERE c.embedding IS NOT NULL {dc} "
           f"ORDER BY c.embedding <=> %s::vector LIMIT %s")
    pooled = {}
    conn = db.connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET hnsw.ef_search = 200")
            cur.execute("SET hnsw.iterative_scan = relaxed_order")
            cur.execute("SET hnsw.max_scan_tuples = 12000")
        for v, w in zip(vecs, weights):
            vs = _vec(v)
            with conn.cursor() as cur:
                cur.execute(sql, [vs, *dp, vs, per_limit])
                for r in cur.fetchall():
                    pid = r["publication_id"]
                    sc = float(w) * float(r["score"])
                    if sc > pooled.get(pid, -1.0):
                        pooled[pid] = sc
    finally:
        conn.close()
    fam_map = fam_map or {}
    ranked = sorted(pooled.items(), key=lambda t: t[1], reverse=True)
    out, seen = [], set()
    for pid, sc in ranked:
        fk = fam_map.get(pid, str(pid))
        if fk in seen:
            continue
        seen.add(fk)
        out.append((fk, pid, sc))
        if len(out) >= topk:
            break
    return out
