"""Thin LLM helper for the agent's language tasks (spec §7): query generation, terminology/
synonyms, CPC suggestions, cross-lingual translation. Deterministic code (dates/dedup/budget/
scoring/stopping) lives elsewhere — NOT here.

Provider: Vertex AI `gemini-2.5-flash` via the GCE service account (OpenAI account is quota-blocked)."""
from __future__ import annotations
import json, threading
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

AGENT_MODEL = "gemini-2.5-flash"
_local = threading.local()
def _client():
    if not hasattr(_local, "c"):
        from google import genai
        _local.c = genai.Client(vertexai=True, project="nimo-gpt", location="us-central1")
    return _local.c

_usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
def usage():
    return dict(_usage)


@retry(wait=wait_exponential(min=1, max=20), stop=stop_after_attempt(5),
       retry=retry_if_exception_type(Exception))
def _call(system, user, max_tokens):
    from google.genai.types import GenerateContentConfig, ThinkingConfig
    # gemini-2.5-flash is a thinking model; thinking tokens eat the output budget and truncate
    # the JSON -> disable thinking for these short structured tasks.
    resp = _client().models.generate_content(
        model=AGENT_MODEL, contents=user,
        config=GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                     temperature=0.2, max_output_tokens=max_tokens,
                                     thinking_config=ThinkingConfig(thinking_budget=0)))
    return resp


def chat_json(system, user, max_tokens=1200):
    try:
        resp = _call(system, user, max_tokens)
    except Exception:
        return {}
    _usage["calls"] += 1
    um = getattr(resp, "usage_metadata", None)
    if um:
        _usage["prompt_tokens"] += getattr(um, "prompt_token_count", 0) or 0
        _usage["completion_tokens"] += getattr(um, "candidates_token_count", 0) or 0
    try:
        return json.loads(resp.text)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Document ingestion helpers (front-door upload / patent-link search).
#   condense_for_search : full document text  -> a compact multi-angle search brief
#   describe_figures     : drawing images      -> a technical description of the drawings
# Both mirror the federated app's approach (patents-app/llm.py) but are SYNCHRONOUS to fit
# this Flask app, and reuse the same Vertex gemini-2.5-flash client/usage accounting above.
# ---------------------------------------------------------------------------
_CONDENSE_SYS = (
    "You are preparing a prior-art SEARCH BRIEF from the full text of a patent or technical "
    "document. Read it and produce a compact, search-optimized disclosure that a patent search "
    "engine can decompose into queries. Include, as flowing prose (NOT headings): a 1-2 sentence "
    "summary of the invention; the key technical features / independent-claim elements; the "
    "mechanism / how it works; and important concepts, materials and SYNONYMS a searcher might "
    "use. 200-400 words. Cover multiple angles (structure, function, application) so downstream "
    "queries are diverse. "
    'Return ONLY JSON: {"disclosure":"<the brief>","title":"<short invention title>"}.'
)


def condense_for_search(full_text: str) -> dict:
    """Turn a long document into a rich, multi-angle search disclosure. Fail-soft: returns a
    truncation of the input on error so a document always yields SOME query text."""
    ft = (full_text or "").strip()
    if not ft:
        return {"disclosure": "", "title": ""}
    # Feed the most informative slices: the head (title/abstract/claims usually near the top)
    # plus a middle slice, capped for token budget.
    head = ft[:12000]
    mid = ft[len(ft) // 2: len(ft) // 2 + 4000] if len(ft) > 16000 else ""
    d = chat_json(_CONDENSE_SYS, f"DOCUMENT TEXT:\n{head}\n\n{mid}", max_tokens=1400) or {}
    disc = (d.get("disclosure") or "").strip()
    return {"disclosure": disc or ft[:4000], "title": (d.get("title") or "").strip()}


_FIGURES_SYS = (
    "You are a patent examiner describing the DRAWINGS of a patent or technical document for a "
    "prior-art search. From the figure image(s) provided, describe what they depict: the "
    "components and parts shown, how they are arranged and connected, and the mechanism, "
    "structure or process illustrated. Use precise engineering vocabulary and the SYNONYMS a "
    "searcher would use, so the description can enrich a text search query. 120-220 words of "
    "flowing prose, no headings. Describe only what is visibly shown; do NOT invent reference "
    "numerals or text you cannot read. If the figures are too unclear to interpret, say so in "
    "one sentence."
)


@retry(wait=wait_exponential(min=1, max=20), stop=stop_after_attempt(3),
       retry=retry_if_exception_type(Exception))
def _call_vision(system, parts, max_tokens):
    from google.genai.types import GenerateContentConfig, ThinkingConfig
    return _client().models.generate_content(
        model=AGENT_MODEL, contents=parts,
        config=GenerateContentConfig(system_instruction=system, temperature=0.2,
                                     max_output_tokens=max_tokens,
                                     thinking_config=ThinkingConfig(thinking_budget=0)))


def describe_figures(image_blobs, context: str = "", max_images: int = 4) -> str:
    """Vision pass over extracted drawing PNGs -> a technical description of the drawings.

    HONEST SCOPE: the corpus is text-embedding based. This does NOT do image similarity; it
    turns the drawings into descriptive TEXT that is folded into the search query so retrieval
    reflects the figures as well as the prose. Fail-soft: returns "" on any error or no images.
    """
    from google.genai import types
    blobs = [b for b in (image_blobs or []) if b][:max_images]
    if not blobs:
        return ""
    parts = [types.Part.from_bytes(data=b, mime_type="image/png") for b in blobs]
    hint = ("Context (from the document text): " + context[:400]) if context else \
        "Describe these patent figures for prior-art search."
    parts.append(hint)
    try:
        resp = _call_vision(_FIGURES_SYS, parts, max_tokens=700)
    except Exception:
        return ""
    _usage["calls"] += 1
    um = getattr(resp, "usage_metadata", None)
    if um:
        _usage["prompt_tokens"] += getattr(um, "prompt_token_count", 0) or 0
        _usage["completion_tokens"] += getattr(um, "candidates_token_count", 0) or 0
    try:
        return (resp.text or "").strip()
    except Exception:
        return ""
