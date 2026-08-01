"""Adaptive multi-channel retrieval cascade + RRF + family dedup + rerank (spec §6).

Separate channels, fused by reciprocal-rank fusion (never mixing raw incomparable scores):
 1 exact/phrase/proximity   2 BM25 (top ~1000)      3 dense (top ~1000)
 4 CPC/IPC hierarchy        5 citation+family graph 6 query-by-example
 7 cross-lingual DE<->EN    8 bibliographic (assignee/inventor)

Date/status filtering (spec §5) is AND-ed into every candidate query when a Subject+Mode is
given, so only citable prior art is returned. Widths kept wide even though the corpus is small
— the pilot measures recall, not speed.
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass, field
import db, embed, rerank as rr
from search_modes import Mode, citable_where
from config import SEED_CPC

RRF_K = 40             # smaller K sharpens the rank-1 advantage of a strong channel
CHUNK_FETCH = 4000     # chunks pulled before aggregating to publications
PUB_CAP = 1000         # per-channel publication cap (the spec's ~1000 width)
# Cross-encoder rerank depth. Raised 25 -> 50 because the deep full-text analysis reads the top
# 50 references, and reading a reference the cross-encoder never scored means charting whatever
# RRF happened to leave at rank 40. Measured ~40 s for 25 passages on this box, so 50 roughly
# doubles that stage; it runs once per search, in the background, and every reference it orders
# is one the agent then reads in full. Env-overridable for a slower box.
RERANK_TOP = int(os.environ.get("RERANK_TOP", "50"))
# Passages scored per cross-encoder call. Pair scores are independent, so slicing changes no
# result — only how often the UI can be told where we are.
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
    # signal. In the claim-search preset the general dense channel is absent, so claim_dense
    # still leads that mode without silently changing the normal-search fusion hierarchy.
    "claim_dense": 1.00,
    # results streamed back from the sibling federated app (federation.py). Ranked below local
    # dense (which is tuned on an in-domain corpus) but above every lexical/classification
    # channel, because federated hits arrive already multi-source-fused, reranked and
    # LLM-scored upstream — their rank order carries real information.
    "federated": 0.90,
    "crosslingual": 0.70,   # dense over a translated query
    "exact": 0.60,          # ordered phrase match — precise
    "citation": 0.55,       # family/citation-graph neighbour of a strong hit
    "qbe": 0.50,            # query-by-example (dense from a strong hit)
    "biblio": 0.30,         # assignee/inventor prior
    "bm25": 0.25,           # broad lexical
    "claim_bm25": 0.35,     # lexical match inside claims only
    "cpc": 0.15,            # very broad classification prior
}
DENSE_FLOOR = 30           # the top-N dense hits are guaranteed a floor so weak channels can
                           # never demote a strong semantic hit out of the head


def _vec(e):
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


# ---- out-of-domain de-dilution (task C) -----------------------------------------------------
# The local index covers ONLY vacuum-gripping art (8 seed CPC branches). For a query OUTSIDE that
# field (domain_detect verdict in_domain=False) the dense channel still returns its nearest
# neighbours and RRF still orders them, so the merged display set is polluted with low-relevance
# local rows that crowd out the federated/PQAI hits — the actual on-topic art for such a query.
# iptorch (pure PQAI) never has this problem. This filter reproduces that behaviour at the display
# layer: on an OOD query it floats the genuinely-relevant hits up and sinks the local noise, WITHOUT
# dropping anything (a demoted card is still present, just lower), so the downstream permutation
# invariant holds and no result is silently lost. In-domain queries are untouched — this is an
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
    """True iff `card` is a LOCAL-corpus candidate with no genuine relevance — the rows that dilute
    an out-of-domain answer. Federated/external hits (``federated_only``) are NEVER noise: they came
    from the wider search an OOD query depends on. A local card is KEPT (not noise) when it either
    covers a claimed element (``n_covers``/``covers_elements`` — a real matched element) or clears
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
        1. FEDERATED / external hits — the wider search (PQAI etc.) is the trustworthy source for a
           query outside the local corpus's field, so these lead (like iptorch, which is pure PQAI);
        2. genuinely-relevant LOCAL hits (clear the relevance floor or cover a claimed element);
        3. LOCAL NOISE (the rest) — sunk to the bottom.
    Order within each tier is preserved. Returns a NEW list that is a PERMUTATION of the input
    (nothing dropped — a demoted card is simply lower), so downstream permutation checks hold.

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


