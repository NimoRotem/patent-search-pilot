"""Parse, chunk and embed workstream C's fetched full text, as a stream rather than a phase.

C fetches a batch; this parses, chunks and embeds that batch while C fetches the next. There is no
"acquisition finishes, then embedding starts": the worker holds a lease, drains whatever its
sources have produced since its own watermark, and idles politely when they have produced nothing.

WHAT IT WILL NOT DO
-------------------
* It never writes `chunks`, `publications`, `claims`, `paragraphs` or `figures`. Vectors land in
  `chunks_stage_v3`; workstream F builds releases from there. Inserting into the live table is an
  insert into a 94 GB HNSW graph while production is querying it.
* It never stages a `paragraph` chunk for a publication that has rows in `paragraphs`. That
  population is `ops/desc_backfill.py`'s, entirely and by definition: the description backfill
  reads `FROM paragraphs` and writes nothing else. The check is a query, not a convention, and
  `tests/test_parsed_embed.py::test_a_publication_with_paragraph_rows_keeps_its_description_with_the_desc_backfill`
  goes red if it is removed.
* It never takes the description backfill's lease. Different advisory-lock namespace, different
  progress table, different `corpus_release` tag.

HOW IT SURVIVES BEING KILLED
----------------------------
A chunk is written to `parsed_embed_item` before anything is paid for, and deleted only in the
same transaction that writes its vector to `chunks_stage_v3`. So at every instant a unit of work
is queued, in a batch job recorded in `parsed_embed_batch`, or staged. A restart polls the jobs it
finds in the database rather than resubmitting them, and requeues only the items a partially
succeeded job failed on.

    python ops/parsed_embed.py 0 1 parsed      # the worker
    python ops/parsed_embed.py status          # counts and watermarks
    python ops/parsed_embed.py estimate 14379018 1078   # cost of N chunks averaging M chars
"""
from __future__ import annotations

import json
import os
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#  The worktree's own .env is the default rather than a requirement, so the worker runs the same
#  way from a shell, from pytest and from Supervisor. There is still no default PASSWORD: a
#  missing setting is a startup error, which is the rule migrate.py already enforces.
ENV_FILE = (os.environ.get("BACKFILL_ENV_FILE") or os.environ.get("PARSED_ENV_FILE")
            or os.path.join(ROOT, ".env"))
if os.path.exists(ENV_FILE):
    load_dotenv(ENV_FILE, override=False)

import embed_common                                                  # noqa: E402
import parsed_sources                                                # noqa: E402
import batch_embed                                                   # noqa: E402
import parsed_norm                                                   # noqa: E402
import stage_chunks                                                  # noqa: E402
from desc_backfill import DDL as STAGE_DDL                           # noqa: E402

# ---------------------------------------------------------------------------- configuration
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))
TASK_TYPE = os.environ.get("EMBED_TASK_TYPE", "RETRIEVAL_DOCUMENT")
MAX_ATTEMPTS = int(os.environ.get("EMBED_MAX_ATTEMPTS", "3"))
CALL_TIMEOUT_MS = int(os.environ.get("EMBED_CALL_TIMEOUT_MS", "120000"))

#  The Vertex embedding quota is ACCOUNT WIDE, so this number is not "how fast can one process
#  go", it is "what is left once the description backfill has taken its six". Measured on
#  2026-08-22: 3 shards x 16 workers tripped 429 RESOURCE_EXHAUSTED, 3 x 12 did not.
WORKERS = int(os.environ.get("PARSED_EMBED_WORKERS", "4"))
SUB = int(os.environ.get("PARSED_EMBED_SUB", "200"))          # inputs per synchronous call
FETCH_DOCS = int(os.environ.get("PARSED_FETCH_DOCS", "200"))  # parsed documents per source round
DRAIN = int(os.environ.get("PARSED_DRAIN", "4000"))           # queued items per synchronous round
IDLE_SLEEP = float(os.environ.get("PARSED_IDLE_SLEEP", "60"))
#  A floor on the loop, not a throttle on the work. Without it a round that only polls a running
#  batch job returns immediately and the worker spins: a listing, a docstore query and a Vertex
#  poll per millisecond, against a database that is serving production.
LOOP_SLEEP = float(os.environ.get("PARSED_LOOP_SLEEP", "5"))

