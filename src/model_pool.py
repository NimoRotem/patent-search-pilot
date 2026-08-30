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
#  `muse` IS IN THE DEFAULT FAST POOL, which the note above says would be a mistake under the old
#  equal round-robin — and it was. `call` no longer round-robins: it takes the fastest provider that
#  has a FREE SLOT, so a slow provider is never handed a call while a fast one is idle, and only
#  absorbs overflow once the fast ones are saturated. Under that rule an extra provider can only add
#  throughput, so the cheapest model in the pool is worth having in it.
FAST = [p for p in os.environ.get("MODEL_POOL_FAST", "vertex-flash,haiku,muse").split(",")
        if p.strip()]
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
#  Prompt tokens served from a provider-side cache, process-wide. Differenced per search by
#  deep_rank alongside the token counters. See `_note_cached`.
_cached_tokens = [0]


def _note_cached(n):
    if not n:
        return
    with _lock:
        _cached_tokens[0] += int(n)
        #  ATTRIBUTE IT. The total answers "did the payload reorder work"; the per-provider split
        #  is what a bill is made of, because a cached token is billed at a fraction of a fresh one
        #  and the providers are priced an order of magnitude apart. `_tl.provider` is set by
        #  `invoke`, which is the only path a provider adapter is ever reached through.
        who = getattr(_tl, "provider", "")
        if who:
            st = _state.setdefault(who, {"fails": 0, "until": 0.0, "calls": 0, "errors": 0,
                                         "last_error": ""})
            st["cached_tokens"] = st.get("cached_tokens", 0) + int(n)


def cached_tokens() -> int:
    with _lock:
        return _cached_tokens[0]


class _Provider:
    def __init__(self, name, tier, call, concurrency, env=None, note="", rate=1.0, model=""):
        self.name, self.tier, self._call, self.note = name, tier, call, note
        #  The model id is otherwise sealed inside the `call` closure, so nothing could name the
        #  model a provider actually reaches. A UI that offers a choice has to be able to say it.
        self.model = model or name
        self.env = env
        self.sem = threading.Semaphore(concurrency)
        self.concurrency = concurrency
        #  Measured calls/s at 24-way concurrency on 60k-char prompts. Used to prefer the fast
        #  providers while a slow one still absorbs overflow. See `_order`.
        self.rate = float(rate)

    def available(self) -> bool:
        if self.env and not os.environ.get(self.env):
            return False
        st = _state.get(self.name) or {}
        if st.get("until", 0) > time.time():
            return False
        return True

    def acquire(self, blocking=True):
        return self.sem.acquire(blocking)

    def release(self):
        try:
            self.sem.release()
        except ValueError:                                   # pragma: no cover - over-release
            pass

    def invoke(self, system, user, max_tokens):
        """Call WITHOUT touching the semaphore. The caller owns the slot."""
        prev = getattr(_tl, "provider", "")
        _tl.provider = self.name          # so `_note_cached` knows whose cache hit this was
        try:
            return self._call(system, user, max_tokens)
        finally:
            _tl.provider = prev

    def call(self, system, user, max_tokens):
        with self.sem:
            return self._call(system, user, max_tokens)


# ---------------------------------------------------------------------------
# provider adapters — each returns (text, prompt_tokens, completion_tokens)
# ---------------------------------------------------------------------------
_tl = threading.local()


#  `user` may be a plain string or a list of SEGMENTS: [{"text": str, "cache": bool}, ...].
#  Segments exist for one reason — Anthropic's prompt cache needs an explicit breakpoint at the
#  end of the stable prefix (the document), while Vertex caches implicitly on the joined bytes.
#  Joined, the segments MUST reproduce exactly the string the caller used to send, so a provider
#  that ignores segmentation (Vertex, meta) behaves byte-identically to before.
def _segments(user):
    if isinstance(user, str):
        return [{"text": user}]
    return [{"text": str(s.get("text") or ""), "cache": bool(s.get("cache"))}
            for s in user if isinstance(s, dict)]


def _joined(user):
    return user if isinstance(user, str) else "".join(s["text"] for s in _segments(user))


