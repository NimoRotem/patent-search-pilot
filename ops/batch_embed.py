"""Gemini Batch embedding on Vertex: submit a job, poll it, collect it, requeue what failed.

WHY BATCH. Per item the sync `embed_content` call is fine and this pipeline uses it for small
queues. In aggregate it is not: the niche is millions of chunks, and a synchronous run holds a
process, a thread pool and an account-wide concurrency budget open for the whole of it, competing
with the description backfill for the same quota. A batch job holds nothing. Submit, record the
job name, and a worker that is killed five minutes later picks the job up from the database and
collects it.

THE PARTS THAT ARE NOT IN THE DOCUMENTATION, measured on 2026-08-22 against
`us-central1-aiplatform.googleapis.com/v1/.../batchPredictionJobs`:

  * `google-genai` 1.47 has `batches.create_embeddings`, and it raises
    "Vertex AI does not support batches.create_embeddings". The REST batchPredictionJobs endpoint
    does support `publishers/google/models/gemini-embedding-001`, so this calls it directly.
  * The input line must be `{"request": {...}}`. A `{"instances": [...], "parameters": {...}}`
    line, which is the shape every older Vertex embedding example uses, is accepted by the job
    submission and then fails EVERY LINE with
    "no such field: 'instances'" in `EmbedContentRequest`. Job succeeded, nothing embedded.
  * `outputDimensionality` goes INSIDE the request, not in `modelParameters`. Measured: 768 values
    out, norm 0.589, cosine distance 1.0e-15 from the same text through the synchronous API. So a
    batch vector and a sync vector are the same vector, and both are in the corpus's space.
  * The output carries no id of ours: it echoes the request verbatim. Correlation is therefore by
    request text. Two items with identical text are interchangeable, because the embedding is a
    deterministic function of the text, the task type and the dimension, all three constant within
    a job.
  * A failed line is `status` non-empty with an empty `response`, IN AN OTHERWISE SUCCEEDED JOB.
    That is the partial failure this requeues one item at a time rather than rerunning the job.
"""
from __future__ import annotations

import collections
import json
import time

import gcs_lite

TERMINAL = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED",
            "JOB_STATE_EXPIRED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
GOOD = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}


class Rest:
    """The one seam every test replaces. Real calls go out over httpx with an ADC bearer token."""

    def __init__(self, timeout=120.0):
        self.timeout = timeout

    def _client(self):
        import httpx
        return httpx.Client(timeout=self.timeout)

    def post(self, url, body):
        with self._client() as c:
            r = c.post(url, headers={"Authorization": f"Bearer {gcs_lite.token()}",
                                     "Content-Type": "application/json"}, json=body)
            if r.status_code >= 300:
                raise RuntimeError(f"POST {url}: {r.status_code} {r.text[:300]}")
            return r.json()

    def get(self, url):
        with self._client() as c:
            r = c.get(url, headers={"Authorization": f"Bearer {gcs_lite.token()}"})
            if r.status_code >= 300:
                raise RuntimeError(f"GET {url}: {r.status_code} {r.text[:300]}")
            return r.json()


class VertexBatchEmbedder:
    def __init__(self, model="gemini-embedding-001", dim=768,
                 task_type="RETRIEVAL_DOCUMENT", bucket="nimo-patents-v3",
                 prefix="embed_batch", project="nimo-gpt", location="us-central1",
                 rest=None, gcs=gcs_lite):
        self.model, self.dim, self.task_type = model, dim, task_type
        self.bucket, self.prefix = bucket, prefix
        self.project, self.location = project, location
        self.rest = rest or Rest()
        self.gcs = gcs

    # ----------------------------------------------------------------- addresses
    @property
    def base(self):
        return (f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}"
                f"/locations/{self.location}/batchPredictionJobs")

    def request_line(self, text):
        return {"request": {"content": {"parts": [{"text": text}]},
                            "outputDimensionality": self.dim,
                            "taskType": self.task_type}}

    # ----------------------------------------------------------------- submit
    def submit(self, items, tag=None):
        """`items`: `[{item_key, text}]`. -> `{job_name, input_uri, output_prefix, n_items}`."""
        if not items:
            raise ValueError("refusing to submit an empty batch")
        tag = tag or f"{int(time.time())}"
        in_name = f"{self.prefix}/{tag}/in.jsonl"
        out_prefix = f"gs://{self.bucket}/{self.prefix}/{tag}/out/"
        body = "\n".join(json.dumps(self.request_line(i["text"])) for i in items)
        self.gcs.write_bytes(self.bucket, in_name, body.encode("utf-8"),
                             "application/jsonl")
        job = self.rest.post(self.base, {
            "displayName": f"patents-v3-embed-{tag}"[:128],
            "model": f"publishers/google/models/{self.model}",
            "inputConfig": {"instancesFormat": "jsonl",
                            "gcsSource": {"uris": [f"gs://{self.bucket}/{in_name}"]}},
            "outputConfig": {"predictionsFormat": "jsonl",
                             "gcsDestination": {"outputUriPrefix": out_prefix}}})
        return {"job_name": job["name"], "state": job.get("state"),
                "input_uri": f"gs://{self.bucket}/{in_name}",
                "output_prefix": out_prefix, "n_items": len(items)}

    # ----------------------------------------------------------------- poll
    def poll(self, job_name):
        job = self.rest.get(f"https://{self.location}-aiplatform.googleapis.com/v1/{job_name}")
        state = job.get("state") or "JOB_STATE_UNSPECIFIED"
        out = (job.get("outputInfo") or {}).get("gcsOutputDirectory") or ""
        return {"state": state, "terminal": state in TERMINAL, "ok": state in GOOD,
                "output_dir": out, "error": (job.get("error") or {}).get("message", "")}

    # ----------------------------------------------------------------- collect
    def collect(self, output_dir, items):
        """-> `(vectors_by_item_key, errors_by_item_key)`.

        An item that never appears in the output is an ERROR, not a silent drop. That is the whole
        difference between requeuing 12 items and quietly losing them.
        """
        by_text = collections.defaultdict(collections.deque)
        for i in items:
            by_text[i["text"]].append(i["item_key"])
        vectors, errors = {}, {}
        bucket, prefix = gcs_lite.parse_uri(output_dir.rstrip("/") + "/")
        for obj in self.gcs.list_objects(bucket, prefix):
            if not obj["name"].endswith(".jsonl"):
                continue
            for line in self.gcs.read_jsonl(bucket, obj["name"]):
                text = _line_text(line)
                queue = by_text.get(text)
                if not queue:
                    continue                       # not ours, or already correlated
                key = queue.popleft()
                values = (((line.get("response") or {}).get("embedding") or {}).get("values"))
                status = line.get("status") or ""
                if values and len(values) == self.dim:
                    vectors[key] = list(values)
                else:
                    errors[key] = (str(status) or "no embedding in response")[:300]
        for text, queue in by_text.items():
            while queue:
                errors[queue.popleft()] = "missing from batch output"
        return vectors, errors


def _line_text(line):
    try:
        return line["request"]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