#  EMBED_MODE: auto | sync | batch. `auto` submits a Gemini Batch job once the queue is worth one
#  and uses the synchronous API below that, which is what keeps a trickle of demand-fetched
#  documents from waiting an hour for a batch that will never fill.
EMBED_MODE = os.environ.get("PARSED_EMBED_MODE", "auto")
BATCH_MIN = int(os.environ.get("PARSED_BATCH_MIN", "5000"))
BATCH_MAX = int(os.environ.get("PARSED_BATCH_MAX", "50000"))
BATCH_BUCKET = os.environ.get("PARSED_BATCH_BUCKET", "nimo-patents-v3")
MAX_ITEM_ATTEMPTS = int(os.environ.get("PARSED_MAX_ITEM_ATTEMPTS", "4"))

CHUNKER_VERSION = stage_chunks.CHUNKER_VERSION
CORPUS_RELEASE = os.environ.get("PARSED_CORPUS_RELEASE", "v3-parsed-20260822")

#  "pars". Deliberately NOT the description backfill's 0x64657363: two jobs over different
#  populations must not be able to take each other's lock because they happen to share a shard
#  number.
LEASE_NAMESPACE = 0x70617273

#  Vertex list price for gemini-embedding-001, USD per million input tokens, 2026-08.
#  Override rather than edit: a price change should not be a code change on four hosts.
PRICE_PER_MTOK = float(os.environ.get("EMBED_PRICE_PER_MTOK", "0.15"))
#  MEASURED 2026-08-22, not assumed: 60 real chunks from `sources_docstore` billed 10,249 tokens
#  for 44,079 characters, so 4.301. The `token_count` COLUMN stays at len/4, because that is what
#  `chunker.py` and `desc_backfill.py` store and a staging column that disagrees with the corpus
#  is worse than one that is approximate.
CHARS_PER_TOKEN = float(os.environ.get("EMBED_CHARS_PER_TOKEN", "4.301"))

PG = embed_common.pg_params()
_log = embed_common.log

SQL_DDL = open(os.path.join(ROOT, "sql", "013_parsed_embed.sql"), encoding="utf-8").read()


def _connect():
    return psycopg.connect(row_factory=dict_row, connect_timeout=30, **PG)


def ensure_schema(conn):
    """Create what this worker owns, plus the stage table it shares with the description backfill.

    `STAGE_DDL` is imported rather than copied so `chunks_stage_v3` has exactly one definition;
    two CREATE TABLE statements for one table is how a column ends up on one host and not another.
    """
    with conn.cursor() as cur:
        cur.execute(STAGE_DDL)
        cur.execute(SQL_DDL)
    conn.commit()


# ---------------------------------------------------------------------------- cost

def estimate_cost(n_chunks, avg_chars, price_per_mtok=PRICE_PER_MTOK,
                  chars_per_token=CHARS_PER_TOKEN):
    """-> `{tokens, usd}`. The only honest way to decide whether to launch a niche."""
    tokens = n_chunks * (avg_chars / chars_per_token)
    return {"chunks": n_chunks, "avg_chars": avg_chars, "tokens": int(tokens),
            "usd": round(tokens / 1_000_000 * price_per_mtok, 2)}


# ---------------------------------------------------------------------------- publication ids

def shard_of(source_key, shards):
    """Which shard owns a document. crc32 rather than hash(), for the same reason the lease key
    uses it: hash() is randomised per process, so two workers would disagree about ownership."""
    return zlib.crc32(source_key.encode()) % max(1, shards)


