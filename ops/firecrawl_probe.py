#!/usr/bin/env python
"""Measure whether the Firecrawl rung reaches anything the running cascade could not.

The `patentdata` session shipped a Firecrawl adapter and this cascade had none, so the adapter
was ported into `acquire.providers.FirecrawlProvider`. Porting it is not evidence that it is
worth a place in `DEFAULT_ORDER`, and two workers are running against the measured order. This
is the evidence.

METHOD. Take publications the pool has already retired to state 'missing': every rung ran, the
page was reached, and no rung found full text. Ask Firecrawl for exactly those. A hit means
Firecrawl reads something the other Google Patents routes do not and the rung earns a place. No
hit means it is standby redundancy for when `serp_self` is cut off and ScrapingBee's balance is
spent, which is worth having and is not worth a place in the default order.

It writes nothing: no pool state, no docstore, no ledger event. It reads the pool and spends
Firecrawl credits, which it reports.

RESULT, 2026-08-22, 10 credits. Firecrawl reaches patents.google.com and returns the Angular
SHELL: 596,384 bytes with no `itemprop=` attribute anywhere, so none of the sections that carry
the claims and the description. Nine publications, 0 claim characters and 0 description
characters each, including three the pool already holds in full through another rung, which is
the control that says the zero is Firecrawl's and not the documents'. `waitFor: 5000`,
`formats: ["markdown"]` and `proxy: "stealth"` all return the same shell. The rung is registered
and stays out of `DEFAULT_ORDER`. Rerun this before anyone proposes it again.
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


def sample(limit: int, state: str) -> list:
    import db
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute("SELECT publication_number, country FROM fulltext_fetch_task "
                    "WHERE state = %s ORDER BY publication_number LIMIT %s", (state, limit))
        return [dict(r) for r in cur.fetchall() or []]


async def probe(rows) -> dict:
    import httpx
    from acquire import providers
    rung = providers.FirecrawlProvider()
    ok, why = rung.available()
    if not ok:
        return {"error": why}
    out, credits = [], 0.0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for row in rows:
            pub = row["publication_number"]
            t0 = time.time()
            try:
                res = await rung.gate.run(lambda: rung.fetch(pub, client))
            except Exception as exc:                       # noqa: BLE001
                out.append({"publication_number": pub, "outcome": "error",
                            "detail": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            ms = round((time.time() - t0) * 1000)
            if res is None:
                out.append({"publication_number": pub, "outcome": "none", "ms": ms})
                continue
            credits += res.credits
            out.append({"publication_number": pub,
                        "outcome": ("hit" if res.complete()
                                    else "reached" if res.reached else "unreached"),
                        "claims_chars": len(res.claims), "desc_chars": len(res.description),
                        "credits": res.credits, "ms": ms})
    return {"attempts": out, "credits_spent": credits}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=12, help="publications to try, one credit each")
    ap.add_argument("--state", default="missing", help="the pool state to sample from")
    args = ap.parse_args(argv)
    rows = sample(args.limit, args.state)
    if not rows:
        print(json.dumps({"sampled": 0, "state": args.state}, indent=2))
        return 0
    result = asyncio.run(probe(rows))
    attempts = result.get("attempts") or []
    tally = {}
    for a in attempts:
        tally[a["outcome"]] = tally.get(a["outcome"], 0) + 1
    print(json.dumps({"state": args.state, "sampled": len(rows), "outcomes": tally,
                      "credits_spent": result.get("credits_spent"),
                      "error": result.get("error"), "attempts": attempts},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
