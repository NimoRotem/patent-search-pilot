"""Weekly incremental corpus refresh.

WHY THIS EXISTS
---------------
The pilot corpus was a frozen snapshot. For a NOVELTY search tool a frozen corpus is not a
staleness annoyance, it is a *correctness bug*: the tool reports "no prior art found" for art
that was simply never ingested. This job keeps the corpus moving forward.

PIPELINE (each stage is idempotent and independently resumable)
--------------------------------------------------------------
  0. lock         flock on data/incremental.lock so two runs can never overlap
  1. watermark    max(publication_date) actually present in Postgres  (never hardcoded)
  2. probe        cheap BigQuery freshness check (~2 GB). If BQ has nothing newer -> exit.
  3. count        cheap-ish seed-CPC row count in the window (~18 GB)
  4. guard        dry-run the full extract; refuse if over the configured GB ceiling
  5. extract      core-shaped delta staging table in BigQuery
  6. load         ingest_pg.load_table(delta, tier="core") -- the EXISTING loader, unchanged
  7. chunk        chunk only publications that have no chunks yet
  8. embed        embed.run() over WHERE embedding IS NULL, throttled, in bounded batches
  9. analyze      ANALYZE the touched tables

BIGQUERY COST MODEL (measured, not assumed)
-------------------------------------------
patents-public-data.patents.publications is 3.1 TB / 170 M rows and is NOT partitioned and
NOT clustered. BigQuery therefore bills the full referenced columns no matter how narrow the
WHERE clause is. Measured dry runs:

    freshness probe (publication_date, country_code)           2.0 GB   $0.01
    delta id/count probe (+ cpc)                             17.7 GB   $0.11
    delta extract WITHOUT description_localized             368.4 GB   $2.30
    delta extract WITH description_localized              1,495.7 GB   $9.35

Two consequences drive the design:
  * A wider date window is FREE. So the safety-lookback can be generous without cost.
  * The expensive extract must be gated behind the cheap probe. A week where BigQuery has
    published nothing costs $0.01, not $9.35.

UPSTREAM FRESHNESS CEILING (important)
--------------------------------------
`patents-public-data.patents.publications` is itself refreshed only every few months
(last modified 2026-04-26, max publication_date 2026-04-21 at the time of writing, and the
dataset keeps dated snapshots publications_YYYYMM at a similar cadence). So this job cannot
make the corpus fresher than BigQuery is. It closes the gap between "frozen in July" and
"as fresh as BigQuery allows", and it will pick up each quarterly BigQuery refresh
automatically within a week. Closing the remaining ~3-month BigQuery lag requires a
different upstream (EPO OPS / SerpApi front-file), which is out of scope for this module.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
import time
from pathlib import Path

import bqclient
import chunker
import db
import embed
import ingest_bq
import ingest_pg
from config import DATA

# --- tunables (env-overridable so the cron entry can tighten them) -----------------------
LOOKBACK_DAYS = int(os.environ.get("DELTA_LOOKBACK_DAYS", "90"))
MAX_EXTRACT_GB = float(os.environ.get("DELTA_MAX_GB", "1800"))
MAX_PROBE_GB = float(os.environ.get("DELTA_PROBE_MAX_GB", "50"))
EMBED_BATCH = int(os.environ.get("DELTA_EMBED_BATCH", "4000"))
EMBED_THROTTLE_S = float(os.environ.get("DELTA_EMBED_THROTTLE_S", "2.0"))
CHUNK_BATCH = int(os.environ.get("DELTA_CHUNK_BATCH", "500"))

LOCKFILE = DATA / "incremental.lock"
LOGFILE = DATA / "incremental_ingest.log"
STATEFILE = DATA / "incremental_state.json"

SOURCE_NAME = "bigquery:patents-public-data"


# ---------------------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------------------
class Log:
    """Timestamped logger that writes to data/incremental_ingest.log and/or stdout.

    `to_file` defaults OFF when DELTA_LOG_TO_FILE=0, which is what ops/refresh_corpus.sh
    sets: under cron the wrapper already redirects stdout+stderr into the log file, so
    writing there directly as well would duplicate every line. Interactive runs (no env
    var) keep both, so an ad-hoc run still lands in the log.
    """

    def __init__(self, path=LOGFILE, echo=True, to_file=None):
        self.path = Path(path)
        if to_file is None:
            to_file = os.environ.get("DELTA_LOG_TO_FILE", "1") != "0"
        self.to_file = to_file
        self.echo = echo
        if self.to_file:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {msg}"
        if self.to_file:
            with open(self.path, "a") as fh:
                fh.write(line + "\n")
        if self.echo:
            print(line, flush=True)


# ---------------------------------------------------------------------------------------
# 0. lock
# ---------------------------------------------------------------------------------------
class Lock:
    """Non-blocking exclusive flock. Guarantees two cron runs never overlap.

    flock is released automatically if the process dies, so a crashed run does not wedge
    the schedule (which is what a plain PID/lock *file* would do).
    """

    def __init__(self, path=LOCKFILE):
        self.path, self.fh = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            raise RuntimeError(f"another incremental ingest holds {self.path}; refusing to run")
        self.fh.write(f"{os.getpid()} {dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()


# ---------------------------------------------------------------------------------------
# 1. watermark
# ---------------------------------------------------------------------------------------
def db_watermark():
    """Max publication_date actually present in Postgres. Never hardcoded.

    Returns None on an empty corpus (caller then requires an explicit --since).
    """
    return db.scalar("SELECT max(publication_date) FROM publications")


def effective_since(watermark, lookback_days=LOOKBACK_DAYS, override=None):
    """Start of the extraction window.

    SAFETY LOOKBACK: the watermark is max(publication_date) already loaded, but rows can
    arrive in BigQuery *behind* that watermark -- an EP or WO document published before our
    newest US document can be added to the dataset weeks later, and corrections/re-loads
    backfill older rows too. A strict `> watermark` filter would skip those permanently,
    which for a novelty tool means silently missing prior art forever.

    We therefore rewind by `lookback_days`. This is cheap for two independent reasons:
      * BigQuery bills whole columns on this unpartitioned table, so a wider window scans
        exactly the same bytes as a narrow one (measured: identical dry-run estimates).
      * The Postgres load is idempotent (ON CONFLICT on the natural key), so re-seeing rows
        we already have is a no-op; only genuinely new publications get chunked and embedded.
    """
    if override:
        return override
    if watermark is None:
        raise ValueError("empty corpus and no --since given; refusing to guess a start date")
    return watermark - dt.timedelta(days=lookback_days)


# ---------------------------------------------------------------------------------------
# 2/3. BigQuery probes
# ---------------------------------------------------------------------------------------
def bq_freshness(log=print):
    """Cheapest possible check: what is the newest publication BigQuery holds for our
    jurisdictions? ~2 GB. Returns a date or None."""
    rows, est, billed = bqclient.run_guarded(
        ingest_bq.FRESHNESS_SQL, MAX_PROBE_GB, "freshness-probe", log=log)
    raw = rows[0]["max_pub_date"] if rows else None
    if not raw:
        return None, billed
    return dt.datetime.strptime(str(int(raw)), "%Y%m%d").date(), billed


def bq_delta_count(since, until=None, log=print):
    """How many seed-CPC publications sit in the window? ~18 GB."""
    sql = ingest_bq.delta_count_sql(since, until)
    rows, est, billed = bqclient.run_guarded(sql, MAX_PROBE_GB, "delta-count-probe", log=log)
    return (rows[0] if rows else {"n": 0}), billed


# ---------------------------------------------------------------------------------------
# 7. chunking (new publications only)
# ---------------------------------------------------------------------------------------
# chunker.run() is a whole-corpus batch job that refuses to run once chunks exist, so it
# cannot be reused directly for a delta. We rebuild the SAME rows for a bounded set of
# publication ids, reusing chunker's own _clip/_tok/MAX_CHARS so the text is byte-identical
# to the existing corpus. tests/test_incremental_ingest.py::test_chunk_parity_with_corpus
# asserts this against publications that the original chunker already processed -- if the
# two ever drift, that test fails.

# Publications that have no chunks AND have some text to chunk. The second condition is not
# cosmetic: 210 publications in the live corpus (old DE-*-C documents) carry bibliographic
# data only -- BigQuery has no title, abstract, claims or description for them, so they are
# permanently unchunkable. Without the text predicate they would be re-queued, re-scanned and
# re-attempted on every weekly run forever, and the "N publications need chunking" log line
# would never reach zero, masking real backlog.
_UNCHUNKED_SQL = """
SELECT p.id FROM publications p
WHERE p.id > %s
  AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.publication_id = p.id)
  AND (coalesce(p.title,'') <> ''
       OR coalesce(p.abstract,'') <> ''
       OR EXISTS (SELECT 1 FROM claims cl WHERE cl.publication_id = p.id)
       OR EXISTS (SELECT 1 FROM paragraphs pa WHERE pa.publication_id = p.id)
       OR EXISTS (SELECT 1 FROM figures fg WHERE fg.publication_id = p.id
                                             AND coalesce(fg.caption,'') <> ''))
