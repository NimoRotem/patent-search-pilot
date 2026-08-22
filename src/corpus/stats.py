"""Completeness statistics for a release, and the comparison that stops a bad one serving.

THE POINT IS THE COMPARISON, NOT THE NUMBERS. A release that holds fewer families than the one it
replaces, or that lost the description text of a tenth of its publications because a backfill was
half finished when the build started, is a recall regression that today would only be visible in a
benchmark run days later. `compare()` puts it in front of the activation instead:
`release_store.activate` calls it and refuses rather than switching.

EVERY METRIC HAS A DIRECTION. `METRICS` says which way is better and how much movement is noise.
A metric with no declared direction is ignored by the comparison, deliberately: silently guessing
that "up is good" for a metric like `bytes` would block an activation that made the index smaller.
"""
from __future__ import annotations

#  (path, direction, tolerance). `direction` is 'up' when larger is better, 'down' when smaller
#  is better. `tolerance` is the relative movement treated as noise.
METRICS = (
    ("counts.families", "up", 0.001),
    ("counts.publications", "up", 0.001),
    ("counts.chunks", "up", 0.001),
    ("coverage.publications_with_claims", "up", 0.005),
    ("coverage.publications_with_description", "up", 0.005),
    ("coverage.publications_with_abstract", "up", 0.005),
    ("coverage.families_with_any_text", "up", 0.005),
    ("coverage.chunks_with_embedding", "up", 0.0),
    ("coverage.chunks_lexically_indexed", "up", 0.0),
    ("reachability.mention_reachability", "up", 0.005),
    ("demand.requests_satisfied", "up", 0.005),
)

#  CJK is 39.9% of the corpus and `to_tsvector('english')` has no segmenter for it, so the share
#  is not degraded, it is dead. A release that lost CJK text is therefore a specific, nameable
#  regression rather than a general drop in coverage.
CJK_RE = r"[぀-ヿ㐀-䶿一-鿿가-힯]"


