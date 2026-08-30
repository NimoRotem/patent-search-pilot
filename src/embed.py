"""Embed chunks (spec §4). 768-dim main run + 1024/3072 bench (Matryoshka via
output_dimensionality). Idempotent/resumable (WHERE embedding IS NULL). Non-paragraph chunks
first so search works before descriptions finish. Concurrent API calls; single-writer DB updates.

Provider: Vertex AI `gemini-embedding-001` via the GCE service account (no key) — the OpenAI
account hit an insufficient_quota wall mid-run, so we pivoted to Vertex (multilingual, supports
the same Matryoshka dimension sweep, uses task_type for retrieval quality). OpenAI path kept as
a fallback behind EMBED_PROVIDER.
"""
from __future__ import annotations
import os, sys, threading
from concurrent.futures import ThreadPoolExecutor
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import db
from config import EMBED_DIM, BENCH_DIMS

EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "vertex")
VERTEX_EMBED_MODEL = "gemini-embedding-001"
SUB = 200          # inputs per API call (gemini-embedding batch)
FETCH = 4000       # rows pulled per DB round (frequent commits, short txns)
#  Concurrent Vertex calls PER PROCESS. Env-overridable because the right value depends on how
#  many shard processes are running: the quota is account-wide, so N shards x WORKERS is what
#  actually has to stay under it. 3 shards x 16 = 48 concurrent tripped 429 RESOURCE_EXHAUSTED,
#  so 3 x 12 = 36 is the tuned setting for a sharded bulk run and 14 remains fine single-stream.
#
#  Sharding is the ONLY lever here that does not take search offline. Measured single-stream on
#  the dedicated box: ~68 chunks/sec with the HNSW index live, i.e. ~25 h for a 6M-chunk backlog.
#  The alternative (drop index -> embed at ~376/sec -> rebuild) is no faster overall once the
#  multi-hour rebuild is counted, and it dark-arts the live app for the whole window.
WORKERS = int(os.environ.get("EMBED_WORKERS", "14"))

_local = threading.local()
def _genai():
    if not hasattr(_local, "client"):
        from google import genai
        _local.client = genai.Client(vertexai=True, project="nimo-gpt", location="us-central1")
    return _local.client


def _is_rate(e):
    s = str(e)
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in s.lower()


@retry(wait=wait_exponential(min=0.5, max=8), stop=stop_after_attempt(10),
       retry=retry_if_exception_type(Exception))
def _embed_vertex(texts, dim, task_type):
    from google.genai.types import EmbedContentConfig
    r = _genai().models.embed_content(
        model=VERTEX_EMBED_MODEL, contents=list(texts),
        config=EmbedContentConfig(output_dimensionality=dim, task_type=task_type))
    return [list(e.values) for e in r.embeddings]


#  ---------------------------------------------------------------- voyage (v2 corpus)
#  MongoDB serves the Voyage models at ai.mongodb.com and that account is PAID; the direct Voyage
#  account has no working payment method and its sync endpoint is capped at 3 requests a minute,
#  which is unusable for interactive search. MEASURED 2026-08-27: 20.8M tokens/min sustained.
VOYAGE_BASE = os.environ.get("VOYAGE_BASE", "https://ai.mongodb.com/v1")
VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-4-lite")
VOYAGE_MAX_INPUTS = 1000           # hard provider limit; 1,001 is rejected outright


@retry(wait=wait_exponential(min=0.5, max=8), stop=stop_after_attempt(8),
       retry=retry_if_exception_type(Exception))
def _voyage_call(texts, dim, input_type):
    import json as _json
    import urllib.request as _url
    body = _json.dumps({"input": list(texts), "model": VOYAGE_MODEL,
                        "input_type": input_type, "output_dimension": dim}).encode()
    r = _url.Request(VOYAGE_BASE + "/embeddings", method="POST", data=body)
    r.add_header("Authorization", "Bearer " + os.environ["MDB_MODEL_API_KEY"])
    r.add_header("Content-Type", "application/json")
    with _url.urlopen(r, timeout=180) as fh:
        d = _json.loads(fh.read())
    #  Order by the returned index rather than trusting arrival order to match the input list.
    return [x["embedding"] for x in sorted(d["data"], key=lambda x: x.get("index", 0))]