ORDER BY p.id
"""


def unchunked_publication_ids(min_pub_id=0, limit=None):
    """The resumable/idempotent work queue: publications that still need chunking.

    If the job dies after loading but before chunking, the next run picks up exactly the
    publications that still need work -- no bookkeeping table required, the absence of
    chunks IS the state.
    """
    q = _UNCHUNKED_SQL
    params = [min_pub_id]
    if limit:
        q += " LIMIT %s"
        params.append(limit)
    with db.cursor() as cur:
        cur.execute(q, params)
        return [r["id"] for r in cur.fetchall()]


def textless_publication_count():
    """Publications with no chunks *because they have no text* (BigQuery biblio-only rows).
    Reported for visibility -- this is the known DE/EP text-depth gap, not a job failure."""
    return db.scalar("""
        SELECT count(*) FROM publications p
        WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.publication_id = p.id)
          AND coalesce(p.title,'') = '' AND coalesce(p.abstract,'') = ''
          AND NOT EXISTS (SELECT 1 FROM claims cl WHERE cl.publication_id = p.id)
          AND NOT EXISTS (SELECT 1 FROM paragraphs pa WHERE pa.publication_id = p.id)""") or 0


def _orig_abstracts_from_staging(staging_tbl, log=print):
    """{publication_number: (text, lang)} for non-English originals, read from the DELTA
    staging table only (small + cheap), mirroring chunker.load_orig_abstracts()."""
    sql = (f"SELECT publication_number, abstract_orig.text t, abstract_orig.lang l "
           f"FROM `{staging_tbl}` WHERE abstract_orig.text IS NOT NULL AND abstract_orig.lang != 'en'")
    out = {}
    try:
        for r in bqclient.run(sql, max_gb_billed=10):
            if r["t"]:
                out[r["publication_number"]] = (r["t"], r["l"])
    except Exception as e:
        log(f"[chunk] WARN could not read original-language abstracts ({e}); continuing EN-only")
    return out


#  Chunk kinds omitted under two-tier depth. Description paragraphs are 62% of all chunks and
#  figure captions ride with them, so skipping both is a ~71% cut in chunk count, embedding spend
#  and index RAM.
#
#  MEASURED COST OF DOING THIS (frozen 11-query gold set, dense channel, everything else
#  identical): recall@100 0.1658 full depth -> 0.1619 without paragraphs -> 0.1619 without
#  paragraphs and captions. So 71% of the corpus budget buys 2.4% of relative recall.
#
#  What it DOES cost is evidence, not retrieval. Paragraphs are what the claim chart quotes and
#  what a reader checks a rationale against — the OPS backfill moved recall by zero but did raise
#  lenient p@10 0.54 -> 0.594 and cut whole-document-only chart cells. So the design is: index
#  shallow, and fetch description on demand for the documents that actually surface (the display
#  path already does exactly this via lemad-Mongo / EPO OPS).
TWO_TIER_SKIP_KINDS = ("paragraph", "figure_caption")


def build_chunk_rows(cur, pub_ids, orig=None, two_tier=False):
    """Assemble chunk rows for `pub_ids`. Mirrors chunker.run()'s per-publication logic.

    two_tier=True indexes abstract + claims + whole only (see TWO_TIER_SKIP_KINDS).

    Returns list of (publication_id, kind, ref_id, coord_json, lang, text, token_count).
    """
    orig = orig or {}
    if not pub_ids:
        return []
    inlist = "(" + ",".join(["%s"] * len(pub_ids)) + ")"
    cur.execute("SELECT id, publication_number, title, abstract, abstract_lang, tier "
                f"FROM publications WHERE id IN {inlist}", pub_ids)
    pubs = cur.fetchall()
    if not pubs:
        return []

    cur.execute(f"SELECT id, publication_id, claim_no, is_independent, lang, text, resolved_text "
                f"FROM claims WHERE publication_id IN {inlist} ORDER BY publication_id, claim_no",
                pub_ids)
    claims_by = {}
    for c in cur.fetchall():
        claims_by.setdefault(c["publication_id"], []).append(c)
    paras_by, figs_by = {}, {}
    #  Under two-tier the paragraph/figure rows are not even SELECTed. Skipping them here rather
    #  than filtering the assembled rows later is what keeps the ingest fast on a wide corpus:
    #  description is by far the largest table to read.
    if not two_tier:
        cur.execute(f"SELECT id, publication_id, para_no, heading, page_no, lang, text "
                    f"FROM paragraphs WHERE publication_id IN {inlist}", pub_ids)
        for p in cur.fetchall():
            paras_by.setdefault(p["publication_id"], []).append(p)
        cur.execute(f"SELECT id, publication_id, figure_no, caption FROM figures "
                    f"WHERE publication_id IN {inlist}", pub_ids)
        for f in cur.fetchall():
            figs_by.setdefault(f["publication_id"], []).append(f)

    _clip, _tok = chunker._clip, chunker._tok
    rows = []
    for p in pubs:
        pid, num = p["id"], p["publication_number"]
        title, abstract = p["title"], p["abstract"]
        whole = _clip((title or "") + (". " + abstract if abstract else ""), 2000)
        if whole:
            rows.append((pid, "whole", None, None, "en", whole, _tok(whole)))
        if abstract:
            a = _clip(abstract)
            rows.append((pid, "abstract", None, None, "en", a, _tok(a)))
        if num in orig:
            otext, olang = orig[num]
            if otext and otext.strip() != (abstract or "").strip():
                oc = _clip(otext)
                rows.append((pid, "abstract", None, json.dumps({"lang": olang}), olang, oc, _tok(oc)))
        for c in claims_by.get(pid, []):
            coord = json.dumps({"claim_no": c["claim_no"]})
            own = _clip(c["text"])
            if own:
                rows.append((pid, "claim_own", c["id"], coord, c["lang"] or "en", own, _tok(own)))
            res = _clip(c["resolved_text"])
            if res and res != own:      # dependent claims only (else duplicate)
                rows.append((pid, "claim_resolved", c["id"], coord, c["lang"] or "en", res, _tok(res)))
        for pa in paras_by.get(pid, []):
            coord = json.dumps({"para_no": pa["para_no"], "heading": pa["heading"],
                                "page_no": pa["page_no"]})
            t = _clip(pa["text"])
            if t:
                rows.append((pid, "paragraph", pa["id"], coord, pa["lang"] or "en", t, _tok(t)))
        for fg in figs_by.get(pid, []):
            if fg["caption"]:
                coord = json.dumps({"figure_no": fg["figure_no"]})
                t = _clip(fg["caption"])
                rows.append((pid, "figure_caption", fg["id"], coord, "en", t, _tok(t)))
    return rows


def chunk_publications(pub_ids, orig=None, log=print, batch=CHUNK_BATCH, two_tier=False):
    """COPY chunk rows for the given publications, in bounded batches.

    Chunks are inserted with embedding NULL. That is deliberate and is what keeps this safe
    on a RAM-constrained box: pgvector's HNSW index does not index NULL vectors, so these
    inserts do ZERO work against the 6 GB ix_chunks_hnsw. All HNSW graph maintenance is
    deferred to the embedding UPDATE in embed_pending(), which is naturally rate-limited by
    the Vertex round-trips. (Measured on this box: 20k NULL inserts left the HNSW index at
    16 kB; the same 20k rows UPDATEd with real vectors grew it by ~6 MB.)
    """
    total = 0
    conn = db.connect()
    cur = conn.cursor()
    try:
        for i in range(0, len(pub_ids), batch):
            sub = pub_ids[i:i + batch]
            rows = build_chunk_rows(cur, sub, orig, two_tier=two_tier)
            if rows:
                with cur.copy("COPY chunks (publication_id, kind, ref_id, coord, lang, text, "
                              "token_count) FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
                conn.commit()
                total += len(rows)
            log(f"[chunk] {min(i+batch, len(pub_ids)):,}/{len(pub_ids):,} pubs -> {total:,} chunks")
    finally:
        cur.close()
        conn.close()
    return total


# ---------------------------------------------------------------------------------------
# 8. embedding
# ---------------------------------------------------------------------------------------
def pending_embeddings():
    return db.scalar("SELECT count(*) FROM chunks WHERE embedding IS NULL") or 0


def embed_pending(log=print, batch=EMBED_BATCH, throttle=EMBED_THROTTLE_S, max_rounds=1000):
    """Embed everything still NULL, in bounded rounds with a pause between them.

    Uses embed.run() unchanged, so the model/dimension/task_type are by construction the
    same as the existing corpus (Vertex gemini-embedding-001, 768-d via
    output_dimensionality, task_type=RETRIEVAL_DOCUMENT for corpus text). Reimplementing
    this would risk silently poisoning the index with mismatched vectors.

    The pause matters on this box: each embedded row is one insert into a 6 GB HNSW graph
    with only ~1 GB shared_buffers, so it is random-I/O heavy. Throttling keeps the live app
    responsive instead of monopolising page cache.
    """
    start = pending_embeddings()
    log(f"[embed] {start:,} chunks pending")
    rounds = 0
    while pending_embeddings() > 0 and rounds < max_rounds:
        embed.run(limit=batch)
        rounds += 1
        if pending_embeddings() > 0:
            time.sleep(throttle)
    done = start - pending_embeddings()
    log(f"[embed] embedded {done:,} chunks in {rounds} round(s)")
    return done


# L2 norm as distance from the origin -- unambiguous, unlike l2_norm() which has several
# overloads (vector/halfvec/sparsevec) in this install and errors with "not unique".
_NORM_SQL = """
SELECT vector_dims(embedding) AS dims,
       avg(embedding <-> array_fill(0::real, ARRAY[768])::vector)::float8 AS avg_norm,
       count(*) AS n
