"""Fail-closed configuration and deterministic identities for Gemini Batch embeddings."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _positive_decimal(env: Mapping[str, str], name: str) -> Decimal:
    raw = _required(env, name)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if not value.is_finite() or value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class EmbeddingSettings:
    model: str
    dimension: int
    task_type: str
    corpus_release: str
    budget_key: str
    budget_limit_usd: Decimal
    price_usd_per_million_tokens: Decimal
    bucket: str
    prefix: str
    project: str
    location: str
    expected_database: str
    database_fingerprint: str
    batch_size: int
    batch_min_items: int
    max_active_batches: int
    ambiguity_grace_seconds: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]):
        model = _required(env, "GEMINI_EMBED_MODEL")
        try:
            dimension = int(_required(env, "GEMINI_EMBED_DIMENSION"))
        except ValueError as exc:
            raise RuntimeError("GEMINI_EMBED_DIMENSION must be an integer") from exc
        if dimension != 768:
            raise RuntimeError(
                "GEMINI_EMBED_DIMENSION must be 768 for the niche_full_v1 vector tables"
            )
        release = _required(env, "NICHE_CORPUS_RELEASE")
        return cls(
            model=model,
            dimension=dimension,
            task_type=_required(env, "GEMINI_EMBED_TASK_TYPE"),
            corpus_release=release,
            budget_key=str(env.get("GEMINI_EMBED_BUDGET_KEY") or release).strip(),
            budget_limit_usd=_positive_decimal(env, "MAX_GEMINI_EMBED_USD_TOTAL"),
            price_usd_per_million_tokens=_positive_decimal(
                env, "GEMINI_EMBED_PRICE_USD_PER_MTOK"
            ),
            bucket=_required(env, "GEMINI_BATCH_BUCKET"),
            prefix=str(env.get("GEMINI_BATCH_PREFIX") or f"{release}/embed_batch").strip("/"),
            project=str(env.get("GCP_PROJECT") or "nimo-gpt").strip(),
            location=str(env.get("GEMINI_BATCH_LOCATION") or "us-central1").strip(),
            expected_database=_required(env, "NICHE_EXPECTED_DATABASE"),
            database_fingerprint=_required(env, "NICHE_DATABASE_FINGERPRINT"),
            batch_size=max(1, min(30_000, int(env.get("GEMINI_BATCH_SIZE") or "5000"))),
            batch_min_items=max(
                1, min(30_000, int(env.get("GEMINI_BATCH_MIN_ITEMS") or "1000"))
            ),
            max_active_batches=max(
                1, min(16, int(env.get("MAX_ACTIVE_GEMINI_BATCHES") or "4"))
            ),
            ambiguity_grace_seconds=max(
                60, int(env.get("GEMINI_SUBMISSION_AMBIGUITY_GRACE_SECONDS") or "600")
            ),
        )


def embedding_key(content_hash: str, model: str, dimension: int, task_type: str) -> str:
    material = "\x1f".join((str(content_hash), str(model), str(int(dimension)), str(task_type)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def submission_key(embedding_keys, settings: EmbeddingSettings) -> str:
    material = {
        "corpus_release": settings.corpus_release,
        "dimension": settings.dimension,
        "embedding_keys": sorted({str(value) for value in embedding_keys}),
        "model": settings.model,
        "task_type": settings.task_type,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def batch_request_line(item_key: str, text: str, settings: EmbeddingSettings) -> dict:
    return {
        "key": str(item_key),
        "request": {
            "content": {"parts": [{"text": str(text)}]},
            "embed_content_config": {
                "output_dimensionality": settings.dimension,
                "task_type": settings.task_type,
            },
        },
    }


def conservative_token_estimate(text: str) -> int:
    """Treat every UTF-8 byte as a token, an intentional upper bound for budget reservation."""
    return max(1, len(str(text).encode("utf-8")))


def projected_cost_usd(token_count: int, settings: EmbeddingSettings) -> Decimal:
    return (
        Decimal(max(0, int(token_count)))
        * settings.price_usd_per_million_tokens
        / Decimal(1_000_000)
    )


def embedding_rows(chunks, settings: EmbeddingSettings) -> tuple[list[dict], list[dict]]:
    """Return one cache row per unique text and one stage row per chunk occurrence."""
    cache_by_key = {}
    stage = []
    for chunk in chunks:
        key = embedding_key(
            chunk["content_hash"],
            settings.model,
            settings.dimension,
            settings.task_type,
        )
        cache_by_key.setdefault(key, {
            "embedding_key": key,
            "content_hash": str(chunk["content_hash"]),
            "model": settings.model,
            "dimension": settings.dimension,
            "task_type": settings.task_type,
            "text": str(chunk["text"]),
            "token_estimate": conservative_token_estimate(chunk["text"]),
        })
        stage.append({
            "chunk_id": str(chunk["chunk_id"]),
            "embedding_key": key,
            "model": settings.model,
            "dimension": settings.dimension,
            "task_type": settings.task_type,
            "corpus_release": settings.corpus_release,
        })
    return list(cache_by_key.values()), stage
