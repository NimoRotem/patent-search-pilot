"""Batch-fetch the missing full text for the indexed field, most-cited documents first.

WHY THIS EXISTS (CITATION_RECALL_2026-08-03.md)
-----------------------------------------------
84% of this corpus is abstract-only, and a reference with no text cannot be ranked on evidence
however relevant it is: it can only be listed. Measured against a real examiner citation list,
almost every cited family that never reached the ranked top 50 was abstract-only here. The search
already fetches text on demand for the ~80 references it is about to read, but that is bounded by
quota and by the search's own latency, and it does not help retrieval because those claims are
never embedded.

This is the other half: a background pass over the FIELD, which persists the text AND embeds it,
so both the reading stage and the retrieval stage improve for every later search.

    publications in the 8 seed CPC branches ..... 81,890
    already have claims ......................... 16,614
    NO claims, the targets ...................... 65,276
    of those, cited by >= 1 corpus document ..... 18,801

ORDER: by how many documents in the corpus CITE it, descending, then oldest first. That is a
query-independent measure of the art people actually reach for, and it is what an examiner
citation list is made of. It matters because the budget is far smaller than the target set.

BUDGET: SerpApi is a fixed monthly allowance shared with live searches, so this refuses to start
without checking the account and always leaves a reserve. It is fully resumable: a publication
that already has claims is skipped, so re-running after the allowance renews simply continues.

    python ops/enrich_field.py --budget 9000            # fetch, then chunk and embed
    python ops/enrich_field.py --budget 9000 --dry-run  # show what it would do
    python ops/enrich_field.py --embed-only             # just chunk+embed what is already fetched
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import db  # noqa: E402
import enrich  # noqa: E402
from config import DATA, SEED_CPC  # noqa: E402

STATE = DATA / "enrich_field_state.json"
#  The account allows 3,000 requests/hour. Stay well under it: this is a background job sharing an
#  allowance with live searches, and being throttled mid-run is worse than being slow.
RATE_PER_HOUR = int(os.environ.get("ENRICH_RATE_PER_HOUR", "2400"))
#  Never spend the last of the monthly allowance: live searches enrich on demand too.
RESERVE = int(os.environ.get("ENRICH_RESERVE", "1200"))
#  Concurrent embedding batches. The Vertex quota is account-wide, so this is what has to stay
#  under it together with anything else embedding at the time.
EMBED_WORKERS = int(os.environ.get("ENRICH_EMBED_WORKERS", "10"))


def account():
    key = os.environ.get("SERPAPI_API_KEY") or enrich.SERP_KEY
    if not key:
        return None
    try:
        with urllib.request.urlopen(f"https://serpapi.com/account?api_key={key}",
                                    timeout=30) as fh:
            return json.load(fh)
    except Exception as exc:
        print(f"[enrich-field] could not read the SerpApi account: {type(exc).__name__} {exc}")
        return None


def targets(limit):
    """Publications in the field with no claims, most-cited first, then oldest first."""
    like = " OR ".join(["cl.symbol LIKE %s"] * len(SEED_CPC))
    pats = [h + "%" for h in SEED_CPC]
    sql = f"""
      WITH f AS (SELECT DISTINCT cl.publication_id pid FROM classifications cl WHERE {like}),
           t AS (SELECT f.pid, pu.publication_number pn, pu.publication_date pd
                 FROM f JOIN publications pu ON pu.id = f.pid
                 WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.publication_id = f.pid))
      SELECT t.pid, t.pn, t.pd,
             (SELECT count(*) FROM citations ci WHERE ci.dst_pub = t.pn) indeg
      FROM t
      ORDER BY indeg DESC, t.pd ASC NULLS LAST
      LIMIT %s"""
    with db.cursor() as cur:
        cur.execute(sql, pats + [limit])
        return [dict(r) for r in cur.fetchall()]


class Rate:
    """Simple global rate limiter: no more than `per_hour` acquisitions per rolling hour."""

    def __init__(self, per_hour):
        self.interval = 3600.0 / max(1, per_hour)
        self.lock = threading.Lock()
        self.next_at = time.monotonic()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next_at < now:
                self.next_at = now
            due = self.next_at
            self.next_at += self.interval
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def fetch(rows, workers, rate, on_tick=None):
    got = [0]
    done = [0]
    lock = threading.Lock()

    def one(r):
        rate.wait()
        try:
            res = enrich.enrich_publication(r["pn"], reembed=False)
            ok = bool(res and res.get("ok") and res.get("added_claims"))
        except Exception:
            ok = False
        with lock:
            done[0] += 1
            got[0] += 1 if ok else 0
            if on_tick and done[0] % 50 == 0:
                on_tick(done[0], got[0], len(rows))
        return ok

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, rows))
    return got[0], done[0]


def chunk_and_embed(pids, log=print):
    """Turn newly-fetched claims into embedded chunks, so RETRIEVAL improves too.

    The on-demand path inside a search deliberately skips this: it needs text, not vectors, and
    embedding inside a request would put a Vertex round-trip on the user's latency. A background
    pass has no such excuse, and without it the new text helps only the documents a search already
    decided to read.
    """
    #  PHASE A: create every chunk first. Pure database work, no model calls.
    ids = []
    t0 = time.time()
    for i, pid in enumerate(pids, 1):
        try:
            ids.extend(enrich.chunk_pub_claims(pid) or [])
        except Exception:
            continue
        if i % 1000 == 0:
            log(f"[enrich-field] chunked {i:,}/{len(pids):,} publications, {len(ids):,} chunks "
                f"({time.time() - t0:.0f}s)")
    log(f"[enrich-field] {len(ids):,} chunks to embed")
    if not ids:
        return 0

    #  PHASE B: embed them in parallel batches. One round-trip per publication measured out at a
    #  chunk a second, because a publication is only ~20 chunks and the call is latency-bound;
    #  batches of 200 across several workers is what the corpus embedder itself does.
    done = [0]
    lock = threading.Lock()
    batches = [ids[i:i + 200] for i in range(0, len(ids), 200)]

    def one(batch):
        try:
            n = enrich._embed_chunk_ids(batch)
        except Exception:
            n = 0
        with lock:
            done[0] += n
            if done[0] % 5000 < 200:
                log(f"[enrich-field] embedded {done[0]:,}/{len(ids):,} chunks "
                    f"({done[0] / max(time.time() - t0, 1):.0f}/sec)")
        return n

    with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as ex:
        list(ex.map(one, batches))
    log(f"[enrich-field] embedded {done[0]:,} chunks in {(time.time() - t0) / 60:.0f} min")
    return done[0]


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"fetched": 0, "gained": 0, "runs": []}


def save_state(st):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st, indent=1, default=str))
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=0,
                    help="maximum SerpApi calls to spend on this run")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--embed-only", action="store_true",
                    help="skip fetching; chunk and embed claims already in the database")
    args = ap.parse_args()

    st = load_state()
    if args.embed_only:
        with db.cursor() as cur:
            cur.execute("""SELECT DISTINCT c.publication_id pid FROM claims c
                           WHERE NOT EXISTS (SELECT 1 FROM chunks ch
                                             WHERE ch.ref_id = c.id AND ch.kind LIKE 'claim%%')""")
            pids = [r["pid"] for r in cur.fetchall()]
        print(f"[enrich-field] {len(pids):,} publications have claims with no chunks")
        if not args.dry_run:
            chunk_and_embed(pids)
        return

    acct = account()
    if acct:
        left = int(acct.get("total_searches_left") or 0)
        print(f"[enrich-field] SerpApi {acct.get('plan_name')}: {left:,} calls left this month "
              f"(reserve {RESERVE:,})")
        spendable = max(0, left - RESERVE)
    else:
        spendable = 0
        print("[enrich-field] no account reading; refusing to guess at the allowance")
    budget = min(args.budget or spendable, spendable)
    if budget <= 0:
        print("[enrich-field] nothing spendable; stopping")
        return

    rows = targets(budget)
    print(f"[enrich-field] {len(rows):,} targets selected, most-cited first")
    if rows:
        print(f"[enrich-field] citation in-degree of the selection: "
              f"max {rows[0]['indeg']}, min {rows[-1]['indeg']}")
        print(f"[enrich-field] at {RATE_PER_HOUR}/hour this run takes "
              f"~{len(rows) / RATE_PER_HOUR:.1f} hours")
    if args.dry_run:
        for r in rows[:10]:
            print(f"    {r['pn']:22s} cited by {r['indeg']:4d}  {str(r['pd'])[:10]}")
        return

    t0 = time.time()
    rate = Rate(RATE_PER_HOUR)

    def tick(done, got, total):
        el = time.time() - t0
        print(f"[enrich-field] {done:,}/{total:,} fetched, {got:,} gained claims, "
              f"{el / 60:.0f} min, {done / max(el, 1) * 3600:.0f}/hour", flush=True)

    got, done = fetch(rows, args.workers, rate, on_tick=tick)
    print(f"[enrich-field] fetched {done:,}, {got:,} gained claims in "
          f"{(time.time() - t0) / 60:.0f} min")

    pids = [r["pid"] for r in rows]
    with db.cursor() as cur:
        cur.execute("""SELECT DISTINCT c.publication_id pid FROM claims c
                       WHERE c.publication_id = ANY(%s)
                         AND NOT EXISTS (SELECT 1 FROM chunks ch
                                         WHERE ch.ref_id = c.id AND ch.kind LIKE 'claim%%')""",
                    (pids,))
        to_chunk = [r["pid"] for r in cur.fetchall()]
    print(f"[enrich-field] chunking and embedding {len(to_chunk):,} publications")
    n_chunks = chunk_and_embed(to_chunk)

    st["fetched"] += done
    st["gained"] += got
    st["runs"].append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "fetched": done, "gained": got, "chunks": n_chunks,
                       "minutes": round((time.time() - t0) / 60, 1)})
    save_state(st)
    print(f"[enrich-field] done: {got:,} publications gained text, {n_chunks:,} chunks embedded")


if __name__ == "__main__":
    main()