def _vertex(model, thinking_budget=0):
    """A Vertex provider. `thinking_budget` is per MODEL, not global, and 0 is not universal.

    gemini-2.5-flash lets thinking be switched off and should have it off: thinking tokens come out
    of the same output budget as the answer, and these are short structured tasks. gemini-2.5-pro
    CANNOT be given a budget of 0 and answers `400 INVALID_ARGUMENT: the model does not support
    setting thinking budget to zero`, which is why pinning the strong tier to it failed outright
    until now. Pro therefore gets a real budget, and it is ADDED to max_output_tokens rather than
    taken out of it, so a caller asking for 1,200 tokens of JSON still gets 1,200 tokens of JSON.
    """
    def go(system, user, max_tokens):
        from google import genai
        from google.genai.types import GenerateContentConfig, ThinkingConfig
        if not hasattr(_tl, "genai"):
            _tl.genai = genai.Client(vertexai=True, project=os.environ.get("GCP_PROJECT",
                                                                          "nimo-gpt"),
                                     location=os.environ.get("VERTEX_LOCATION", "us-central1"))
        cfg = GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            temperature=0.2, max_output_tokens=max_tokens + max(0, thinking_budget),
            thinking_config=ThinkingConfig(thinking_budget=thinking_budget))
        r = _tl.genai.models.generate_content(model=model, contents=_joined(user), config=cfg)
        um = getattr(r, "usage_metadata", None)
        #  CACHED PROMPT TOKENS, counted separately. `prompt_token_count` INCLUDES cached tokens, so
        #  it cannot tell you whether caching engaged — and after reordering the reader payload so
        #  the document leads (deep_analysis._ask), whether it engaged is the whole question. A run
        #  that reports 112M prompt tokens looks identical cached and uncached; only this field
        #  separates them, and they are billed at 0.25x.
        _note_cached(getattr(um, "cached_content_token_count", 0) if um else 0)
        return (getattr(r, "text", "") or "",
                getattr(um, "prompt_token_count", 0) if um else 0,
                getattr(um, "candidates_token_count", 0) if um else 0)
    return go


