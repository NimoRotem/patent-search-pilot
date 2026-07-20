"""Listwise agentic reranker (task C).

The rest of the pipeline ranks POINTWISE: the bge cross-encoder scores each candidate against
the query in isolation (top-25), and the CoverageAgent scores element coverage per reference.
That misses cross-candidate context: a reference that looks strong alone may be a near-duplicate
of something better, or a broad independent reference may deserve promotion once the field is in
view. This module ranks SEVERAL candidates AT A TIME, each judged in the context of the others,
using an LLM (Vertex gemini-2.5-flash via ``llm.chat_json`` — thinking_budget=0, JSON mode).

Design (RankGPT-style sliding-window / bubble listwise reranker):
  * The merged, DEDUPED candidate set is larger than one prompt, so we rank in overlapping
    WINDOWS. The window slides from the BACK of the list to the FRONT; each window's LLM ranking
    lets its best items bubble upward, and the OVERLAP carries a promoted item into the next
    (higher) window, so a strong reference found deep in the list can climb to the very top over
    a pass. Multiple PASSES refine the order globally.
  * CONTEXT CARRY-OVER: every window prompt also shows the current LEADERS (the items already
    ranked above the window) so the batch is judged relative to the best art seen so far, not
    just within-window — e.g. a near-duplicate of a leader gets demoted, a reference that covers
    ground the leaders miss gets promoted.
  * CHANNEL-AGNOSTIC: candidates are plain dicts from ANY channel (federated API hit, semantic
    chunk hit, image-similarity hit). We rank on relevance to the invention only; the channel a
    reference came from is not used as a ranking signal. Runs AFTER dedup, on the merged set.
  * DETERMINISTIC + ROBUST: reranking only REORDERS. The output is verified to be a permutation
    of the input (same objects, none added/dropped). Any LLM failure/garbage falls back to the
    incoming order for that window; a corrupted global result falls back to the full incoming
    order. It never crashes and never silently drops a candidate.

The LLM-call count is bounded and reported by ``plan_calls`` / ``ListwiseConfig`` so callers can
budget cost before running.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------


@dataclass
class ListwiseConfig:
    window_size: int = 8      # candidates ranked together in one LLM call
    overlap: int = 3          # items shared between adjacent windows (carries promotions upward)
    passes: int = 2           # full back-to-front sweeps; more passes = better global order
    depth: Optional[int] = 40  # rerank only the top `depth` candidates; None = the whole set.
    #                            the tail (beyond depth) is kept in its incoming order and appended,
    #                            so the result is still a permutation of ALL candidates.
    n_leaders: int = 5        # how many current leaders to show a window as context
    passage_chars: int = 700  # per-candidate matched-text budget in the prompt
    max_llm_calls: Optional[int] = None  # hard cap; None = the natural bound from the params.
    # NOTE: sampling temperature is fixed at 0.2 by llm._call (shared client); this reranker does
    # not override it (doing so would require editing llm.py, out of scope for this module).

    def step(self) -> int:
        return max(1, self.window_size - self.overlap)


def plan_calls(n: int, cfg: ListwiseConfig) -> int:
    """Upper bound on LLM calls for `n` candidates under `cfg` — the cost the caller will pay.
    Independent of the LLM (pure arithmetic), so it can be shown/budgeted before running."""
    depth = n if cfg.depth is None else min(cfg.depth, n)
    if depth <= 1:
        return 0
    if depth <= cfg.window_size:
        windows = 1
    else:
        windows = math.ceil((depth - cfg.window_size) / cfg.step()) + 1
    natural = cfg.passes * windows
    return natural if cfg.max_llm_calls is None else min(natural, cfg.max_llm_calls)


# ---------------------------------------------------------------------------------------------
# Candidate field extraction (channel-agnostic)
# ---------------------------------------------------------------------------------------------
# Candidates come from different channels/builders, so we read fields defensively by trying a few
# common names. A candidate only needs a stable identity + some text; everything else is optional.

_ID_KEYS = ("family", "family_id", "sfid", "pub", "publication_number", "pid", "id")


def candidate_id(c: Any, id_key: Optional[str] = None) -> str:
    if id_key is not None:
        return str(_get(c, id_key))
    for k in _ID_KEYS:
        v = _get(c, k)
        if v not in (None, "", []):
            return str(v)
    # last resort: object identity so distinct dicts never collide
    return f"__obj_{id(c)}"


def _get(c: Any, key: str, default=None):
    if isinstance(c, dict):
        return c.get(key, default)
    return getattr(c, key, default)


def _first_str(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _matched_text(c: Any, budget: int) -> str:
    """The passage that actually matched — NOT just the title. Reasoning from titles alone is the
    exact defect this reranker must avoid, so we pull the matched claim/coordinate/abstract text
    through. Preference: explicit matched snippet > best claim text > abstract > description head."""
    parts: list[str] = []

    coord = _get(c, "match_coord") or _get(c, "matched_coord_raw")
    if isinstance(coord, str) and coord.strip():
        parts.append(coord.strip())
    elif isinstance(coord, dict):
        parts.append(_first_str(coord.get("text"), coord.get("passage"), coord.get("snippet")))

    # matched claim(s): prefer independent claims, else the first claim
    claims = _get(c, "claims")
    if isinstance(claims, list) and claims:
        indep = [cl for cl in claims if isinstance(cl, dict) and cl.get("independent")]
        pool = (indep or [cl for cl in claims if isinstance(cl, dict)])[:2]
        for cl in pool:
            parts.append(_first_str(cl.get("text"), cl.get("claim_text")))
    elif isinstance(claims, str):
        parts.append(claims)

    parts.append(_first_str(_get(c, "abstract"), _get(c, "snippet"), _get(c, "passage"),
                            _get(c, "matched_text")))

    desc = _get(c, "description")
    if isinstance(desc, list) and desc:
        parts.append(_first_str(*[d if isinstance(d, str) else _first_str(_get(d, "text")) for d in desc[:2]]))
    elif isinstance(desc, str):
        parts.append(desc)

    text = "  ".join(p for p in parts if p)
    text = " ".join(text.split())          # collapse whitespace
    return text[:budget] if text else ""


def _assignee(c: Any) -> str:
    a = _get(c, "assignees") or _get(c, "assignee") or _get(c, "applicants")
    if isinstance(a, list):
        return ", ".join(str(x) for x in a[:2] if x)
    return str(a) if a else ""


def _date(c: Any) -> str:
    return _first_str(_get(c, "publication_date"), _get(c, "priority_date"),
                      _get(c, "filing_date"), _get(c, "date"))


def _brief(c: Any, cfg: ListwiseConfig) -> str:
    """A compact, judgeable block for ONE candidate. Title + assignee + date + matched passage."""
    title = _first_str(_get(c, "title"), _get(c, "name")) or "(untitled)"
    who = _assignee(c)
    when = _date(c)
    head = title
    if who:
        head += f" — {who}"
    if when:
        head += f" ({when})"
    body = _matched_text(c, cfg.passage_chars)
    return head + ("\n  matched text: " + body if body else "\n  matched text: (none available)")


# ---------------------------------------------------------------------------------------------
# Query representation
# ---------------------------------------------------------------------------------------------


def _query_text(query: Any) -> str:
    """Accept a plain string OR a dict with elements/brief/claims (the query representation used
    upstream). Assemble a compact invention description the LLM ranks against."""
    if isinstance(query, str):
        return query.strip()[:4000]
    if not isinstance(query, dict):
        return str(query)[:4000]
    bits: list[str] = []
    b = _first_str(query.get("brief"), query.get("disclosure"), query.get("summary"),
                   query.get("query"), query.get("text"))
    if b:
        bits.append(b)
    els = query.get("elements")
    if isinstance(els, list) and els:
        bits.append("Key elements: " + "; ".join(str(e) for e in els if e))
    cl = query.get("claims")
    if isinstance(cl, str) and cl.strip():
        bits.append("Claims: " + cl.strip())
    elif isinstance(cl, list) and cl:
        txts = [_first_str(x if isinstance(x, str) else _get(x, "text")) for x in cl[:2]]
        bits.append("Claims: " + " ".join(t for t in txts if t))
    return "\n".join(bits)[:4000] or "(no query description)"


# ---------------------------------------------------------------------------------------------
# LLM prompt for one window
# ---------------------------------------------------------------------------------------------

_SYS = (
    "You are a patent prior-art examiner RANKING candidate references for relevance to a target "
    "invention. You are given the invention, optionally a few TOP references already ranked above "
    "this batch (context), and a numbered BATCH of candidate references with the actual text that "
    "matched. Rank the BATCH from most to least relevant to the invention. Judge on the MATCHED "
    "TEXT and technical substance, never on the title alone. Consider the batch AS A GROUP and "
    "relative to the top references: DEMOTE a candidate that is a near-duplicate of, or clearly "
    "weaker than, an already-higher reference; PROMOTE a candidate that independently discloses "
    "core elements of the invention or covers ground the top references miss. "
    'Return ONLY JSON: {"order": [ids most-relevant first]} where ids are the batch numbers. '
    "Include every batch id exactly once."
)


def _rank_window(query_text: str, window: Sequence[Any], leaders: Sequence[Any],
                 cfg: ListwiseConfig, chat_fn: Callable) -> list[int]:
    """Ask the LLM to order `window` (0-based indices returned). Falls back to identity
    [0..k-1] on any failure/garbage. Always returns a permutation of range(len(window))."""
    k = len(window)
    identity = list(range(k))
    if k <= 1:
        return identity

    lead_block = ""
    if leaders:
        lines = [f"  - {_brief(c, cfg)}" for c in leaders[: cfg.n_leaders]]
        lead_block = ("TOP REFERENCES ALREADY RANKED ABOVE THIS BATCH (context — do not re-rank "
                      "these, rank the batch relative to them):\n" + "\n".join(lines) + "\n\n")

    batch_lines = [f"[{i + 1}] {_brief(c, cfg)}" for i, c in enumerate(window)]
    user = (f"INVENTION:\n{query_text}\n\n{lead_block}"
            f"BATCH TO RANK ({k} candidates):\n" + "\n".join(batch_lines) +
            f"\n\nReturn the {k} batch numbers (1..{k}) ordered most- to least-relevant.")

    try:
        out = chat_fn(_SYS, user, max_tokens=400) or {}
    except Exception:
        return identity
    if not isinstance(out, dict):
        return identity
    raw = out.get("order")
    if not isinstance(raw, list):
        return identity

    # Robust parse: map 1-based ids -> 0-based, keep valid unique ones in given order, then append
    # any missing indices in their original order. Guarantees a permutation regardless of LLM output.
    seen: set[int] = set()
    order: list[int] = []
    for v in raw:
        try:
            idx = int(v) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < k and idx not in seen:
            seen.add(idx)
            order.append(idx)
    for i in identity:
        if i not in seen:
            order.append(i)
    return order


# ---------------------------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------------------------


def listwise_rerank(query: Any, candidates: Sequence[Any], *,
                    cfg: Optional[ListwiseConfig] = None,
                    id_key: Optional[str] = None,
                    chat_fn: Optional[Callable] = None,
                    on_progress: Optional[Callable[[int, int], None]] = None) -> list:
    """Reorder `candidates` by listwise relevance to `query`, in the context of one another.

    Args:
      query:      invention description — a string, or a dict with any of {brief/disclosure,
                  elements:[...], claims}. This is the query representation, NOT a candidate.
      candidates: merged, DEDUPED list of candidate references (dicts or objects) from ANY
                  channel. Each should carry a stable id field (family/pub/id) and some text
                  (title + claims/abstract/matched coord). Channel of origin is NOT a ranking
                  signal.
      cfg:        ListwiseConfig (window/overlap/passes/depth/max_llm_calls).
      id_key:     force the identity field used for the permutation check (default: auto-detect).
      chat_fn:    LLM callable (system, user, max_tokens=..)->dict. Defaults to llm.chat_json.
                  Injectable for tests/offline runs.
      on_progress: optional (calls_done, calls_total) callback for UI streaming.

    Returns:
      A NEW list containing exactly the input objects, reordered. Guaranteed to be a permutation
      of `candidates` (nothing added or dropped). On any failure the incoming order is preserved.
    """
    cfg = cfg or ListwiseConfig()
    cands = list(candidates)
    n = len(cands)
    if n <= 1:
        return cands

    if chat_fn is None:
        import llm
        chat_fn = llm.chat_json

    qtext = _query_text(query)

    depth = n if cfg.depth is None else min(cfg.depth, n)
    head = cands[:depth]           # the part we actually rerank
    tail = cands[depth:]           # kept as-is, appended to preserve the permutation

    calls_total = plan_calls(n, cfg)
    calls_done = 0

    def tick():
        nonlocal calls_done
        calls_done += 1
        if on_progress:
            try:
                on_progress(calls_done, calls_total)
            except Exception:
                pass

    step = cfg.step()
    budget = calls_total  # hard ceiling on LLM calls actually issued
    for _ in range(cfg.passes):
        # Slide the window from the BACK to the FRONT so strong items bubble upward and the
        # overlap carries a promoted item into the next (higher) window.
        end = len(head)
        while end > 0 and budget > 0:
            start = max(0, end - cfg.window_size)
            window = head[start:end]
            leaders = head[:start]                       # everything currently ranked above
            order = _rank_window(qtext, window, leaders, cfg, chat_fn)
            head[start:end] = [window[i] for i in order]
            budget -= 1
            tick()
            if start == 0:
                break
            end -= step

    result = head + tail

    # ---- permutation invariant: reranking must REORDER, never add/drop -----------------------
    if not _same_multiset(cands, result, id_key):
        # Should be unreachable (we only reorder slices), but if identity resolution collapsed two
        # distinct candidates we refuse to return a corrupted set — fall back to the input order.
        return cands
    return result


def _same_multiset(a: Sequence[Any], b: Sequence[Any], id_key: Optional[str]) -> bool:
    if len(a) != len(b):
        return False
    from collections import Counter
    ca = Counter(candidate_id(x, id_key) for x in a)
    cb = Counter(candidate_id(x, id_key) for x in b)
    return ca == cb


def _domain_in_domain(domain: Any) -> bool:
    """Interpret a domain verdict (a domain_detect.DomainVerdict OR its .to_dict()) -> in_domain
    bool. Defaults to True when the verdict is missing/garbled, so the OOD de-dilution filter is
    only ever applied on an EXPLICIT out-of-domain verdict — an absent verdict never silently
    demotes local results."""
    if domain is None:
        return True
    v = domain.get("in_domain") if isinstance(domain, dict) else getattr(domain, "in_domain", None)
    return True if v is None else bool(v)


def _relevancy_order(cards: Sequence[Any], *, in_domain: bool) -> list:
    """Final display order: per-result relevancy SCORE is the primary key (iptorch-style), the
    incoming LISTWISE position is the tiebreak (so the in-context work is preserved among equal
    scores), UNSCORED tail candidates sink below scored ones, and — only when the query is out of
    domain — local NOISE is demoted beneath everything relevant. A PERMUTATION of the input
    (nothing dropped)."""
    import relevancy
    import retrieval
    pos = {id(c): i for i, c in enumerate(cards)}   # listwise position = stable tiebreak

    def key(c):
        s = relevancy.get_score(c)
        s = s if s is not None else -1.0            # unscored -> below every scored card
        noise = (not in_domain) and retrieval.is_local_noise(c, score_key=relevancy.SCORE_KEY)
        return (1 if noise else 0, -float(s), pos[id(c)])

    return sorted(cards, key=key)


def rerank_report_cards(query: Any, cards: Sequence[dict], *,
                        cfg: Optional[ListwiseConfig] = None,
                        chat_fn: Optional[Callable] = None,
                        on_progress: Optional[Callable[[int, int], None]] = None,
                        domain: Any = None,
                        score_top_n: int = 40,   # align to ListwiseConfig.depth: every card the
                        #                        listwise pass reranks also gets a relevancy score, so a
                        #                        floated federated hit at head position 33-40 is not left
                        #                        UNSCORED (which would sink it below the display cut).
                        relevancy_batch: int = 5) -> list[dict]:
    """Produce the AUTHORITATIVE display order for the report's `cards` list, combining three
    signals in one place so the page/exports render a domain-expert-sensible ranking:

      1. OOD DE-DILUTION (cheap, no LLM): when the query is out of domain, float federated + any
         genuinely-relevant local card to the top and sink local noise, so the bounded relevancy
         scoring and the listwise window spend their budget on the hits that matter (see
         retrieval.deprioritize_ood_local). In-domain queries skip this entirely.
      2. LISTWISE rerank: order the candidates IN CONTEXT of one another (existing behaviour).
      3. PER-RESULT relevancy SCORE + OPINION on the top ``score_top_n`` shown candidates
         (relevancy.score_cards) — iptorch's ranking signal — then a final sort that makes the
         score the primary key and the listwise position the tiebreak, demoting score-0 / OOD noise
         to the bottom WITHOUT dropping anything.

    The OOD verdict comes from the ``domain`` argument, or (for callers that cannot change the
    call signature) from ``query["domain"]`` when `query` is a dict; absent -> treated as in-domain.

    Returns a new list; the input cards' ``rank`` is rewritten 1..N on the returned copies, and the
    scored cards carry ``relevancy_score`` / ``relevancy_opinion`` for display. Guaranteed to be a
    permutation of the input cards; on any internal inconsistency it falls back to the listwise
    order rather than returning a corrupted (dropped/duplicated) set."""
    cards = list(cards)
    if not cards:
        return []
    if domain is None and isinstance(query, dict):
        domain = query.get("domain")
    in_domain = _domain_in_domain(domain)

    import relevancy
    import retrieval

    # (1) OOD pre-pass — cheap partition before we spend any LLM budget.
    pre = retrieval.deprioritize_ood_local(cards, in_domain=in_domain, score_key=relevancy.SCORE_KEY)

    # (2) listwise in-context ordering.
    ordered = listwise_rerank(query, pre, cfg=cfg, chat_fn=chat_fn, on_progress=on_progress)

    # (3) per-result score + opinion on the top score_top_n (bounded LLM), then score-primary order.
    try:
        relevancy.score_cards(query, ordered, chat_fn=chat_fn,
                              batch_size=relevancy_batch, max_cards=score_top_n)
        final = _relevancy_order(ordered, in_domain=in_domain)
    except Exception:
        final = ordered

    # permutation guard: never drop/duplicate a card. Fall back to the listwise order if the
    # relevancy re-sort somehow produced a non-permutation.
    if not _same_multiset(cards, final, None):
        final = ordered

    out = []
    for i, c in enumerate(final, 1):
        d = dict(c)
        d["rank"] = i
        out.append(d)
    return out
