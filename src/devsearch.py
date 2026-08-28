"""Side by side: the same dense query against the hot corpus and against a cold domain shard.

WHY THIS EXISTS. `domain_03` holds 66,500 publications with their claims AND descriptions, 1.4M
passages, on a machine whose index fits in its RAM. The hot corpus holds 4,984,254 publications of
which 814,523 have parsed claims, on a machine whose 94 GB index does not fit in 62 GB. Those are
different products and the only honest way to choose between them is to ask both the same question
and read both answers.

WHAT IT DOES NOT DO. It does not register a shard backend. `shard_manager.register_backend` stays
untouched, so nothing here changes what a real search does: a live run still fans out over the hot
corpus exactly as it did before this file existed. This page opens its own connection to the shard
and runs the SQL itself. That is deliberate. Turning the cold tier on for real is a decision about
every customer's search, not a side effect of adding a test page.

THE SQL IS THE SAME ON BOTH SIDES, which is the whole point of the comparison and is also the
contract in docs/shard_and_global_seams.md section 5.4: a shard holds the hot schema so "the cold
channels are the hot SQL; there is deliberately no second implementation to adapt". If this file
ever needs a special case for the shard, the shard is built wrong.

Both sides get the same `hnsw.ef_search` and the same statement timeout, or the comparison measures
the settings rather than the corpora.
"""
from __future__ import annotations

import os
import time

import psycopg

import db
import embed
from config import EMBED_DIM

#  The shard to compare against. One host today; when there are eight this becomes a lookup in
#  ops/shards/shards.tsv rather than a second env var.
SHARD_HOST = os.environ.get("DEVSEARCH_SHARD_HOST", "10.128.0.66").strip()
SHARD_PORT = os.environ.get("DEVSEARCH_SHARD_PORT", "5432").strip()
SHARD_DB = os.environ.get("DEVSEARCH_SHARD_DB", "patents").strip()
SHARD_USER = os.environ.get("DEVSEARCH_SHARD_USER", "patents").strip()
SHARD_NAME = os.environ.get("DEVSEARCH_SHARD_NAME", "domain_03").strip()

EF_SEARCH = int(os.environ.get("DEVSEARCH_EF_SEARCH", "100"))
TIMEOUT_MS = int(os.environ.get("DEVSEARCH_TIMEOUT_MS", "60000"))

#  Ask for more rows than we show, then keep the best passage per publication: without this a
#  single verbose patent fills the page with its own paragraphs and the two sides cannot be
#  compared by eye.
FETCH_MULTIPLE = 6

SQL = """
    SELECT p.publication_number, p.title, c.kind, c.text,
           1 - (c.embedding <=> %s::vector) AS score
      FROM chunks c
      JOIN publications p ON p.id = c.publication_id
     WHERE c.embedding IS NOT NULL
     ORDER BY c.embedding <=> %s::vector
     LIMIT %s
"""


def _vec(e):
    return "[" + ",".join("%.6f" % x for x in e) + "]"


def _collapse(rows, limit):
    """Best passage per publication, order preserved.

    The two sides hand back different row types and that is not a bug to paper over on one side:
    `db.cursor()` uses a dict row factory, while the shard connection here is plain psycopg and
    yields tuples. Unpacking a dict row positionally silently iterates its KEYS, which is how this
    first failed with "could not convert string to float: 'score'". Normalise once, here.
    """
    seen, out = set(), []
    for row in rows:
        if hasattr(row, "keys"):
            pn = row["publication_number"]; title = row["title"]; kind = row["kind"]
            text = row["text"]; score = row["score"]
        else:
            pn, title, kind, text, score = row
        if pn in seen:
            continue
        seen.add(pn)
        out.append({"publication_number": pn, "title": title or "", "kind": kind or "",
                    "passage": (text or "").strip().replace("\n", " ")[:400],
                    "score": round(float(score), 4)})
        if len(out) >= limit:
            break
    return out


def _prepare(cur):
    cur.execute("SET hnsw.ef_search = %s" % EF_SEARCH)
    cur.execute("SET statement_timeout = %s" % TIMEOUT_MS)


def _run_hot(qvec, limit):
    t0 = time.time()
    with db.cursor(autocommit=True, readonly=True) as cur:
        _prepare(cur)
        cur.execute(SQL, (qvec, qvec, limit * FETCH_MULTIPLE))
        rows = cur.fetchall()
    return _collapse(rows, limit), (time.time() - t0) * 1000.0


def _run_shard(qvec, limit):
    dsn = "host=%s port=%s dbname=%s user=%s password=%s" % (
        SHARD_HOST, SHARD_PORT, SHARD_DB, SHARD_USER, os.environ.get("PGPASSWORD", ""))
    t0 = time.time()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            _prepare(cur)
            cur.execute(SQL, (qvec, qvec, limit * FETCH_MULTIPLE))
            rows = cur.fetchall()
    return _collapse(rows, limit), (time.time() - t0) * 1000.0


def shard_status():
    """What the shard says about itself, so the page never reports an empty shard as a miss.

    `shard_status.state` is 'building' from bootstrap until a load commits the flip, and section 5.5
    of the seams doc is explicit that an empty answer and a genuine miss are indistinguishable
    downstream. So say which one this is.
    """
    dsn = "host=%s port=%s dbname=%s user=%s password=%s" % (
        SHARD_HOST, SHARD_PORT, SHARD_DB, SHARD_USER, os.environ.get("PGPASSWORD", ""))
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=6) as conn:
            row = conn.execute(
                "SELECT shard, state, n_chunks, note FROM shard_status ORDER BY shard LIMIT 1"
            ).fetchone()
            pubs = conn.execute("SELECT count(*) FROM publications").fetchone()[0]
        return {"reachable": True, "shard": row[0], "state": row[1], "chunks": row[2],
                "note": row[3] or "", "publications": pubs}
    except Exception as exc:
        return {"reachable": False, "error": str(exc).split("\n")[0][:200],
                "shard": SHARD_NAME, "state": "unknown", "chunks": 0, "publications": 0}


def compare(query: str, limit: int = 10) -> dict:
    """Embed once, ask both, time both. Never raises: a side that fails reports its error."""
    out = {"query": query, "limit": limit, "ef_search": EF_SEARCH,
           "shard_name": SHARD_NAME, "shard_host": SHARD_HOST,
           "hot": {"rows": [], "ms": None, "error": ""},
           "shard": {"rows": [], "ms": None, "error": ""}}
    if not (query or "").strip():
        return out

    t0 = time.time()
    qvec = _vec(embed.embed_query(query, EMBED_DIM))
    out["embed_ms"] = round((time.time() - t0) * 1000.0, 1)

    for key, fn in (("shard", _run_shard), ("hot", _run_hot)):
        try:
            rows, ms = fn(qvec, limit)
            out[key]["rows"] = rows
            out[key]["ms"] = round(ms, 1)
        except Exception as exc:
            out[key]["error"] = str(exc).split("\n")[0][:300]
    return out
