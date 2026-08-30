"""Full-text acquisition. One entry point, cheapest source first, kept forever.

Ported from patents-app `fulltext.py` (the ladder that ended the amnesiac
engine). The engine used to buy every full record from SerpApi at ~$0.0092 each
— $1.10 of a $1.65 search — and then forget it. Two things were wrong with
that. The text is largely available free from sources we already hold
credentials for, and nothing was ever kept, so the next search in the same
field bought the same documents again.

THE LADDER, cheapest first:

  0. docstore          already ours. Free, instant, and the reason a second search
                       in a technical field costs a fraction of the first.
  1. PQAI              US only, but free AND NOT QUOTA-COUNTED (its search has a
                       monthly cap; /patents/{pn} does not). Verified: full claims
                       and description for US, nothing at all for EP/WO/CN/DE/JP.
  2. EPO OPS           EP and WO only, free inside the 4 GB/week tier — order of
                       200,000 documents a week at ~20 KB each. A published quota,
                       not a scrape: it will not blacklist us.
  3. Google direct     every jurisdiction, in English, free — but rate-limited far
                       harder than it first appears (see gpatents_direct:
                       ~80 requests bought a whole-IP 503 lasting minutes). Used
                       gently, for what steps 1-2 cannot cover, and it self-disables.
  3b. ScrapingBee      the scale channel: the same Google Patents pages through
                       their proxies (their IP takes the rate limit, 100 slots).
  4. HimmPat           CN/JP/KR English translations, metered trial key, so it is
                       gated on its own spend ledger and asked last among the free-ish.
  5. SerpApi           paid, $0.0092/document, capped per run. The backstop that
                       makes every step above optional rather than load-bearing.

Everything acquired at any step is written back to the docstore, so the ladder
gets shorter every time the engine runs.

Port note: the docstore is Postgres here; if Postgres is unreachable the ladder
degrades to "no cache" (rung 0 skipped, nothing persisted) and says so in
`warnings` instead of failing the whole fetch — App A's SQLite could not
disappear, the pilot's PG can (e.g. running the suite on another host).
"""
from __future__ import annotations

import asyncio
import os

import httpx

from . import docstore
from .schema import canonical_pub, country_of

# A document counts as "read in full" once we hold this much description. Below it
# we have a stub — an abstract, or a filing receipt with no body — and should try
# the next source rather than call it done.
MIN_DESC_CHARS = int(os.environ.get("PATENTS_MIN_DESC_CHARS", "800"))
MIN_CLAIMS_CHARS = int(os.environ.get("PATENTS_MIN_CLAIMS_CHARS", "200"))
# Paid fallback ceiling per search. This is the only line in the ladder that costs
# money, so it is the only one with a hard cap.
SERP_FULLTEXT_CAP = int(os.environ.get("PATENTS_SERP_FULLTEXT_CAP", "25"))
# ScrapingBee pages per search. 15 credits each against ~912,000 remaining, so this
# is a throughput decision, not a money one — it is what lets a search read
# thousands of documents in full instead of dozens.
SB_CAP = int(os.environ.get("PATENTS_SB_FULLTEXT_CAP", "900"))
PQAI_CONCURRENCY = int(os.environ.get("PATENTS_PQAI_FT_CONCURRENCY", "12"))
OPS_CONCURRENCY = int(os.environ.get("PATENTS_OPS_FT_CONCURRENCY", "6"))


def _complete(rec: dict) -> bool:
    return (len(rec.get("description") or "") >= MIN_DESC_CHARS
            or len(rec.get("claims") or "") >= MIN_CLAIMS_CHARS)