def _embed_voyage(texts, dim, task_type):
    #  Voyage embeds queries and documents asymmetrically, and the app expresses that with Gemini's
    #  task_type vocabulary. Translate rather than leak the provider's naming upward.
    input_type = "query" if str(task_type).upper() == "RETRIEVAL_QUERY" else "document"
    texts = list(texts)
    out = []
    for i in range(0, len(texts), VOYAGE_MAX_INPUTS):
        out.extend(_voyage_call(texts[i:i + VOYAGE_MAX_INPUTS], dim, input_type))
    return out


def embed_texts(texts, dim, task_type="RETRIEVAL_DOCUMENT"):
    if EMBED_PROVIDER == "voyage":
        return _embed_voyage(texts, dim, task_type)
    if EMBED_PROVIDER == "vertex":
        return _embed_vertex(texts, dim, task_type)
    from openai import OpenAI
    from config import OPENAI_API_KEY, EMBED_MODEL
    c = OpenAI(api_key=OPENAI_API_KEY)
    return [d.embedding for d in c.embeddings.create(model=EMBED_MODEL, input=list(texts), dimensions=dim).data]


_QCACHE = {}                       # (text, dim) -> vector; query embeds are deterministic and repeat
_QCACHE_LOCK = threading.Lock()    # a lot within one report (the seed query is embedded for the
_QCACHE_MAX = 8192                 # search AND for grounding; elements re-embed across rounds), so
                                   # memoize to cut redundant Vertex round-trips (the bottleneck).


def embed_query(text, dim=EMBED_DIM):
    """Query-side embedding uses the RETRIEVAL_QUERY task type (asymmetric retrieval).
    An empty/whitespace query would make the embedding API error; substitute a neutral token so
    the call never crashes (callers should short-circuit empty queries anyway). Memoized in-process
    so the same query text isn't re-embedded (search + grounding + cross-round repeats)."""
    if not text or not str(text).strip():
        text = "patent"
    key = (text, dim)
    with _QCACHE_LOCK:
        hit = _QCACHE.get(key)
    if hit is not None:
        return hit
    v = embed_texts([text], dim, task_type="RETRIEVAL_QUERY")[0]
    with _QCACHE_LOCK:
        if len(_QCACHE) >= _QCACHE_MAX:
            _QCACHE.clear()
        _QCACHE[key] = v
    return v


def _vec(e):
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


def _bulk_update(conn, table, col, pairs):
    """pairs: list of (id, embedding). UPDATE via VALUES join."""
    if not pairs:
        return
    with conn.cursor() as cur:
        for i in range(0, len(pairs), 256):
            sub = pairs[i:i + 256]
            vals = ",".join(["(%s,%s::vector)"] * len(sub))
            flat = []
            for cid, e in sub:
                flat += [cid, _vec(e)]
            key = "chunk_id" if table != "chunks" else "id"
            cur.execute(
                f"UPDATE {table} SET {col}=d.emb FROM (VALUES {vals}) AS d(id,emb) "
                f"WHERE {table}.{key}=d.id", flat)
    conn.commit()


