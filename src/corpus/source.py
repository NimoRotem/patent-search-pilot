"""Reading the live corpus, offline and politely, and mirroring what the builder needs.

THE BUILDER MUST NOT DEPEND ON PRODUCTION. It reads the corpus once, into its own database, and
every later stage runs against the mirror. That is not tidiness: the assignment pass has to look at
every publication and every classification of every family it places, and doing that against the
serving box is the unbounded full-corpus scan the brief forbids and previous sessions have already
caused.

HOW IT READS. Batched by primary key range, never by OFFSET and never unbounded. `publications` is
~5M rows and `chunks` is 27.6M, so a key-range COPY is one index-ordered pass with a bounded working
set, while an OFFSET walk re-reads the prefix on every page and a bare SELECT materialises the lot.
`explain()` is available and `mirror_light_tables` runs it before the first batch, because a plan
that turned into a sequential scan on a serving box is worth finding in the first second rather
than the tenth minute.

WHAT IS MIRRORED. Only what placement needs: publication identity, family, tier and dates, and the
classification symbols. That is 4.7 GB plus 2.9 GB measured, against 312 GB for `chunks`. Chunks
themselves are pulled per release, for the families that release actually holds, so a domain shard
never reads the other seven domains' text.
"""
from __future__ import annotations

import os
import time

import psycopg
from psycopg.rows import dict_row

from corpus import assign

