"""Weighted reciprocal-rank fusion, the out-of-domain display filter and the cross-encoder head.

Channels are fused by RANK and never by raw score: a cosine, a lexeme count and a
classification-match count are not on the same scale and averaging them is meaningless.
"""
from __future__ import annotations

import os

import rerank as rr

RRF_K = 40             # smaller K sharpens the rank-1 advantage of a strong channel

# Cross-encoder rerank depth. Raised 25 -> 50 because the deep full-text analysis reads the top
# 50 references, and reading a reference the cross-encoder never scored means charting whatever
# RRF happened to leave at rank 40. Measured ~40 s for 25 passages on this box, so 50 roughly
# doubles that stage; it runs once per search, in the background, and every reference it orders
# is one the agent then reads in full. Env-overridable for a slower box.
RERANK_TOP = int(os.environ.get("RERANK_TOP", "50"))
# Passages scored per cross-encoder call. Pair scores are independent, so slicing changes no
# result, only how often the UI can be told where we are.
#
# DEFAULT OFF (0 = one call), because slicing is NOT free here. Measured on this box, 25
# passages, two interleaved trials:
#     single call    39.9 s / 43.3 s
#     chunks of 5    76.0 s / 83.2 s
#     chunks of 8    85.3 s / 83.0 s
# i.e. roughly 2x SLOWER. The model batches internally (batch_size=16), so 25 passages is ~2 full
# batches while chunks of 5 are five poorly-utilised ones. Doubling a 40 s stage to animate a
# progress bar is a bad trade, so the UI gets an elapsed-time heartbeat instead (see webapp
# _generate) and this stays available for a GPU/idle box where the throughput loss is affordable.
RERANK_CHUNK = int(os.environ.get("RERANK_CHUNK", "0"))

# Per-channel RRF weights (spec §2). Unweighted RRF let broad, noisy channels (CPC ranks 1000
# docs by classification-match count; BM25 by lexeme count) out-vote a strong #1 dense hit when
# a mediocre doc appeared in several of them. Dense semantic relevance is the dominant signal;
# classification/keyword breadth is a weak prior. Channels that are themselves dense-derived
# (crosslingual, qbe) or meaningful graph signals (citation) keep moderate weight so
# multi-strong-channel agreement can still rank ABOVE pure dense (the agent's unique-find lift).
CHANNEL_WEIGHTS = {
    "dense": 1.00,
    # Explicit claim search.  This is intentionally its own channel rather than a client-side
    # label: both semantic and lexical candidates are restricted to claim chunks, then the normal
    # citation/family/QBE expansion recovers related filings around those claim-level seeds.
    # Keep the global invariant that dense semantic retrieval is the strongest individual
    # signal. Claim focus is a BOOST, not a filter: the claim-search preset now runs the general
    # dense channel too (see `presets`), and claim_dense carries the same weight so claim-level
    # matches lead without the description text becoming unsearchable.
    "claim_dense": 1.00,
    # results streamed back from the sibling federated app (federation.py). Ranked below local
    # dense (which is tuned on an in-domain corpus) but above every lexical/classification
    # channel, because federated hits arrive already multi-source-fused, reranked and
    # LLM-scored upstream, their rank order carries real information.
    # Abstract/whole-only semantic search: the pool where a document whose entire indexed text is
    # an abstract can compete. Below dense because a summary match is weaker than a passage match,
    # above the lexical channels because it is still semantic.
    "brief_dense": 0.80,
    "federated": 0.90,
    #  The 170M-publication global catalog and the external APIs (workstream E/global_search).
    #  Same standing as the federated bridge: the hits arrive already ranked by a real engine, and
    #  they are the only source that can reach art this corpus does not hold at all.
    "global": 0.90,
    "crosslingual": 0.70,   # dense over a translated query
    "exact": 0.60,          # ordered phrase match, precise
    "citation": 0.55,       # family/citation-graph neighbour of a strong hit
    "qbe": 0.50,            # query-by-example (dense from a strong hit)
    "biblio": 0.30,         # assignee/inventor prior
    "bm25": 0.25,           # broad lexical
    "claim_bm25": 0.35,     # lexical match inside claims only
    "cpc": 0.15,            # very broad classification prior
}
DENSE_FLOOR = 30           # the top-N dense hits are guaranteed a floor so weak channels can
                           # never demote a strong semantic hit out of the head

#  A cold-shard channel is the SAME query against a different host, so it carries the SAME weight
#  as its hot counterpart: `cold:dense` is weighted exactly like `dense`. Deriving the weight
#  rather than copying the table is deliberate. Two tables drift, and the day someone retunes
#  `dense` and forgets `cold:dense` the fusion silently prefers whichever half of the corpus
#  happens to be hot, which is a ranking that depends on which VM is awake.
COLD_PREFIX = "cold:"


def channel_weight(name, weights=None):
    """The RRF weight for a channel name, resolving the `cold:` prefix to its hot counterpart."""
    w = CHANNEL_WEIGHTS if weights is None else weights
    if name in w:
        return w[name]
    if isinstance(name, str) and name.startswith(COLD_PREFIX):
        return w.get(name[len(COLD_PREFIX):], 0.5)
    return 0.5