def _date_clause(subject, mode, alias="p"):
    """(where_fragment, params) restricting to citable prior art; ('', []) if no subject/mode."""
    if subject is None or mode is None:
        return "", []
    frag, params = citable_where(mode, subject, alias)
    # never return the subject's own family as prior art
    if subject.number:
        frag += f" AND ({alias}.simple_family_id IS NULL OR {alias}.simple_family_id <> "
        frag += "(SELECT simple_family_id FROM publications WHERE publication_number=%s LIMIT 1))"
        params = list(params) + [subject.number]
    return "AND " + frag, params


@dataclass
class Result:
    ranked_pubs: list           # [(publication_id, fused_score, provenance dict)]
    family_ranked: list         # [(family_key, publication_id, fused_score, provenance)]
    channel_hits: dict          # channel -> [publication_id,...] in rank order
    query: str
    # --- optional, populated by the federation bridge / domain detector -------------------
    # publication_id -> federation.FederatedHit for hits that have NO local row. A local id is
    # a bigint; an external one is the string "fed:<PUBNUM>". Renderers must look here when a
    # publication_id is not an int.
    external: dict = field(default_factory=dict)
    federation: dict = None     # federation.FederatedResult.to_dict(), when federation ran
    domain: dict = None         # domain_detect.DomainVerdict.to_dict(), when it was computed
    # True when the query looks out-of-domain and the UI should OFFER the wider federated
    # search. Federation is never run implicitly — see federation.search_two_tier.
    federation_offered: bool = False

    def channel_hits_ranked(self) -> dict:
        """channel_hits as {name: [(pid, score)]} so a Result can be re-fused. Only the rank
        ORDER matters to RRF, so a synthetic descending score is sufficient."""
        return {k: [(p, float(len(v) - i)) for i, p in enumerate(v)]
                for k, v in (self.channel_hits or {}).items()}

    def is_external(self, pid) -> bool:
        return isinstance(pid, str) and pid.startswith("fed:")