#  Builder-local staging. Not part of sql/010: nothing here is released, it is the scratch the
#  build works from and it is dropped or overwritten by the next build.
MIRROR_DDL = """
CREATE TABLE IF NOT EXISTS src_publications (
  id                 bigint PRIMARY KEY,
  publication_number text NOT NULL DEFAULT '',
  simple_family_id   text NOT NULL DEFAULT '',
  family_key         text NOT NULL DEFAULT '',
  tier               text NOT NULL DEFAULT '',
  country            text NOT NULL DEFAULT '',
  publication_date   date
);
CREATE INDEX IF NOT EXISTS ix_src_pub_family ON src_publications (family_key);

CREATE TABLE IF NOT EXISTS src_classifications (
  publication_id bigint NOT NULL,
  symbol         text   NOT NULL,
  is_first       boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS ix_src_class_pub ON src_classifications (publication_id);

CREATE TABLE IF NOT EXISTS src_family_home (
  family_key        text PRIMARY KEY,
  home_domain       text NOT NULL,
  secondary_domains text[] NOT NULL DEFAULT '{}',
  n_publications    int  NOT NULL DEFAULT 0,
  n_chunks          int  NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_src_family_home_domain ON src_family_home (home_domain);

CREATE TABLE IF NOT EXISTS src_mirror_state (
  name       text PRIMARY KEY,
  watermark  bigint NOT NULL DEFAULT 0,
  rows_done  bigint NOT NULL DEFAULT 0,
  detail     jsonb  NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""

PUB_COLS = ("id", "publication_number", "simple_family_id", "family_key", "tier", "country",
            "publication_date")


def connect_source(dsn=None, *, statement_timeout="300s"):
    """A READ ONLY connection to the live corpus. Postgres enforces it, not this module.

    `default_transaction_read_only` is a session GUC set at connect time, so it holds for every
    statement including autocommit ones, which `conn.read_only` does not: psycopg applies that on
    BEGIN and an autocommit connection never issues one.
    """
    dsn = dsn or os.environ.get("CORPUS_SOURCE_DSN") or _dsn_from_env()
    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True,
                           options="-c default_transaction_read_only=on")
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = '{statement_timeout}'")
    return conn


def _dsn_from_env():
    keys = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
    if not all(os.environ.get(k) for k in keys):
        raise RuntimeError("no source DSN: set CORPUS_SOURCE_DSN or PGHOST/PGPORT/PGDATABASE/"
                           "PGUSER/PGPASSWORD")
    return (f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} "
            f"dbname={os.environ['PGDATABASE']} user={os.environ['PGUSER']} "
            f"password={os.environ['PGPASSWORD']}")


def ensure_mirror(dst):
    with dst.cursor() as cur:
        cur.execute(MIRROR_DDL)
    dst.commit()


def explain(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute("EXPLAIN " + sql, params or ())
        return " | ".join(r["QUERY PLAN"] for r in cur.fetchall())


def _plan_is_seqscan(plan):
    return "Seq Scan" in plan and "Index" not in plan


WATERMARK_TABLES = ("publications", "chunks", "paragraphs", "claims", "chunks_stage_v3")


def source_watermarks(src):
    """What the mirror was taken from. Recorded in the manifest so a release names the corpus
    state it was built out of rather than a wall-clock time nobody can reproduce.

    `max(id)` on a bigserial primary key is an index-backed one-row read, and `reltuples` is a
    catalog lookup, so this costs nothing even on the serving box. A count(*) here would be the
    unbounded scan the brief forbids, for a number that is an estimate anyway.
    """
    out = {}
    with src.cursor() as cur:
        for tbl in WATERMARK_TABLES:
            cur.execute("SELECT to_regclass(%s) t", (tbl,))
            if cur.fetchone()["t"] is None:
                continue
            cur.execute(f"SELECT COALESCE(max(id), 0) m FROM {tbl}")
            out[f"{tbl}_max_id"] = int(cur.fetchone()["m"])
            cur.execute("SELECT reltuples::bigint est FROM pg_class WHERE relname=%s "
                        "AND relnamespace='public'::regnamespace", (tbl,))
            row = cur.fetchone()
            out[f"{tbl}_est_rows"] = int(row["est"]) if row else 0
        cur.execute("SELECT now() AT TIME ZONE 'UTC' t")
        out["read_at"] = str(cur.fetchone()["t"])
    return out


def mirror_light_tables(src, dst, *, id_lo=0, id_hi=None, batch=50_000, progress=None,
                        refresh=False):
    """Copy publication identity and classification symbols into the builder database.

    Key-range batched on `publications.id`, which is the primary key, so each batch is an index
    range scan and the batches touch disjoint pages. Resumable: the watermark is committed with
    the rows, so a killed mirror costs one batch.
    """
    ensure_mirror(dst)
    if refresh:
        with dst.cursor() as cur:
            cur.execute("TRUNCATE src_publications, src_classifications, src_family_home")
            cur.execute("DELETE FROM src_mirror_state WHERE name='publications'")
        dst.commit()

    with dst.cursor() as cur:
        cur.execute("INSERT INTO src_mirror_state (name) VALUES ('publications') "
                    "ON CONFLICT (name) DO NOTHING")
        cur.execute("SELECT watermark, rows_done FROM src_mirror_state WHERE name='publications'")
        st = cur.fetchone()
    dst.commit()
    watermark = max(int(st["watermark"]), int(id_lo))
    rows_done = int(st["rows_done"])

    pub_sql = ("SELECT id, publication_number, simple_family_id, tier, country, publication_date "
               "FROM publications WHERE id > %s" + (" AND id <= %s" if id_hi else "") +
               " ORDER BY id LIMIT %s")
    plan = explain(src, pub_sql, (watermark, id_hi, batch) if id_hi else (watermark, batch))
    if _plan_is_seqscan(plan):
        raise RuntimeError(f"refusing to mirror: the publications read is a sequential scan on a "
                           f"serving database. plan: {plan}")
    cls_sql = ("SELECT publication_id, symbol, COALESCE(is_first,false) is_first "
               "FROM classifications WHERE publication_id = ANY(%s) AND symbol IS NOT NULL")
    plan_c = explain(src, cls_sql, ([1],))
    if _plan_is_seqscan(plan_c):
        raise RuntimeError(f"refusing to mirror: the classifications read is a sequential scan. "
                           f"plan: {plan_c}")

    t0 = time.time()
    while True:
        with src.cursor() as cur:
            cur.execute(pub_sql, (watermark, id_hi, batch) if id_hi else (watermark, batch))
            pubs = cur.fetchall()
        if not pubs:
            break
        pids = [int(p["id"]) for p in pubs]
        with src.cursor() as cur:
            cur.execute(cls_sql, (pids,))
            cls = cur.fetchall()

        with dst.cursor() as cur:
            with cur.copy(f"COPY src_publications ({', '.join(PUB_COLS)}) FROM STDIN") as cp:
                for p in pubs:
                    cp.write_row((int(p["id"]), p["publication_number"] or "",
                                  p["simple_family_id"] or "",
                                  assign.family_key(p["simple_family_id"], p["publication_number"]),
                                  p["tier"] or "", p["country"] or "", p["publication_date"]))
            with cur.copy("COPY src_classifications (publication_id, symbol, is_first) "
                          "FROM STDIN") as cp:
                for c in cls:
                    cp.write_row((int(c["publication_id"]), c["symbol"], bool(c["is_first"])))
            watermark = pids[-1]
            rows_done += len(pubs)
            cur.execute("UPDATE src_mirror_state SET watermark=%s, rows_done=%s, updated_at=now(), "
                        "detail=%s WHERE name='publications'",
                        (watermark, rows_done, psycopg.types.json.Json(
                            {"classification_rows": len(cls)})))
        dst.commit()
        if progress:
            progress({"rows": rows_done, "watermark": watermark,
                      "elapsed_s": round(time.time() - t0, 1)})
        if id_hi and watermark >= id_hi:
            break
    return {"publications": rows_done, "watermark": watermark,
            "elapsed_s": round(time.time() - t0, 1)}


def assign_family_homes(dst, *, batch=20_000, progress=None):
    """Compute every family's home domain, on the MIRROR, in one streamed pass.

    Streamed and grouped by family key rather than held in a dict: 3,409,514 families is a Python
    dict this process does not need to hold, and the ordering guarantee is free because
    `ix_src_pub_family` already orders by family key.

    ONE implementation of the rule, the one in `corpus.assign`, which itself imports
    `shard_router.domain_of`. A SQL reimplementation would be faster and would be the way the
    router and the shards come to disagree about where a family lives.
    """
    ensure_mirror(dst)
    with dst.cursor() as cur:
        cur.execute("TRUNCATE src_family_home")
    dst.commit()

    #  Key-range batching on family_key, not a server side cursor: a DECLARE held open while the
    #  same connection runs COPY is a way to wedge a build, and a key range is resumable where a
    #  cursor is not.
    n_fams, last_fk = 0, ""
    t0 = time.time()
    while True:
        with dst.cursor() as cur:
            cur.execute("SELECT DISTINCT family_key FROM src_publications WHERE family_key > %s "
                        "ORDER BY family_key LIMIT %s", (last_fk, batch))
            keys = [r["family_key"] for r in cur.fetchall()]
        if not keys:
            break
        with dst.cursor() as cur:
            cur.execute("""SELECT p.family_key, p.id AS publication_id, c.symbol, c.is_first
                             FROM src_publications p
                             LEFT JOIN src_classifications c ON c.publication_id = p.id
                            WHERE p.family_key = ANY(%s)""", (keys,))
            rows = cur.fetchall()
        fams = {}
        for row in rows:
            e = fams.setdefault(row["family_key"], {}).setdefault(
                row["publication_id"], {"symbols": [], "first_symbols": []})
            if row["symbol"]:
                e["symbols"].append(row["symbol"])
                if row["is_first"]:
                    e["first_symbols"].append(row["symbol"])
        out = []
        for fk in keys:
            pubmap = fams.get(fk) or {}
            members = list(pubmap.values()) or [{"symbols": [], "first_symbols": []}]
            home, weights = assign.home_domain(members)
            out.append((fk, home, assign.secondary_domains(home, weights), len(members)))
        with dst.cursor() as cur:
            _write_homes(cur, out)
        dst.commit()
        n_fams += len(out)
        last_fk = keys[-1]
        if progress:
            progress({"families": n_fams, "elapsed_s": round(time.time() - t0, 1)})
    return {"families": n_fams, "elapsed_s": round(time.time() - t0, 1)}


def _write_homes(cur, rows):
    with cur.copy("COPY src_family_home (family_key, home_domain, secondary_domains, "
                  "n_publications) FROM STDIN") as cp:
        for fk, home, sec, n in rows:
            cp.write_row((fk, home, list(sec), int(n)))


def fill_family_chunk_counts(src, dst, *, batch=5000, progress=None):
    """How many chunks each family actually has, which is the mass the shard plan packs.

    Publication counts are the wrong unit: an unclassified biblio-only publication has an abstract
    chunk and nothing else, while a fully texted one has a measured 56.1. Packing by publications
    would put the byte-heavy families in one bin and call the plan balanced.
    """
    with dst.cursor() as cur:
        cur.execute("UPDATE src_family_home SET n_chunks = 0 WHERE n_chunks <> 0")
        cur.execute("SELECT to_regclass('chunks_stage_v3') t")
    dst.commit()
    with src.cursor() as cur:
        cur.execute("SELECT to_regclass('chunks_stage_v3') t")
        has_stage = cur.fetchone()["t"] is not None

    t0 = time.time()
    watermark, total, seen = -1, 0, 0
    while True:
        with dst.cursor() as cur:
            cur.execute("SELECT id, family_key FROM src_publications WHERE id > %s "
                        "ORDER BY id LIMIT %s", (watermark, batch))
            rows = cur.fetchall()
        if not rows:
            break
        by_pid = {int(r["id"]): r["family_key"] for r in rows}
        window = sorted(by_pid)
        counts = {}
        with src.cursor() as cur:
            cur.execute("SELECT publication_id p, count(*) n FROM chunks "
                        "WHERE publication_id = ANY(%s) GROUP BY 1", (window,))
            for r in cur.fetchall():
                fk = by_pid[int(r["p"])]
                counts[fk] = counts.get(fk, 0) + int(r["n"])
            if has_stage:
                cur.execute("SELECT publication_id p, count(*) n FROM chunks_stage_v3 "
                            "WHERE publication_id = ANY(%s) GROUP BY 1", (window,))
                for r in cur.fetchall():
                    fk = by_pid[int(r["p"])]
                    counts[fk] = counts.get(fk, 0) + int(r["n"])
        #  Accumulated, not assigned: a family's publications can straddle two id batches, and a
        #  plain SET would keep only whichever batch ran last.
        if counts:
            with dst.cursor() as cur:
                cur.executemany("UPDATE src_family_home SET n_chunks = n_chunks + %s "
                                "WHERE family_key = %s",
                                [(n, fk) for fk, n in counts.items()])
            dst.commit()
        total += sum(counts.values())
        seen += len(window)
        watermark = window[-1]
        if progress:
            progress({"publications": seen, "chunks": total,
                      "elapsed_s": round(time.time() - t0, 1)})
    return {"publications": seen, "chunks": total, "elapsed_s": round(time.time() - t0, 1)}


def domain_mass(dst):
    """{domain: {families, publications, chunks}} over the mirror. The input to the shard plan."""
    with dst.cursor() as cur:
        cur.execute("""SELECT home_domain d, count(*) fams, COALESCE(sum(n_publications),0) pubs,
                              COALESCE(sum(n_chunks),0) chunks
                       FROM src_family_home GROUP BY 1 ORDER BY 4 DESC""")
        return {r["d"]: {"families": int(r["fams"]), "publications": int(r["pubs"]),
                         "chunks": int(r["chunks"])} for r in cur.fetchall()}


#  Selection order matters when a build is capped. `family_key` is the default because it is
#  deterministic AND representative: the same mirror selects the same families every time, which
#  content addressing requires, and a capped build gets a cross-section rather than only the
#  fattest families. `mass` (n_chunks DESC) is there for a deliberately byte-weighted build.
ORDERS = {"family_key": "h.family_key", "mass": "h.n_chunks DESC, h.family_key"}


def _order(order_by):
    try:
        return ORDERS[order_by]
    except KeyError:
        raise ValueError(f"unknown order_by {order_by!r}; expected one of {sorted(ORDERS)}")


def families_in_domains(dst, domains, *, limit=None, order_by="family_key"):
    with dst.cursor() as cur:
        cur.execute("SELECT h.* FROM src_family_home h WHERE h.home_domain = ANY(%s) "
                    "ORDER BY " + _order(order_by) + (" LIMIT %s" if limit else ""),
                    (list(domains), limit) if limit else (list(domains),))
        return cur.fetchall()


def families_matching_symbols(dst, patterns, *, limit=None, order_by="family_key"):
    """The niche: families with any publication carrying one of the seed subgroups.

    Subgroup granularity, not subclass: `B65G` as a whole is 12.7% of all classification rows and
    is not the niche, while `B65G47/91` is.
    """
    with dst.cursor() as cur:
        cur.execute("""SELECT h.* FROM src_family_home h
                       WHERE EXISTS (
                           SELECT 1 FROM src_publications p
                             JOIN src_classifications c ON c.publication_id = p.id
                            WHERE p.family_key = h.family_key
                              AND replace(c.symbol,' ','') LIKE ANY(%s))
                       ORDER BY """ + _order(order_by) + (" LIMIT %s" if limit else ""),
                    (list(patterns), limit) if limit else (list(patterns),))
        return cur.fetchall()


def publication_ids_for_families(dst, family_keys):
    with dst.cursor() as cur:
        cur.execute("SELECT id, family_key, publication_number FROM src_publications "
                    "WHERE family_key = ANY(%s) ORDER BY id", (list(family_keys),))
        return cur.fetchall()


CHUNK_SELECT = """
    SELECT c.id, c.publication_id, c.kind, c.ref_id, c.coord, c.lang, c.text, c.token_count,
           c.embedding
      FROM chunks c
     WHERE c.publication_id = ANY(%s) AND c.embedding IS NOT NULL
     ORDER BY c.id
