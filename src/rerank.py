"""Cross-encoder reranker (spec §6 step 4): bge-reranker-v2-m3, CPU. Pluggable + graceful
fallback so the pipeline runs even if the model can't load."""
from __future__ import annotations
import threading

_model = None
_lock = threading.Lock()
_failed = False


def _load():
    global _model, _failed
    if _model is not None or _failed:
        return _model
    with _lock:
        if _model is not None or _failed:
            return _model
        try:
            from FlagEmbedding import FlagReranker
            _model = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=False)
            print("[rerank] loaded bge-reranker-v2-m3 (CPU)")
        except Exception as e:  # noqa
            print(f"[rerank] model unavailable ({e}); using score-preserving fallback")
            _failed = True
    return _model


def available():
    return _load() is not None


def rerank(query: str, passages: list[str], top_k=None):
    """Return list of (index, score) sorted desc. Fallback: identity order, score 0."""
    m = _load()
    if m is None:
        out = [(i, 0.0) for i in range(len(passages))]
    else:
        pairs = [[query, p] for p in passages]
        scores = m.compute_score(pairs, normalize=True)
        if not isinstance(scores, list):
            scores = [scores]
        out = sorted(enumerate(scores), key=lambda t: t[1], reverse=True)
    return out[:top_k] if top_k else out