class Acquisition:
    """Per-search acquisition run. Holds the paid budget and the tally."""

    def __init__(self, adapters: dict):
        self.adapters = adapters
        self.serp_spent = 0
        self.sb_spent = 0
        self.stats: dict[str, int] = {}
        self.warnings: list[str] = []
        self._docstore_dead = False

    def _tally(self, source: str, n: int = 1) -> None:
        self.stats[source] = self.stats.get(source, 0) + n

    async def fetch(self, pubs: list, client: httpx.AsyncClient,
                    *, want=None) -> dict:
        """Full text for as many of `pubs` as the ladder can supply.

        Returns {pub_number: record}. Records are also persisted, so the caller
        never has to think about caching and the next run starts further along.
        """
        pubs = [canonical_pub(p) for p in pubs if p]
        pubs = list(dict.fromkeys([p for p in pubs if p]))
        if want:
            pubs = pubs[:want]
        if not pubs:
            return {}

        out: dict[str, dict] = {}

        # 0. what we already own -------------------------------------------
        try:
            held = await docstore.have(pubs)
        except Exception as exc:
            held = {}
            self._docstore_dead = True
            self.warnings.append(
                f"docstore unreachable ({type(exc).__name__}) — running without the "
                f"local corpus: every document is re-fetched and nothing is persisted")
        cached = [p for p in pubs
                  if held.get(p, {}).get("desc_chars", 0) >= MIN_DESC_CHARS
                  or held.get(p, {}).get("claims_chars", 0) >= MIN_CLAIMS_CHARS]
        for p in cached:
            try:
                rec = await docstore.get(p)
            except Exception:
                rec = None
            if rec:
                out[p] = rec
        self._tally("docstore", len(out))
        todo = [p for p in pubs if p not in out]
        if not todo:
            return out

        # 1-3. free sources, routed by where the document actually lives ----
        for step in (self._pqai, self._ops, self._google, self._scrapingbee):
            if not todo:
                break
            got = await step(todo, client)
            for pn, rec in got.items():
                if _complete(rec):
                    out[pn] = rec
            todo = [p for p in todo if p not in out]

        # 4. HimmPat for the Asian documents nothing else reached -----------
        if todo:
            got = await self._himmpat(todo, client)
            for pn, rec in got.items():
                if _complete(rec):
                    out[pn] = rec
            todo = [p for p in todo if p not in out]

        # 5. paid backstop ---------------------------------------------------
        if todo:
            got = await self._serpapi(todo, client)
            for pn, rec in got.items():
                if _complete(rec):
                    out[pn] = rec
            todo = [p for p in todo if p not in out]

        if todo:
            self._tally("unavailable", len(todo))
        return out

    # -- individual rungs ---------------------------------------------------
    async def _persist(self, recs: dict, source: str) -> None:
        for pn, rec in recs.items():
            rec.setdefault("source", source)
            rec.setdefault("pub_number", pn)
        if recs and not self._docstore_dead:
            try:
                await docstore.put_many(recs)
            except Exception:
                self._docstore_dead = True

    async def _pqai(self, pubs: list, client) -> dict:
        a = self.adapters.get("pqai")
        targets = [p for p in pubs if country_of(p) == "US"]
        if not a or not a.enabled() or not targets:
            return {}
        sem = asyncio.Semaphore(PQAI_CONCURRENCY)

        async def one(pn):
            async with sem:
                try:
                    d = await a.details(pn, client)
                except Exception:
                    return pn, {}
                if not d:
                    return pn, {}
                return pn, {"claims": d.get("claims", ""), "description": d.get("description", ""),
                            "abstract": d.get("abstract", ""),
                            "claims_lang": "en", "desc_lang": "en"}

        recs = {pn: r for pn, r in await asyncio.gather(*[one(p) for p in targets]) if r}
        recs = {k: v for k, v in recs.items() if _complete(v)}
        await self._persist(recs, "pqai")
        self._tally("pqai", len(recs))
        return recs

    async def _ops(self, pubs: list, client) -> dict:
        a = self.adapters.get("epo_ops")
        targets = [p for p in pubs if country_of(p) in ("EP", "WO")]
        if not a or not a.enabled() or not targets or not hasattr(a, "fulltext"):
            return {}
        sem = asyncio.Semaphore(OPS_CONCURRENCY)

        async def one(pn):
            async with sem:
                try:
                    return pn, await a.fulltext(pn, client)
                except Exception:
                    return pn, {}

        recs = {pn: r for pn, r in await asyncio.gather(*[one(p) for p in targets]) if r}
        recs = {k: v for k, v in recs.items() if _complete(v)}
        await self._persist(recs, "epo_ops")
        self._tally("epo_ops", len(recs))
        return recs

    async def _google(self, pubs: list, client) -> dict:
        a = self.adapters.get("gpatents_direct")
        if not a or not a.enabled() or not pubs:
            return {}
        # Deliberately sequential-ish and capped: this rung is the one that can get
        # the box blocked, and the adapter paces itself. Take the documents no other
        # free source covers first (DE, CN, JP, KR and the rest).
        from . import gpatents_direct as G
        order = sorted(pubs, key=lambda p: country_of(p) in ("US", "EP", "WO"))
        cap = int(os.environ.get("PATENTS_GOOGLE_FULLTEXT_CAP", "40"))
        recs: dict[str, dict] = {}
        for pn in order[:cap]:
            if not G.available():
                self.warnings.append(
                    f"google patents direct went into cooldown ({G.block_reason()}) with "
                    f"{len(order[:cap]) - len(recs)} document(s) still to fetch — the paid "
                    f"fallback picks these up")
                break
            try:
                d = await a.details(pn, client)
            except Exception:
                continue
            if d and _complete(d):
                recs[pn] = d
        await self._persist(recs, "gpatents_direct")
        self._tally("gpatents_direct", len(recs))
        return recs

    async def _scrapingbee(self, pubs: list, client) -> dict:
        """The scale channel: Google Patents through ScrapingBee's proxies.

        Same page and the same English-translated itemprop sections as fetching
        Google ourselves, but it is their IP being rate-limited rather than this
        box, and they hold 100 concurrent slots. ScrapingBee classes
        patents.google.com as Google, so it demands custom_google=True and bills
        15 credits a page rather than 1 — which still leaves roughly 60,000 pages
        on the current balance, i.e. this is what makes reading thousands of full
        documents a real option instead of an aspiration.
        """
        key = os.environ.get("SCRAPINGBEE_API_KEY", "")
        if not key or not pubs:
            return {}
        room = max(0, SB_CAP - self.sb_spent)
        targets = pubs[:room]
        if not targets:
            if pubs:
                self.warnings.append(
                    f"ScrapingBee full-text budget spent ({self.sb_spent}/{SB_CAP} pages "
                    f"this search); {len(pubs)} document(s) fell through to the paid path")
            return {}
        from .gpatents_direct import parse_document
        sem = asyncio.Semaphore(int(os.environ.get("PATENTS_SB_CONCURRENCY", "16")))

        async def one(pn):
            async with sem:
                try:
                    r = await client.get(
                        "https://app.scrapingbee.com/api/v1/",
                        params={"api_key": key,
                                "url": f"https://patents.google.com/patent/{pn}/en",
                                "custom_google": "True", "render_js": "False"},
                        timeout=90)
                except Exception:
                    return pn, {}
                if r.status_code != 200:
                    return pn, {}
                try:
                    return pn, parse_document(r.text, pn)
                except Exception:
                    return pn, {}

        got = await asyncio.gather(*[one(p) for p in targets])
        self.sb_spent += len(targets)
        recs = {pn: d for pn, d in got if d and _complete(d)}
        await self._persist(recs, "scrapingbee_gpatents")
        self._tally("scrapingbee", len(recs))
        return recs

    async def _himmpat(self, pubs: list, client) -> dict:
        a = self.adapters.get("himmpat")
        targets = [p for p in pubs if country_of(p) in ("CN", "JP", "KR", "TW")]
        if not a or not a.enabled() or not targets:
            return {}
        recs: dict[str, dict] = {}
        for pn in targets[:int(os.environ.get("PATENTS_HIMMPAT_FULLTEXT_CAP", "10"))]:
            try:
                d = await a.details(pn, client)
            except Exception:
                continue
            if not d:
                continue
            recs[pn] = {"claims": d.get("claims", ""), "description": d.get("description", ""),
                        "abstract": d.get("abstract", ""), "title": d.get("title", ""),
                        "claims_lang": d.get("claims_lang", ""), "desc_lang": d.get("description_lang", "")}
        recs = {k: v for k, v in recs.items() if _complete(v)}
        await self._persist(recs, "himmpat")
        self._tally("himmpat", len(recs))
        return recs

    async def _serpapi(self, pubs: list, client) -> dict:
        a = self.adapters.get("serpapi_gpatents")
        if not a or not a.enabled():
            return {}
        room = max(0, SERP_FULLTEXT_CAP - self.serp_spent)
        targets = pubs[:room]
        if not targets:
            if pubs:
                self.warnings.append(
                    f"paid full-text budget spent ({self.serp_spent}/{SERP_FULLTEXT_CAP}); "
                    f"{len(pubs)} document(s) read from abstract only")
            return {}
        sem = asyncio.Semaphore(int(os.environ.get("PATENTS_SERP_FT_CONCURRENCY", "8")))

        async def one(pn):
            async with sem:
                try:
                    return pn, await a.details(pn, client)
                except Exception:
                    return pn, {}

        got = await asyncio.gather(*[one(p) for p in targets])
        self.serp_spent += len(targets)
        recs = {}
        for pn, d in got:
            if not d:
                continue
            recs[pn] = {
                "claims": d.get("claims", ""), "description": d.get("description", ""),
                "abstract": d.get("abstract", ""), "title": d.get("title", ""),
                "assignee": d.get("assignee", ""), "inventors": d.get("inventors", []),
                "cpc": d.get("cpc", []), "images": d.get("images", []), "pdf": d.get("pdf", ""),
                "family": d.get("family", []), "backward": d.get("patent_citations", []),
                "forward": d.get("cited_by", []),
                "legal_state": d.get("legal_state", ""), "legal_status": d.get("legal_status", ""),
                "claims_lang": "", "desc_lang": "",
            }
        recs = {k: v for k, v in recs.items() if _complete(v)}
        await self._persist(recs, "serpapi_gpatents")
        self._tally("serpapi_paid", len(recs))
        return recs

    def summary(self) -> dict:
        return {"by_source": dict(self.stats),
                "paid_documents": self.serp_spent,
                "paid_usd": round(self.serp_spent * 0.00917, 4),
                "scrapingbee_pages": self.sb_spent,
                "scrapingbee_credits": self.sb_spent * 15,
                "warnings": list(self.warnings)}