def _post(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as fh:
        return json.loads(fh.read().decode())


def _anthropic(model, temperature=0.2, thinking_off=False):
    def go(system, user, max_tokens):
        #  CACHE_CONTROL ON THE STABLE PREFIX. The reader sends the same document to the same
        #  model a dozen times per reference; without an explicit breakpoint Anthropic bills the
        #  full document on every call. The system prompt (stable per stage) always gets a
        #  breakpoint; caller-flagged segments (the document) get up to three more — four is the
        #  API's limit. Cached reads bill at ~0.1x, so this is most of the Anthropic bill.
        segs = _segments(user)
        content, marked = [], 0
        for s in segs:
            blk = {"type": "text", "text": s["text"]}
            if s.get("cache") and marked < 3:
                blk["cache_control"] = {"type": "ephemeral"}
                marked += 1
            content.append(blk)
        payload = {"model": model, "max_tokens": max(max_tokens, 1024),
                   "system": [{"type": "text", "text": system,
                               "cache_control": {"type": "ephemeral"}}],
                   "messages": [{"role": "user", "content": content}]}
        #  Newer models (claude-sonnet-5 and up) reject sampling parameters with a 400.
        if temperature is not None:
            payload["temperature"] = temperature
        if thinking_off:
            payload["thinking"] = {"type": "disabled"}
        d = _post("https://api.anthropic.com/v1/messages", payload,
                  {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                   "anthropic-version": "2023-06-01"})
        u = d.get("usage") or {}
        cache_read = u.get("cache_read_input_tokens", 0) or 0
        cache_write = u.get("cache_creation_input_tokens", 0) or 0
        _note_cached(cache_read)
        #  `input_tokens` EXCLUDES cache reads/writes on Anthropic; report the total submitted so
        #  the spend line stays comparable with Vertex's inclusive `prompt_token_count`.
        return ("".join(b.get("text", "") for b in (d.get("content") or [])),
                (u.get("input_tokens", 0) or 0) + cache_read + cache_write,
                u.get("output_tokens", 0))
    return go


def _meta(model):
    def go(system, user, max_tokens):
        #  A REASONING model. Below ~2,500 the whole completion budget goes on reasoning tokens and
        #  `content` comes back null — measured: max_tokens=600 produced 597 reasoning tokens and
        #  an empty answer. Floor it rather than let a caller's sensible 1,200 return nothing.
        d = _post("https://api.meta.ai/v1/chat/completions",
                  {"model": model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": _joined(user)}],
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
                              rate=5.97, note="5.97 calls/s at 24 workers",
                              model="gemini-2.5-flash"),
    "haiku": _Provider("haiku", "fast", _anthropic("claude-haiku-4-5-20251001"), 48,
                       env="ANTHROPIC_API_KEY", rate=7.96, note="7.96 calls/s at 24 workers",
                       model="claude-haiku-4-5-20251001"),
    "muse": _Provider("muse", "fast", _meta("muse-spark-1.2"), 12, env="META_API_KEY",
                      rate=0.99, note="reasoning model, 0.99 calls/s, capacity and diversity",
                      model="muse-spark-1.2"),
    "sonnet": _Provider("sonnet", "strong", _anthropic("claude-sonnet-4-5-20250929"), 24,
                        env="ANTHROPIC_API_KEY", rate=3.0,
                        model="claude-sonnet-4-5-20250929"),
    #  Claude Sonnet 5: rejects sampling params (temperature=None omits it) and thinks adaptively
    #  by default, which is unwanted latency on short structured judgments — disabled explicitly.
    #  Not in any tier by default; opt in with MODEL_POOL_STRONG=sonnet5,vertex-flash for an A/B.
    "sonnet5": _Provider("sonnet5", "strong",
                         _anthropic("claude-sonnet-5", temperature=None, thinking_off=True), 24,
                         env="ANTHROPIC_API_KEY", rate=3.0, model="claude-sonnet-5"),
    #  THE KEYLESS STRONG MODEL. Served by the GCE service account, so it is the one strong
    #  provider that keeps working when the Anthropic key is spend-capped. 512 thinking tokens:
    #  enough for a judgement on a passage, far short of the latency a full reasoning budget costs.
    "vertex-pro": _Provider("vertex-pro", "strong",
                            _vertex("gemini-2.5-pro", thinking_budget=512), 16, rate=2.0,
                            model="gemini-2.5-pro"),
}


_TIERS = {"fast": lambda: FAST, "read": lambda: READ, "strong": lambda: STRONG}


def _configured(tier):
    """The tier's provider names, honouring the settings page over the environment default."""
    try:
        import model_settings
        return model_settings.tier_providers(tier, _TIERS.get(tier, lambda: FAST)())
    except Exception:
        return _TIERS.get(tier, lambda: FAST)()


def providers(tier) -> list:
    names = _configured(tier)
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


def _mark(name, ok, err="", prompt_tokens=0, completion_tokens=0):
    with _lock:
        st = _state.setdefault(name, {"fails": 0, "until": 0.0, "calls": 0, "errors": 0,
                                      "last_error": ""})
        st["calls"] += 1
        if ok:
            st["fails"] = 0
            #  PER-PROVIDER TOKENS. The process-wide counter in `llm` cannot be priced: one number
            #  covering models that differ by 30x in cost per token is not a bill, it is an
            #  average of things nobody bought.
            st["prompt_tokens"] = st.get("prompt_tokens", 0) + int(prompt_tokens or 0)
            st["completion_tokens"] = st.get("completion_tokens", 0) + int(completion_tokens or 0)
            return
        st["fails"] += 1
        st["errors"] += 1
        #  KEEP WHAT IT SAID. A provider that is refusing every call looks identical to a healthy
        #  one through `available()`, which only knows whether a key exists and whether the
        #  provider is latched off. The reason is the whole difference between "spend limit until
        #  the 1st" and "transient 503", and without it the only way to find out was to read an
        #  error count out of a finished report.
        if err:
            st["last_error"] = str(err)[:300]
        if st["fails"] >= FAIL_LIMIT:
            st["until"] = time.time() + COOLDOWN
            st["fails"] = 0
            print(f"[models] {name} latched off for {COOLDOWN:.0f}s after {FAIL_LIMIT} "
                  f"consecutive failures", flush=True)


def _describe_error(p, e):
    body = ""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode()[:160]
        except Exception:
            pass
    return f"{p.name}: {type(e).__name__}: {str(e)[:120]} {body}"


def choices():
    """Every provider a caller may ask for by name, with whether it can be used right now.

    -> [{"name", "tier", "model", "available", "why"}]

    For the UI that lets a reader pick the model behind a rebuild. `available` is the pool's own
    answer, so a provider whose key is missing or which is in cooldown says so rather than being
    offered and then quietly swapped underneath.
    """
    out = []
    for name, p in _ALL.items():
        why = ""
        if p.env and not os.environ.get(p.env):
            why = "%s is not set on this host" % p.env
        elif not p.available():
            why = "temporarily latched off after repeated failures"
        out.append({"name": name, "tier": p.tier, "model": getattr(p, "model", "") or name,
                    "available": p.available(), "why": why,
                    "note": getattr(p, "note", "") or ""})
    out.sort(key=lambda d: (not d["available"], d["tier"], d["name"]))
    return out


def call(system, user, max_tokens=1200, tier="fast", provider=None):
    """Ask the tier, or one named provider. -> (text, provider_name, prompt_tokens, completion_tokens).

    `provider` pins the call to one member of `_ALL` by name. It is for a person who has asked for
    a specific model, so it does NOT silently fall back to the tier: an unknown or unavailable name
    raises, because quietly answering from a different model than the one someone chose makes the
    comparison they were running meaningless. Everything else about the call is unchanged.

    CAPACITY-AWARE, NOT ROUND-ROBIN, and the difference is the whole reason a third provider is
    worth adding. Round-robin hands 1/N of the calls to the slowest member whatever the queue looks
    like, so adding `muse` (0.99 calls/s against Vertex's 5.97 and Anthropic's 7.96) used to make
    the pool SLOWER: a third of the traffic went to a provider five times slower than the one
    sitting idle beside it.

    Two passes instead:
      1. the FASTEST provider that has a free slot right now, acquired non-blocking;
      2. only if every provider is saturated, block on the round-robin choice.

    Under that rule the pool's throughput is the SUM of its members and a slow member can never be
    the reason a call waits. It only ever picks up work the fast providers had no room for, which
    is exactly what a cheap model should be doing.

    Raises only when every provider in the tier failed, so the caller's existing failclosed path
    still sees a real exception rather than a silent empty string.
    """
    last = None
    if provider:
        p = _ALL.get(str(provider))
        if p is None:
            raise ValueError("unknown model %r; known: %s"
                             % (provider, ", ".join(sorted(_ALL))))
        if not p.available():
            raise RuntimeError("model %r is not available on this host: %s"
                               % (provider, (p.env and not os.environ.get(p.env))
                                  and ("%s is not set" % p.env) or "latched off after failures"))
        ps = [p]
    else:
        ps = _order(tier)

    def attempt(p, blocking):
        """-> (text, pt, ct) on success, None if busy or failed. Sets `last` on failure."""
        nonlocal last
        if not p.acquire(blocking=blocking):
            return None
        try:
            text, pt, ct = p.invoke(system, user, max_tokens)
        except Exception as e:
            last = _describe_error(p, e)
            _mark(p.name, False, err=last)
            return None
        finally:
            p.release()
        if not (text or "").strip():
            #  An empty body is a failure for our purposes, whatever the transport said. This is
            #  exactly how muse-spark fails when its budget went on reasoning.
            last = f"{p.name}: empty response body"
            _mark(p.name, False, err=last)
            return None
        _mark(p.name, True, prompt_tokens=pt, completion_tokens=ct)
        return text, pt, ct

    #  PASS 1 — spare capacity, fastest first.
    for p in sorted(ps, key=lambda x: -x.rate):
        got = attempt(p, blocking=False)
        if got:
            return got[0], p.name, got[1], got[2]
    #  PASS 2 — everything is busy or everything just failed. Wait for a slot, in round-robin order
    #  so the queue spreads instead of piling onto whichever provider is nominally fastest.
    for p in ps:
        got = attempt(p, blocking=True)
        if got:
            return got[0], p.name, got[1], got[2]
    raise RuntimeError(f"every provider in tier '{tier}' failed; last: {last}")


def stats() -> dict:
    """Per-provider calls and errors, for the run log."""
    with _lock:
        return {k: {"calls": v.get("calls", 0), "errors": v.get("errors", 0),
                    "prompt_tokens": v.get("prompt_tokens", 0),
                    "completion_tokens": v.get("completion_tokens", 0),
                    "cached_tokens": v.get("cached_tokens", 0),
                    "latched": v.get("until", 0) > time.time(),
                    "last_error": v.get("last_error", "")}
                for k, v in _state.items()}


def describe() -> str:
    f = ", ".join(p.name for p in providers("fast"))
    s = ", ".join(p.name for p in providers("strong"))
    return f"fast=[{f}] strong=[{s}]"


def probe(name, timeout_tokens=2600):
    """Ask one provider a trivial question right now. -> (ok, detail).

    `available()` answers "is there a key and is it not latched off", which a spend-capped key
    passes while refusing every call. This answers the question an operator is actually asking.
    The token budget is deliberately generous: muse-spark is a reasoning model and returns an
    empty body below roughly REASONING_MIN_TOKENS, which would read as a dead provider.
    """
    p = _ALL.get(name)
    if not p:
        return False, "no such provider"
    if p.env and not os.environ.get(p.env):
        return False, "no %s on this host" % p.env
    t0 = time.time()
    try:
        text, _pt, _ct = p.invoke("You return JSON only.", '{"say": "ok"}', timeout_tokens)
    except Exception as e:                                                # noqa: BLE001
        return False, _describe_error(p, e)
    if not (text or "").strip():
        return False, "empty response body"
    return True, "answered in %.1fs" % (time.time() - t0)
