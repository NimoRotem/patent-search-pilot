"""Load the acquisition cohort's description text into the TREATMENT database only, and publish
the funnel.

TWO SOURCES, AND THE CHEAPER ONE WAS HIDING IN PLAIN SIGHT
----------------------------------------------------------
  already_held  the publication ALREADY has paragraph rows in the database. Nothing to fetch: the
                two-tier chunker (src/incremental_ingest.py:305) deliberately skipped paragraphs
                when building the index, so the text is stored and simply never became searchable
                chunks. Corpus-wide this is 398,665 publications against 19,580 that were indexed.
  bigquery      no paragraphs held, so the description came from patents-public-data in
                ops/acquire_fetch.py. 5,362 documents, US only: the mirror carries no EP or WO
                description text, measured, 0 of 613 and 0 of 247.

WHY THIS TOUCHES 5434 AND NOTHING ELSE. 5433 is the control corpus and the live app. The whole
point of cloning was that the two arms differ by exactly one variable, so a stray write to the
control destroys the experiment silently. The port is asserted at startup, not assumed.

Every stage is counted, so a shortfall names itself rather than surfacing later as "acquisition
did not help".

    PGPORT=5434 python ops/acquire_load.py --batch 1 --limit 200
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "eval"))

import config  # noqa: E402
import db  # noqa: E402
import incremental_ingest as ing  # noqa: E402
from acquisition_cohort import FROZEN  # noqa: E402

TREATMENT_PORT = int(os.environ.get("TREATMENT_PORT", "5434"))
FETCHED = os.path.join(ROOT, "data", "acquire", "batch1.jsonl")
MIN_PARA_CHARS = 40


def guard_target():
    """Refuse to run against the control. A stray write there is silent and unrecoverable."""
    if int(config.PG["port"]) != TREATMENT_PORT:
        raise SystemExit(
            f"REFUSING: connected to port {config.PG['port']}, not the treatment instance "
            f"{TREATMENT_PORT}. 5433 is the control corpus and the live app; writing there would "
            f"make the control and treatment differ by more than the treatment. "
            f"Run with PGPORT={TREATMENT_PORT}.")
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM chunks WHERE kind='paragraph'")
        print(f"[guard] treatment on port {config.PG['port']}, "
              f"{cur.fetchone()['n']:,} paragraph chunks before load")


def split_paragraphs(text):
    out, buf = [], []
    for line in (text or "").replace("\r", "").split("\n"):
        s = line.strip()
        if not s:
            if buf:
                out.append(" ".join(buf))
                buf = []
            continue
        buf.append(s)
        if sum(len(x) for x in buf) > 2500:
            out.append(" ".join(buf))
            buf = []
    if buf:
        out.append(" ".join(buf))
    return [p for p in out if len(p) >= MIN_PARA_CHARS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--embed", action="store_true", help="also embed the new chunks")
    args = ap.parse_args()
    guard_target()

    rec = json.load(open(FROZEN))
    members = [m for m in rec["members"] if m["batch"] == args.batch]
    if args.limit:
        members = members[:args.limit]
    fetched = {}
    if os.path.exists(FETCHED):
        for line in open(FETCHED):
            d = json.loads(line)
            fetched[d["publication_number"]] = d
    print(f"cohort batch {args.batch}: {len(members):,} families; "
          f"{len(fetched):,} fetched documents on disk")

    f = collections.Counter()
    f["cohort_families"] = len(members)
    pubs = [m["fetch_pub"] for m in members if m["fetch_pub"]]

    #  Resolve to publication ids and find who already holds paragraphs.
    ids, has_para = {}, set()
    CH = 500
    for i in range(0, len(pubs), CH):
        b = pubs[i:i + CH]
        with db.cursor() as cur:
            cur.execute("""SELECT p.id, p.publication_number,
                                  EXISTS (SELECT 1 FROM paragraphs pa
                                           WHERE pa.publication_id = p.id) hp,
                                  EXISTS (SELECT 1 FROM chunks ch
                                           WHERE ch.publication_id = p.id
                                             AND ch.kind='paragraph') hc
                             FROM publications p
                            WHERE p.publication_number = ANY(%s)""", (b,))
            for r in cur.fetchall():
                ids[r["publication_number"]] = r["id"]
                if r["hp"]:
                    has_para.add(r["publication_number"])
                if r["hc"]:
                    f["already_indexed"] += 1
    f["resolved_in_corpus"] = len(ids)
    f["already_hold_paragraphs"] = len(has_para)

    #  Insert paragraphs for the ones we had to fetch.
    to_insert, inserted_rows = [], 0
    for p in pubs:
        if p in has_para or p not in ids:
            continue
        d = fetched.get(p)
        if not d:
            f["no_text_available"] += 1
            continue
        paras = split_paragraphs(d["description"])
        if not paras:
            f["fetched_but_unsplittable"] += 1
            continue
        to_insert.append((ids[p], paras, d.get("description_lang") or "en"))
    print(f"[load] {len(to_insert):,} publications need paragraph rows inserted")

    t0 = time.time()
    for n, (pid, paras, lang) in enumerate(to_insert, 1):
        with db.cursor(autocommit=True) as cur:
            cur.executemany(
                "INSERT INTO paragraphs (publication_id, para_no, lang, text) "
                "VALUES (%s, %s, %s, %s)",
                [(pid, f"a{j:04d}", lang, t) for j, t in enumerate(paras, 1)])
        inserted_rows += len(paras)
        f["paragraphs_inserted_pubs"] += 1
        if n % 500 == 0:
            print(f"   {n:,}/{len(to_insert):,} inserted ({time.time() - t0:.0f}s)", flush=True)
    f["paragraph_rows_inserted"] = inserted_rows

    #  Chunk everything in the cohort that now has paragraphs but no paragraph chunks.
    targets = []
    for p in pubs:
        pid = ids.get(p)
        if pid and (p in has_para or p in fetched):
            targets.append(pid)
    targets = sorted(set(targets))
    print(f"[chunk] chunking {len(targets):,} publications at FULL depth (two_tier=False)")
    before = _count_para_chunks()
    ing.chunk_publications(targets, two_tier=False, log=lambda m: print("   " + str(m), flush=True))
    after = _count_para_chunks()
    f["paragraph_chunks_created"] = after - before
    print(f"[chunk] paragraph chunks {before:,} -> {after:,}  (+{after - before:,})")

    if args.embed:
        import embed
        print("[embed] embedding new chunks...")
        embed.run()
        f["embedded_after"] = _count_embedded()

    print(f"\n{'stage':<32s}{'n':>10s}")
    for k in ("cohort_families", "resolved_in_corpus", "already_hold_paragraphs",
              "already_indexed", "no_text_available", "fetched_but_unsplittable",
              "paragraphs_inserted_pubs", "paragraph_rows_inserted",
              "paragraph_chunks_created", "embedded_after"):
        if k in f:
            print(f"{k:<32s}{f[k]:>10,}")

    out = os.path.join(ROOT, "data", "logs", "acquisition_load_funnel.json")
    json.dump({"cohort_version": rec["cohort_version"], "batch": args.batch,
               "port": config.PG["port"], "funnel": dict(f)}, open(out, "w"), indent=1)
    print(f"\nwritten {out}")


def _count_para_chunks():
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM chunks WHERE kind='paragraph'")
        return cur.fetchone()["n"]


def _count_embedded():
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM chunks WHERE embedding IS NOT NULL")
        return cur.fetchone()["n"]


if __name__ == "__main__":
    main()
