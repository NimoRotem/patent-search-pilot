"""Vertex Batch request construction for the isolated niche embedding pipeline."""
from __future__ import annotations

import hashlib
import json
import math
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urlsplit

from .embedding import (
    EmbeddingSettings,
    batch_request_line,
    projected_cost_usd,
    submission_key,
)


class AmbiguousSubmission(RuntimeError):
    pass


class SubmissionRejected(RuntimeError):
    pass


class RestTransport:
    def __init__(self, timeout: float = 120.0):
        self.timeout = float(timeout)

    @staticmethod
    def _token() -> str:
        import google.auth
        from google.auth.transport.requests import Request

        credentials, _project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(Request())
        return credentials.token

    def get(self, url: str, params=None) -> dict:
        import httpx

        response = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=self.timeout,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Vertex GET failed: {response.status_code} {response.text[:300]}")
        return response.json()

    def post(self, url: str, body: dict) -> dict:
        import httpx

        response = httpx.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        if 400 <= response.status_code < 500:
            raise SubmissionRejected(
                f"Vertex POST rejected: {response.status_code} {response.text[:300]}"
            )
        if response.status_code >= 300:
            raise RuntimeError(f"Vertex POST failed: {response.status_code} {response.text[:300]}")
        return response.json()


def vertex_job_body(batch: dict) -> dict:
    submission = str(batch["submission_key"])
    return {
        "displayName": str(batch["display_name"])[:128],
        "labels": {
            "pipeline": "niche_full_v1",
            "submission": submission[:32],
        },
        "model": f"publishers/google/models/{batch['model']}",
        "inputConfig": {
            "instancesFormat": "jsonl",
            "gcsSource": {"uris": [str(batch["input_uri"])]},
        },
        "outputConfig": {
            "predictionsFormat": "jsonl",
            "gcsDestination": {"outputUriPrefix": str(batch["output_prefix"])},
        },
    }


class VertexBatchClient:
    TERMINAL = frozenset({
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
        "JOB_STATE_PARTIALLY_SUCCEEDED",
    })
    SUCCESS = frozenset({"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"})

    def __init__(
        self,
        *,
        project: str,
        location: str = "us-central1",
        transport=None,
    ):
        self.project = str(project)
        self.location = str(location)
        self.transport = transport or RestTransport()

    @property
    def collection_url(self) -> str:
        return (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}"
            f"/locations/{self.location}/batchPredictionJobs"
        )

    def collection_url_for(self, batch: dict) -> str:
        project = str(batch.get("gcp_project") or self.project)
        location = str(batch.get("gcp_location") or self.location)
        return (
            f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
            f"/locations/{location}/batchPredictionJobs"
        )

    @staticmethod
    def _matches(job: dict, batch: dict) -> bool:
        expected = vertex_job_body(batch)
        return (
            str(job.get("displayName") or "") == expected["displayName"]
            and dict(job.get("labels") or {}).get("submission")
            == expected["labels"]["submission"]
            and str(job.get("model") or "") == expected["model"]
            and list(((job.get("inputConfig") or {}).get("gcsSource") or {}).get("uris") or [])
            == [batch["input_uri"]]
            and str(
                ((job.get("outputConfig") or {}).get("gcsDestination") or {}).get(
                    "outputUriPrefix"
                ) or ""
            ) == str(batch["output_prefix"])
        )

    def find_matches(self, batch: dict) -> list[dict]:
        label = str(batch["submission_key"])[:32]
        response = self.transport.get(
            self.collection_url_for(batch),
            params={"filter": f"labels.submission={label}", "pageSize": "100"},
        )
        return [
            job
            for job in response.get("batchPredictionJobs") or []
            if self._matches(job, batch)
        ]

    def submit(self, batch: dict) -> dict:
        matches = self.find_matches(batch)
        if len(matches) > 1:
            raise AmbiguousSubmission("multiple Vertex jobs match one submission key")
        if matches:
            return matches[0]
        return self.create(batch)

    def create(self, batch: dict) -> dict:
        return self.transport.post(self.collection_url_for(batch), vertex_job_body(batch))

    def poll(self, job_name: str, batch: dict | None = None) -> dict:
        location = str((batch or {}).get("gcp_location") or self.location)
        url = f"https://{location}-aiplatform.googleapis.com/v1/{job_name}"
        return self.transport.get(url)


