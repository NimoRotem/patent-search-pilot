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
            import torch
            torch.set_num_threads(4)              # use all cores for CPU inference
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
    """Return list of (index, score) sorted desc. Best-effort: reranking only re-orders the head,
    so ANY failure (no model, empty input, tokenizer 'Already borrowed' under contention) falls
    back to identity order rather than crashing the caller (a report generation)."""
    if not passages:                              # nothing to rerank
        return []
    import failclosed
    m = _load()
    identity = [(i, 0.0) for i in range(len(passages))]
    if m is None:
        #  IDENTITY ORDER IS NOT A RANKING. It is "we did not rank", and it is indistinguishable
        #  downstream from "the cross-encoder agreed with the incoming order", which is a real and
        #  very different statement. A benchmark run must not be scored on an unranked list.
        out = failclosed.fallback("rerank.rerank", "reranker model unavailable", identity,
                                  kind="rerank_identity")
    else:
        try:
            # cap length -> bge-reranker cost scales with sequence length; 256 tokens is plenty.
            pairs = [[(query or "")[:600], (p or "")[:600]] for p in passages]
            scores = m.compute_score(pairs, normalize=True, batch_size=16, max_length=256)
            if not isinstance(scores, list):
                scores = [scores]
            if len(scores) != len(passages):      # defensive: never trust the shape
                failclosed.fallback(
                    "rerank.rerank",
                    f"model returned {len(scores)} scores for {len(passages)} passages",
                    None, kind="rerank_bad_shape")
                return identity[:top_k] if top_k else identity
            out = sorted(enumerate(scores), key=lambda t: t[1], reverse=True)
        except Exception as e:                    # noqa — reranking is non-fatal
            out = failclosed.fallback(
                "rerank.rerank", f"{type(e).__name__}: {str(e)[:120]}", identity,
                kind="rerank_identity")
    return out[:top_k] if top_k else out
