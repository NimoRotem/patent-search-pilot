"""Per-result relevancy score + written opinion (task C) — iptorch's ranking signal.

iptorch scores every candidate 0-100 with an LLM and writes a short "opinion" (why it is/ isn't
relevant, plus risk/avoid guidance), dedups by publication number, DROPS score-0 / abstract-less
hits and sorts by score desc. That per-result score is BOTH the displayed "Why relevant" text and
the sort key that makes its results read sensibly to an examiner.

This module reproduces that signal, but INTEGRATES with the existing listwise reranker rather than
replacing it (see rerank_listwise.rerank_report_cards):

  * the listwise pass orders candidates IN CONTEXT of one another (near-duplicates sink, an
    independently strong reference rises);
  * this module then ANNOTATES the top-N candidates that will actually be shown with a 0-100
    score + opinion and lets the score INFORM the final order — score is the primary key, the
    listwise position is the tiebreak;
  * score-0 / no-genuine-relevance candidates are DEMOTED to the bottom, never dropped, so the
    permutation invariant the listwise reranker guarantees still holds — nothing silently
    disappears from the result set.

Every candidate is judged on its REAL claim / matched-passage text (via rerank_listwise._brief),
NEVER the title alone: a prior bug had a rationale assert an adhesive wall-fixing patent disclosed
a "driver pin" purely from shared title words, so a title is never sufficient evidence here.

COST is bounded: scoring runs only on the top ``max_cards`` (the ones that will be displayed),
in small BATCHES, so the LLM-call count is ~ceil(max_cards / batch_size), never the whole pool.

FAIL-SOFT: llm.chat_json returns {} on any error; an unscored candidate then falls back to a
DETERMINISTIC score derived from the cosine-based ``relevancy`` already on the card, with a
templated opinion. An LLM outage therefore degrades to the existing pointwise (cosine) order and
never crashes and never contradicts the retrieval signal.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional, Sequence

import rerank_listwise as _rl

# Keys written onto each scored candidate. Kept distinct from webview's cosine ``relevancy`` so
# both survive on the card (cosine display relevancy vs. LLM examiner score+opinion).
SCORE_KEY = "relevancy_score"
OPINION_KEY = "relevancy_opinion"
SOURCE_KEY = "relevancy_source"   # "llm" | "fallback" — provenance for diagnostics/tests

# richer per-candidate text budget than the listwise window (we score one batch, not a whole list)
_CFG = _rl.ListwiseConfig(passage_chars=900)

_SYS = (
    "You are a patent prior-art examiner SCORING candidate references for relevance to a target "
    "invention. For EACH candidate you are given the actual text that matched (title, assignee, "
    "date and the real claim/abstract/passage text). Judge ONLY on that technical substance, "
    "NEVER on the title or shared words alone. "
    "Give an integer SCORE 0-100: 90-100 = discloses essentially the same invention / all core "
    "elements; 70-89 = strongly relevant, discloses most core elements; 40-69 = related field, "
    "some overlapping features; 1-39 = same broad area but different problem/solution; 0 = "
    "unrelated. Write a 1-2 sentence OPINION that names the SPECIFIC disclosed feature that makes "
    "it relevant (or why it is not), and any risk/avoid guidance for the examiner. Do not claim a "
    "feature the matched text does not actually show. "
    'Return ONLY JSON: {"results":[{"id":<batch number>,"score":<0-100>,"opinion":"<text>"}]} '
    "with one entry per candidate, every batch id exactly once."
)


def _fallback_score(cand: Any) -> int:
    """Deterministic score from signals already on the candidate (no LLM). Prefers the cosine-based
    display ``relevancy`` (0-100) so the fallback order equals the existing pointwise order; else
    maps the raw cosine ``match_score``; else a neutral-low 30."""
    r = _rl._get(cand, "relevancy")
    try:
        if r is not None:
            return int(max(0, min(100, round(float(r)))))
    except (TypeError, ValueError):
        pass
    ms = _rl._get(cand, "match_score")
    try:
        if ms is not None:
            pct = (float(ms) - 0.35) / (0.90 - 0.35) * 100.0
            return int(max(0, min(100, round(pct))))
    except (TypeError, ValueError):
        pass
    return 30


def _fallback_opinion(cand: Any, score: int) -> str:
    title = _rl._first_str(_rl._get(cand, "title"), _rl._get(cand, "name")) or "This reference"
    if score >= 70:
        band = "appears strongly relevant on its matched text"
    elif score >= 40:
        band = "is in a related area with some overlapping features"
    else:
        band = "appears only loosely related"
    return (f"{title} {band} (auto-estimated relevance {score}/100 from the retrieval match; "
            "an examiner review is advised).")


def score_one(query_text: str, cand: Any, chat_fn: Optional[Callable] = None) -> dict:
    """Score a SINGLE candidate. Convenience wrapper over score_batch (one-item batch); mainly for
    tests and ad-hoc use. Returns {"score":int,"opinion":str,"source":"llm"|"fallback"}."""
    return score_batch(query_text, [cand], chat_fn=chat_fn)[0]


def score_batch(query_text: str, batch: Sequence[Any],
                chat_fn: Optional[Callable] = None, max_tokens: int = 900) -> list[dict]:
    """Score a small batch of candidates in ONE LLM call. Returns a list aligned to `batch` of
    {"score":int 0-100,"opinion":str,"source":"llm"|"fallback"}. Any candidate the LLM did not
    return (or a whole-call failure) falls back deterministically — this never raises and always
    returns exactly len(batch) entries."""
    k = len(batch)
    results: list[Optional[dict]] = [None] * k
    if k == 0:
        return []

    if chat_fn is None:
        import llm
        chat_fn = llm.chat_json

    lines = [f"[{i + 1}] {_rl._brief(c, _CFG)}" for i, c in enumerate(batch)]
    user = (f"INVENTION:\n{query_text}\n\nCANDIDATE REFERENCES ({k}):\n" + "\n".join(lines) +
            f"\n\nScore all {k} candidates.")
    try:
        out = chat_fn(_SYS, user, max_tokens=max_tokens) or {}
    except Exception:
        out = {}

    if isinstance(out, dict) and isinstance(out.get("results"), list):
        for item in out["results"]:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("id")) - 1
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < k) or results[idx] is not None:
                continue
            try:
                score = int(max(0, min(100, round(float(item.get("score"))))))
            except (TypeError, ValueError):
                continue
            opinion = str(item.get("opinion") or "").strip()
            if not opinion:
                opinion = _fallback_opinion(batch[idx], score)
            results[idx] = {"score": score, "opinion": opinion[:600], "source": "llm"}

    for i in range(k):
        if results[i] is None:
            s = _fallback_score(batch[i])
            results[i] = {"score": s, "opinion": _fallback_opinion(batch[i], s), "source": "fallback"}
    return results  # type: ignore[return-value]


def score_cards(query: Any, cards: Sequence[Any], *,
                chat_fn: Optional[Callable] = None,
                batch_size: int = 5, max_cards: int = 25) -> list:
    """Annotate the TOP ``max_cards`` of `cards` (assumed already in display order) with
    ``relevancy_score`` (0-100), ``relevancy_opinion`` (str) and ``relevancy_source``. Candidates
    beyond ``max_cards`` are left UNSCORED (no key added) — they are the tail that the display trims
    off anyway, and leaving them unscored keeps the LLM-call count bounded to ~max_cards/batch_size.

    Mutates the card dicts in place (they are freshly-built view dicts) AND returns `cards` for
    chaining. Fail-soft throughout (see score_batch)."""
    qtext = _rl._query_text(query)
    n = min(max_cards, len(cards))
    for start in range(0, n, max(1, batch_size)):
        batch = cards[start:min(start + batch_size, n)]
        scored = score_batch(qtext, batch, chat_fn=chat_fn)
        for c, sc in zip(batch, scored):
            if isinstance(c, dict):
                c[SCORE_KEY] = sc["score"]
                c[OPINION_KEY] = sc["opinion"]
                c[SOURCE_KEY] = sc["source"]
    return list(cards)


def get_score(cand: Any) -> Optional[float]:
    """The LLM relevancy score on a candidate, or None if it was never scored."""
    s = _rl._get(cand, SCORE_KEY)
    if isinstance(s, (int, float)):
        return float(s)
    return None