"""

STAGE_SELECT = """
    SELECT s.id, s.publication_id, s.kind, s.ref_id, s.coord, s.lang, s.text, s.token_count,
           s.embedding, s.embed_model, s.embed_dim, s.task_type, s.chunker_version
      FROM chunks_stage_v3 s
     WHERE s.publication_id = ANY(%s) AND s.embedding IS NOT NULL
     ORDER BY s.id
"""


def iter_chunks(src, publication_ids, *, batch=2000, include_stage=True, legacy_model="",
                legacy_dim=768, stats=None):
    """Chunks for these publications, live rows first and staged rows filling the gaps.

    A chunk with a NULL embedding is not carried into a release at all: `chunks_release.embedding`
    is NOT NULL, so "every chunk in a release is searchable" is a property of the schema rather
    than a hope. `stats` is a dict the caller passes in and this fills, because a generator's
    return value is unreachable to a `for` loop and a dropped count that nobody can read is the
    same as no count at all.

    `chunks` has no model, dimension or chunker column, which is exactly the defect the stage table
    fixed, so rows sourced from it are stamped with the model the corpus was actually embedded
    with, passed in rather than guessed.
    """
    pids = sorted(set(int(p) for p in publication_ids))
    seen_refs = set()
    stats = stats if stats is not None else {}
    stats.update({"live": 0, "stage": 0, "stage_duplicate": 0, "publications": len(pids)})
    for i in range(0, len(pids), batch):
        window = pids[i:i + batch]
        with src.cursor() as cur:
            cur.execute(CHUNK_SELECT, (window,))
            for r in cur.fetchall():
                if r["ref_id"] is not None:
                    seen_refs.add((r["kind"], int(r["ref_id"])))
                stats["live"] += 1
                yield {**r, "embed_model": legacy_model, "embed_dim": legacy_dim,
                       "task_type": "", "chunker_version": "", "source": "corpus"}
        if not include_stage:
            continue
        with src.cursor() as cur:
            cur.execute("SELECT to_regclass('chunks_stage_v3') t")
            if cur.fetchone()["t"] is None:
                continue
            cur.execute(STAGE_SELECT, (window,))
            for r in cur.fetchall():
                key = (r["kind"], int(r["ref_id"])) if r["ref_id"] is not None else None
                if key is not None and key in seen_refs:
                    stats["stage_duplicate"] += 1
                    continue
                if key is not None:
                    seen_refs.add(key)
                stats["stage"] += 1
                yield {**r, "source": "stage"}