def resolve_publication_ids(conn, pubs):
    """-> `{publication_number: id}` for the publications the corpus already holds.

    `publications` is UNIQUE on `(publication_number, kind_code)`, not on the number alone, so a
    number can carry more than one row. The lowest id is taken and the choice is deterministic,
    which matters because two workers must stage against the same id.
    """
    if not pubs:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT publication_number, min(id) AS id FROM publications "
                    "WHERE publication_number = ANY(%s) GROUP BY publication_number", (list(pubs),))
        return {r["publication_number"]: r["id"] for r in cur.fetchall()}


def surrogate_publication_ids(conn, pubs):
    """Stable NEGATIVE ids for publications the corpus does not hold.

    `chunks_stage_v3.publication_id` is NOT NULL and `publications` may not be written, so a
    fetched document that is not in the corpus needs an id of its own. Negating it means such a
    row can never be silently joined to a real publication, and grouping by publication still
    works.
    """
    if not pubs:
        return {}
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO parsed_stage_pub (publication_number) VALUES (%s) "
                        "ON CONFLICT (publication_number) DO NOTHING", [(p,) for p in pubs])
        cur.execute("SELECT id, publication_number FROM parsed_stage_pub "
                    "WHERE publication_number = ANY(%s)", (list(pubs),))
        return {r["publication_number"]: -r["id"] for r in cur.fetchall()}


def publications_with_paragraphs(conn, pub_ids):
    """THE DISJOINTNESS CHECK. `ops/desc_backfill.py` reads `FROM paragraphs` and writes nothing
    else, so a publication with a row in `paragraphs` has its description owned by that job. This
    pipeline stages no `paragraph` chunk for one."""
    ids = [i for i in pub_ids if i and i > 0]
    if not ids:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT publication_id AS p FROM paragraphs "
                    "WHERE publication_id = ANY(%s)", (ids,))
        return {r["p"] for r in cur.fetchall()}


# ---------------------------------------------------------------------------- ledger + queue

def ledgered(conn, source_keys):
    if not source_keys:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT source_key FROM parsed_doc_ledger WHERE source_key = ANY(%s)",
                    (list(source_keys),))
        return {r["source_key"] for r in cur.fetchall()}


