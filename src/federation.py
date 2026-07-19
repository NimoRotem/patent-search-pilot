"""Bridge to the sibling federated search app (patents-app on builder).

WHY
---
The pilot owns a deep local index of one narrow field. The federated app owns no corpus at
all and fans out live over SerpApi / BigQuery / PQAI / OpenAlex. They are complementary: when
domain_detect says a query is outside the pilot's 8 seed CPC branches, the pilot has nothing
useful to say and the federated app does.

REACHABILITY (verified 2026-07-19, not assumed)
-----------------------------------------------
  http://10.128.0.13:8630   builder's internal VPC address. NOT REACHABLE from instance-3.
      The GCP firewall does allow it (default-allow-internal covers tcp:0-65535 from
      10.128.0.0/9) and uvicorn does bind 0.0.0.0:8630 — but builder runs ufw with a default
      DROP policy and only 22/80/443 opened, so the connection times out at the host firewall.
  https://rotem.ai/patents-engine  the public nginx route to App A. VERIFIED reachable
      from instance-3 (GET /api/health -> 200). This is the DEFAULT base URL.
      NOTE 2026-07-19: /patents is now THIS app (the unified front door). App A moved to
      /patents-engine. Pointing FEDERATION_BASE_URL back at /patents would make the pilot
      call itself; _is_self() refuses that.

If someone later opens 8630 on builder's ufw for 10.128.0.0/9, set FEDERATION_BASE_URL to the
internal address to skip the public TLS round trip. Nothing else needs to change. We do NOT
open that port here: the federated app is unauthenticated, and exposing it VPC-wide is a
decision for whoever owns builder's firewall, not a side effect of this bridge.
App A is NO LONGER unauthenticated: it requires X-Patents-Key (FEDERATION_KEY).

COST / LATENCY DISCIPLINE
-------------------------
A federated call runs a 4-round agent loop over PAID APIs and takes minutes. So:
  * opt-in per request only (never on a hot path, never implicit),
  * hard timeout (FEDERATION_TIMEOUT, default 240s),
  * on-disk cache keyed by (disclosure, mode) with a TTL, so a repeat or a page refresh is free,
  * a single-flight lock so two concurrent identical requests make one upstream call,
  * every failure degrades to local-only. A federation outage must never break a local search.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import DATA

# --- configuration --------------------------------------------------------------------------
BASE_URL = os.environ.get("FEDERATION_BASE_URL", "https://rotem.ai/patents-engine").rstrip("/")
SELF_URL = os.environ.get("FEDERATION_SELF_URL", "https://rotem.ai/patents").rstrip("/")
FED_KEY = os.environ.get("FEDERATION_KEY", "")
INTERNAL_URL = os.environ.get("FEDERATION_INTERNAL_URL", "http://10.128.0.13:8630").rstrip("/")
ENABLED = os.environ.get("FEDERATION_ENABLED", "1") != "0"
TIMEOUT = float(os.environ.get("FEDERATION_TIMEOUT", "240"))     # whole-stream budget, seconds
CONNECT_TIMEOUT = float(os.environ.get("FEDERATION_CONNECT_TIMEOUT", "10"))
HEALTH_TIMEOUT = float(os.environ.get("FEDERATION_HEALTH_TIMEOUT", "6"))
CACHE_TTL = float(os.environ.get("FEDERATION_CACHE_TTL", str(14 * 24 * 3600)))
CACHE_DIR = Path(os.environ.get("FEDERATION_CACHE_DIR", str(DATA / "federation_cache")))

# The federated app only accepts these three.
FED_MODES = ("novelty", "inventive_step", "fto")

# RRF weight for the federated channel when fused alongside local channels. Below "dense"
# (1.00) because local dense is tuned on an in-domain corpus, but above every lexical/
# classification channel: federated hits are already multi-source RRF-fused, reranked and
# LLM-scored upstream, so their rank order carries real information.
FEDERATION_WEIGHT = 0.90

_locks: dict = {}
_locks_guard = threading.Lock()


# --- normalisation --------------------------------------------------------------------------
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_US_APP = re.compile(r"^US(19\d\d|20\d\d)(\d{4,7})(A[19])$")


def join_key(pub: str) -> str:
    """Publication number -> a join key comparable across both systems.

    The pilot stores 'US-11999030-B2'; the federated app emits 'US11999030B2'. Strip every
    non-alphanumeric and upper-case, and both collapse to 'US11999030B2'.
    """
    if not pub:
        return ""
    pub = str(pub)
    if "patent/" in pub:
        pub = pub.split("patent/", 1)[1].split("/")[0]
    return _NON_ALNUM.sub("", pub).upper()


def key_variants(pub: str) -> list:
    """US pre-grant numbers disagree between the two systems on zero-padding: BigQuery (and so
    the pilot) stores US-2023003794-A1, while the federated app's canonical_pub pads the
    sequence to 7 digits (US20230003794A1). Try both so these still join."""
    k = join_key(pub)
    if not k:
        return []
    out = [k]
    m = _US_APP.match(k)
    if m:
        year, seq, kind = m.group(1), m.group(2), m.group(3)
        padded = f"US{year}{int(seq):07d}{kind}"
        stripped = f"US{year}{int(seq)}{kind}"
        for v in (padded, stripped):
            if v not in out:
                out.append(v)
    return out


# --- result shapes --------------------------------------------------------------------------
@dataclass
class FederatedHit:
    """One federated family, normalised. `pub_number` is the representative publication."""
    pub_number: str
    rank: int                       # 0-based rank in the federated shortlist
    title: str = ""
    abstract: str = ""
    assignee: str = ""
    date: str = ""
    country: str = ""
    cpc: list = field(default_factory=list)
    url: str = ""
    family_id: str = ""
    members: list = field(default_factory=list)     # sibling publication numbers
    sources: list = field(default_factory=list)     # which federated sources hit it
    score: float = 0.0
    relevance_note: str = ""

    def keys(self) -> list:
        """Every join key this family could match a local publication on."""
        out = []
        for p in [self.pub_number] + list(self.members):
            for v in key_variants(p):
                if v not in out:
                    out.append(v)
        return out


@dataclass
class FederatedResult:
    ok: bool
    hits: list = field(default_factory=list)        # [FederatedHit] best-first
    elements: list = field(default_factory=list)    # agent-extracted claim elements
    error: str = ""
    elapsed: float = 0.0
    cached: bool = False
    base_url: str = ""
    raw_done: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "n_hits": len(self.hits), "error": self.error,
                "elapsed": round(self.elapsed, 1), "cached": self.cached,
                "base_url": self.base_url, "elements": self.elements}


# --- cache ----------------------------------------------------------------------------------
def _cache_path(disclosure: str, mode: str) -> Path:
    h = hashlib.sha256(f"{mode}\x00{disclosure.strip()}".encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{mode}_{h}.json"


def _cache_read(disclosure: str, mode: str):
    p = _cache_path(disclosure, mode)
    try:
        if not p.exists() or (time.time() - p.stat().st_mtime) > CACHE_TTL:
            return None
        return json.loads(p.read_text())
    except Exception:
        return None


def _cache_write(disclosure: str, mode: str, done: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_path(disclosure, mode)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(done))
        tmp.replace(p)                       # atomic — a torn file would poison the cache
    except Exception:
        pass


def _lock_for(disclosure: str, mode: str) -> threading.Lock:
    k = str(_cache_path(disclosure, mode))
    with _locks_guard:
        if k not in _locks:
            _locks[k] = threading.Lock()
        return _locks[k]


# --- transport ------------------------------------------------------------------------------
def _headers() -> dict:
    """App A gates /api/search, /api/patent etc. behind a shared secret (X-Patents-Key).
    Without this every federated search 401s while health() still says ok."""
    return {"X-Patents-Key": FED_KEY} if FED_KEY else {}


def _is_self(url: str) -> bool:
    """True if the given base URL is this very app. /patents is now the pilot front door, so a stale
    FEDERATION_BASE_URL pointing there would make the pilot federate to ITSELF -- an
    infinite, paid recursion. Compare (scheme, host, path) exactly: /patents-engine is a
    different path from /patents and must NOT be caught by a startswith()."""
    from urllib.parse import urlparse
    a, b = urlparse(url.rstrip("/")), urlparse(SELF_URL)
    return (a.netloc, a.path.rstrip("/")) == (b.netloc, b.path.rstrip("/"))


def _bases() -> list:
    """Preferred base URLs, best first. The internal address is only tried when explicitly
    configured, since it is firewalled off by default (see module docstring)."""
    out = [BASE_URL]
    if os.environ.get("FEDERATION_TRY_INTERNAL") == "1" and INTERNAL_URL not in out:
        out.insert(0, INTERNAL_URL)
    return [u for u in out if not _is_self(u)]


def health(timeout: float = HEALTH_TIMEOUT) -> dict:
    """Probe the federated app. Never raises. -> {"ok":bool,"base_url":str,"sources":[...]}"""
    if not ENABLED:
        return {"ok": False, "error": "federation disabled (FEDERATION_ENABLED=0)"}
    import requests
    last = ""
    for base in _bases():
        try:
            r = requests.get(f"{base}/api/health", headers=_headers(), timeout=timeout)
            if r.status_code == 200:
                d = r.json()
                return {"ok": True, "base_url": base, "model": d.get("model", ""),
                        "sources": [s["name"] for s in d.get("sources", [])
                                    if s.get("search_available")]}
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)[:200]
    return {"ok": False, "error": last or "unreachable"}


def _stream_search(disclosure: str, mode: str, on_event=None) -> dict:
    """POST /api/search and consume the SSE stream to its `done` event.
    -> the done payload dict. Raises on transport failure or if no done event arrives."""
    import requests

    body = {"disclosure": disclosure[:20000],
            "mode": mode if mode in FED_MODES else "novelty"}
    last_err = ""
    for base in _bases():
        deadline = time.time() + TIMEOUT
        try:
            with requests.post(f"{base}/api/search", json=body, stream=True,
                               timeout=(CONNECT_TIMEOUT, TIMEOUT),
                               headers={**_headers(),
                                        "Accept": "text/event-stream"}) as r:
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    continue
                done = None
                for line in r.iter_lines(decode_unicode=True):
                    if time.time() > deadline:
                        raise TimeoutError(f"federated search exceeded {TIMEOUT:.0f}s")
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except Exception:
                        continue
                    kind = ev.get("kind")
                    if on_event:
                        try:
                            on_event(ev)
                        except Exception:
                            pass          # a bad progress callback must not kill the search
                    if kind == "error":
                        raise RuntimeError(str(ev.get("error"))[:300])
                    if kind == "done":
                        done = ev
                    elif kind == "end":
                        break
                if done is None:
                    raise RuntimeError("federated stream ended without a done event")
                return done
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
    raise RuntimeError(last_err or "federation unreachable")


# --- normalisation into the pilot's shape ---------------------------------------------------
def _to_hits(done: dict) -> list:
    hits = []
    for i, f in enumerate(done.get("shortlist") or []):
        pn = f.get("pub_number") or ""
        if not pn:
            continue
        hits.append(FederatedHit(
            pub_number=pn, rank=i,
            title=f.get("title") or "",
            abstract=f.get("abstract") or "",
            assignee=f.get("assignee") or "",
            date=f.get("date") or "",
            country=f.get("country") or "",
            cpc=list(f.get("cpc") or []),
            url=f.get("url") or f.get("google_url") or "",
            family_id=str(f.get("family_id") or ""),
            members=list(f.get("members") or []),
            sources=list(f.get("sources") or []),
            score=float(f.get("final_score") or 0.0),
            relevance_note=f.get("relevance_note") or "",
        ))
    return hits


def search(disclosure: str, mode: str = "novelty", use_cache: bool = True,
           on_event=None) -> FederatedResult:
    """Run a federated search. NEVER raises — a failure comes back as ok=False so the caller
    can fall through to local-only results.

    This is the expensive path: minutes of wall time and paid API credits. Call it only when
    the user opted in, or when domain_detect says the local corpus cannot answer.
    """
    if not ENABLED:
        return FederatedResult(ok=False, error="federation disabled (FEDERATION_ENABLED=0)")
    if not disclosure or not disclosure.strip():
        return FederatedResult(ok=False, error="empty disclosure")

    mode = mode if mode in FED_MODES else "novelty"
    t0 = time.time()

    if use_cache:
        cached = _cache_read(disclosure, mode)
        if cached is not None:
            return FederatedResult(ok=True, hits=_to_hits(cached),
                                   elements=list(cached.get("elements") or []),
                                   elapsed=0.0, cached=True, base_url="cache", raw_done=cached)

    # single-flight: a second identical request waits and then reads the cache the first wrote
    lock = _lock_for(disclosure, mode)
    with lock:
        if use_cache:
            cached = _cache_read(disclosure, mode)
            if cached is not None:
                return FederatedResult(ok=True, hits=_to_hits(cached),
                                       elements=list(cached.get("elements") or []),
                                       elapsed=time.time() - t0, cached=True,
                                       base_url="cache", raw_done=cached)
        try:
            done = _stream_search(disclosure, mode, on_event=on_event)
        except Exception as e:
            return FederatedResult(ok=False, error=f"{type(e).__name__}: {str(e)[:300]}",
                                   elapsed=time.time() - t0)
        if use_cache:
            _cache_write(disclosure, mode, done)
        return FederatedResult(ok=True, hits=_to_hits(done),
                               elements=list(done.get("elements") or []),
                               elapsed=time.time() - t0, base_url=BASE_URL, raw_done=done)


def patent_detail(pn: str, timeout: float = 30.0) -> dict:
    """GET /api/patent/{pn} — merged record for a publication the local corpus lacks.
    Never raises; returns {} on failure."""
    if not ENABLED or not pn:
        return {}
    import requests
    for base in _bases():
        try:
            r = requests.get(f"{base}/api/patent/{join_key(pn)}", headers=_headers(), timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return {}


# --- fusion into the pilot's ranking --------------------------------------------------------
def external_id(hit: FederatedHit) -> str:
    """Stable virtual publication id for a federated hit with no local row."""
    return f"fed:{join_key(hit.pub_number)}"


def as_channel(retriever, fed: FederatedResult) -> tuple:
    """Turn federated hits into a pilot retrieval channel.

    This is the whole point of the adapter: federated results must enter the SAME fusion the
    local channels go through, not sit beside it. Concretely:

      * a federated hit whose publication IS already in the local corpus resolves to that
        publication's real local id, so RRF sees cross-system agreement and family dedup
        collapses the two into one row (86% of gold families are in-corpus, so this is the
        common case, not an edge case);
      * a federated hit with no local row gets a virtual id 'fed:<PUBNUM>' whose family key is
        registered with the retriever, so dedup_family treats it exactly like a local family.

    -> ([(pid, score)] best-first, {pid: FederatedHit} for the externals)
    """
    if not fed.ok or not fed.hits:
        return [], {}

    resolved = retriever.resolve_pub_numbers(
        [k for h in fed.hits for k in h.keys()])

    channel, externals = [], {}
    seen = set()
    for h in fed.hits:
        pid = None
        for k in h.keys():
            if k in resolved:
                pid = resolved[k][0]
                break
        if pid is None:
            pid = external_id(h)
            # family key: prefer the federated family id so siblings collapse together
            fam = f"fedfam:{h.family_id}" if h.family_id else pid
            retriever.register_external(pid, fam)
            externals[pid] = h
        if pid in seen:
            continue
        seen.add(pid)
        # RRF consumes rank order, so the raw score only breaks ties
        channel.append((pid, float(len(fed.hits) - h.rank)))
    return channel, externals


def fuse(retriever, local, fed: FederatedResult, strategy: str = "augment",
         do_rerank: bool = False, topk: int = 1000):
    """Fuse federated hits into a local retrieval Result. -> a new Result.

    strategy:
      "augment" — keep every local channel and add the federated one. For an IN-DOMAIN query
                  where the user asked for a wider search. The local dense floor stays on.
      "replace" — federated hits only. For an OUT-OF-DOMAIN query, where the local channels
                  are actively misleading. Critically this also turns OFF the dense floor,
                  which otherwise pins 30 irrelevant local documents into the head purely
                  because they were the nearest neighbours in a corpus lacking the field.
    """
    from retrieval import Result

    channel, externals = as_channel(retriever, fed)
    if not channel:
        return local

    if strategy == "replace":
        chans = {"federated": channel}
        dense_floor = False
    else:
        chans = dict(local.channel_hits_ranked() if local else {})
        chans["federated"] = channel
        dense_floor = True

    prev = getattr(local, "external", {}) or {}
    merged_ext = dict(prev)
    merged_ext.update(externals)

    fused = retriever.rrf(chans, dense_floor=dense_floor)
    fam = retriever.dedup_family(fused)
    if do_rerank and fam:
        fam = retriever.rerank_families(local.query if local else "", fam,
                                        external=merged_ext)
    return Result(
        ranked_pubs=[(p, s, pr) for _, p, s, pr in fam][:topk],
        family_ranked=fam[:topk],
        channel_hits={k: [p for p, _ in v] for k, v in chans.items()},
        query=local.query if local else "",
        external=merged_ext,
        federation=fed.to_dict(),
        domain=getattr(local, "domain", None) if local else None,
    )


# --- two-tier orchestration -----------------------------------------------------------------
# This is the function the web layer should call. It owns the policy; the web layer only owns
# presentation.
#
#   wide=False (default)  local index only. If the query is out of domain the results are
#                         returned but the Result carries a domain verdict saying so, and
#                         `federation_offered` tells the UI to offer the wider search. NOTHING
#                         expensive happens — no federated call, no paid API credit spent.
#   wide=True             the user explicitly asked for the wider search. Federation runs,
#                         bounded and cached. In-domain -> "augment" (local + federated fused);
#                         out-of-domain -> "replace" (federated only, local dense floor off).
#
# Federation is NEVER triggered implicitly. An out-of-domain query gets a HONEST WARNING, not
# an automatic multi-minute paid search the user did not ask for.

def search_two_tier(retriever, query, subject=None, mode="novelty", config="hybrid",
                    wide=False, detect_domain=True, use_cache=True, on_event=None,
                    do_rerank=None, topk=1000, **search_kw):
    """Two-tier search: local index first, federation as an opt-in fallback/expansion.

    -> retrieval.Result, with `.domain`, `.federation` and `.external` populated as applicable,
       plus a `federation_offered` attribute the UI can branch on.

    Never raises on federation failure — a federation outage degrades to local-only results.
    """
    verdict = None
    if detect_domain:
        try:
            import domain_detect
            verdict = domain_detect.detect(query, retriever=retriever)
        except Exception:
            verdict = None            # detector failure must not break search

    local = retriever.search(query, subject=subject, mode=mode, config=config,
                             do_rerank=do_rerank, topk=topk, **search_kw)
    local.domain = verdict.to_dict() if verdict else None

    if not wide:
        # cheap path: tell the caller federation is AVAILABLE, don't spend anything on it
        local.federation_offered = bool(verdict and verdict.should_federate)
        return local

    fed_mode = mode if mode in FED_MODES else "novelty"
    fed = search(query, mode=fed_mode, use_cache=use_cache, on_event=on_event)
    if not fed.ok:
        local.federation = fed.to_dict()          # carries the error for display
        local.federation_offered = True
        return local

    strategy = "replace" if (verdict is not None and not verdict.in_domain) else "augment"
    out = fuse(retriever, local, fed, strategy=strategy,
               do_rerank=bool(do_rerank), topk=topk)
    out.domain = local.domain
    out.federation_offered = False
    return out