def _get(d, path, default=None):
    cur = d or {}
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def compute(conn, release_id, *, lexical_docs=None, deep=True):
    """Everything measurable about a built release, from the release database itself.

    `lexical_docs` is the document count the Tantivy build reported. It is passed in rather than
    read back out of the index so that a lexical index that failed to build shows up as coverage
    0.0 instead of as a missing key that the comparison then ignores.
    """
    out = {"release_id": release_id, "counts": {}, "coverage": {}, "kinds": {}, "langs": {},
           "domains": {}, "reachability": {}, "demand": {}, "bytes": {}}
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) chunks, count(DISTINCT publication_id) pubs,
                              count(DISTINCT family_key) fams,
                              COALESCE(sum(length(text)), 0) chars,
                              count(*) FILTER (WHERE embedding IS NOT NULL) emb
                       FROM chunks_release WHERE release_id=%s""", (release_id,))
        r = cur.fetchone()
        n_chunks = int(r["chunks"])
        out["counts"] = {"chunks": n_chunks, "publications": int(r["pubs"]),
                         "families": int(r["fams"]), "text_chars": int(r["chars"])}
        out["coverage"]["chunks_with_embedding"] = _frac(int(r["emb"]), n_chunks)

        cur.execute("SELECT kind, count(*) n FROM chunks_release WHERE release_id=%s GROUP BY 1",
                    (release_id,))
        out["kinds"] = {row["kind"]: int(row["n"]) for row in cur.fetchall()}

        cur.execute("SELECT COALESCE(lang,'') lang, count(*) n FROM chunks_release "
                    "WHERE release_id=%s GROUP BY 1 ORDER BY n DESC LIMIT 25", (release_id,))
        out["langs"] = {row["lang"]: int(row["n"]) for row in cur.fetchall()}

        #  Text coverage is measured per PUBLICATION, because a release with every claim of half
        #  its publications and nothing of the other half has the same chunk count as one with
        #  half the claims of all of them and is a completely different corpus.
        cur.execute("""SELECT
                 count(DISTINCT publication_id) FILTER (WHERE kind IN ('claim_own','claim_resolved')) c,
                 count(DISTINCT publication_id) FILTER (WHERE kind = 'paragraph') d,
                 count(DISTINCT publication_id) FILTER (WHERE kind = 'abstract') a,
                 count(DISTINCT family_key) f
               FROM chunks_release WHERE release_id=%s""", (release_id,))
        r = cur.fetchone()
        npub = out["counts"]["publications"]
        out["coverage"]["publications_with_claims"] = _frac(int(r["c"]), npub)
        out["coverage"]["publications_with_description"] = _frac(int(r["d"]), npub)
        out["coverage"]["publications_with_abstract"] = _frac(int(r["a"]), npub)
        out["coverage"]["families_with_any_text"] = _frac(int(r["f"]), out["counts"]["families"])

        if deep and n_chunks:
            cur.execute("SELECT count(*) n FROM chunks_release WHERE release_id=%s AND text ~ %s",
                        (release_id, CJK_RE))
            out["coverage"]["chunks_cjk"] = _frac(int(cur.fetchone()["n"]), n_chunks)

        out["coverage"]["chunks_lexically_indexed"] = (
            _frac(int(lexical_docs), n_chunks) if lexical_docs is not None else 0.0)

        cur.execute("""SELECT home_domain, count(*) fams, sum(n_chunks) chunks,
                              sum(n_publications) pubs
                       FROM corpus_release_member WHERE release_id=%s GROUP BY 1
                       ORDER BY 3 DESC NULLS LAST""", (release_id,))
        out["domains"] = {row["home_domain"]: {"families": int(row["fams"]),
                                               "chunks": int(row["chunks"] or 0),
                                               "publications": int(row["pubs"] or 0)}
                          for row in cur.fetchall()}

        #  THE COST OF KEEPING A FAMILY WHOLE, as a number. A family lives in one domain but may
        #  mention several. A query routed to a mentioned-but-not-home domain wakes a shard that
        #  does not hold this family. This is the fraction of all (family, mentioned domain) pairs
        #  that this release can actually answer for.
        cur.execute("""SELECT
                 count(*) FILTER (WHERE cardinality(secondary_domains) > 0) cross_fams,
                 count(*) fams,
                 COALESCE(sum(1 + cardinality(secondary_domains)), 0) mentions
               FROM corpus_release_member WHERE release_id=%s""", (release_id,))
        r = cur.fetchone()
        fams, mentions = int(r["fams"]), int(r["mentions"])
        held = _domains_held(cur, release_id)
        cur.execute("""SELECT COALESCE(sum(cardinality(ARRAY(
                            SELECT unnest(secondary_domains) INTERSECT SELECT unnest(%s::text[])
                       ))), 0) n
                       FROM corpus_release_member WHERE release_id=%s""", (list(held), release_id))
        secondary_held = int(cur.fetchone()["n"])
        out["reachability"] = {
            "families": fams,
            "cross_domain_families": int(r["cross_fams"]),
            "cross_domain_share": _frac(int(r["cross_fams"]), fams),
            "domain_mentions": mentions,
            "mentions_served": fams + secondary_held,
            "mention_reachability": _frac(fams + secondary_held, mentions),
        }

        out["demand"] = _demand(cur, release_id)
        out["bytes"] = _bytes(cur, release_id)
    return out


def _domains_held(cur, release_id):
    cur.execute("SELECT domain FROM corpus_release_shard WHERE release_id=%s", (release_id,))
    return [r["domain"] for r in cur.fetchall()]


def _demand(cur, release_id):
    """How much of the demand queue this release answered.

    `corpus_ingest_queue.request_count` is the demand signal: a publication four searches wanted
    is worth four times one that one search wanted. So the number that matters is the share of
    REQUESTS satisfied, not the share of rows.
    """
    out = {"requests_satisfied": 0.0, "publications_included": 0, "requests_included": 0,
           "requests_outstanding": 0}
    cur.execute("SELECT to_regclass('corpus_fetch_ledger') t")
    if cur.fetchone()["t"] is None:
        return out
    cur.execute("""SELECT count(*) n, COALESCE(sum(request_count), 0) req
                   FROM corpus_fetch_ledger WHERE release_id=%s AND state='included'""",
                (release_id,))
    r = cur.fetchone()
    out["publications_included"] = int(r["n"])
    out["requests_included"] = int(r["req"])
    cur.execute("""SELECT COALESCE(sum(request_count), 0) req FROM corpus_fetch_ledger
                   WHERE release_id='' AND state <> 'rejected'""")
    out["requests_outstanding"] = int(cur.fetchone()["req"])
    total = out["requests_included"] + out["requests_outstanding"]
    out["requests_satisfied"] = _frac(out["requests_included"], total)
    return out


def _bytes(cur, release_id):
    part = f"chunks_release_{release_id}"
    cur.execute("SELECT COALESCE(pg_relation_size(to_regclass(%s)), 0) heap, "
                "COALESCE(pg_indexes_size(to_regclass(%s)), 0) idx, "
                "COALESCE(pg_total_relation_size(to_regclass(%s)), 0) total", (part, part, part))
    r = cur.fetchone()
    return {"heap": int(r["heap"]), "index": int(r["idx"]), "total": int(r["total"])}


def _frac(n, d):
    return round(float(n) / float(d), 6) if d else 0.0


def compare(previous, new, *, metrics=METRICS):
    """-> {"ok", "regressions", "improvements", "unchanged"}.

    An empty `previous` is the first release for a shard key and has nothing to be worse than, so
    it produces no regressions. That is not a hole: the gate exists to catch a release that went
    BACKWARDS, and a first release cannot.
    """
    regressions, improvements, unchanged = [], [], []
    if not previous:
        return {"ok": True, "regressions": [], "improvements": [], "unchanged": [],
                "note": "no previous release for this shard key"}
    for path, direction, tol in metrics:
        old, cur = _get(previous, path), _get(new, path)
        if old is None or cur is None:
            continue
        try:
            old, cur = float(old), float(cur)
        except (TypeError, ValueError):
            continue
        delta = cur - old
        rel = (delta / old) if old else (1.0 if delta > 0 else 0.0)
        worse = (delta < 0 and direction == "up") or (delta > 0 and direction == "down")
        noise = abs(rel) <= float(tol)
        entry = {"metric": path, "previous": old, "new": cur, "delta": round(delta, 6),
                 "relative": round(rel, 6),
                 "detail": f"{path} {old:g} -> {cur:g} ({rel:+.2%})"}
        if worse and not noise:
            regressions.append(entry)
        elif not worse and not noise:
            improvements.append(entry)
        else:
            unchanged.append(entry)
    return {"ok": not regressions, "regressions": regressions, "improvements": improvements,
            "unchanged": unchanged}


def summary_line(s):
    c = s.get("counts") or {}
    cov = s.get("coverage") or {}
    return (f"{s.get('release_id','?')}: {c.get('families',0):,} families / "
            f"{c.get('publications',0):,} pubs / {c.get('chunks',0):,} chunks; "
            f"claims {cov.get('publications_with_claims',0):.1%}, "
            f"description {cov.get('publications_with_description',0):.1%}, "
            f"lexical {cov.get('chunks_lexically_indexed',0):.1%}, "
            f"reach {(s.get('reachability') or {}).get('mention_reachability',0):.1%}")
