"""Thin LLM helper for the agent's language tasks (spec §7): query generation, terminology/
synonyms, CPC suggestions, cross-lingual translation. Deterministic code (dates/dedup/budget/
scoring/stopping) lives elsewhere — NOT here.

Provider: Vertex AI `gemini-2.5-flash` via the GCE service account (OpenAI account is quota-blocked)."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
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
_usage_lock = threading.Lock()
_usage_scope = ContextVar("patent_llm_usage_scope", default=None)


def usage():
    """Usage for the active search scope, or process totals outside one.

    Agent jobs run concurrently in long-lived gunicorn workers. A process-global counter cannot
    enforce a per-search budget: after one 40-call job, every later job would start exhausted and
    silently skip refinement. Context-local accounting isolates each job while process totals are
    retained for operational telemetry outside a scope.
    """
    scoped = _usage_scope.get()
    if scoped is not None:
        return dict(scoped)
    with _usage_lock:
        return dict(_usage)


@contextmanager
def usage_session():
    """Start a zeroed, concurrency-safe per-search usage counter."""
    token = _usage_scope.set({"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    try:
        yield
    finally:
        _usage_scope.reset(token)


def _record_usage(prompt_tokens=0, completion_tokens=0):
    values = {
        "calls": 1,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
    }
    scoped = _usage_scope.get()
    if scoped is not None:
        for key, value in values.items():
            scoped[key] += value
    with _usage_lock:
        for key, value in values.items():
            _usage[key] += value


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
    """-> parsed JSON, or {} .

    {} IS AMBIGUOUS AND THAT AMBIGUITY HAS COST REAL EXPERIMENTS. Every caller reads it as "the
    model had nothing to say", and it is also what a 503, a quota refusal and a truncated response
    return. The tournament comparisons failed this way for a whole A/B: the JSON was truncated at
    max_tokens, every group returned {}, and the run reported a 40% ranking regression that was
    entirely a broken call.

    So the two cases are now separated. A transport or parse FAILURE goes through failclosed, which
    raises in benchmark mode and records the degradation in production. A model that genuinely
    returned empty JSON still returns {} and is not an error.
    """
    import failclosed
    try:
        resp = _call(system, user, max_tokens)
    except Exception as e:
        return failclosed.fallback("llm.chat_json", f"{type(e).__name__}: {str(e)[:160]}",
                                   {}, kind="llm_call_failed")
    um = getattr(resp, "usage_metadata", None)
    _record_usage(
        getattr(um, "prompt_token_count", 0) if um else 0,
        getattr(um, "candidates_token_count", 0) if um else 0,
    )
    text = getattr(resp, "text", None)
    if not text:
        return failclosed.fallback("llm.chat_json", "empty response body", {},
                                   kind="llm_empty_response")
    try:
        return json.loads(text)
    except Exception as e:
        #  Truncation is the common case and it looks exactly like an empty answer downstream.
        #  Throwing the whole response away is far worse than it sounds: measured on the fifty
        #  subject freeze, the disclosure extractor produced 21,364 characters for
        #  b65g_ep1795464a3, ran out of output tokens mid-object, and the subject was recorded as
        #  disclosing NOTHING. Two of the three unusable dev subjects failed this exact way, and
        #  both were claim-rich subjects, which is to say the ones that matter most.
        #  A truncated list is still a list up to the cut. Keep the complete prefix and SAY it is
        #  a prefix, so a caller can decide whether a prefix is acceptable; silently returning one
        #  as if it were whole is the failure this repair exists to avoid.
        obj = _salvage_json(text)
        if obj is not None:
            obj["_truncated"] = True
            print(f"[llm] SALVAGED a truncated response: {len(text)} chars, "
                  f"kept {sum(len(v) for v in obj.values() if isinstance(v, list))} list items; "
                  f"the result is a PREFIX, not a complete answer", flush=True)
            return obj
        return failclosed.fallback(
            "llm.chat_json",
            f"unparseable JSON ({type(e).__name__}); {len(text)} chars, "
            f"ends {text[-40:]!r}", {}, kind="llm_bad_json")


def _salvage_json(text):
    """The complete prefix of a truncated JSON object, or None.

    Handles the one shape that actually occurs: {"key": [ {...}, {...}, {"text": "Rele
    i.e. a top level object holding one array, cut off inside an element. Walks the text tracking
    string state and nesting depth, remembers the last position at which the array had just closed
    an element, and rebuilds from there. Deliberately narrow: a clever general repair would invent
    structure, and inventing structure in a metric denominator is worse than losing it.
    """
    if not text or "[" not in text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc, cut = 0, False, False, -1
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            #  depth 2 means we just closed an element INSIDE the array inside the object
            if depth == 2:
                cut = i
    if cut < 0:
        return None
    for suffix in ("]}", "}]}"):
        try:
            return json.loads(text[start:cut + 1] + suffix)
        except Exception:
            continue
    return None


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
    um = getattr(resp, "usage_metadata", None)
    _record_usage(
        getattr(um, "prompt_token_count", 0) if um else 0,
        getattr(um, "candidates_token_count", 0) if um else 0,
    )
    try:
        return (resp.text or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Query improvement: the search is only as good as what it is asked
# ---------------------------------------------------------------------------
_IMPROVE_SYS = (
    "You are improving the DESCRIPTION OF AN INVENTION that will be used as a prior-art search "
    "query against a semantic patent index. You are given what the user typed. Rewrite it so the "
    "engine can find the right art: keep every technical feature the user actually stated, add "
    "the standard terminology and SYNONYMS a patent examiner would use for each component, name "
    "the structure, the mechanism and the field of use, and spell out anything the user implied "
    "but left unsaid. "
    "Do NOT invent features the user did not describe, do not add a different embodiment, and do "
    "not turn it into claim language. 120-300 words of flowing prose, no headings, no bullets. "
    "Also list the specific questions that, if answered, would most improve the search — things "
    "the description leaves genuinely ambiguous. "
    'Return ONLY JSON: {"improved":"<the rewritten description>",'
    '"added":["what you added and why", ...],'
    '"questions":["a question that would sharpen the search", ...]}'
)


def improve_query(text: str) -> dict:
    """A typed query -> a fuller search description, plus what changed and what is still unclear.

    Retrieval here is semantic, so a three-word query retrieves the FIELD rather than the
    invention: "vacuum lifter" matches ten thousand documents equally well. Expanding the user's
    own words into the vocabulary the corpus is written in is the single cheapest improvement
    available to a search, and showing WHAT was added is what stops it being a black box — the
    user can reject an expansion that drifted.

    Fail-soft: returns the original text with no additions when the model is unavailable.
    """
    text = (text or "").strip()
    if len(text) < 8:
        return {"improved": text, "added": [], "questions": [], "changed": False}
    d = chat_json(_IMPROVE_SYS, text[:16000], max_tokens=1800) or {}
    improved = (d.get("improved") or "").strip()
    if not improved:
        return {"improved": text, "added": [], "questions": [], "changed": False}
    return {"improved": improved,
            "added": [str(x)[:300] for x in (d.get("added") or [])][:8],
            "questions": [str(x)[:300] for x in (d.get("questions") or [])][:6],
            "changed": improved.strip() != text}