def write_ledger(conn, row):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO parsed_doc_ledger
                       (source_key, publication_number, publication_id, content_sha, state, code,
                        reason, detail, n_claims, n_paragraphs, n_chunks, corpus_release)
                       VALUES (%(source_key)s, %(publication_number)s, %(publication_id)s,
                               %(content_sha)s, %(state)s, %(code)s, %(reason)s, %(detail)s,
                               %(n_claims)s, %(n_paragraphs)s, %(n_chunks)s, %(corpus_release)s)
                       ON CONFLICT (source_key) DO UPDATE SET
                          state = EXCLUDED.state, code = EXCLUDED.code, reason = EXCLUDED.reason,
                          detail = EXCLUDED.detail, n_chunks = EXCLUDED.n_chunks,
                          updated_at = now()""", row)


def enqueue(conn, rows, source_key, publication_id, shard):
    """Queue chunk rows for embedding. ON CONFLICT on `item_key` is the idempotence: a document
    re-listed by its source costs nothing."""
    if not rows:
        return 0
    flat, values = [], []
    for r in rows:
        values.append("(%s, %s, %s, %s, %s, %s, %s, %s)")
        flat += [r["item_key"], source_key, publication_id, shard, r["kind"],
                 stage_chunks.coord_json(r["coord"]), r["lang"], r["text"]]
    with conn.cursor() as cur:
        cur.execute("INSERT INTO parsed_embed_item "
                    "(item_key, source_key, publication_id, shard, kind, coord, lang, text) "
                    "VALUES " + ", ".join(values) +
                    " ON CONFLICT (item_key) DO NOTHING RETURNING id", flat)
        return len(cur.fetchall())


# ---------------------------------------------------------------------------- ingest

def ingest_round(conn, sources, prog, shard, shards):
    """Pull the next slice from every source, parse it, queue its chunks. -> counts."""
    counts = {"seen": 0, "staged": 0, "rejected": 0, "skipped": 0, "items": 0}
    for src in sources:
        wm = (prog["watermarks"] or {}).get(src.name, "")
        try:
            docs, new_wm, err = src.fetch(wm, limit=FETCH_DOCS)
        except Exception as exc:                                     # noqa: BLE001
            _log("source_error", source=src.name, err=str(exc)[:200])
            continue
        if err:
            _log("source_error", source=src.name, err=err)
        if new_wm and new_wm != wm:
            prog["watermarks"][src.name] = new_wm
        if not docs:
            continue
        counts["seen"] += len(docs)

        mine = [d for d in docs if shard_of(d["source_key"], shards) == shard]
        have = ledgered(conn, [d["source_key"] for d in mine])
        mine = [d for d in mine if d["source_key"] not in have]
        if not mine:
            continue

        pubs = [d["publication_number"] for d in mine if d["publication_number"]]
        real = resolve_publication_ids(conn, pubs)
        missing = [p for p in pubs if p not in real]
        surrogate = surrogate_publication_ids(conn, missing)
        conn.commit()
        with_paras = publications_with_paragraphs(conn, list(real.values()))

        for d in mine:
            pub = d["publication_number"]
            pid = real.get(pub, surrogate.get(pub))
            sha = parsed_norm.content_sha(d["payload"])
            base = {"source_key": d["source_key"], "publication_number": pub or "",
                    "publication_id": pid or 0, "content_sha": sha,
                    "corpus_release": CORPUS_RELEASE, "n_claims": 0, "n_paragraphs": 0,
                    "n_chunks": 0, "code": None, "reason": None, "detail": None}
            if not pub or pid is None:
                write_ledger(conn, dict(base, state="rejected", code="NO_PUBLICATION",
                                        reason="record carries no usable publication number"))
                counts["rejected"] += 1
                continue
            try:
                doc = parsed_norm.normalise(d["payload"], publication_number=pub,
                                            allow_claim_defect=True)
            except parsed_norm.ParseRejected as exc:
                write_ledger(conn, dict(base, state="rejected", code=exc.code,
                                        reason=exc.message[:500],
                                        detail=json.dumps(exc.detail, default=str)[:4000]))
                counts["rejected"] += 1
                _log("doc_rejected", pub=pub, code=exc.code, reason=exc.message[:200])
                continue

            dropped_paras = 0
            if pid in with_paras and doc.paragraphs:
                dropped_paras = len(doc.paragraphs)
                doc.paragraphs = []
                doc.notes.append("description left to patents-desc-backfill: this publication "
                                 "already has paragraphs rows")

            rows = stage_chunks.chunk_rows(doc, d["source_key"])
            n = enqueue(conn, rows, d["source_key"], pid, shard)
            #  `partial` means the abstract and description were staged and the CLAIMS were
            #  refused. It is a distinct state so workstream F can exclude, or chase, exactly
            #  those publications instead of discovering the gap in a search result.
            state = "partial" if doc.claim_defect else "staged"
            write_ledger(conn, dict(base, state=state, n_claims=len(doc.claims),
                                    n_paragraphs=len(doc.paragraphs), n_chunks=len(rows),
                                    code=(doc.claim_defect or {}).get("code"),
                                    reason=((doc.claim_defect or {}).get("reason") or "")[:500]
                                           or None,
                                    detail=json.dumps({"notes": doc.notes,
                                                       "declared_claims": doc.declared_claim_count,
                                                       "claim_defect": doc.claim_defect,
                                                       "paragraphs_left_to_backfill": dropped_paras
                                                       }, default=str)[:4000]))
            counts["staged"] += 1
            counts["items"] += n
            if dropped_paras:
                counts["skipped"] += 1
        conn.commit()
    return counts


# ---------------------------------------------------------------------------- staging writes

STAGE_COLS = ("publication_id, kind, ref_id, coord, lang, text, token_count, embedding, "
              "embed_model, embed_dim, task_type, chunker_version, corpus_release")


def stage_vectors(conn, pairs):
    """`pairs`: `[(item_row, vector)]`. Writes the vectors and DELETES the queue rows, in ONE
    transaction, which is what makes the pipeline exactly-once rather than merely resumable."""
    if not pairs:
        return 0
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS _parsed_in "
                    "(LIKE chunks_stage_v3 INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("TRUNCATE _parsed_in")
        with cur.copy(f"COPY _parsed_in ({STAGE_COLS}) FROM STDIN") as cp:
            for r, e in pairs:
                coord = r["coord"] if isinstance(r["coord"], str) else json.dumps(r["coord"])
                cp.write_row((r["publication_id"], r["kind"], None, coord, r["lang"] or "en",
                              r["text"], embed_common.tok(r["text"]), embed_common.vec(e),
                              EMBED_MODEL, EMBED_DIM, TASK_TYPE, CHUNKER_VERSION, CORPUS_RELEASE))
        cur.execute(f"INSERT INTO chunks_stage_v3 ({STAGE_COLS}) SELECT {STAGE_COLS} "
                    f"FROM _parsed_in ON CONFLICT DO NOTHING")
        cur.execute("DELETE FROM parsed_embed_item WHERE item_key = ANY(%s)",
                    ([r["item_key"] for r, _ in pairs],))
    conn.commit()
    return len(pairs)


def requeue(conn, item_keys, error):
    """Only what failed goes back, with its attempt count. An item that has failed
    MAX_ITEM_ATTEMPTS times is parked as `failed` rather than looping for ever: a poison chunk
    must not be able to stall a queue behind it."""
    if not item_keys:
        return
    with conn.cursor() as cur:
        cur.execute("""UPDATE parsed_embed_item
                       SET state = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'queued' END,
                           attempts = attempts + 1, batch_id = NULL, last_error = %s
                       WHERE item_key = ANY(%s)""",
                    (MAX_ITEM_ATTEMPTS, str(error)[:500], list(item_keys)))
    conn.commit()


# ---------------------------------------------------------------------------- synchronous drain

def drain_sync(conn, shard, limit=DRAIN):
    """The proven path: bounded, retried `embed_content` calls of SUB inputs each.

    Items are NOT marked in flight. A kill mid-call leaves them queued, which costs one round of
    embedding and never loses a row, and is only safe because the lease guarantees one worker.
    """
    with conn.cursor() as cur:
        cur.execute("""SELECT id, item_key, publication_id, kind, coord, lang, text
                       FROM parsed_embed_item WHERE state = 'queued' AND shard = %s
                       ORDER BY id LIMIT %s""", (shard, limit))
        batch = cur.fetchall()
    if not batch:
        return 0, 0
    subs = [batch[i:i + SUB] for i in range(0, len(batch), SUB)]

    def work(sub):
        return list(zip(sub, embed_common.embed_texts(
            [r["text"] for r in sub], EMBED_MODEL, EMBED_DIM, TASK_TYPE,
            max_attempts=MAX_ATTEMPTS, timeout_ms=CALL_TIMEOUT_MS)))

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(work, s) for s in subs]
        for sub, fut in zip(subs, futures):
            try:
                done += stage_vectors(conn, fut.result())
            except Exception as exc:                                 # noqa: BLE001
                _log("sub_failed", n=len(sub), err=str(exc)[:200])
                requeue(conn, [r["item_key"] for r in sub], exc)
    return done, len(subs)


# ---------------------------------------------------------------------------- batch drain

def _embedder():
    return batch_embed.VertexBatchEmbedder(model=EMBED_MODEL, dim=EMBED_DIM,
                                           task_type=TASK_TYPE, bucket=BATCH_BUCKET)


def submit_batch(conn, shard, embedder=None, limit=BATCH_MAX):
    """Claim queued items, record the job, then submit it. -> the batch id, or None."""
    embedder = embedder or _embedder()
    with conn.cursor() as cur:
        cur.execute("""SELECT id, item_key, publication_id, kind, coord, lang, text
                       FROM parsed_embed_item WHERE state = 'queued' AND shard = %s
                       ORDER BY id LIMIT %s""", (shard, limit))
        items = cur.fetchall()
        if not items:
            return None
        cur.execute("""INSERT INTO parsed_embed_batch (state, n_items, shard)
                       VALUES ('building', %s, %s) RETURNING id""", (len(items), shard))
        batch_id = cur.fetchone()["id"]
        cur.execute("""UPDATE parsed_embed_item SET state = 'submitted', batch_id = %s,
                                                    attempts = attempts + 1
                       WHERE item_key = ANY(%s)""", (batch_id, [i["item_key"] for i in items]))
    conn.commit()

    tag = f"{batch_id}-{int(time.time())}"
    try:
        job = embedder.submit([{"item_key": i["item_key"], "text": i["text"]} for i in items], tag)
    except Exception as exc:                                         # noqa: BLE001
        _log("batch_submit_failed", batch_id=batch_id, err=str(exc)[:300])
        with conn.cursor() as cur:
            cur.execute("UPDATE parsed_embed_batch SET state = 'failed', last_error = %s, "
                        "updated_at = now() WHERE id = %s", (str(exc)[:500], batch_id))
        conn.commit()
        requeue(conn, [i["item_key"] for i in items], exc)
        return None
    with conn.cursor() as cur:
        cur.execute("""UPDATE parsed_embed_batch SET state = 'submitted', job_name = %s,
                              input_uri = %s, output_prefix = %s, job_state = %s, updated_at = now()
                       WHERE id = %s""",
                    (job["job_name"], job["input_uri"], job["output_prefix"], job.get("state"),
                     batch_id))
    conn.commit()
    _log("batch_submitted", batch_id=batch_id, job=job["job_name"], n=len(items))
    return batch_id


def collect_batch(conn, batch, embedder=None):
    """Poll one recorded job and finish it. -> rows staged."""
    embedder = embedder or _embedder()
    with conn.cursor() as cur:
        cur.execute("""SELECT id, item_key, publication_id, kind, coord, lang, text
                       FROM parsed_embed_item WHERE batch_id = %s""", (batch["id"],))
        items = cur.fetchall()
    if not items:
        with conn.cursor() as cur:
            cur.execute("UPDATE parsed_embed_batch SET state = 'collected', updated_at = now() "
                        "WHERE id = %s", (batch["id"],))
        conn.commit()
        return 0

    #  A job we recorded but never named is a worker that died between claiming the items and the
    #  submission returning. The job may or may not exist; the items definitely do, so they go
    #  back on the queue. Paying twice for a job nobody collects is the cheaper mistake.
    if not batch.get("job_name"):
        requeue(conn, [i["item_key"] for i in items], "worker died before the job was named")
        with conn.cursor() as cur:
            cur.execute("UPDATE parsed_embed_batch SET state = 'failed', last_error = %s, "
                        "updated_at = now() WHERE id = %s",
                        ("no job name recorded", batch["id"]))
        conn.commit()
        return 0

    st = embedder.poll(batch["job_name"])
    with conn.cursor() as cur:
        cur.execute("UPDATE parsed_embed_batch SET job_state = %s, updated_at = now() WHERE id = %s",
                    (st["state"], batch["id"]))
    conn.commit()
    if not st["terminal"]:
        return 0
    if not st["ok"] or not st["output_dir"]:
        _log("batch_failed", batch_id=batch["id"], state=st["state"], err=st["error"][:200])
        requeue(conn, [i["item_key"] for i in items], st["error"] or st["state"])
        with conn.cursor() as cur:
            cur.execute("UPDATE parsed_embed_batch SET state = 'failed', last_error = %s, "
                        "n_failed = %s, updated_at = now() WHERE id = %s",
                        ((st["error"] or st["state"])[:500], len(items), batch["id"]))
        conn.commit()
        return 0

    vectors, errors = embedder.collect(
        st["output_dir"], [{"item_key": i["item_key"], "text": i["text"]} for i in items])
    pairs = [(i, vectors[i["item_key"]]) for i in items if i["item_key"] in vectors]
    staged = stage_vectors(conn, pairs)
    if errors:
        _log("batch_partial", batch_id=batch["id"], ok=len(vectors), failed=len(errors),
             sample=list(errors.values())[:1])
        requeue(conn, list(errors), "; ".join(sorted(set(errors.values()))[:3]))
    with conn.cursor() as cur:
        cur.execute("""UPDATE parsed_embed_batch SET state = 'collected', output_dir = %s,
                              n_ok = %s, n_failed = %s, updated_at = now() WHERE id = %s""",
                    (st["output_dir"], len(vectors), len(errors), batch["id"]))
    conn.commit()
    _log("batch_collected", batch_id=batch["id"], staged=staged, failed=len(errors))
    return staged


def resume_batches(conn, shard, embedder=None):
    with conn.cursor() as cur:
        cur.execute("""SELECT id, job_name, output_prefix FROM parsed_embed_batch
                       WHERE state IN ('building', 'submitted') AND shard = %s ORDER BY id""",
                    (shard,))
        batches = cur.fetchall()
    return sum(collect_batch(conn, b, embedder) for b in batches)


def in_flight_batches(conn, shard):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) c FROM parsed_embed_batch "
                    "WHERE state IN ('building', 'submitted') AND shard = %s", (shard,))
        return cur.fetchone()["c"]


def queued_count(conn, shard):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) c FROM parsed_embed_item WHERE state = 'queued' AND shard = %s",
                    (shard,))
        return cur.fetchone()["c"]


def drain(conn, shard, embedder=None):
    """-> (rows staged, api calls, batch jobs submitted)."""
    n = queued_count(conn, shard)
    if not n:
        return 0, 0, 0
    mode = EMBED_MODE
    if mode == "auto":
        mode = "batch" if n >= BATCH_MIN else "sync"
    if mode == "batch":
        return 0, 0, (1 if submit_batch(conn, shard, embedder) else 0)
    done, calls = drain_sync(conn, shard)
    return done, calls, 0


# ---------------------------------------------------------------------------- progress

def load_progress(conn, shard, shards, pass_name):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO parsed_embed_progress (shard, shards, pass_name)
                       VALUES (%s, %s, %s) ON CONFLICT (shard) DO NOTHING""",
                    (shard, shards, pass_name))
        conn.commit()
        cur.execute("SELECT * FROM parsed_embed_progress WHERE shard = %s", (shard,))
        st = dict(cur.fetchone())
    #  A watermark only means anything within one (pass, shard geometry). Change either and it
    #  points into a range this process no longer owns, which silently skips work instead of doing
    #  it. Reset on both, exactly as the description backfill does.
    if st["pass_name"] != pass_name or st["shards"] != shards:
        with conn.cursor() as cur:
            cur.execute("""UPDATE parsed_embed_progress SET pass_name = %s, shards = %s,
                                  watermarks = '{}'::jsonb WHERE shard = %s""",
                        (pass_name, shards, shard))
        conn.commit()
        st.update(pass_name=pass_name, shards=shards, watermarks={})
    st["watermarks"] = st["watermarks"] or {}
    return st