FROM (SELECT embedding FROM chunks WHERE embedding IS NOT NULL ORDER BY id {order} LIMIT %s) t
GROUP BY 1
"""

NORM_DRIFT_TOLERANCE = 0.25


def verify_embeddings(log=print, sample=200, tolerance=NORM_DRIFT_TOLERANCE):
    """Guard against a silently mis-configured embedder.

    A wrong output_dimensionality fails loudly (the column is vector(768)), but a wrong
    MODEL or TASK_TYPE yields same-shape vectors with a different magnitude profile, which
    would degrade retrieval invisibly -- exactly the "silently poison the index" failure.

    gemini-embedding-001 @768d is not unit-normalised (only 3072-d is), and the established
    corpus sits tightly around ||v|| ~= 0.58. Comparing the newest vectors' mean norm to the
    oldest therefore fingerprints the model+task_type combination.
    """
    with db.cursor() as cur:
        cur.execute(_NORM_SQL.format(order="DESC"), (sample,))
        newest = cur.fetchall()
        cur.execute(_NORM_SQL.format(order="ASC"), (sample,))
        oldest = cur.fetchall()
    if not newest or not oldest:
        log("[verify] no embedded chunks to compare")
        return {"newest": newest, "oldest": oldest, "ok": True}

    nd, od = newest[0]["dims"], oldest[0]["dims"]
    nn, on = newest[0]["avg_norm"], oldest[0]["avg_norm"]
    log(f"[verify] newest {sample}: dims={nd} mean_norm={nn:.4f} | "
        f"corpus baseline: dims={od} mean_norm={on:.4f}")
    if nd != od:
        raise RuntimeError(f"embedding dimension drift: newest={nd} vs corpus={od}")
    drift = abs(nn - on) / on if on else 0.0
    if drift > tolerance:
        raise RuntimeError(
            f"embedding norm drift {drift:.1%} (newest {nn:.4f} vs corpus {on:.4f}) exceeds "
            f"{tolerance:.0%} -- wrong model or task_type? Refusing to certify this batch.")
    return {"newest": newest, "oldest": oldest, "drift": drift, "ok": True}


# ---------------------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------------------
def write_state(d):
    STATEFILE.parent.mkdir(parents=True, exist_ok=True)
    prev = read_state()
    prev.update(d)
    STATEFILE.write_text(json.dumps(prev, indent=2, default=str))


def read_state():
    try:
        return json.loads(STATEFILE.read_text())
    except Exception:
        return {}


# ---------------------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------------------
def run(since=None, until=None, lookback_days=LOOKBACK_DAYS, max_gb=MAX_EXTRACT_GB,
        dry_run=False, force=False, skip_embed=False, max_pubs=None, log=None):
    log = log or Log()
    t0 = time.time()
    billed_total = 0.0
    log("=" * 78)
    log(f"[run] incremental ingest start dry_run={dry_run} lookback={lookback_days}d "
        f"max_gb={max_gb} force={force}")

    # 1. watermark
    wm = db_watermark()
    log(f"[watermark] corpus max(publication_date) = {wm}")
    since_eff = effective_since(wm, lookback_days, override=since)
    log(f"[window] since={since_eff} until={until or 'open'} "
        f"(safety lookback {lookback_days}d behind watermark)")

    # 2. freshness probe
    bq_max, billed = bq_freshness(log=log)
    billed_total += billed
    log(f"[probe] BigQuery max(publication_date) = {bq_max}")
    if bq_max is None:
        log("[probe] BigQuery returned no max date; aborting")
        return {"status": "no_bq_data", "billed_gb": billed_total}
    if wm is not None and bq_max <= wm and not force:
        log(f"[probe] BigQuery ({bq_max}) is not ahead of the corpus ({wm}). "
            f"Nothing to ingest. Cost this run: {billed_total:.2f} GB "
            f"(~${bqclient.usd(billed_total):.2f}).")
        write_state({"last_run": dt.datetime.now(dt.timezone.utc).isoformat(),
                     "status": "up_to_date", "watermark": str(wm), "bq_max": str(bq_max),
                     "billed_gb": round(billed_total, 2)})
        return {"status": "up_to_date", "watermark": wm, "bq_max": bq_max,
                "billed_gb": billed_total}

    # 3. count probe
    cnt, billed = bq_delta_count(since_eff, until, log=log)
    billed_total += billed
    log(f"[probe] {cnt.get('n', 0):,} seed-CPC publications in window "
        f"({cnt.get('mn')} .. {cnt.get('mx')})")
    if not cnt.get("n"):
        log("[probe] window is empty; nothing to do")
        return {"status": "empty_window", "billed_gb": billed_total}

    # 4/5. guarded extract
    sql = ingest_bq.delta_extract_sql(since_eff, until)
    if dry_run:
        est = bqclient.dry_run_gb(sql)
        log(f"[dry-run] delta extract would scan {est:,.1f} GB (~${bqclient.usd(est):,.2f}); "
            f"ceiling {max_gb:,.1f} GB -> "
            f"{'WOULD REFUSE' if est > max_gb else 'would proceed'}")
        log(f"[dry-run] would extract <= {cnt['n']:,} publications, load them into Postgres, "
            f"chunk the new ones and embed. No changes made.")
        log(f"[dry-run] probe cost actually incurred: {billed_total:.2f} GB "
            f"(~${bqclient.usd(billed_total):.2f})")
        return {"status": "dry_run", "estimate_gb": est, "candidates": cnt["n"],
                "billed_gb": billed_total, "would_refuse": est > max_gb}

    bqclient.ensure_dataset()
    n_staged, est, billed = bqclient.run_ddl_guarded(
        sql, ingest_bq.DELTA_TBL, max_gb, "delta-extract", log=log)
    billed_total += billed

    # 6. load through the EXISTING loader (idempotent on (publication_number, kind_code))
    pubs_before = db.scalar("SELECT count(*) FROM publications")
    max_pub_before = db.scalar("SELECT COALESCE(max(id),0) FROM publications")
    src_id = db.get_source_id(SOURCE_NAME, f"delta-{dt.date.today():%Y-%m-%d}")
    log(f"[load] loading {n_staged:,} staged rows (source_id={src_id})")
    ingest_pg.load_table(ingest_bq.DELTA_TBL, "core", src_id, limit=max_pubs)
    pubs_after = db.scalar("SELECT count(*) FROM publications")
    new_pubs = pubs_after - pubs_before
    log(f"[load] publications {pubs_before:,} -> {pubs_after:,} (+{new_pubs:,})")

    # 7. chunk only what is unchunked
    todo = unchunked_publication_ids(min_pub_id=0)
    log(f"[chunk] {len(todo):,} publications need chunking "
        f"({textless_publication_count():,} others are biblio-only in BigQuery and have no "
        f"text to chunk -- expected, not a failure)")
    n_chunks = 0
    if todo:
        orig = _orig_abstracts_from_staging(ingest_bq.DELTA_TBL, log=log)
        n_chunks = chunk_publications(todo, orig, log=log)
    log(f"[chunk] inserted {n_chunks:,} chunks (embedding NULL -> no HNSW work yet)")

    # 8. embed
    n_emb = 0
    if skip_embed:
        log(f"[embed] skipped by flag; {pending_embeddings():,} chunks left unembedded")
    else:
        n_emb = embed_pending(log=log)
        try:
            verify_embeddings(log=log)
        except Exception as e:
            log(f"[verify] FAILED: {e}")
            raise

    # 9. analyze
    with db.cursor(autocommit=True) as cur:
        cur.execute("ANALYZE chunks")
        cur.execute("ANALYZE publications")
    log("[analyze] done")

    wm_after = db_watermark()
    result = {
        "status": "ok",
        "watermark_before": str(wm), "watermark_after": str(wm_after),
        "bq_max": str(bq_max),
        "staged_rows": n_staged, "new_publications": new_pubs,
        "new_chunks": n_chunks, "embedded": n_emb,
        "billed_gb": round(billed_total, 2),
        "billed_usd": round(bqclient.usd(billed_total), 2),
        "seconds": round(time.time() - t0, 1),
    }
    write_state({"last_run": dt.datetime.now(dt.timezone.utc).isoformat(), **result})
    log(f"[done] {json.dumps(result)}")
    return result


def _parse_date(s):
    return dt.datetime.strptime(s, "%Y-%m-%d").date() if s else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Incremental BigQuery -> Postgres corpus refresh")
    ap.add_argument("--dry-run", action="store_true",
                    help="probe + cost-estimate only; makes no changes")
    ap.add_argument("--since", type=_parse_date,
                    help="override window start (YYYY-MM-DD); default = watermark - lookback")
    ap.add_argument("--until", type=_parse_date, help="window end (YYYY-MM-DD), inclusive")
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--max-gb", type=float, default=MAX_EXTRACT_GB,
                    help="hard ceiling on the delta extract's dry-run estimate")
    ap.add_argument("--force", action="store_true",
                    help="run the extract even if BigQuery is not ahead of the watermark")
    ap.add_argument("--skip-embed", action="store_true")
    ap.add_argument("--max-pubs", type=int, help="cap publications loaded (bounded test runs)")
    a = ap.parse_args(argv)

    try:
        with Lock():
            r = run(since=a.since, until=a.until, lookback_days=a.lookback_days,
                    max_gb=a.max_gb, dry_run=a.dry_run, force=a.force,
                    skip_embed=a.skip_embed, max_pubs=a.max_pubs)
    except bqclient.CostCeilingExceeded as e:
        Log()(f"[abort] {e}")
        return 2
    except RuntimeError as e:
        Log()(f"[abort] {e}")
        return 3
    return 0 if r.get("status") in ("ok", "up_to_date", "dry_run", "empty_window") else 1


if __name__ == "__main__":
    sys.exit(main())
