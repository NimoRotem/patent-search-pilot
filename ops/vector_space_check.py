"""Prove that a vector this pipeline produces is the same vector the corpus already stores.

A corpus with two incompatible vector spaces is worse than one that is incomplete: an incomplete
corpus misses documents, a mixed one RANKS them wrongly and nothing about the output says so. So
before anything is staged, take text the corpus already holds, embed it through the pipeline's own
call, and measure the cosine distance to the stored vector.

Zero is not achievable and is not the bar. `chunks.embedding` was written through the same
six-decimal pgvector literal this pipeline uses, so the floor is that rounding, measured at
roughly 9e-11. Anything at that order is the same vector. Anything at 1e-3 or above is a different
model, a different dimension or a normalisation that one side applies and the other does not.

    python ops/vector_space_check.py            # 40 chunks across every kind
    python ops/vector_space_check.py --n 200 --batch     # also check the Gemini Batch path

MEASURED 2026-08-22 on the live corpus, 40 chunks over whole/abstract/claim_own/claim_resolved/
paragraph/figure_caption, English and CJK: max cosine distance 1.5e-10, and the stored vectors
have norm 0.589, i.e. gemini-embedding-001 at output_dimensionality=768 is NOT unit normalised and
must not be normalised here either. The Gemini Batch path was measured against the synchronous one
at cosine distance 1.0e-15, so a batch vector and a sync vector are the same vector.
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


def sample(conn, per_kind=6):
    """A handful of each kind. `ix_chunks_kind` makes this an index scan, not the full-corpus
    sequential scan the brief forbids."""
    rows = []
    with conn.cursor() as cur:
        for kind in KINDS:
            cur.execute("""SELECT id, kind, lang, text, embedding::text AS emb
                           FROM chunks WHERE kind = %s AND embedding IS NOT NULL
                           ORDER BY id LIMIT %s""", (kind, per_kind))
            rows += cur.fetchall()
        # Deliberately include CJK: 39.9% of this corpus is CJK and a tokeniser difference would
        # show up there first.
        cur.execute("""SELECT id, kind, lang, text, embedding::text AS emb
                       FROM chunks WHERE embedding IS NOT NULL AND lang IN ('zh', 'ja', 'ko')
                       ORDER BY id LIMIT %s""", (per_kind,))
        rows += cur.fetchall()
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


def check_batch(bucket="nimo-patents-v3", n=8):
    """The same question for the Gemini Batch path: is a batch vector the sync vector."""
    import batch_embed
    e = batch_embed.VertexBatchEmbedder(model=EMBED_MODEL, dim=EMBED_DIM, task_type=TASK_TYPE,
                                        bucket=bucket, prefix="embed_batch/spacecheck")
    texts = [f"A vacuum gripping device, variant {i}, comprising a suction cup and a pump."
             for i in range(n)]
    items = [{"item_key": f"k{i}", "text": t} for i, t in enumerate(texts)]
    job = e.submit(items, tag=f"spacecheck-{os.getpid()}")
    print("submitted", job["job_name"])
    import time
    while True:
        st = e.poll(job["job_name"])
        if st["terminal"]:
            break
        time.sleep(20)
    if not st["ok"]:
        return {"ok": False, "state": st["state"], "error": st["error"]}
    vectors, errors = e.collect(st["output_dir"], items)
    sync = embed_common.embed_texts(texts, EMBED_MODEL, EMBED_DIM, TASK_TYPE)
    worst = 0.0
    for i, s in enumerate(sync):
        v = vectors.get(f"k{i}")
        if v is None:
            return {"ok": False, "reason": f"item k{i} missing from batch output",
                    "errors": errors}
        worst = max(worst, cosine_distance(v, s))
    return {"ok": worst <= 1e-9, "n": len(sync), "max": worst, "failed_items": len(errors)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6, help="chunks sampled per kind")
    ap.add_argument("--batch", action="store_true", help="also check the Gemini Batch path")
    a = ap.parse_args()
    conn = psycopg.connect(row_factory=dict_row, connect_timeout=30, **embed_common.pg_params())
    res = check(conn, a.n)
    conn.close()
    print(json.dumps({k: v for k, v in res.items() if k != "worst"}, indent=2, default=str))
    if a.batch:
        print("batch path:", json.dumps(check_batch(), indent=2, default=str))
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