def run(dim=EMBED_DIM, limit=None, order_priority=True, shard=None, where_sql=""):
    """shard=(k,n) processes only chunks with id % n == k -> run n processes in parallel to beat
    the GIL bottleneck on response parsing (Vertex has no rate-limit here).

    where_sql is an extra AND-fragment restricting WHICH pending chunks to embed. It exists so a
    wide ingest can be embedded in a chosen ORDER rather than by chunk id: on the Tier-1 corpus
    the wide gold set measured B66C (2.6% reachable) and B66F (4.0%) as corpus-starved while
    B25J (29.3%) and B65G (31.4%) are ranking-limited, so embedding the starved subclasses first
    makes the first re-measurable improvement land hours earlier — and if a multi-hour run is
    interrupted, the most valuable part is already live. Callers build the fragment; it is never
    taken from user input.
    """
    conn = db.connect()
    shard_sql = f" AND id %% {shard[1]} = {shard[0]}" if shard else ""
    shard_sql += where_sql
    total = db.scalar(f"SELECT count(*) FROM chunks WHERE embedding IS NULL{shard_sql}")
    print(f"[embed] {total:,} chunks pending at {dim}-dim shard={shard}")
    order = "CASE kind WHEN 'paragraph' THEN 2 WHEN 'figure_caption' THEN 3 ELSE 1 END, id" \
            if order_priority else "id"
    done = 0
    def work(rows):
        embs = embed_texts([r["text"] for r in rows], dim)
        return [(rows[j]["id"], embs[j]) for j in range(len(rows))]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:   # persistent pool -> reuse thread-local clients
        while True:
            with conn.cursor() as cur:
                cur.execute(f"SELECT id, text FROM chunks WHERE embedding IS NULL{shard_sql} "
                            f"ORDER BY {order} LIMIT %s", (FETCH,))
                batch = cur.fetchall()
            if not batch:
                break
            subs = [batch[i:i + SUB] for i in range(0, len(batch), SUB)]
            allpairs = [p for sub in ex.map(work, subs) for p in sub]
            _bulk_update(conn, "chunks", "embedding", allpairs)
            done += len(allpairs)
            print(f"  embedded {done:,}/{total:,}", end="\r", flush=True)
            if limit and done >= limit:
                break
    print(f"\n[embed] done at {dim}-dim: {done:,}")
    conn.close()


def run_bench(n=8000, dims=BENCH_DIMS, pub_ids=None):
    """Embed a small subset at 1024 and 3072 dims for the dimension comparison (§8).
    Subset = abstract+claim chunks. If pub_ids given (the gold-relevant docs), target those so
    the recall comparison is informative; otherwise a leading sample."""
    conn = db.connect()
    with conn.cursor() as cur:
        if pub_ids:
            cur.execute("""SELECT id, text FROM chunks
                           WHERE kind IN ('abstract','claim_own','claim_resolved')
                           AND publication_id = ANY(%s) ORDER BY id LIMIT %s""", (list(pub_ids), n))
        else:
            cur.execute("""SELECT id, text FROM chunks
                           WHERE kind IN ('abstract','claim_own','claim_resolved')
                           ORDER BY id LIMIT %s""", (n,))
        rows = cur.fetchall()
    print(f"[bench] embedding {len(rows):,} chunks at dims {dims}")
    for dim in dims:
        table = f"bench_emb_{dim}"
        # resume: skip already-done
        done_ids = set(r["chunk_id"] for r in db.connect().cursor().execute(
            f"SELECT chunk_id FROM {table}").fetchall()) if False else set()
        todo = [r for r in rows if r["id"] not in done_ids]
        subs = [todo[i:i + SUB] for i in range(0, len(todo), SUB)]
        allpairs = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            def work(rows_):
                embs = embed_texts([r["text"] for r in rows_], dim)
                return [(rows_[j]["id"], embs[j]) for j in range(len(rows_))]
            for pairs in ex.map(work, subs):
                allpairs += pairs
        with conn.cursor() as cur:
            for i in range(0, len(allpairs), 256):
                sub = allpairs[i:i + 256]
                vals = ",".join(["(%s,%s::vector)"] * len(sub))
                flat = []
                for cid, e in sub:
                    flat += [cid, _vec(e)]
                cur.execute(f"INSERT INTO {table} (chunk_id, embedding) VALUES {vals} "
                            f"ON CONFLICT (chunk_id) DO NOTHING", flat)
            conn.commit()
        print(f"  bench {dim}-dim: {len(allpairs):,} rows")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "bench":
        run_bench()
    elif cmd == "shard":
        k, n = int(sys.argv[2]), int(sys.argv[3])
        run(shard=(k, n))
    else:
        lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
        run(limit=lim)
