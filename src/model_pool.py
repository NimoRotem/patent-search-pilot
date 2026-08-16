"""Many fast models for the big passes, a strong one only where the answer is asserted.

WHY, MEASURED
-------------
Every language task in this pipeline went to one model, `gemini-2.5-flash` on Vertex, one call at a
time. Profiling the stage that dominates a search — reading references in full — on real documents
from `adhoc-a2fec8ee8ba2`:

    per reference          11 LLM calls, issued SEQUENTIALLY inside analyse_reference
    at CHART_WORKERS=24    21.8s per reference, 2.70 calls/s overall
    657 references         77 minutes

and, measured separately at the same 60k-character prompt size and the same 24-way concurrency:

    vertex gemini-2.5-flash   median 2.8s   5.97 calls/s
    anthropic claude-haiku-4.5 median 2.6s  7.96 calls/s
    meta muse-spark-1.2       median 14.3s  0.99 calls/s

So one provider alone sustains more than twice what the whole pipeline was achieving, and neither
Vertex nor Anthropic showed any rate limiting at 24 concurrent calls. **The constraint was ours.**
Two providers in a pool roughly double the ceiling again, and the ceiling is what a 2.5-hour search
is spending its time under.

MUSE IS IN THE POOL BUT NOT IN THE HOT PATH BY DEFAULT. `muse-spark-1.2` is a reasoning model: it
burned ~1,600 reasoning tokens on a prompt whose answer was 37 tokens, returns EMPTY below about
2,000 max_tokens, and measured five times slower than the other two. Round-robining it equally into
the fast pool would slow the pool down. It is enabled with `MODEL_POOL_FAST=...,muse` when the
diversity is worth the wall clock — a different model family disagrees differently, and that is the
same argument that made two samples of the query facets worth more than one.

TIERS
-----
`fast`   volume where a cheaper model costs latency and not evidence: screening thousands of
         candidates from a title and an abstract, generating query facets (which are sampled twice
         and merged anyway).
`read`   the full-text chart. High volume too, but this is where the evidence is MADE, and a
         cheaper model was measured to find less of it — see READ below. Flash only by default.
`strong` the passes whose output IS the assertion: the refuter that decides what may be claimed,
         the claim -> limitation split that everything else is keyed on, and the second look.
         A few hundred calls against tens of thousands.

Nothing here changes what a caller asks for. `llm.chat_json` keeps its signature and its salvage
behaviour; it gains a `tier` argument that defaults to `fast`.

FAIL-SOFT AND SELF-HEALING. A provider that errors or returns unusable output is latched off for
`COOLDOWN` seconds and the call is retried on the next healthy provider, so one bad key or one
regional outage costs latency rather than a search. With no extra keys configured the pool is
exactly the single Vertex provider this pipeline already used, and behaviour is unchanged.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
import urllib.error
import urllib.request

try:                                     # side effect: config loads .env, where the keys live
    import config                        # noqa: F401
except Exception:
    pass

#  Which providers serve which tier, in preference order. Comma-separated names, env-overridable so
#  a bad provider can be dropped without a deploy.
FAST = [p for p in os.environ.get("MODEL_POOL_FAST", "vertex-flash,haiku").split(",") if p.strip()]
#  READ is its own tier and NOT the fast pool, because reading is where a cheaper model costs
#  evidence rather than latency. MEASURED on US-11999030-B2 — the reference an examiner applied
#  under 102(a)(2) to thirteen claims — asking the same 68 limitations with the same prompt:
#
#      vertex-flash   20 disclosed,  8 partial, 40 absent   7 claims with a DISCLOSED, 28 quoted
#      haiku           9 disclosed, 14 partial, 45 absent   3 claims with a DISCLOSED, 23 quoted
#      sonnet         21 disclosed,  8 partial, 39 absent   8 claims with a DISCLOSED, 29 quoted
#
#  Haiku is fine at scoring a candidate 0-100 from a title and an abstract, and materially worse at
#  finding a teaching in 90,000 characters and quoting it. Sonnet buys one claim over flash for
#  twice the latency across seven hundred references, which is not worth it on the FIRST pass —
#  the place to spend a strong model is the second look, and that is already on the strong tier.
READ = [p for p in os.environ.get("MODEL_POOL_READ", "vertex-flash").split(",") if p.strip()]
STRONG = [p for p in os.environ.get("MODEL_POOL_STRONG", "sonnet,vertex-flash").split(",")
          if p.strip()]
#  Consecutive failures before a provider is latched off, and for how long. Lens sat dead for days
#  behind a 401 because nothing latched; this is the same lesson applied up front.
FAIL_LIMIT = int(os.environ.get("MODEL_POOL_FAIL_LIMIT", "4"))
COOLDOWN = float(os.environ.get("MODEL_POOL_COOLDOWN", "120"))
TIMEOUT = float(os.environ.get("MODEL_POOL_TIMEOUT", "180"))
#  muse-spark returns nothing at all below roughly this, having spent the budget on reasoning.
REASONING_MIN_TOKENS = int(os.environ.get("MODEL_POOL_REASONING_MIN", "2500"))

_lock = threading.Lock()
_state: dict = {}
_rr: dict = {}


class _Provider:
    def __init__(self, name, tier, call, concurrency, env=None, note=""):
        self.name, self.tier, self._call, self.note = name, tier, call, note
        self.env = env
        self.sem = threading.Semaphore(concurrency)

    def available(self) -> bool:
        if self.env and not os.environ.get(self.env):
            return False
        st = _state.get(self.name) or {}
        if st.get("until", 0) > time.time():
            return False
        return True

    def call(self, system, user, max_tokens):
        with self.sem:
            return self._call(system, user, max_tokens)


# ---------------------------------------------------------------------------
# provider adapters — each returns (text, prompt_tokens, completion_tokens)
# ---------------------------------------------------------------------------
_tl = threading.local()


def _vertex(model):
    def go(system, user, max_tokens):
        from google import genai
        from google.genai.types import GenerateContentConfig, ThinkingConfig
        if not hasattr(_tl, "genai"):
            _tl.genai = genai.Client(vertexai=True, project=os.environ.get("GCP_PROJECT",
                                                                          "nimo-gpt"),
                                     location=os.environ.get("VERTEX_LOCATION", "us-central1"))
        cfg = GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            temperature=0.2, max_output_tokens=max_tokens,
            #  gemini-2.5-flash is a thinking model; thinking tokens eat the output budget and
            #  truncate the JSON. Off for these short structured tasks.
            thinking_config=ThinkingConfig(thinking_budget=0))
        r = _tl.genai.models.generate_content(model=model, contents=user, config=cfg)
        um = getattr(r, "usage_metadata", None)
        return (getattr(r, "text", "") or "",
                getattr(um, "prompt_token_count", 0) if um else 0,
                getattr(um, "candidates_token_count", 0) if um else 0)
    return go


def _post(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as fh:
        return json.loads(fh.read().decode())


def _anthropic(model):
    def go(system, user, max_tokens):
        d = _post("https://api.anthropic.com/v1/messages",
                  {"model": model, "max_tokens": max(max_tokens, 1024), "temperature": 0.2,
                   "system": system, "messages": [{"role": "user", "content": user}]},
                  {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                   "anthropic-version": "2023-06-01"})
        u = d.get("usage") or {}
        return ("".join(b.get("text", "") for b in (d.get("content") or [])),
                u.get("input_tokens", 0), u.get("output_tokens", 0))
    return go


def _meta(model):
    def go(system, user, max_tokens):
        #  A REASONING model. Below ~2,500 the whole completion budget goes on reasoning tokens and
        #  `content` comes back null — measured: max_tokens=600 produced 597 reasoning tokens and
        #  an empty answer. Floor it rather than let a caller's sensible 1,200 return nothing.
        d = _post("https://api.meta.ai/v1/chat/completions",
                  {"model": model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "max_tokens": max(max_tokens, REASONING_MIN_TOKENS), "temperature": 0.2},
                  {"Authorization": f"Bearer {os.environ['META_API_KEY']}"})
        u = d.get("usage") or {}
        ch = (d.get("choices") or [{}])[0]
        return ((ch.get("message") or {}).get("content") or "",
                u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    return go


#  Concurrency per provider. These are semaphores, not rate limits: measured, neither Vertex nor
#  Anthropic throttled at 24 concurrent calls on 60k-character prompts.
_ALL = {
    "vertex-flash": _Provider("vertex-flash", "fast", _vertex("gemini-2.5-flash"), 48,
                              note="5.97 calls/s at 24 workers"),
    "haiku": _Provider("haiku", "fast", _anthropic("claude-haiku-4-5-20251001"), 48,
                       env="ANTHROPIC_API_KEY", note="7.96 calls/s at 24 workers"),
    "muse": _Provider("muse", "fast", _meta("muse-spark-1.2"), 12, env="META_API_KEY",
                      note="reasoning model, 0.99 calls/s — diversity, not speed"),
    "sonnet": _Provider("sonnet", "strong", _anthropic("claude-sonnet-4-5-20250929"), 24,
                        env="ANTHROPIC_API_KEY"),
    "vertex-pro": _Provider("vertex-pro", "strong", _vertex("gemini-2.5-pro"), 16),
}


_TIERS = {"fast": lambda: FAST, "read": lambda: READ, "strong": lambda: STRONG}


def providers(tier) -> list:
    names = _TIERS.get(tier, lambda: FAST)()
    out = [_ALL[n] for n in names if n in _ALL and _ALL[n].available()]
    #  Never leave a tier empty. A strong tier with no key must fall back to a fast provider rather
    #  than fail the call: a degraded refuter is far better than no chart at all.
    if not out:
        out = [p for p in _ALL.values() if p.tier == "fast" and p.available()]
    if not out:
        out = [_ALL["vertex-flash"]]
    return out


def _order(tier):
    """Round robin, so load spreads instead of hammering the first healthy provider."""
    ps = providers(tier)
    if len(ps) < 2:
        return ps
    with _lock:
        c = _rr.setdefault(tier, itertools.count())
        i = next(c)
    return ps[i % len(ps):] + ps[:i % len(ps)]


def _mark(name, ok):
    with _lock:
        st = _state.setdefault(name, {"fails": 0, "until": 0.0, "calls": 0, "errors": 0})
        st["calls"] += 1
        if ok:
            st["fails"] = 0
            return
        st["fails"] += 1
        st["errors"] += 1
        if st["fails"] >= FAIL_LIMIT:
            st["until"] = time.time() + COOLDOWN
            st["fails"] = 0
            print(f"[models] {name} latched off for {COOLDOWN:.0f}s after {FAIL_LIMIT} "
                  f"consecutive failures", flush=True)


def call(system, user, max_tokens=1200, tier="fast"):
    """Ask the tier. -> (text, provider_name, prompt_tokens, completion_tokens).

    Tries each healthy provider in round-robin order and returns the first non-empty answer. Raises
    only when every provider in the tier failed, so the caller's existing failclosed path still
    sees a real exception rather than a silent empty string.
    """
    last = None
    for p in _order(tier):
        try:
            text, pt, ct = p.call(system, user, max_tokens)
        except Exception as e:
            body = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    body = e.read().decode()[:160]
                except Exception:
                    pass
            last = f"{p.name}: {type(e).__name__}: {str(e)[:120]} {body}"
            _mark(p.name, False)
            continue
        if not (text or "").strip():
            #  An empty body is a failure for our purposes, whatever the transport said. This is
            #  exactly how muse-spark fails when its budget went on reasoning.
            last = f"{p.name}: empty response body"
            _mark(p.name, False)
            continue
        _mark(p.name, True)
        return text, p.name, pt, ct
    raise RuntimeError(f"every provider in tier '{tier}' failed; last: {last}")


def stats() -> dict:
    """Per-provider calls and errors, for the run log."""
    with _lock:
        return {k: {"calls": v.get("calls", 0), "errors": v.get("errors", 0),
                    "latched": v.get("until", 0) > time.time()}
                for k, v in _state.items()}


def describe() -> str:
    f = ", ".join(p.name for p in providers("fast"))
    s = ", ".join(p.name for p in providers("strong"))
    return f"fast=[{f}] strong=[{s}]"
