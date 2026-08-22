"""Prove that a vector this pipeline produces is the same vector the corpus already stores.

A corpus with two incompatible vector spaces is worse than one that is incomplete: an incomplete
corpus misses documents, a mixed one RANKS them wrongly and nothing about the output says so. So
before anything is staged, take text the corpus already holds, embed it through the pipeline's own
call, and measure the cosine distance to the stored vector.

Zero is not achievable and is not the bar. `chunks.embedding` was written through the same
six-decimal pgvector literal this pipeline uses, so the floor is that rounding, measured at
roughly 9e-11. Anything at that order is the same vector. Anything at 1e-3 or above is a different
model, a different dimension or a normalisation that one side applies and the other does not.

    python ops/vector_space_check.py                    # 8 of each kind, plus CJK
    python ops/vector_space_check.py --n 6 --batch      # also send corpus text through a batch job

MEASURED 2026-08-22 on the live corpus.

**Synchronous path: max cosine distance 2.22e-16 over 56 chunks**, covering all six kinds
(whole, abstract, claim_own, claim_resolved, paragraph, figure_caption) in English and Chinese,
sampled from six windows spread over the 27.6M rows. 2.22e-16 is double precision epsilon: after
the six decimal pgvector literal this pipeline writes, the vector it produces is not merely close
to the stored one, it is the SAME LITERAL.

**Gemini Batch path: max cosine distance 2.99e-12 over 12 corpus chunks**, sent through a real
Vertex batch prediction job (`8184722921751576576`, about four minutes end to end) and compared to
what the corpus stores, not to what the synchronous API returns. Measuring batch against sync
would leave open the possibility that the two agree with each other and neither agrees with the
corpus.

The stored vectors have **norm 0.589**: `gemini-embedding-001` at `output_dimensionality=768` is
NOT unit normalised, and must not be normalised here either. Normalising one side and not the
other lands near 1e-1, a `task_type` difference near 1e-2, and both fail the 1e-7 tolerance loudly.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.environ.get("BACKFILL_ENV_FILE") or os.path.join(ROOT, ".env"), override=False)

import embed_common                                                  # noqa: E402

KINDS = ("whole", "abstract", "claim_own", "claim_resolved", "paragraph", "figure_caption")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))
TASK_TYPE = os.environ.get("EMBED_TASK_TYPE", "RETRIEVAL_DOCUMENT")

#  The rounding floor plus three orders of headroom. Tight enough that a normalisation difference
#  (which lands near 1e-1) or a task_type difference (near 1e-2) fails, loose enough that float
#  noise does not.
TOLERANCE = float(os.environ.get("VECTOR_SPACE_TOLERANCE", "1e-7"))


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


def cosine_distance(a, b):
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 1.0
    return 1.0 - sum(x * y for x, y in zip(a, b)) / (na * nb)


def parse_vector(s):
    return [float(x) for x in s.strip("[]").split(",")]


#  Where the sample is drawn from. NOT `ORDER BY id LIMIT n` from the start of the table, which
#  draws every row from the oldest publications the corpus ingested and would miss a model or a
#  chunker that changed part way through the backfill. Six windows spread over the 27.6M rows.
SPREAD = (1, 3_000_000, 7_000_000, 12_000_000, 18_000_000, 24_000_000)

#  Rows read per window. Chunks for one publication are consecutive in id, and a fully texted
#  publication is 56.1 chunks, so 600 rows is roughly ten publications and carries every kind.
WINDOW = int(os.environ.get("VECTOR_SPACE_WINDOW", "600"))

#  A hard ceiling on every statement this tool issues against the production box. An earlier
#  version filtered on `kind` inside the window and the planner answered it with a primary key
#  scan that walked millions of rows looking for a `figure_caption` above id 24,000,000; it ran
#  for over a minute before it was cancelled by hand. A pure key range scan cannot do that, and
#  this makes sure that a future edit which reintroduces the mistake fails loudly in ten seconds
#  instead of quietly loading a database that is serving searches.
STATEMENT_TIMEOUT_MS = int(os.environ.get("VECTOR_SPACE_STATEMENT_TIMEOUT_MS", "10000"))

#  Where to look for CJK, and how far to walk. There is no index on `lang`, so an unrestricted
#  `lang IN ('zh','ja','ko')` predicate is answered with a sequential scan of a 290 GB table,
#  which is the thing the brief forbids by name. MEASURED 2026-08-22: the lowest CJK chunk in the
#  corpus is id 1,840,937, and an `ORDER BY id LIMIT 6` probe starting from id 1 walks those
#  1.84M rows and takes 39 seconds. Starting from just below it costs milliseconds and finds the
#  same rows. These offsets are a shortcut, not a contract: if they stop finding CJK the tool
#  says so in its output rather than falling back to a scan.
CJK_FROM = (1_840_000, 5_000_000, 11_000_000, 20_000_000)
CJK_SPAN = int(os.environ.get("VECTOR_SPACE_CJK_SPAN", "200000"))

SELECT = "SELECT id, kind, lang, text, embedding::text AS emb FROM chunks"


def probe(conn, sql, params):
    """One bounded read, in its own transaction, with its own deadline. -> rows, or [].

    A probe that would take longer than `STATEMENT_TIMEOUT_MS` is ABANDONED rather than waited
    for. This box is serving production searches and no diagnostic is worth loading it: a sample
    that is one kind short is a note in the output, a ten minute scan of a 290 GB table is an
    incident. An earlier version of this file had exactly that, filtering on `kind` inside a key
    window and being answered with a primary key scan that walked millions of rows looking for a
    `figure_caption`; it ran for over a minute before it was cancelled by hand.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
                cur.execute(sql, params)
                return cur.fetchall()
    except psycopg.errors.QueryCanceled:
        return []