def rrf(channel_results: dict, weighted=True, dense_floor=True):
    """Weighted reciprocal-rank fusion. channel_results: {name: [(pid, score)] best-first}.
    Each channel contributes w_channel / (K + rank + 1). A dense-hit floor guarantees the
    top-N dense results a minimum score so broad/noisy channels can't demote them below the
    head (spec §2). -> fused [(pid, score, prov)] best-first.

    `dense_floor=False` disables that floor. Required when the query is OUT OF DOMAIN and
    federated results are standing in for the local index: the floor's premise is that a
    strong local dense hit is semantically meaningful, which is exactly what stops holding
    when the corpus does not cover the field. Left on, it would pin 30 irrelevant local
    documents into the head of an out-of-domain answer."""
    fused, prov = {}, {}
    for name, res in channel_results.items():
        w = channel_weight(name) if weighted else 1.0
        for rank, (pid, _s) in enumerate(res):
            fused[pid] = fused.get(pid, 0.0) + w / (RRF_K + rank + 1)
            prov.setdefault(pid, {})[name] = rank + 1
    # dense floor: the top-DENSE_FLOOR dense hits can't score below the DENSE_FLOOR-th
    # pure-dense contribution, protects strong semantic hits from weak-channel dilution.
    dense_name = "claim_dense" if channel_results.get("claim_dense") else "dense"
    dense = channel_results.get(dense_name)
    if weighted and dense_floor and dense:
        floor = CHANNEL_WEIGHTS[dense_name] / (RRF_K + DENSE_FLOOR)
        for rank, (pid, _s) in enumerate(dense[:DENSE_FLOOR]):
            if fused.get(pid, 0.0) < floor:
                fused[pid] = floor
    order = sorted(fused.items(), key=lambda t: t[1], reverse=True)
    return [(pid, sc, prov[pid]) for pid, sc in order]


# ---- out-of-domain de-dilution (task C) -----------------------------------------------------
# The local index covers ONLY vacuum-gripping art (8 seed CPC branches). For a query OUTSIDE that
# field (domain_detect verdict in_domain=False) the dense channel still returns its nearest
# neighbours and RRF still orders them, so the merged display set is polluted with low-relevance
# local rows that crowd out the federated/PQAI hits, the actual on-topic art for such a query.
# iptorch (pure PQAI) never has this problem. This filter reproduces that behaviour at the display
# layer: on an OOD query it floats the genuinely-relevant hits up and sinks the local noise, WITHOUT
# dropping anything (a demoted card is still present, just lower), so the downstream permutation
# invariant holds and no result is silently lost. In-domain queries are untouched, this is an
# OOD-CONDITIONED filter, never a blanket disable of the local corpus.
OOD_LOCAL_RELEVANCY_FLOOR = 55   # 0-100; a LOCAL card must clear this (LLM relevancy score if
                                 # present, else cosine `relevancy`) OR cover a claimed element to
                                 # stay above the noise line for an out-of-domain query.


def _cget(c, key, default=None):
    """Read a field from a candidate that may be a dict or an object (channel-agnostic)."""
    if isinstance(c, dict):
        return c.get(key, default)
    return getattr(c, key, default)


def is_local_noise(card, *, floor: int = OOD_LOCAL_RELEVANCY_FLOOR,
                   score_key: str = "relevancy_score") -> bool:
    """True iff `card` is a LOCAL-corpus candidate with no genuine relevance, the rows that dilute
    an out-of-domain answer. Federated/external hits (``federated_only``) are NEVER noise: they came
    from the wider search an OOD query depends on. A local card is KEPT (not noise) when it either
    covers a claimed element (``n_covers``/``covers_elements``, a real matched element) or clears
    the relevance ``floor`` on its LLM relevancy score when present, else on the cosine display
    ``relevancy``. Everything else is noise. Callers apply this ONLY when the query is out of
    domain."""
    if _cget(card, "federated_only"):
        return False
    if _cget(card, "n_covers") or _cget(card, "covers_elements"):
        return False
    s = _cget(card, score_key)
    if s is None:
        s = _cget(card, "relevancy")
    try:
        s = float(s)
    except (TypeError, ValueError):
        s = 0.0
    return s < floor


