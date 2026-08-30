"""What a search costs, per model and per API, priced from token and call counts.

WHY THIS EXISTS. The pipeline reported "53,884,326 tokens" for a search and nothing else. That is
not a number anybody can act on: the models in the pool differ by more than 30x per token, half the
prompt tokens are served from a provider-side cache at a fraction of the price, and the external
patent APIs are billed per CALL, not per token, so they were invisible in a token count entirely.

EVERY PRICE HERE IS A QUOTED RATE WITH A SOURCE AND A DATE, and it will go stale. `PRICED_ON` is
printed next to any figure this module produces, so a number nobody has revisited says so rather
than looking freshly measured. An unknown model prices at zero and is listed by name in
`unpriced`, because silently pricing something at nothing is how a bill goes missing.
"""
from __future__ import annotations

PRICED_ON = "2026-08-27"

M = 1_000_000.0

#  USD per million tokens: (input, cached input, output).
#
#  Anthropic first-party rates, confirmed 2026-08-27 against the current model table.
#  Cached input is the documented ~0.1x cache-read multiplier; a cache WRITE is ~1.25x and is not
#  separated here because the pool does not report writes.
#
#  Vertex AI (Google) rates for the Gemini models, standard context. Gemini caching is billed at
#  0.25x, which is the multiplier `model_pool._note_cached` was written against.
#
#  muse-spark-1.2 from the advisor's own record for meta-model-api-muse: $1.25 in, $0.15 cached in,
#  $4.25 out, standard tier.
MODEL_PRICES = {
    "gemini-2.5-flash":            (0.30, 0.075, 2.50),
    "gemini-2.5-pro":              (1.25, 0.3125, 10.00),
    "claude-haiku-4-5-20251001":   (1.00, 0.10, 5.00),
    "claude-sonnet-4-5-20250929":  (3.00, 0.30, 15.00),
    "claude-sonnet-5":             (3.00, 0.30, 15.00),
    "muse-spark-1.2":              (1.25, 0.15, 4.25),
}

#  USD per CALL for the metered non-LLM services. A patent API is billed per request, so it cannot
#  be derived from tokens and was missing from every spend figure until now.
#
#  serpapi: the Big Data Plan is $250/month for 30,000 searches = $0.00833 a search.
#  scrapingbee: Freelance plan, 1,000,000 credits for $99; a Google Patents page costs 25 credits
#  with JS rendering off, so $0.002475 a fetch.
#  himmpat: metered CN/JP/KR full text, quoted per document.
#  voyage-4-lite embeddings are per token, not per call, and are priced separately below.
#  Everything else in the cascade is free: PQAI, the EPO's OPS, BigQuery under the byte cap, and
#  documents we already hold.
CALL_PRICES = {
    "serpapi": 0.00833,
    "serpapi_gpatents": 0.00833,
    "scrapingbee": 0.002475,
    "himmpat": 0.01,
    "pqai": 0.0,
    "epo_ops": 0.0,
    "uspto": 0.0,
    "bigquery_gpatents": 0.0,
    "corpus": 0.0,
    "serp_self": 0.0,
}

#  USD per million tokens embedded. voyage-4-lite through the paid MongoDB sync route; the batch
#  route is $0.0134 and is not what a live query uses.
EMBED_PRICE_PER_M = 0.02

FREE_NOTE = "free"


def price_tokens(model: str, prompt: int = 0, cached: int = 0, completion: int = 0) -> float:
    """USD for one model's token usage. `cached` is a SUBSET of `prompt`, billed at the cache rate.

    An unknown model returns 0.0; the caller is expected to surface it as unpriced rather than
    fold a guess into a total.
    """
    rate = MODEL_PRICES.get(model)
    if not rate:
        return 0.0
    p_in, p_cached, p_out = rate
    fresh = max(0, int(prompt or 0) - int(cached or 0))
    return (fresh * p_in + int(cached or 0) * p_cached + int(completion or 0) * p_out) / M


def price_calls(source: str, calls: int) -> float:
    return float(CALL_PRICES.get(source, 0.0)) * max(0, int(calls or 0))


def breakdown(providers: dict, models: dict | None = None, external: dict | None = None,
              embed_tokens: int = 0) -> dict:
    """-> {"total_usd", "lines": [...], "unpriced": [...], "priced_on"}.

    `providers` is `model_pool.stats()`-shaped, differenced for this search:
        {provider_name: {calls, prompt_tokens, completion_tokens, cached_tokens}}
    `models` maps provider name -> model id. `external` is {source_name: calls}.

    A line is what one model or one API cost, and why. The lines are what a reader clicks through
    to; the total is their sum and nothing else, so the two can never disagree.
    """
    models = models or {}
    lines, unpriced, total = [], [], 0.0

    for name, u in sorted((providers or {}).items()):
        calls = int((u or {}).get("calls") or 0)
        if not calls:
            continue
        model = models.get(name) or name
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
        cached = min(int(u.get("cached_tokens") or 0), pt)
        errors = int((u or {}).get("errors") or 0)
        usd = price_tokens(model, pt, cached, ct)
        if model not in MODEL_PRICES:
            unpriced.append(model)
        #  A PROVIDER THAT ONLY FAILED COSTS NOTHING, and its line has to say that rather than
        #  read as "24 calls, 0 tokens", which looks like a metering bug. This is what a
        #  spend-capped key looks like on the bill: attempted, refused, free.
        if errors >= calls and not pt and not ct:
            detail = "%s call%s, all refused , nothing billed" % (f"{calls:,}",
                                                                  "" if calls == 1 else "s")
        else:
            detail = "%s calls, %s prompt tokens (%s from cache) + %s output" % (
                f"{calls:,}", f"{pt:,}", f"{cached:,}", f"{ct:,}")
            if errors:
                detail += ", %s failed" % f"{errors:,}"
        lines.append({
            "kind": "model", "name": name, "model": model, "calls": calls, "errors": errors,
            "prompt_tokens": pt, "cached_tokens": cached, "completion_tokens": ct,
            "usd": round(usd, 4), "detail": detail,
        })
        total += usd

    for source, calls in sorted((external or {}).items()):
        calls = int(calls or 0)
        if not calls:
            continue
        usd = price_calls(source, calls)
        if source not in CALL_PRICES:
            unpriced.append(source)
        lines.append({
            "kind": "api", "name": source, "model": "", "calls": calls, "usd": round(usd, 4),
            "detail": "%s call%s%s" % (f"{calls:,}", "" if calls == 1 else "s",
                                       "" if usd else ", " + FREE_NOTE),
        })
        total += usd

    if embed_tokens:
        usd = int(embed_tokens) * EMBED_PRICE_PER_M / M
        lines.append({"kind": "embed", "name": "voyage-4-lite", "model": "voyage-4-lite",
                      "calls": 0, "usd": round(usd, 4),
                      "detail": "%s tokens embedded" % f"{int(embed_tokens):,}"})
        total += usd

    lines.sort(key=lambda d: -d["usd"])
    return {"total_usd": round(total, 4), "lines": lines,
            "unpriced": sorted(set(unpriced)), "priced_on": PRICED_ON}


def fmt(usd) -> str:
    """A dollar figure a person reads, not a float. Sub-cent work is real and must not read $0.00."""
    try:
        v = float(usd or 0)
    except (TypeError, ValueError):
        return "$0.00"
    if v <= 0:
        return "$0.00"
    if v < 0.01:
        return "<$0.01"
    if v < 10:
        return "$%.2f" % v
    return "$%.0f" % v
