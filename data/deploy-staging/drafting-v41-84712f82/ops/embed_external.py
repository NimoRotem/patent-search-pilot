"""Chunk and embed the publications a live search DISCOVERED, so the next search can retrieve them.

WHY THIS EXISTS
---------------
external.materialise() inserts a publication the corpus did not hold the moment a search finds it
through the patent APIs. That row arrives with a title, an abstract, dates and CPC, which is
enough for the screen to judge it and the reader to fetch its text on demand. It is NOT enough to
retrieve it: with no chunks it has no vectors, so no dense channel can ever reach it again and the
only way to find it a second time is to run the same external query a second time.

Measured 2026-08-05: 1,854 externally-discovered rows, 0 embedded. One of them was `US 5,269,665`,
a document cited against the Schmalz application that this corpus had never held and that the
fan-out had just found. It was in the corpus and still unretrievable.

The weekly refresh (ops/refresh_corpus.sh -> incremental_ingest.py) drains the SAME queue with no
tier filter, so this closes on its own every Sunday. This script exists so it can be closed now,
without a BigQuery delta scan, and so the loop can be verified rather than assumed.

    python ops/embed_external.py                 # chunk + embed every unchunked external row
    python ops/embed_external.py --dry-run
    python ops/embed_external.py --tiers core,expanded,external   # or the whole queue
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import db  # noqa: E402
import incremental_ingest as inc  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="external",
                    help="comma-separated tiers to drain, or 'all'")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-embed", action="store_true",
                    help="chunk only; leave the vectors for the weekly pass")
    args = ap.parse_args()

    tiers = None if args.tiers.strip().lower() == "all" else \
        tuple(t.strip() for t in args.tiers.split(",") if t.strip())

    with db.cursor() as cur:
        cur.execute("SELECT tier, count(*) n FROM publications GROUP BY tier ORDER BY n DESC")
        print("[embed-external] corpus by tier:", {r["tier"]: r["n"] for r in cur.fetchall()})

    t0 = time.time()
    todo = inc.unchunked_publication_ids(min_pub_id=0, tiers=tiers)
    print(f"[embed-external] {len(todo):,} publications need chunking "
          f"(tiers={tiers or 'all'})")
    if not todo:
        print("[embed-external] nothing to do")
        return
    if args.dry_run:
        with db.cursor() as cur:
            cur.execute("SELECT publication_number, tier, left(coalesce(title,''),60) t "
                        "FROM publications WHERE id = ANY(%s) LIMIT 10", (todo[:10],))
            for r in cur.fetchall():
                print(f"    {r['publication_number']:22s} {str(r['tier']):9s} {r['t']}")
        return

    #  No staging table to read original-language abstracts from: these rows came from an API,
    #  not from a BigQuery delta, so the non-English original is whatever the source gave us and
    #  is already in `abstract`.
    n_chunks = inc.chunk_publications(todo, {}, log=print)
    print(f"[embed-external] inserted {n_chunks:,} chunks in {(time.time()-t0)/60:.1f} min")

    if args.skip_embed:
        print(f"[embed-external] embedding skipped; {inc.pending_embeddings():,} chunks pending")
        return
    n_emb = inc.embed_pending(log=print)
    print(f"[embed-external] embedded {n_emb:,} chunks")
    try:
        inc.verify_embeddings(log=print)
    except Exception as e:
        print(f"[embed-external] VERIFY FAILED: {e}")
        raise

    with db.cursor(autocommit=True) as cur:
        cur.execute("ANALYZE chunks")
        cur.execute("ANALYZE publications")
    left = inc.unchunked_publication_ids(min_pub_id=0, tiers=tiers)
    print(f"[embed-external] done in {(time.time()-t0)/60:.1f} min; "
          f"{len(left):,} still unchunked (expect 0 unless a row has no text at all)")


if __name__ == "__main__":
    main()
