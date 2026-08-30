"""Which model does which part of the work, as a setting rather than a deployment.

WHY THIS EXISTS. The routing was real but invisible and immovable: three tiers defined at import
from environment variables, and every stage naming its tier in code. That is fine until a provider
goes down, and then it is silently wrong. On 2026-08-27 the Anthropic key hit its spend limit and
the STRONG tier degraded to `gemini-2.5-flash`, the same model the fast tier already uses, for the
refuter, the second look and the concise-description text that gets FILED with the USPTO. Nothing
raised, no page said so, and the only way to find out was to read a provider-error count out of a
finished report.

So the routing is now a settings file that the pool reads on every call, and a page that shows what
is actually resolved, including which providers can answer right now.

TWO LEVELS, and they are different questions:

    tiers   which providers serve `fast`, `read` and `strong`, in preference order
    stages  which TIER each stage of the pipeline asks for

Precedence is settings file, then environment variable, then the code default, so an operator who
has never opened the page gets exactly today's behaviour and a deploy cannot quietly overwrite a
deliberate choice.
"""
from __future__ import annotations

import json
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("MODEL_SETTINGS_PATH", os.path.join(ROOT, "data", "model_settings.json"))
TTL = float(os.environ.get("MODEL_SETTINGS_TTL", "5"))

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}

#  EVERY LANGUAGE-MODEL STAGE IN THE PIPELINE, in the order a search meets them. `tier` is the
#  code's own default and is what the page shows as "default" beside whatever is set.
#  `weight` is the share of a deep search's wall clock the stage accounted for on the measured run
#  of 2026-08-27 (871 s), so the page can say where the time actually goes rather than implying
#  every stage costs the same.
STAGES = [
    {"key": "query_planning", "tier": "fast", "weight": 0.02,
     "label": "Planning the search",
     "what": "Splits the invention into elements and generates the query vocabulary."},
    {"key": "document_brief", "tier": "fast", "weight": 0.03,
     "label": "Writing the search brief",
     "what": "Condenses an uploaded or linked document into the text the whole cascade "
             "searches with. Everything downstream inherits this."},
    {"key": "figure_reading", "tier": "fast", "weight": 0.01, "vision": True,
     "label": "Reading the drawings",
     "what": "Describes the figures for the viewer and the image channel. It no longer writes "
             "the search brief when the document has its own claims."},
    {"key": "wrapper_read", "tier": "strong", "weight": 0.02, "vision": True,
     "label": "Reading the file wrapper",
     "what": "Transcribes the IDS and any office action. Every number on those forms is a "
             "reference an examiner already considered, so a dropped row is art the search then "
             "has to rediscover by similarity."},
    {"key": "screening", "tier": "fast", "weight": 0.05,
     "label": "Screening candidates",
     "what": "Scores a couple of thousand candidates from title and abstract. Volume work, and "
             "every answer is gated again downstream."},
    {"key": "reading", "tier": "read", "weight": 0.31,
     "label": "Reading references in full",
     "what": "The pass that MAKES the evidence: finds each teaching in up to 90,000 characters "
             "and quotes it. A cheaper model here costs report cells, not seconds."},
    {"key": "evidence_sweep", "tier": "fast", "weight": 0.33,
     "label": "The evidence sweep",
     "what": "Re-reads the strongest references against every single requirement, in batches."},
    {"key": "claim_rescue", "tier": "fast", "weight": 0.02,
     "label": "Claim rescue",
     "what": "Plans a second retrieval round for claim limitations nothing has reached."},
    {"key": "refuting", "tier": "strong", "weight": 0.02,
     "label": "Refuting a finding",
     "what": "Tries to knock down a disclosure the reader asserted. Its output IS the assertion, "
             "so this is a place to spend."},
    {"key": "second_look", "tier": "strong", "weight": 0.02,
     "label": "The second look",
     "what": "Re-examines the closest references against the claim elements."},
    {"key": "concise_description", "tier": "strong", "weight": 0.01,
     "label": "Writing the filed text",
     "what": "Drafts the 'relevant disclosure' wording in the 37 CFR 1.290 submission. This text "
             "is read by a USPTO examiner."},
]
STAGE_BY_KEY = {s["key"]: s for s in STAGES}
TIERS = ("fast", "read", "strong")


def _read():
    try:
        with open(PATH) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load(force=False) -> dict:
    """The settings file, cached briefly. Never raises: a broken file means default behaviour."""
    now = time.time()
    with _lock:
        if not force and _cache["data"] is not None and now - _cache["at"] < TTL:
            return _cache["data"]
        d = _read()
        _cache["at"], _cache["data"] = now, d
        return d


def save(data: dict) -> dict:
    """Write the settings and drop the cache. Only known tiers and known stages survive."""
    tiers = {t: [str(x) for x in (data.get("tiers") or {}).get(t) or [] if str(x).strip()]
             for t in TIERS}
    tiers = {t: v for t, v in tiers.items() if v}
    stages = {k: v for k, v in (data.get("stages") or {}).items()
              if k in STAGE_BY_KEY and v in TIERS}
    out = {"tiers": tiers, "stages": stages, "saved_at": time.time()}
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(out, fh, indent=1)
    os.replace(tmp, PATH)                        # atomic: a half-written file must not be read
    with _lock:
        _cache["at"], _cache["data"] = 0.0, None
    return out


def tier_providers(tier: str, default: list) -> list:
    """Provider names for a tier: the setting if there is one, else what the code resolved."""
    got = (load().get("tiers") or {}).get(tier)
    return [str(x) for x in got] if got else list(default)


def tier_for(stage_key: str, default: str = "fast") -> str:
    """The tier a stage should ask for. Unknown stage or unset -> the code's own default."""
    got = (load().get("stages") or {}).get(stage_key)
    return got if got in TIERS else default