def save_progress(conn, shard, st):
    with conn.cursor() as cur:
        cur.execute("""UPDATE parsed_embed_progress
                       SET watermarks = %s, docs_seen = %s, docs_staged = %s, docs_rejected = %s,
                           docs_skipped = %s, rows_done = %s, chars_done = %s, api_calls = %s,
                           batch_jobs = %s, updated_at = now()
                       WHERE shard = %s""",
                    (json.dumps(st["watermarks"]), st["docs_seen"], st["docs_staged"],
                     st["docs_rejected"], st["docs_skipped"], st["rows_done"], st["chars_done"],
                     st["api_calls"], st["batch_jobs"], shard))
    conn.commit()


# ---------------------------------------------------------------------------- the worker

def run(shard=0, shards=1, pass_name="parsed", once=False):
    conn = _connect()
    conn.execute("SET lock_timeout = '15s'")
    ensure_schema(conn)

    # Exit 0, not an error: another worker legitimately holds this pass. Supervisor is configured
    # autorestart=unexpected with exitcodes=0, so a clean refusal does not become a restart loop.
    if not embed_common.take_lease(conn, LEASE_NAMESPACE, pass_name, shard):
        _log("lease_refused", pass_name=pass_name, shard=shard,
             key=embed_common.lease_key(pass_name, shard))
        conn.close()
        sys.exit(0)
    _log("lease_taken", pass_name=pass_name, shard=shard,
         key=embed_common.lease_key(pass_name, shard), namespace=LEASE_NAMESPACE)

    st = load_progress(conn, shard, shards, pass_name)
    sources = parsed_sources.build(conn)
    _log("start", shard=shard, shards=shards, pass_name=pass_name, mode=EMBED_MODE,
         sources=[s.name for s in sources], model=EMBED_MODEL, dim=EMBED_DIM,
         release=CORPUS_RELEASE, watermarks=st["watermarks"])

    t0 = time.time()
    while True:
        work = 0
        work += resume_batches(conn, shard)
        counts = ingest_round(conn, sources, st, shard, shards)
        st["docs_seen"] += counts["seen"]
        st["docs_staged"] += counts["staged"]
        st["docs_rejected"] += counts["rejected"]
        st["docs_skipped"] += counts["skipped"]
        work += counts["items"]

        staged, calls, jobs = drain(conn, shard)
        st["rows_done"] += staged
        st["api_calls"] += calls
        st["batch_jobs"] += jobs
        work += staged + jobs
        save_progress(conn, shard, st)

        if work:
            _log("progress", shard=shard, docs_seen=st["docs_seen"], docs_staged=st["docs_staged"],
                 docs_rejected=st["docs_rejected"], rows=st["rows_done"],
                 queued=queued_count(conn, shard), api_calls=st["api_calls"],
                 batch_jobs=st["batch_jobs"], elapsed=int(time.time() - t0))
        if once:
            break
        if not work:
            _log("idle", shard=shard, queued=queued_count(conn, shard),
                 in_flight=in_flight_batches(conn, shard), sleep=IDLE_SLEEP,
                 watermarks=st["watermarks"])
            time.sleep(IDLE_SLEEP)
        else:
            time.sleep(LOOP_SLEEP)
    conn.close()
    return st


