"""Discard one parse-and-embed run so it can be done again from the sources.

WHY THIS EXISTS. The interrupted run of 2026-08-22 staged 51 documents, and every one of them
carries a NEGATIVE surrogate publication id. All 51 are in `publications` under the hyphenated
spelling; the run joined on the compact one and matched nothing (see `src/corpus_pub.py`). Rows
staged that way are not merely mislabelled: workstream F cannot join one back to a real
publication, and the disjointness check against `patents-desc-backfill` was asked about ids that
do not exist and answered "none of them". Correcting the resolution does not correct the rows,
because `parsed_doc_ledger` is keyed on the source object and a document already ledgered is never
parsed again. So the run has to be discarded and redone.

WHAT IT WILL AND WILL NOT DELETE. Only this pipeline's own tables, and inside `chunks_stage_v3`
only rows carrying THIS release tag and `ref_id IS NULL`. `ops/desc_backfill.py`'s 8.8M rows carry
`v3-desc-backfill-*` and `ref_id = paragraphs.id`; both halves of that filter have to be wrong at
once for this to touch one of them, and the count is printed and confirmed before anything runs.

    python ops/parsed_reset.py                 # say what would go
    python ops/parsed_reset.py --yes           # do it
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parsed_embed as pe                                            # noqa: E402


def plan(conn, release):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) c FROM chunks_stage_v3 "
                    "WHERE corpus_release = %s AND ref_id IS NULL", (release,))
        stage = cur.fetchone()["c"]
        cur.execute("SELECT count(*) c FROM chunks_stage_v3 "
                    "WHERE corpus_release = %s AND ref_id IS NOT NULL", (release,))
        foreign = cur.fetchone()["c"]
        cur.execute("SELECT count(*) c FROM parsed_doc_ledger WHERE corpus_release = %s",
                    (release,))
        ledger = cur.fetchone()["c"]
        cur.execute("SELECT count(*) c FROM parsed_embed_item")
        items = cur.fetchone()["c"]
        cur.execute("SELECT count(*) c FROM parsed_embed_done")
        done = cur.fetchone()["c"]
        cur.execute("SELECT count(*) c FROM parsed_stage_pub")
        surrogates = cur.fetchone()["c"]
    return {"release": release, "stage_rows": stage, "stage_rows_with_ref_id": foreign,
            "ledger_rows": ledger, "queue_rows": items, "done_receipts": done,
            "surrogate_pubs": surrogates}


def reset(conn, release):
    """Everything in one transaction: a half-reset leaves a ledger row with no chunks behind it,
    which is the one state nothing downstream can distinguish from a document that legitimately
    produced none."""
    with conn.cursor() as cur:
        #  ref_id IS NULL is not decoration. It is the second half of the filter that makes it
        #  impossible for a mistyped release tag to reach the description backfill's rows.
        cur.execute("DELETE FROM chunks_stage_v3 WHERE corpus_release = %s AND ref_id IS NULL",
                    (release,))
        cur.execute("DELETE FROM parsed_doc_ledger WHERE corpus_release = %s", (release,))
        cur.execute("DELETE FROM parsed_embed_item")
        cur.execute("DELETE FROM parsed_embed_done")
        cur.execute("DELETE FROM parsed_stage_pub")
        cur.execute("UPDATE parsed_embed_progress SET watermarks = '{}'::jsonb, docs_seen = 0, "
                    "docs_staged = 0, docs_rejected = 0, docs_skipped = 0, rows_done = 0, "
                    "chars_done = 0, api_calls = 0, batch_jobs = 0, updated_at = now()")
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default=pe.CORPUS_RELEASE)
    ap.add_argument("--yes", action="store_true", help="actually delete")
    a = ap.parse_args()
    conn = pe._connect()
    pe.ensure_schema(conn)
    p = plan(conn, a.release)
    for k, v in p.items():
        print(f"  {k}: {v}")
    if not a.yes:
        print("\ndry run. pass --yes to delete.")
        return
    reset(conn, a.release)
    print("\nafter:")
    for k, v in plan(conn, a.release).items():
        print(f"  {k}: {v}")
    conn.close()


if __name__ == "__main__":
    main()