class GCSBatchStore:
    def __init__(self, client=None):
        if client is None:
            from google.cloud import storage
            client = storage.Client()
        self.client = client

    @staticmethod
    def _target(uri: str) -> tuple[str, str]:
        parsed = urlsplit(str(uri))
        if parsed.scheme != "gs" or not parsed.netloc or not parsed.path.lstrip("/"):
            raise ValueError("batch object URI must be a complete gs:// URI")
        return parsed.netloc, parsed.path.lstrip("/")

    def ensure_input(self, batch: dict, content: bytes) -> None:
        digest = hashlib.sha256(content).hexdigest()
        if digest != str(batch["request_digest"]):
            raise RuntimeError("batch input digest differs from the prepared database record")
        bucket_name, object_name = self._target(batch["input_uri"])
        blob = self.client.bucket(bucket_name).blob(object_name)
        if blob.exists():
            existing = blob.download_as_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise RuntimeError("deterministic batch input URI contains different content")
            return
        try:
            blob.upload_from_string(
                content,
                content_type="application/jsonl",
                if_generation_match=0,
            )
        except Exception as exc:
            if type(exc).__name__ not in {"PreconditionFailed", "Conflict"}:
                raise
            existing = blob.download_as_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise RuntimeError(
                    "concurrent batch input upload wrote different content"
                ) from exc

    def collect(self, output_directory: str, expected_keys) -> tuple[dict, dict]:
        bucket_name, prefix = self._target(output_directory.rstrip("/") + "/placeholder")
        prefix = prefix.rsplit("/placeholder", 1)[0].rstrip("/") + "/"
        expected = {str(value) for value in expected_keys}
        vectors, errors = {}, {}
        for blob in self.client.list_blobs(bucket_name, prefix=prefix):
            if not str(blob.name).endswith(".jsonl"):
                continue
            for raw_line in blob.download_as_bytes().decode("utf-8", "replace").splitlines():
                try:
                    line = json.loads(raw_line)
                except ValueError:
                    continue
                key = str(line.get("key") or "")
                if key not in expected or key in vectors or key in errors:
                    continue
                response = line.get("response") or {}
                values = ((response.get("embedding") or {}).get("values"))
                usage = response.get("usageMetadata") or response.get("usage_metadata") or {}
                token_value = (
                    response.get("tokenCount")
                    or usage.get("promptTokenCount")
                    or usage.get("prompt_token_count")
                )
                token_count = int(token_value) if token_value is not None else None
                if values:
                    vectors[key] = {"values": list(values), "token_count": token_count}
                else:
                    errors[key] = str(line.get("status") or "no embedding in response")[:500]
        for key in expected - set(vectors) - set(errors):
            errors[key] = "missing from batch output"
        return vectors, errors


class BudgetExhausted(RuntimeError):
    pass


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


def _vector_literal(values) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"


def project_embedding(values, dimension: int) -> list[float]:
    """Take the MRL prefix and normalize it for gemini-embedding-001."""
    target = max(1, int(dimension))
    source = [float(value) for value in values]
    if len(source) < target:
        raise ValueError("embedding has fewer values than the configured dimension")
    projected = source[:target]
    if not all(math.isfinite(value) for value in projected):
        raise ValueError("embedding contains a non-finite value")
    norm = math.sqrt(math.fsum(value * value for value in projected))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding has zero or invalid norm")
    return [value / norm for value in projected]