def status():
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("SELECT corpus_release, kind, count(*) c FROM chunks_stage_v3 "
                    "WHERE corpus_release = %s GROUP BY 1, 2 ORDER BY 3 DESC", (CORPUS_RELEASE,))
        print("staged by kind:", cur.fetchall())
        cur.execute("SELECT state, code, count(*) c FROM parsed_doc_ledger GROUP BY 1, 2 "
                    "ORDER BY 3 DESC")
        print("ledger:", cur.fetchall())
        cur.execute("SELECT state, count(*) c FROM parsed_embed_item GROUP BY 1")
        print("queue:", cur.fetchall())
        cur.execute("SELECT id, state, job_state, n_items, n_ok, n_failed, job_name "
                    "FROM parsed_embed_batch ORDER BY id DESC LIMIT 10")
        for r in cur.fetchall():
            print("batch:", r)
        cur.execute("SELECT * FROM parsed_embed_progress ORDER BY shard")
        for r in cur.fetchall():
            print("progress:", r)
    conn.close()


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv[:1] == ["status"]:
        status()
    elif argv[:1] == ["estimate"]:
        print(json.dumps(estimate_cost(int(argv[1]), float(argv[2])), indent=2))
    else:
        run(int(argv[0]) if argv else 0,
            int(argv[1]) if len(argv) > 1 else 1,
            argv[2] if len(argv) > 2 else "parsed",
            once="--once" in argv)