class Retriever:
    def __init__(self, family_map=None):
        self.conn = db.connect()
        self.conn.autocommit = True
        with self.conn.cursor() as c:
            c.execute("SET hnsw.ef_search = 200")
            # iterative scan (pgvector 0.8): keep scanning past ef_search until enough rows pass
            # the date/status WHERE filter -> fixes the "few results under a restrictive filter"
            # problem that otherwise starves the dense channel.
            c.execute("SET hnsw.iterative_scan = relaxed_order")
            c.execute("SET hnsw.max_scan_tuples = 12000")
        # pre-load pid -> family map once (one query) — per-pub lookups during dedup were the
        # dominant hidden cost (~1000 queries/search).
        if family_map is None:
            self._fam = {}
            with self.conn.cursor() as c:
                c.execute("SELECT id, COALESCE(NULLIF(simple_family_id,''), publication_number) k FROM publications")
                for r in c.fetchall():
                    self._fam[r["id"]] = r["k"]
        else:
            # Agent element searches run concurrently on independent PostgreSQL connections. The
            # publication->family map is several million rows and immutable during a search, so
            # share it instead of re-reading and duplicating it for every short-lived worker.
            self._fam = family_map

    def fork(self):
        """Return a search worker with its own DB connection and the shared read-only family map."""
        return Retriever(family_map=self._fam)

    def close(self):
        """Release this retriever's connection (used by bounded agent search workers)."""
        self.conn.close()

    # ---- helpers -------------------------------------------------------------------------
    def family_key(self, pid):
        return self._fam.get(pid, str(pid))

    def _pubs_from_chunks(self, sql, params, cap=PUB_CAP):
        """Run a chunk-level ranking query, aggregate to best-per-publication, cap."""
        with self.conn.cursor() as c:
            c.execute(sql, params)
            best = {}
            for r in c.fetchall():
                pid = r["publication_id"]
                if pid not in best:      # rows already ordered best-first
                    best[pid] = r["score"]
                if len(best) >= cap:
                    break
        return list(best.items())   # [(pid, score)] best-first

    # ---- channels ------------------------------------------------------------------------
    def channel_dense(self, qvec, subject=None, mode=None):
        dc, dp = _date_clause(subject, mode)
        sql = (f"SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score "
               f"FROM chunks c JOIN publications p ON p.id=c.publication_id "
               f"WHERE c.embedding IS NOT NULL {dc} "
               f"ORDER BY c.embedding <=> %s::vector LIMIT %s")
        v = _vec(qvec)
        return self._pubs_from_chunks(sql, [v, *dp, v, CHUNK_FETCH])

    def channel_claim_dense(self, qvec, subject=None, mode=None):
        """Semantic search restricted to patent claims (the claim-search product mode)."""
        dc, dp = _date_clause(subject, mode)
        sql = (f"SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score "
               f"FROM chunks c JOIN publications p ON p.id=c.publication_id "
               f"WHERE c.embedding IS NOT NULL AND c.kind IN ('claim_own','claim_resolved') {dc} "
               f"ORDER BY c.embedding <=> %s::vector LIMIT %s")
        v = _vec(qvec)
        return self._pubs_from_chunks(sql, [v, *dp, v, CHUNK_FETCH])

    def channel_bm25(self, q, subject=None, mode=None):
        # OR the query's lexemes (websearch/plainto AND every term -> a long query-by-example
        # text would match nothing). ts_rank_cd still ranks by term density. GIN-indexed.
        if not q or not q.strip():
            return []
        dc, dp = _date_clause(subject, mode)
        # OR the query's lexemes (AND would match nothing for a long query-by-example), capped to
        # the ~22 most specific (longest) lexemes, and restricted to non-paragraph chunks
        # (abstract/claims/whole carry the key terms; excluding descriptions keeps the match set
        # and the ts_rank_cd GROUP BY bounded -> fast).
        # Rank publications by the COUNT of matching non-paragraph chunks (how many of the
        # doc's claims/abstract hit the query lexemes) instead of ts_rank_cd over every match —
        # count(*) is far cheaper than density ranking, and RRF fuses by rank anyway.
        sql = (f"WITH tq AS (SELECT to_tsquery('english', NULLIF(array_to_string(ARRAY("
               f"  SELECT w FROM unnest(tsvector_to_array(to_tsvector('english', %s))) w "
               f"  ORDER BY length(w) DESC LIMIT 18), ' | '), '')) q) "
               f"SELECT c.publication_id, count(*) AS score "
               f"FROM chunks c JOIN publications p ON p.id=c.publication_id, tq "
               f"WHERE tq.q IS NOT NULL AND c.kind <> 'paragraph' AND c.tsv @@ tq.q {dc} "
               f"GROUP BY c.publication_id ORDER BY score DESC LIMIT %s")
        return self._pubs_from_chunks(sql, [q, *dp, PUB_CAP])

    def channel_claim_bm25(self, q, subject=None, mode=None):
        """Precise lexical search restricted to claims, fused with claim_dense by weighted RRF.

        Claim mode used to OR as many as 18 terms and then group every matching claim chunk.  On
        the 25M-chunk corpus, a short element such as ``base plate controller`` matched millions of
        rows and one pass took more than five minutes in production.  Claim-dense already supplies
        the broad-recall channel, so this complementary lexical channel deliberately requires the
        four most specific (longest) stemmed terms.  The GIN index can intersect those postings
        directly, keeping the pass bounded while still surfacing literal claim-language matches.
        """
        if not q or not q.strip():
            return []
        dc, dp = _date_clause(subject, mode)
        sql = (f"WITH tq AS (SELECT to_tsquery('english', NULLIF(array_to_string(ARRAY("
               f"  SELECT w FROM unnest(tsvector_to_array(to_tsvector('english', %s))) w "
               f"  ORDER BY length(w) DESC LIMIT 4), ' & '), '')) q) "
               f"SELECT c.publication_id, max(ts_rank_cd(c.tsv, tq.q)) AS score "
               f"FROM chunks c JOIN publications p ON p.id=c.publication_id, tq "
               f"WHERE tq.q IS NOT NULL AND c.kind IN ('claim_own','claim_resolved') "
               f"AND c.tsv @@ tq.q {dc} GROUP BY c.publication_id ORDER BY score DESC LIMIT %s")
        return self._pubs_from_chunks(sql, [q, *dp, PUB_CAP])

    def channel_exact(self, phrases, subject=None, mode=None):
        """Exact phrase / proximity via phraseto_tsquery (ordered adjacency)."""
        if not phrases:
            return []
        dc, dp = _date_clause(subject, mode)
        out = {}
        for ph in phrases:
            sql = (f"SELECT c.publication_id, max(ts_rank_cd(c.tsv, phraseto_tsquery('english',%s))) AS score "
                   f"FROM chunks c JOIN publications p ON p.id=c.publication_id "
                   f"WHERE c.tsv @@ phraseto_tsquery('english',%s) {dc} "
                   f"GROUP BY c.publication_id ORDER BY score DESC LIMIT 300")
            for pid, sc in self._pubs_from_chunks(sql, [ph, ph, *dp], cap=300):
                out[pid] = max(out.get(pid, 0), sc)
        return sorted(out.items(), key=lambda t: t[1], reverse=True)

    def channel_cpc(self, cpc_hints, subject=None, mode=None):
        hints = list(cpc_hints or SEED_CPC)
        dc, dp = _date_clause(subject, mode)
        like = " OR ".join(["cl.symbol LIKE %s"] * len(hints))
        params = [h + "%" for h in hints] + list(dp)
        sql = (f"SELECT p.id AS publication_id, count(*) AS score "
               f"FROM classifications cl JOIN publications p ON p.id=cl.publication_id "
               f"WHERE ({like}) {dc} GROUP BY p.id ORDER BY score DESC LIMIT %s")
        with self.conn.cursor() as c:
            c.execute(sql, params + [PUB_CAP])
            return [(r["publication_id"], r["score"]) for r in c.fetchall()]

    def channel_citation_family(self, seed_pids, subject=None, mode=None):
        """Expand strong hits along citation edges + DOCDB family (both directions)."""
        if not seed_pids:
            return []
        dc, dp = _date_clause(subject, mode)
        seeds = seed_pids[:40]
        inlist = "(" + ",".join(["%s"] * len(seeds)) + ")"
        sql = (
            f"WITH seed AS (SELECT id, publication_number, simple_family_id FROM publications WHERE id IN {inlist}), "
            f"fam AS (SELECT p.id, 2 AS w FROM publications p JOIN seed s ON p.simple_family_id=s.simple_family_id "
            f"        WHERE p.simple_family_id IS NOT NULL), "
            f"cited AS (SELECT p.id, 3 AS w FROM citations ci JOIN seed s ON ci.src_pub=s.publication_number "
            f"          JOIN publications p ON p.publication_number=ci.dst_pub), "
            f"citing AS (SELECT p.id, 1 AS w FROM citations ci JOIN seed s ON ci.dst_pub=s.publication_number "
            f"           JOIN publications p ON p.publication_number=ci.src_pub), "
            f"u AS (SELECT id, sum(w) AS score FROM (SELECT * FROM fam UNION ALL SELECT * FROM cited "
            f"      UNION ALL SELECT * FROM citing) z GROUP BY id) "
            f"SELECT u.id AS publication_id, u.score FROM u JOIN publications p ON p.id=u.id "
            f"WHERE true {dc} ORDER BY u.score DESC LIMIT %s")
        with self.conn.cursor() as c:
            c.execute(sql, list(seeds) + list(dp) + [PUB_CAP])
            return [(r["publication_id"], float(r["score"])) for r in c.fetchall()]

    def channel_qbe(self, seed_pids, subject=None, mode=None, per=1):
        """Query-by-example: dense search from the best chunks of the strongest hits."""
        if not seed_pids:
            return []
        with self.conn.cursor() as c:
            c.execute("SELECT embedding FROM chunks WHERE publication_id = ANY(%s) "
                      "AND kind IN ('whole','abstract','claim_own') AND embedding IS NOT NULL "
                      "LIMIT %s", (seed_pids[:5], per * 5))
            vecs = [r["embedding"] for r in c.fetchall()]
        agg = {}
        for v in vecs[:per]:
            for pid, sc in self.channel_dense_raw(v, subject, mode, limit=500):
                agg[pid] = max(agg.get(pid, 0), sc)
        return sorted(agg.items(), key=lambda t: t[1], reverse=True)

    def channel_dense_raw(self, vecstr, subject=None, mode=None, limit=CHUNK_FETCH):
        dc, dp = _date_clause(subject, mode)
        sql = (f"SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS score FROM chunks c "
               f"JOIN publications p ON p.id=c.publication_id WHERE c.embedding IS NOT NULL {dc} "
               f"ORDER BY c.embedding <=> %s::vector LIMIT %s")
        return self._pubs_from_chunks(sql, [vecstr, *dp, vecstr, limit])

    def channel_biblio(self, assignee_hints, subject=None, mode=None):
        if not assignee_hints:
            return []
        dc, dp = _date_clause(subject, mode)
        like = " OR ".join(["pa.normalized_name LIKE %s"] * len(assignee_hints))
        params = [f"%{a.upper()}%" for a in assignee_hints] + list(dp)
        sql = (f"SELECT p.id AS publication_id, count(*) AS score FROM parties pa "
               f"JOIN publications p ON p.id=pa.publication_id WHERE ({like}) {dc} "
               f"GROUP BY p.id ORDER BY score DESC LIMIT %s")
        with self.conn.cursor() as c:
            c.execute(sql, params + [PUB_CAP])
            return [(r["publication_id"], r["score"]) for r in c.fetchall()]

    def channel_crosslingual(self, alt_query_vecs, subject=None, mode=None):
        """Dense search from translated/alternate-language query embeddings (agent-supplied)."""
        agg = {}
        for v in alt_query_vecs:
            for pid, sc in self.channel_dense_raw(_vec(v), subject, mode, limit=800):
                agg[pid] = max(agg.get(pid, 0), sc)
        return sorted(agg.items(), key=lambda t: t[1], reverse=True)

    def query_translations(self, query):
        """Translate the query to the OTHER language (DE<->EN) and embed — cached per query.
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
            vecs.append(embed.embed_query(t[:8000], 768))
        self._xl_cache[key] = vecs
        return vecs

    # ---- fusion --------------------------------------------------------------------------
    @staticmethod
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
            w = CHANNEL_WEIGHTS.get(name, 0.5) if weighted else 1.0
            for rank, (pid, _s) in enumerate(res):
                fused[pid] = fused.get(pid, 0.0) + w / (RRF_K + rank + 1)
                prov.setdefault(pid, {})[name] = rank + 1
        # dense floor: the top-DENSE_FLOOR dense hits can't score below the DENSE_FLOOR-th
        # pure-dense contribution — protects strong semantic hits from weak-channel dilution.
        dense_name = "claim_dense" if channel_results.get("claim_dense") else "dense"
        dense = channel_results.get(dense_name)
        if weighted and dense_floor and dense:
            floor = CHANNEL_WEIGHTS[dense_name] / (RRF_K + DENSE_FLOOR)
            for rank, (pid, _s) in enumerate(dense[:DENSE_FLOOR]):
                if fused.get(pid, 0.0) < floor:
                    fused[pid] = floor
        order = sorted(fused.items(), key=lambda t: t[1], reverse=True)
        return [(pid, sc, prov[pid]) for pid, sc in order]

    # ---- external (federated) publications ------------------------------------------------
    def register_external(self, pid, family_key):
        """Register a virtual publication id (e.g. 'fed:EP1234567A1') so family_key() and
        dedup_family() treat a federated hit exactly like a local one."""
        self._fam[pid] = family_key

    def resolve_pub_numbers(self, keys):
        """Map normalised publication-number join keys to local rows.

        keys: ['US11999030B2', ...] (alphanumeric-only, upper-case — federation.join_key).
        -> {key: (publication_id, family_key)} for those present in the corpus.

        Matching is done in SQL against a normalised publication_number rather than by holding
        a 107k-entry dict in memory: this box is memory constrained and the lookup only runs on
        the federation path, so one sequential scan is the cheaper trade.
        """
        keys = [k for k in dict.fromkeys(keys) if k]
        if not keys:
            return {}
        sql = ("SELECT id, upper(regexp_replace(publication_number, '[^A-Za-z0-9]', '', 'g')) AS k, "
               "COALESCE(NULLIF(simple_family_id,''), publication_number) AS fam "
               "FROM publications "
               "WHERE upper(regexp_replace(publication_number, '[^A-Za-z0-9]', '', 'g')) = ANY(%s)")
        out = {}
        with self.conn.cursor() as c:
            c.execute(sql, (keys,))
            for r in c.fetchall():
                out[r["k"]] = (r["id"], r["fam"])
        return out

    def dedup_family(self, ranked):
        seen, out = set(), []
        for pid, sc, prov in ranked:
            fk = self.family_key(pid)
            if fk in seen:
                continue
            seen.add(fk)
            out.append((fk, pid, sc, prov))
        return out

    # ---- top-level search ----------------------------------------------------------------
    def search(self, query, subject=None, mode=None, config="hybrid",
               cpc_hints=None, assignee_hints=None, phrases=None, alt_query_vecs=None,
               do_rerank=None, topk=1000):
        mode = Mode(mode) if isinstance(mode, str) else mode
        if not query or not query.strip():          # degenerate: an empty query has no signal
            return Result(ranked_pubs=[], family_ranked=[], channel_hits={}, query=query or "")
        qvec = embed.embed_query(query[:8000], 768)
        ch = {}
        presets = {
            "keyword": ["exact", "bm25"],
            "vector": ["dense"],
            "hybrid": ["exact", "bm25", "dense", "cpc"],
            "hybrid_rerank": ["exact", "bm25", "dense", "cpc"],
            "agentic": ["dense", "cpc", "citation", "qbe", "biblio", "crosslingual"],
            "claim_agentic": ["claim_dense", "claim_bm25", "cpc", "citation", "qbe",
                              "biblio", "crosslingual"],
        }
        # Callers may pass an explicit bounded channel sequence.  Resolve that before a mapping
        # lookup: ``dict.get(config, ...)`` still hashes ``config`` before evaluating its fallback,
        # so a list raises ``TypeError: unhashable type: 'list'`` even though it is otherwise a
        # valid explicit preset.
        if isinstance(config, (list, tuple)):
            preset = list(config)
        else:
            preset = presets.get(config, ["bm25", "dense"])
        # Cross-lingual query translation is available (query_translations) and used by the agent,
        # but M5 diagnosis showed it does NOT help the DE gap and even hurts (the corpus is
        # English-dominant, so translating a German query to English promotes English distractors).
        # The DE fix that worked is embedding the enriched DE/EP/WO claims (see M5 report). Enable
        # per-request via alt_query_vecs / xlingual=True when a caller wants it.
        if getattr(self, "_force_xlingual", False) and "crosslingual" not in preset:
            preset = preset + ["crosslingual"]
        if ("crosslingual" in preset and not alt_query_vecs
                and config not in ("agentic", "claim_agentic")):
            alt_query_vecs = self.query_translations(query)

        if "dense" in preset:
            ch["dense"] = self.channel_dense(qvec, subject, mode)
        if "claim_dense" in preset:
            ch["claim_dense"] = self.channel_claim_dense(qvec, subject, mode)
        if "bm25" in preset:
            ch["bm25"] = self.channel_bm25(query, subject, mode)
        if "claim_bm25" in preset:
            ch["claim_bm25"] = self.channel_claim_bm25(query, subject, mode)
        if "exact" in preset and phrases:
            ch["exact"] = self.channel_exact(phrases, subject, mode)
        if "cpc" in preset:
            ch["cpc"] = self.channel_cpc(cpc_hints, subject, mode)
        # channels that depend on strong base hits
        base_fused = self.rrf({k: v for k, v in ch.items()})
        strong = [pid for pid, _, _ in base_fused[:40]]
        if "citation" in preset:
            ch["citation"] = self.channel_citation_family(strong, subject, mode)
        if "qbe" in preset:
            ch["qbe"] = self.channel_qbe(strong, subject, mode)
        if "biblio" in preset and assignee_hints:
            ch["biblio"] = self.channel_biblio(assignee_hints, subject, mode)
        if "crosslingual" in preset and alt_query_vecs:
            ch["crosslingual"] = self.channel_crosslingual(alt_query_vecs, subject, mode)

        fused = self.rrf(ch)
        fam = self.dedup_family(fused)

        do_rerank = (config == "hybrid_rerank") if do_rerank is None else do_rerank
        if do_rerank and fam:
            fam = self.rerank_families(query, fam, top=min(RERANK_TOP, len(fam)))
        return Result(ranked_pubs=[(p, s, pr) for _, p, s, pr in fam][:topk],
                      family_ranked=fam[:topk], channel_hits={k: [p for p, _ in v] for k, v in ch.items()},
                      query=query)

    def best_text(self, pid, query=None, external=None):
        """Best representative text for a publication. `external` maps virtual federated ids to
        FederatedHit objects — a federated hit has no local chunks, so its text comes from the
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
    mode = Mode(mode) if isinstance(mode, str) else mode
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