def deprioritize_ood_local(cards, *, in_domain: bool,
                           floor: int = OOD_LOCAL_RELEVANCY_FLOOR,
                           score_key: str = "relevancy_score") -> list:
    """STABLE-partition `cards` into three tiers for an OUT-OF-DOMAIN query:
        1. FEDERATED / external hits, the wider search (PQAI etc.) is the trustworthy source for a
           query outside the local corpus's field, so these lead (like iptorch, which is pure PQAI);
        2. genuinely-relevant LOCAL hits (clear the relevance floor or cover a claimed element);
        3. LOCAL NOISE (the rest), sunk to the bottom.
    Order within each tier is preserved. Returns a NEW list that is a PERMUTATION of the input
    (nothing dropped, a demoted card is simply lower), so downstream permutation checks hold.

    Floating federated to the FRONT here is a display-window choice, not the final order: it ensures
    the bounded relevancy scoring and the listwise window actually cover the federated hits (they
    are the ones that matter for an OOD query) instead of spending the budget on local neighbours.
    The FINAL order is still score-driven (see rerank_listwise._relevancy_order), so a high-scoring
    local hit can still outrank a weak federated one.

    IN-DOMAIN queries are returned unchanged: the local corpus genuinely answers them, so demoting
    local rows would be a regression. This is the whole point of gating on the domain verdict."""
    cards = list(cards)
    if in_domain:
        return cards
    fed, local_keep, local_noise = [], [], []
    for c in cards:
        if _cget(c, "federated_only"):
            fed.append(c)
        elif is_local_noise(c, floor=floor, score_key=score_key):
            local_noise.append(c)
        else:
            local_keep.append(c)
    return fed + local_keep + local_noise


# ---- cross-encoder head --------------------------------------------------------------------
def _rerank_progressive(query, passages, on_progress=None, chunk=None):
    """rr.rerank over `passages`, reporting progress, returning the same (index, score) list.

    Output is identical to a single rr.rerank call: the cross-encoder scores each (query, passage)
    pair independently, so slicing is invisible to the result -- only to how often we can say
    where we are. Falls back to one call when chunking is off or there is nothing to gain.
    """
    n = len(passages)
    chunk = RERANK_CHUNK if chunk is None else chunk
    if not n:
        return []
    if chunk <= 0 or n <= chunk:
        out = rr.rerank(query, passages)
        if on_progress:
            on_progress(n, n)
        return out
    scored = []
    for start in range(0, n, chunk):
        part = passages[start:start + chunk]
        # rr.rerank returns indices LOCAL to the slice; shift them back to absolute positions.
        scored.extend((start + i, sc) for i, sc in rr.rerank(query, part))
        if on_progress:
            on_progress(min(start + chunk, n), n)
    return sorted(scored, key=lambda t: t[1], reverse=True)


class FusionMixin:
    """RRF plus the cross-encoder reordering of its head."""

    rrf = staticmethod(rrf)

    def best_text(self, pid, query=None, external=None):
        """Best representative text for a publication. `external` maps virtual federated ids to
        FederatedHit objects, a federated hit has no local chunks, so its text comes from the
        record the federated app returned. Without this guard a 'fed:...' id would be handed to
        a bigint column comparison and blow up the reranker."""
        if isinstance(pid, str) and pid.startswith("fed:"):
            h = (external or {}).get(pid)
            if h is None:
                return ""
            return ((getattr(h, "title", "") or "") + "\n" +
                    (getattr(h, "abstract", "") or "")).strip()
        with self.conn.cursor() as c:
            c.execute("SELECT text FROM chunks WHERE publication_id=%s "
                      "AND kind IN ('abstract','claim_own','whole') ORDER BY "
                      "CASE kind WHEN 'claim_own' THEN 0 WHEN 'abstract' THEN 1 ELSE 2 END LIMIT 1",
                      (pid,))
            r = c.fetchone()
            return r["text"] if r else ""

    def rerank_families(self, query, fam, top=RERANK_TOP, external=None, on_progress=None,
                        return_meta=False):
        """`on_progress(done, total)` is called as scoring advances, if given.

        The cross-encoder is by far the longest single step in a run -- measured at ~2.4-3.1 s per
        passage on this box, so ~60 s for the 25-passage head -- and it used to run as ONE opaque
        call, which is why the UI sat on "Reranking N families…" for 56 s with no change. Scores
        are computed per (query, passage) pair and are independent of the other pairs, so scoring
        in slices is mathematically identical to scoring all at once and lets us report real,
        countable progress through it.
        """
        head, tail = fam[:top], fam[top:]
        passages = [self.best_text(pid, external=external) for _, pid, _, _ in head]
        order = _rerank_progressive(query, passages, on_progress=on_progress)
        reordered = [head[i] for i, _ in order]
        # Both reranker implementations deliberately return identity order with exact 0.0 scores
        # when the model is unavailable, times out, or raises. Preserve that graceful fallback,
        # but expose whether a real model result was obtained so the end-to-end acceptance gate
        # can distinguish "the reranking stage ran" from "the reranker actually scored it".
        # Keep this metadata local to the call: reports are generated concurrently, so a module-
        # level "last status" would race and could attribute another request's outcome.
        try:
            scores = [float(score) for _, score in order]
        except (TypeError, ValueError):
            scores = []
        meta = {
            "attempted": bool(head),
            "applied": bool(head) and len(order) == len(head) and len(scores) == len(order)
                       and any(abs(score) > 1e-12 for score in scores),
            "scored": len(order),
            "requested": len(head),
            "model": "BAAI/bge-reranker-v2-m3",
        }
        # blend reranker score into tuple position; keep tail after
        result = reordered + tail
        return (result, meta) if return_meta else result
