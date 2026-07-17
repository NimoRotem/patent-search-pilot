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
from dataclasses import dataclass, field
import db, embed, rerank as rr
from search_modes import Mode, Subject, citable_where
from config import SEED_CPC

RRF_K = 60
CHUNK_FETCH = 4000     # chunks pulled before aggregating to publications
PUB_CAP = 1000         # per-channel publication cap (the spec's ~1000 width)
RERANK_TOP = 60        # cross-encoder rerank depth (spec says ~300; 60 keeps CPU time sane on
                       # the shared box — RRF already orders well; tune up with a GPU/idle box)


def _vec(e):
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


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


class Retriever:
    def __init__(self):
        self.conn = db.connect()
        self.conn.autocommit = True
        with self.conn.cursor() as c:
            c.execute("SET hnsw.ef_search = 200")
        self._fam = {}

    # ---- helpers -------------------------------------------------------------------------
    def family_key(self, pid):
        if pid not in self._fam:
            with self.conn.cursor() as c:
                c.execute("SELECT COALESCE(NULLIF(simple_family_id,''), publication_number) k "
                          "FROM publications WHERE id=%s", (pid,))
                row = c.fetchone()
                self._fam[pid] = row["k"] if row else str(pid)
        return self._fam[pid]

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

    def channel_bm25(self, q, subject=None, mode=None):
        # OR the query's lexemes (websearch/plainto AND every term -> a long query-by-example
        # text would match nothing). ts_rank_cd still ranks by term density. GIN-indexed.
        if not q or not q.strip():
            return []
        dc, dp = _date_clause(subject, mode)
        # OR the query's lexemes (AND would match nothing for a long query-by-example), but cap
        # to the ~40 most specific (longest) lexemes so the tsquery stays bounded and fast.
        sql = (f"WITH tq AS (SELECT to_tsquery('english', NULLIF(array_to_string(ARRAY("
               f"  SELECT w FROM unnest(tsvector_to_array(to_tsvector('english', %s))) w "
               f"  ORDER BY length(w) DESC LIMIT 40), ' | '), '')) q) "
               f"SELECT c.publication_id, max(ts_rank_cd(c.tsv, tq.q)) AS score "
               f"FROM chunks c JOIN publications p ON p.id=c.publication_id, tq "
               f"WHERE tq.q IS NOT NULL AND c.tsv @@ tq.q {dc} "
               f"GROUP BY c.publication_id ORDER BY score DESC LIMIT %s")
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

    def channel_qbe(self, seed_pids, subject=None, mode=None, per=2):
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

    # ---- fusion --------------------------------------------------------------------------
    @staticmethod
    def rrf(channel_results: dict):
        """channel_results: {name: [(pid, score)] best-first}. -> fused [(pid, score, prov)]."""
        fused = {}
        prov = {}
        for name, res in channel_results.items():
            for rank, (pid, _s) in enumerate(res):
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
                prov.setdefault(pid, {})[name] = rank + 1
        order = sorted(fused.items(), key=lambda t: t[1], reverse=True)
        return [(pid, sc, prov[pid]) for pid, sc in order]

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
        qvec = embed.embed_query(query[:8000], 768)
        ch = {}
        preset = {
            "keyword": ["exact", "bm25"],
            "vector": ["dense"],
            "hybrid": ["exact", "bm25", "dense", "cpc"],
            "hybrid_rerank": ["exact", "bm25", "dense", "cpc"],
            "agentic": ["exact", "bm25", "dense", "cpc", "citation", "qbe", "biblio", "crosslingual"],
        }.get(config, config if isinstance(config, list) else ["bm25", "dense"])

        if "dense" in preset:
            ch["dense"] = self.channel_dense(qvec, subject, mode)
        if "bm25" in preset:
            ch["bm25"] = self.channel_bm25(query, subject, mode)
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

    def best_text(self, pid, query=None):
        with self.conn.cursor() as c:
            c.execute("SELECT text FROM chunks WHERE publication_id=%s "
                      "AND kind IN ('abstract','claim_own','whole') ORDER BY "
                      "CASE kind WHEN 'claim_own' THEN 0 WHEN 'abstract' THEN 1 ELSE 2 END LIMIT 1",
                      (pid,))
            r = c.fetchone()
            return r["text"] if r else ""

    def rerank_families(self, query, fam, top=RERANK_TOP):
        head, tail = fam[:top], fam[top:]
        passages = [self.best_text(pid) for _, pid, _, _ in head]
        order = rr.rerank(query, passages)
        reordered = [head[i] for i, _ in order]
        # blend reranker score into tuple position; keep tail after
        return reordered + tail