def sample(conn, per_kind=6):
    """A handful of each kind, drawn from six places in the corpus rather than one.

    Three bounded reads, in order of preference:

      * six `chunks_pkey` range windows. No filter the key cannot answer, so the cost is `WINDOW`
        rows per window whatever the distribution of kinds. This is where the spread comes from.
      * a top-up through `ix_chunks_kind` for any kind those windows missed. `paragraph` is 4% of
        the corpus and `figure_caption` 0.6%, so a window of a few hundred consecutive rows often
        holds neither. Measured at 20 ms, because it is an index scan that stops at LIMIT.
      * CJK, inside a bounded id span rather than over the table. 39.9% of the corpus is CJK and a
        tokeniser difference would show up there first, so it is sampled deliberately instead of
        being left to whatever the windows happened to contain. There is no index on `lang`, so
        each span is small and a span that does not answer in time is skipped.
    """
    by_kind, cjk, seen = {}, [], set()

    def take(r):
        if r["id"] in seen:
            return
        seen.add(r["id"])
        bucket = by_kind.setdefault(r["kind"], [])
        if len(bucket) < per_kind:
            bucket.append(r)

    for lo in SPREAD:
        for r in probe(conn, f"{SELECT} WHERE id >= %s AND embedding IS NOT NULL "
                             f"ORDER BY id LIMIT %s", (lo, WINDOW)):
            take(r)

    for kind in KINDS:
        short = per_kind - len(by_kind.get(kind, []))
        if short <= 0:
            continue
        for r in probe(conn, f"{SELECT} WHERE kind = %s AND embedding IS NOT NULL LIMIT %s",
                       (kind, short)):
            take(r)

    for lo in CJK_FROM:
        if len(cjk) >= per_kind:
            break
        for r in probe(conn, f"{SELECT} WHERE id >= %s AND id < %s AND embedding IS NOT NULL "
                             f"AND lang IN ('zh', 'ja', 'ko') ORDER BY id LIMIT %s",
                       (lo, lo + CJK_SPAN, per_kind - len(cjk))):
            cjk.append(r)

    rows = [r for kind in KINDS for r in by_kind.get(kind, [])]
    have = {r["id"] for r in rows}
    rows += [r for r in cjk if r["id"] not in have]
    missing = [k for k in KINDS if not by_kind.get(k)]
    if missing:
        print(f"  note: no {', '.join(missing)} chunk could be sampled", flush=True)
    if not cjk:
        print("  note: no CJK chunk fell in any sampled span", flush=True)
    return rows