class BatchRepository:
    """Database-global budget, queue and result ledger for Gemini Batch work."""

    def __init__(self, connection_factory, settings: EmbeddingSettings):
        self.connection_factory = connection_factory
        self.settings = settings

    def prepare(self, *, force: bool = False) -> dict | None:
        settings = self.settings
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM niche_corpus.embedding_budget WHERE budget_key=%s FOR UPDATE",
                (settings.budget_key,),
            )
            budget = cursor.fetchone()
            if not budget:
                raise RuntimeError("embedding budget row is not initialized")
            if Decimal(str(budget["limit_usd"])) != settings.budget_limit_usd:
                raise RuntimeError("embedding budget environment does not match the database")
            cursor.execute(
                """
                SELECT embedding_key, text, token_estimate
                  FROM niche_corpus.niche_embedding_cache
                 WHERE status='pending' AND model=%s AND dimension=%s AND task_type=%s
                   AND EXISTS (
                       SELECT 1 FROM niche_corpus.niche_embedding_stage AS stage
                        WHERE stage.embedding_key=niche_embedding_cache.embedding_key
                          AND stage.active
                   )
                 ORDER BY embedding_key
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
                """,
                (settings.model, settings.dimension, settings.task_type, settings.batch_size),
            )
            items = [dict(row) for row in cursor.fetchall()]
            if not items or (not force and len(items) < settings.batch_min_items):
                return None
            key = submission_key([item["embedding_key"] for item in items], settings)
            lines = [
                json.dumps(
                    batch_request_line(item["embedding_key"], item["text"], settings),
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for item in items
            ]
            content = ("\n".join(lines) + "\n").encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            input_uri = f"gs://{settings.bucket}/{settings.prefix}/{key}/in.jsonl"
            output_prefix = f"gs://{settings.bucket}/{settings.prefix}/{key}/out/"
            display_name = f"niche-full-v1-{key[:24]}"
            estimated_tokens = sum(int(item["token_estimate"]) for item in items)
            reserved = _money(projected_cost_usd(estimated_tokens, settings))
            cursor.execute(
                """
                INSERT INTO niche_corpus.niche_embedding_batches
                    (submission_key,status,model,dimension,task_type,corpus_release,
                     request_digest,input_uri,output_prefix,display_name,n_items,
                     estimated_tokens,reserved_usd,budget_key,
                     price_usd_per_million_tokens,gcp_project,gcp_location)
                VALUES (%s,'prepared',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (submission_key) DO NOTHING
                RETURNING *
                """,
                (
                    key, settings.model, settings.dimension, settings.task_type,
                    settings.corpus_release, digest, input_uri, output_prefix, display_name,
                    len(items), estimated_tokens, reserved, settings.budget_key,
                    settings.price_usd_per_million_tokens,
                    settings.project, settings.location,
                ),
            )
            batch = cursor.fetchone()
            if batch is None:
                cursor.execute(
                    "SELECT * FROM niche_corpus.niche_embedding_batches WHERE submission_key=%s",
                    (key,),
                )
                batch = cursor.fetchone()
                return dict(batch) if batch else None
            cursor.execute(
                """
                UPDATE niche_corpus.embedding_budget
                   SET reserved_usd=reserved_usd + %s, updated_at=now()
                 WHERE budget_key=%s AND limit_usd=%s
                   AND spent_usd + reserved_usd + %s <= limit_usd
                RETURNING budget_key
                """,
                (reserved, settings.budget_key, settings.budget_limit_usd, reserved),
            )
            if cursor.fetchone() is None:
                raise BudgetExhausted("Gemini embedding budget is exhausted")
            cursor.executemany(
                "INSERT INTO niche_corpus.niche_embedding_batch_items "
                "(batch_id,item_index,embedding_key) VALUES (%s,%s,%s)",
                [
                    (batch["batch_id"], index, item["embedding_key"])
                    for index, item in enumerate(items)
                ],
            )
            cursor.execute(
                "UPDATE niche_corpus.niche_embedding_cache SET status='submitted', "
                "batch_id=%s, updated_at=now() WHERE embedding_key=ANY(%s)",
                (batch["batch_id"], [item["embedding_key"] for item in items]),
            )
            result = dict(batch)
            result["input_bytes"] = content
            return result

    def load(self, batch_id: int) -> dict | None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM niche_corpus.niche_embedding_batches WHERE batch_id=%s",
                (int(batch_id),),
            )
            batch = cursor.fetchone()
            if not batch:
                return None
            result = dict(batch)
            cursor.execute(
                """
                SELECT items.item_index, cache.embedding_key, cache.text
                  FROM niche_corpus.niche_embedding_batch_items AS items
                  JOIN niche_corpus.niche_embedding_cache AS cache
                    ON cache.embedding_key=items.embedding_key
                 WHERE items.batch_id=%s ORDER BY items.item_index
                """,
                (int(batch_id),),
            )
            items = [dict(row) for row in cursor.fetchall()]
        lines = [
            json.dumps(
                {
                    "key": str(item["embedding_key"]),
                    "request": {
                        "content": {"parts": [{"text": str(item["text"])}]},
                        "embed_content_config": {
                            "output_dimensionality": int(result["dimension"]),
                            "task_type": str(result["task_type"]),
                        },
                    },
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for item in items
        ]
        result["items"] = items
        result["input_bytes"] = ("\n".join(lines) + "\n").encode("utf-8")
        return result

    def begin_submission(self, batch_id: int | None = None) -> dict | None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT batch_id FROM niche_corpus.niche_embedding_batches
                     WHERE status='prepared' AND (%s IS NULL OR batch_id=%s)
                     ORDER BY batch_id
                     FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE niche_corpus.niche_embedding_batches AS batches
                   SET status='submitting', updated_at=now()
                  FROM candidate WHERE batches.batch_id=candidate.batch_id
                RETURNING batches.*
                """,
                (batch_id, batch_id),
            )
            row = cursor.fetchone()
        return self.load(row["batch_id"]) if row else None

    def batches(self, statuses) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM niche_corpus.niche_embedding_batches "
                "WHERE status=ANY(%s) ORDER BY batch_id",
                (list(statuses),),
            )
            return [dict(row) for row in cursor.fetchall()]

    def adopt(self, batch_id: int, job: dict) -> None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE niche_corpus.niche_embedding_batches SET status='submitted', "
                "provider_job_name=%s, provider_state=%s, updated_at=now() "
                "WHERE batch_id=%s AND status IN ('submitting','submitted','ambiguous')",
                (job.get("name"), job.get("state"), int(batch_id)),
            )

    def ambiguous(self, batch_id: int, error: str) -> None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE niche_corpus.niche_embedding_batches SET status='ambiguous', "
                "last_error=%s, updated_at=now() "
                "WHERE batch_id=%s AND status IN ('submitting','ambiguous')",
                (str(error)[:2000], int(batch_id)),
            )

    def retry_prepared(self, batch_id: int, error: str) -> None:
        """Return work only when failure happened before a provider POST was attempted."""
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE niche_corpus.niche_embedding_batches SET status='prepared', "
                "last_error=%s, updated_at=now() "
                "WHERE batch_id=%s AND status='submitting'",
                (str(error)[:2000], int(batch_id)),
            )

    def update_provider_state(self, batch_id: int, state: str) -> None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE niche_corpus.niche_embedding_batches SET provider_state=%s, "
                "updated_at=now() WHERE batch_id=%s",
                (str(state), int(batch_id)),
            )

    def settle_success(self, batch_id: int, vectors: dict, errors: dict) -> dict:
        errors = dict(errors)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM niche_corpus.niche_embedding_batches "
                "WHERE batch_id=%s FOR UPDATE",
                (int(batch_id),),
            )
            batch = cursor.fetchone()
            if not batch:
                return {"ok": 0, "failed": 0}
            valid_vectors = {}
            for key, value in vectors.items():
                try:
                    projected = project_embedding(value["values"], int(batch["dimension"]))
                except (TypeError, ValueError) as exc:
                    errors[key] = str(exc)
                    continue
                valid_vectors[key] = value
                cursor.execute(
                    "UPDATE niche_corpus.niche_embedding_cache SET status='complete', "
                    "vector=%s::vector, actual_tokens=%s, last_error=NULL, updated_at=now() "
                    "WHERE embedding_key=%s AND batch_id=%s",
                    (
                        _vector_literal(projected),
                        int(value.get("token_count") or 0),
                        key,
                        int(batch_id),
                    ),
                )
            if errors:
                cursor.executemany(
                    "UPDATE niche_corpus.niche_embedding_cache SET status='failed', "
                    "last_error=%s, updated_at=now() WHERE embedding_key=%s AND batch_id=%s",
                    [(str(error)[:1000], key, int(batch_id)) for key, error in errors.items()],
                )
            cursor.execute(
                """
                UPDATE niche_corpus.niche_embedding_stage AS stage
                   SET status=CASE cache.status WHEN 'complete' THEN 'complete' ELSE 'failed' END,
                       updated_at=now()
                  FROM niche_corpus.niche_embedding_cache AS cache
                 WHERE cache.embedding_key=stage.embedding_key AND cache.batch_id=%s
                """,
                (int(batch_id),),
            )
            token_counts = [value.get("token_count") for value in vectors.values()]
            actual_tokens = sum(int(value) for value in token_counts if value is not None)
            price = Decimal(str(batch["price_usd_per_million_tokens"]))
            actual = _money(
                Decimal(actual_tokens) * price / Decimal(1_000_000)
            )
            reserved = Decimal(str(batch["reserved_usd"]))
            charged = reserved if errors or any(value is None for value in token_counts) else actual
            if charged > reserved:
                raise RuntimeError("reported Gemini tokens exceed the conservative reservation")
            budget_key = str(batch["budget_key"] or "")
            if not budget_key:
                raise RuntimeError("embedding batch is missing its budget key")
            if batch["status"] == "succeeded":
                old_actual = Decimal(str(batch["actual_usd"]))
                cursor.execute(
                    "UPDATE niche_corpus.embedding_budget SET "
                    "spent_usd=spent_usd-%s+%s, updated_at=now() "
                    "WHERE budget_key=%s RETURNING budget_key",
                    (old_actual, charged, budget_key),
                )
            else:
                cursor.execute(
                    "UPDATE niche_corpus.embedding_budget SET reserved_usd=reserved_usd-%s, "
                    "spent_usd=spent_usd+%s, updated_at=now() "
                    "WHERE budget_key=%s RETURNING budget_key",
                    (reserved, charged, budget_key),
                )
            if cursor.fetchone() is None:
                raise RuntimeError("embedding batch budget row is missing")
            cursor.execute(
                "UPDATE niche_corpus.niche_embedding_batches SET status='succeeded', "
                "n_items=n_items, actual_tokens=%s, actual_usd=%s, "
                "provider_state='JOB_STATE_SUCCEEDED', completed_at=now(), updated_at=now(), "
                "last_error=%s WHERE batch_id=%s",
                (
                    actual_tokens,
                    charged,
                    (f"{len(errors)} item failures" if errors else None),
                    int(batch_id),
                ),
            )
        return {"ok": len(valid_vectors), "failed": len(errors)}

    def settle_failure(self, batch_id: int, state: str, error: str) -> None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM niche_corpus.niche_embedding_batches "
                "WHERE batch_id=%s FOR UPDATE",
                (int(batch_id),),
            )
            batch = cursor.fetchone()
            if not batch or batch["status"] == "failed":
                return
            reserved = Decimal(str(batch["reserved_usd"]))
            budget_key = str(batch["budget_key"] or "")
            if not budget_key:
                raise RuntimeError("embedding batch is missing its budget key")
            cursor.execute(
                "UPDATE niche_corpus.niche_embedding_cache SET status='failed', "
                "last_error=%s, updated_at=now() WHERE batch_id=%s",
                (str(error)[:1000], int(batch_id)),
            )
            cursor.execute(
                "UPDATE niche_corpus.niche_embedding_stage AS stage SET status='failed', "
                "updated_at=now() FROM niche_corpus.niche_embedding_cache AS cache "
                "WHERE cache.embedding_key=stage.embedding_key AND cache.batch_id=%s",
                (int(batch_id),),
            )
            cursor.execute(
                "UPDATE niche_corpus.embedding_budget SET reserved_usd=reserved_usd-%s, "
                "spent_usd=spent_usd+%s, updated_at=now() "
                "WHERE budget_key=%s RETURNING budget_key",
                (reserved, reserved, budget_key),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("embedding batch budget row is missing")
            cursor.execute(
                "UPDATE niche_corpus.niche_embedding_batches SET status='failed', "
                "provider_state=%s, actual_usd=%s, last_error=%s, completed_at=now(), "
                "updated_at=now() WHERE batch_id=%s",
                (str(state), reserved, str(error)[:2000], int(batch_id)),
            )

    def status(self) -> dict:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, count(*) n FROM niche_corpus.niche_embedding_cache "
                "GROUP BY status"
            )
            cache = {row["status"]: int(row["n"]) for row in cursor.fetchall()}
            cursor.execute(
                "SELECT status, count(*) n FROM niche_corpus.niche_embedding_batches "
                "GROUP BY status"
            )
            batches = {row["status"]: int(row["n"]) for row in cursor.fetchall()}
            cursor.execute(
                "SELECT limit_usd,reserved_usd,spent_usd FROM niche_corpus.embedding_budget "
                "WHERE budget_key=%s",
                (self.settings.budget_key,),
            )
            budget = dict(cursor.fetchone() or {})
        return {"cache": cache, "batches": batches, "budget": budget}
