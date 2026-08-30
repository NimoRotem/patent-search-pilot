#!/usr/bin/env python
"""Operate the continuous full-text acquisition worker.

    ensure-schema                 apply sql/014 (all IF NOT EXISTS). Explicit, once, by a human.
    ensure-bucket                 create the GCS bucket if it is not there
    seed [--limit N]              pull the next slice of the niche manifest into the work pool
    run --shard i --of n          the worker. This is what Supervisor runs.
    status [--json]               pool state, per-provider outcomes, spend, rate
    quota                         what the paid providers say they have left, live

Seeding and running are separate on purpose. Seeding reads the corpus and the manifest; running
spends money. An operator who wants to see the pool before anything is fetched seeds, looks, and
only then starts the worker.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import config  # noqa: E402,F401  (loads .env)
from acquire import blobstore, ledger, manifest, providers, tasks, worker  # noqa: E402


def cmd_ensure_schema(args) -> int:
    missing = tasks.ensure_schema()
    if missing:
        print(f"still missing after apply: {missing}")
        return 1
    print(f"schema present: {', '.join(tasks.REQUIRED_TABLES)}")
    return 0


def cmd_ensure_bucket(args) -> int:
    bucket = args.bucket or blobstore.bucket_name()
    if not bucket:
        print("no bucket: set FULLTEXT_GCS_BUCKET or pass --bucket")
        return 2

    async def go():
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            return await blobstore.ensure_bucket(client, bucket, location=args.location)

    print(json.dumps(asyncio.run(go()), indent=2))
    return 0


def cmd_seed(args) -> int:
    tasks.require_schema()
    reader = manifest.open_reader(args.manifest)
    if args.reset:
        manifest.reset_cursor(reader.name)
    cursor = manifest.get_cursor(reader.name)
    total_offered = total_added = 0
    page = max(1, int(args.page))
    t0 = time.time()
    while True:
        entries, cursor, exhausted = reader.read(cursor, page)
        if entries:
            res = tasks.seed([e.as_dict() for e in entries], manifest=reader.name)
            total_offered += res["offered"]
            total_added += res["added"]
        manifest.set_cursor(reader.name, cursor, seeded_delta=len(entries))
        print(f"  cursor={cursor} offered={total_offered} added={total_added}", flush=True)
        if exhausted:
            break
        if args.limit and total_offered >= args.limit:
            break
    print(json.dumps({"reader": reader.name, "cursor": cursor, "offered": total_offered,
                      "added": total_added, "seconds": round(time.time() - t0, 1),
                      "pool": tasks.counts()}, indent=2))
    return 0


def cmd_run(args) -> int:
    out = worker.run(args.shard, args.of, max_publications=args.max,
                     max_batches=args.max_batches, dry_run=args.dry_run)
    print(json.dumps(out, indent=2))
    return 0


def cmd_status(args) -> int:
    tasks.require_schema()
    p = ledger.progress(minutes=args.minutes)
    if args.json:
        print(json.dumps(p, indent=2, default=str))
        return 0
    pool = p["pool"]
    total = sum(pool.values()) or 1
    print("pool:")
    for state in ("pending", "leased", "done", "missing", "failed", "skipped"):
        n = pool.get(state, 0)
        print(f"  {state:<9} {n:>9,}  {100.0 * n / total:5.1f}%")
    print(f"\nrate: {p['hits_per_hour']:,.0f} hits/hour "
          f"({p['hits_in_window']:,} in the last {p['window_minutes']} min); "
          f"{p['hits_total']:,} total")
    print(f"spend: {p['credits_total']:,.0f} credits, ${p['usd_total']:,.2f}")
    print("\nproviders:")
    print(f"  {'provider':<16}{'outcome':<10}{'n':>9}{'credits':>10}{'usd':>9}{'avg ms':>9}")
    for r in p["providers"]:
        print(f"  {r['provider']:<16}{r['outcome']:<10}{r['n']:>9,}{r['credits']:>10,.0f}"
              f"{r['usd']:>9,.2f}{r['avg_ms']:>9,}")
    print("\nbudgets:")
    for b in p["budgets"]:
        print(f"  {b['provider']:<16}{b['spent']:>10,.0f} / {b['cap']:<10,.0f} "
              f"left {b['left']:,.0f}")
    return 0


def cmd_quota(args) -> int:
    """What the vendors themselves say. Run this before any bulk pass: our ledger knows what THIS
    fetcher spent, not what the rest of the fleet spent out of the same account."""
    import httpx
    out = {}
    key = os.environ.get("SERPAPI_API_KEY", "") or os.environ.get("SERPAPI_KEY", "")
    if key:
        try:
            r = httpx.get("https://serpapi.com/account", params={"api_key": key}, timeout=30)
            j = r.json()
            out["serpapi"] = {k: j.get(k) for k in
                              ("plan_name", "searches_per_month", "total_searches_left",
                               "this_month_usage", "account_rate_limit_per_hour",
                               "plan_renewal_date")}
        except Exception as exc:
            out["serpapi"] = {"error": f"{type(exc).__name__}: {exc}"}
    sb = os.environ.get("SCRAPINGBEE_API_KEY", "")
    if sb:
        try:
            r = httpx.get("https://app.scrapingbee.com/api/v1/usage",
                          params={"api_key": sb}, timeout=30)
            j = r.json()
            j["left"] = int(j.get("max_api_credit", 0)) - int(j.get("used_api_credit", 0))
            j["pages_left"] = j["left"] // 15
            out["scrapingbee"] = j
        except Exception as exc:
            out["scrapingbee"] = {"error": f"{type(exc).__name__}: {exc}"}
    out["our_caps"] = providers.DEFAULT_CAPS
    try:
        out["our_spend"] = ledger.budget_state()
    except Exception as exc:
        out["our_spend"] = {"error": str(exc)[:160]}
    print(json.dumps(out, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ensure-schema").set_defaults(fn=cmd_ensure_schema)

    b = sub.add_parser("ensure-bucket")
    b.add_argument("--bucket", default="")
    b.add_argument("--location", default="US-CENTRAL1")
    b.set_defaults(fn=cmd_ensure_bucket)

    s = sub.add_parser("seed")
    s.add_argument("--manifest", default="", help="corpus-niche (default), or a path/glob of JSONL")
    s.add_argument("--limit", type=int, default=0, help="stop after roughly this many entries")
    s.add_argument("--page", type=int, default=2000)
    s.add_argument("--reset", action="store_true", help="start the manifest cursor over")
    s.set_defaults(fn=cmd_seed)

    r = sub.add_parser("run")
    r.add_argument("--shard", type=int, default=0)
    r.add_argument("--of", type=int, default=1)
    r.add_argument("--max", type=int, default=0, help="stop after this many publications")
    r.add_argument("--max-batches", type=int, default=0)
    r.add_argument("--dry-run", action="store_true", help="run the cascade, store nothing")
    r.set_defaults(fn=cmd_run)

    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    st.add_argument("--minutes", type=int, default=60)
    st.set_defaults(fn=cmd_status)

    sub.add_parser("quota").set_defaults(fn=cmd_quota)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