def check(conn, per_kind=6, verbose=True):
    rows = sample(conn, per_kind)
    if not rows:
        return {"n": 0, "max": None, "ok": False, "reason": "no embedded chunks sampled"}
    out = []
    for i in range(0, len(rows), 100):
        sub = rows[i:i + 100]
        fresh = embed_common.embed_texts([r["text"] for r in sub], EMBED_MODEL, EMBED_DIM,
                                         TASK_TYPE)
        for r, f in zip(sub, fresh):
            stored = parse_vector(r["emb"])
            #  Through `embed_common.vec` and back, which is the six-decimal pgvector literal the
            #  staging writer actually emits. Comparing the raw float32 list would measure the
            #  model and skip the format, and the format is half of "the same vector".
            f = parse_vector(embed_common.vec(f))
            out.append({"id": r["id"], "kind": r["kind"], "lang": r["lang"],
                        "chars": len(r["text"]), "stored_norm": round(_norm(stored), 6),
                        "fresh_norm": round(_norm(f), 6),
                        "cosine_distance": cosine_distance(stored, f)})
    worst = max(out, key=lambda r: r["cosine_distance"])
    res = {"n": len(out), "max": worst["cosine_distance"], "worst": worst,
           "tolerance": TOLERANCE, "ok": worst["cosine_distance"] <= TOLERANCE,
           "model": EMBED_MODEL, "dim": EMBED_DIM, "task_type": TASK_TYPE,
           "by_kind": {}}
    for r in out:
        res["by_kind"].setdefault(r["kind"], []).append(r["cosine_distance"])
    res["by_kind"] = {k: {"n": len(v), "max": max(v)} for k, v in res["by_kind"].items()}
    if verbose:
        for r in sorted(out, key=lambda r: -r["cosine_distance"])[:10]:
            print(f"  {r['kind']:15s} {r['lang'] or '-':3s} chars={r['chars']:6,d} "
                  f"stored_norm={r['stored_norm']:.4f} cos_dist={r['cosine_distance']:.3e}")
    return res


def check_batch(conn, bucket=None, n=12, poll_s=20):
    """The same question for the Gemini Batch path, asked against the CORPUS and not against the
    synchronous API.

    Batch equals sync equals corpus is the chain that matters, and measuring the middle link only
    leaves the possibility that both paths agree with each other and neither agrees with what is
    stored. So this takes text the corpus already holds, sends it through a real Vertex batch
    prediction job, and compares to the stored vector.
    """
    import batch_embed
    bucket = bucket or os.environ.get("PARSED_BATCH_BUCKET", "nimo-patents-v3")
    e = batch_embed.VertexBatchEmbedder(model=EMBED_MODEL, dim=EMBED_DIM, task_type=TASK_TYPE,
                                        bucket=bucket, prefix="embed_batch/spacecheck")
    rows = sample(conn, max(1, n // len(KINDS) + 1))[:n]
    if not rows:
        return {"ok": False, "reason": "no embedded chunks sampled"}
    items = [{"item_key": f"c{r['id']}", "text": r["text"]} for r in rows]
    job = e.submit(items, tag=f"spacecheck-{os.getpid()}")
    print("submitted", job["job_name"], f"({len(items)} corpus chunks)", flush=True)
    import time
    while True:
        st = e.poll(job["job_name"])
        print("  ", st["state"], flush=True)
        if st["terminal"]:
            break
        time.sleep(poll_s)
    if not st["ok"]:
        return {"ok": False, "state": st["state"], "error": st["error"]}
    vectors, errors = e.collect(st["output_dir"], items)
    out = []
    for r in rows:
        v = vectors.get(f"c{r['id']}")
        if v is None:
            return {"ok": False, "reason": f"chunk {r['id']} missing from batch output",
                    "errors": errors}
        v = parse_vector(embed_common.vec(v))
        out.append({"id": r["id"], "kind": r["kind"],
                    "cosine_distance": cosine_distance(parse_vector(r["emb"]), v)})
    worst = max(out, key=lambda x: x["cosine_distance"])
    return {"ok": worst["cosine_distance"] <= TOLERANCE, "n": len(out),
            "max": worst["cosine_distance"], "worst": worst, "tolerance": TOLERANCE,
            "failed_items": len(errors), "job": job["job_name"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6, help="chunks sampled per kind")
    ap.add_argument("--batch", action="store_true", help="also check the Gemini Batch path")
    ap.add_argument("--batch-n", type=int, default=12, help="corpus chunks sent through a batch job")
    a = ap.parse_args()
    conn = psycopg.connect(row_factory=dict_row, connect_timeout=30, **embed_common.pg_params())
    res = check(conn, a.n)
    conn.close()
    print(json.dumps({k: v for k, v in res.items() if k != "worst"}, indent=2, default=str))
    if a.batch:
        conn = psycopg.connect(row_factory=dict_row, connect_timeout=30, **embed_common.pg_params())
        b = check_batch(conn, n=a.batch_n)
        conn.close()
        print("batch path:", json.dumps(b, indent=2, default=str))
        if not b.get("ok"):
            sys.exit(1)
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
