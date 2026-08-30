"""GCS `raw/` and `parsed/`, over the JSON API, with no new dependency.

`google-cloud-storage` is not installed in the shared virtualenv, and the shared virtualenv is
what production `patent-results` runs from. Installing a package into it to store a blob is a
dependency resolution against a live service, which is a much larger risk than the thing it buys.
The GCS JSON API is one authenticated POST, and the VM's service account token comes from the GCE
metadata server, so this needs nothing that is not already here.

LAYOUT
    raw/{publication}/{provider}.{ext}.gz    exactly what the provider returned, gzipped
    parsed/{publication}/{provider}.json     the normalised record, plain JSON so a reader needs
                                             no decompression step

WHY RAW AS WELL AS PARSED. Every parser in this tree has been wrong at least once about a shape it
had never seen (OPS `exchange-documents`, the ODP field prefixes). Keeping the bytes means a parser
fix is a re-parse rather than a re-fetch, and a re-fetch is the part that costs money and quota.

FAILURE IS NOT FATAL. A GCS outage must not lose text we have already paid for. An upload failure
is recorded in the ledger as an error event and the record still goes to `sources_docstore` and
`corpus_ingest_queue`; the task row simply carries no `raw_uri`, which is how a later pass finds
what to re-upload.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import subprocess
import time
import urllib.parse

import httpx

METADATA_TOKEN = ("http://metadata.google.internal/computeMetadata/v1/instance/"
                  "service-accounts/default/token")
UPLOAD = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
OBJECT = "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}"

_TOKEN = ""
_TOKEN_EXP = 0.0
_TOKEN_LOCK = None


def bucket_name() -> str:
    """Read from the environment on every call, not captured at import: the CLI and the tests
    both set it after this module is already loaded."""
    return os.environ.get("FULLTEXT_GCS_BUCKET", "")


def enabled() -> bool:
    return bool(bucket_name())


def _lock():
    global _TOKEN_LOCK
    if _TOKEN_LOCK is None:
        _TOKEN_LOCK = asyncio.Lock()
    return _TOKEN_LOCK


def _gcloud_token() -> str:
    """Off GCE (the builder box, a laptop) there is no metadata server. `gcloud` already holds a
    credential there, so shell out rather than fail."""
    out = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True,
                         text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"gcloud auth print-access-token failed: {out.stderr.strip()[:160]}")
    return out.stdout.strip()


async def token(client: httpx.AsyncClient) -> str:
    global _TOKEN, _TOKEN_EXP
    if _TOKEN and _TOKEN_EXP > time.monotonic() + 60:
        return _TOKEN
    async with _lock():
        if _TOKEN and _TOKEN_EXP > time.monotonic() + 60:
            return _TOKEN
        try:
            r = await client.get(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"},
                                 timeout=10)
            r.raise_for_status()
            j = r.json()
            _TOKEN = j["access_token"]
            _TOKEN_EXP = time.monotonic() + int(j.get("expires_in", 3000))
        except Exception:
            _TOKEN = await asyncio.to_thread(_gcloud_token)
            _TOKEN_EXP = time.monotonic() + 1800
        return _TOKEN


def gs_uri(name: str, bucket: str = "") -> str:
    return f"gs://{bucket or bucket_name()}/{name}"


async def put(client: httpx.AsyncClient, name: str, data: bytes, content_type: str,
              *, bucket: str = "", timeout: float = 60.0) -> str:
    """Upload one object. Returns its gs:// URI, or "" when no bucket is configured."""
    bucket = bucket or bucket_name()
    if not bucket:
        return ""
    tok = await token(client)
    r = await client.post(
        UPLOAD.format(bucket=urllib.parse.quote(bucket, safe="")),
        params={"uploadType": "media", "name": name},
        headers={"Authorization": f"Bearer {tok}", "Content-Type": content_type},
        content=data, timeout=timeout)
    r.raise_for_status()
    return gs_uri(name, bucket)


async def put_raw(client: httpx.AsyncClient, publication: str, provider: str, data,
                  ext: str = "json", **kw) -> str:
    """`raw/{publication}/{provider}.{ext}.gz`. Gzipped: a Google Patents page is ~115 KB and
    compresses to roughly a fifth of that, and at 50,000 publications that difference is real."""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    blob = gzip.compress(data, 6)
    return await put(client, f"raw/{publication}/{provider}.{ext}.gz", blob,
                     "application/gzip", **kw)


async def put_parsed(client: httpx.AsyncClient, publication: str, provider: str, record: dict,
                     **kw) -> str:
    """`parsed/{publication}/{provider}.json`, uncompressed so a consumer can read it directly."""
    blob = json.dumps(record, ensure_ascii=False, default=str).encode("utf-8")
    return await put(client, f"parsed/{publication}/{provider}.json", blob,
                     "application/json; charset=utf-8", **kw)


async def get_json(client: httpx.AsyncClient, name: str, *, bucket: str = "",
                   timeout: float = 60.0):
    """Read one object back. This is how workstream D reads what has been parsed without needing
    a GCS client library either."""
    bucket = bucket or bucket_name()
    if not bucket:
        return None
    tok = await token(client)
    r = await client.get(OBJECT.format(bucket=urllib.parse.quote(bucket, safe=""),
                                       name=urllib.parse.quote(name, safe="")),
                         params={"alt": "media"},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def ensure_bucket(client: httpx.AsyncClient, bucket: str, project: str = "",
                        location: str = "US-CENTRAL1") -> dict:
    """Create the bucket if it is not there. Idempotent; a 409 means somebody already made it."""
    tok = await token(client)
    r = await client.get(f"https://storage.googleapis.com/storage/v1/b/{bucket}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    if r.status_code == 200:
        return {"created": False, "bucket": bucket}
    r = await client.post(
        "https://storage.googleapis.com/storage/v1/b",
        params={"project": project or os.environ.get("GCP_PROJECT", "nimo-gpt")},
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        content=json.dumps({"name": bucket, "location": location,
                            "storageClass": "STANDARD",
                            "iamConfiguration": {
                                "uniformBucketLevelAccess": {"enabled": True},
                                "publicAccessPrevention": "enforced"}}),
        timeout=60)
    if r.status_code == 409:
        return {"created": False, "bucket": bucket}
    r.raise_for_status()
    return {"created": True, "bucket": bucket}
