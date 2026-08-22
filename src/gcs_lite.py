"""A three-verb Google Cloud Storage client over the JSON API.

WHY NOT `google-cloud-storage`. The virtualenv this runs in is SHARED with the live
`patent-results` gunicorn, and pip-installing a dependency tree into it to get three verbs is a
production risk out of all proportion to the need: a resolver that decides to move `google-auth`
or `protobuf` takes the search service with it. `google-auth` and `httpx` are already installed
(google-genai depends on both) and the JSON API is three requests.

Credentials come from the VM's service account via Application Default Credentials, so there is
no key to hold and nothing to rotate.

Listing is resumable by design: `list_objects(start_after=...)` uses the API's own lexicographic
`startOffset`, so a worker that dies mid-scan restarts from its watermark instead of from the
beginning of a prefix that will eventually hold millions of objects.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse

BASE = "https://storage.googleapis.com/storage/v1"
UPLOAD = "https://storage.googleapis.com/upload/storage/v1"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_lock = threading.Lock()
_creds = None
_expiry = 0.0


class GcsError(RuntimeError):
    pass


def parse_uri(uri: str):
    """`gs://bucket/some/name` -> `("bucket", "some/name")`."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// uri: {uri!r}")
    rest = uri[5:]
    bucket, _, name = rest.partition("/")
    return bucket, name


def token() -> str:
    """A cached ADC access token, refreshed a minute before it expires.

    Refreshing on every call costs a metadata-server round trip per object, which at a few
    thousand objects a minute is the dominant cost of listing.
    """
    global _creds, _expiry
    with _lock:
        now = time.time()
        if _creds is not None and now < _expiry:
            return _creds.token
        import google.auth
        from google.auth.transport.requests import Request
        if _creds is None:
            _creds, _ = google.auth.default(scopes=[SCOPE])
        _creds.refresh(Request())
        exp = getattr(_creds, "expiry", None)
        _expiry = (exp.timestamp() - 60) if exp else (now + 1800)
        return _creds.token


def _client():
    import httpx
    return httpx.Client(timeout=120.0)


def _headers(extra=None):
    h = {"Authorization": f"Bearer {token()}"}
    if extra:
        h.update(extra)
    return h


def list_objects(bucket: str, prefix: str = "", start_after: str | None = None,
                 limit: int | None = None):
    """Yield `{name, size, updated, generation}` in lexicographic order.

    `start_after` is EXCLUSIVE: the object whose name equals it is skipped, so a watermark can be
    the last name processed rather than "the name after it", which nothing can compute.
    """
    got = 0
    page = None
    with _client() as c:
        while True:
            params = {"prefix": prefix, "maxResults": "1000",
                      "fields": "items(name,size,updated,generation),nextPageToken"}
            if start_after:
                params["startOffset"] = start_after
            if page:
                params["pageToken"] = page
            url = f"{BASE}/b/{urllib.parse.quote(bucket, safe='')}/o?" + urllib.parse.urlencode(params)
            r = c.get(url, headers=_headers())
            if r.status_code == 404:
                return
            if r.status_code >= 300:
                raise GcsError(f"list {bucket}/{prefix}: {r.status_code} {r.text[:200]}")
            body = r.json()
            for item in body.get("items") or []:
                if start_after and item["name"] <= start_after:
                    continue
                yield item
                got += 1
                if limit and got >= limit:
                    return
            page = body.get("nextPageToken")
            if not page:
                return


def read_bytes(bucket: str, name: str) -> bytes:
    with _client() as c:
        url = f"{BASE}/b/{urllib.parse.quote(bucket, safe='')}/o/{urllib.parse.quote(name, safe='')}?alt=media"
        r = c.get(url, headers=_headers())
        if r.status_code >= 300:
            raise GcsError(f"read gs://{bucket}/{name}: {r.status_code} {r.text[:200]}")
        return r.content


def read_json(bucket: str, name: str):
    return json.loads(read_bytes(bucket, name).decode("utf-8", "replace"))


def read_jsonl(bucket: str, name: str):
    """Yield one parsed object per non-empty line. A malformed line is skipped, not fatal:
    a batch output file with one bad line must still deliver the other 4,999 predictions."""
    for line in read_bytes(bucket, name).decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def write_bytes(bucket: str, name: str, data: bytes,
                content_type: str = "application/octet-stream") -> dict:
    with _client() as c:
        params = urllib.parse.urlencode({"uploadType": "media", "name": name})
        url = f"{UPLOAD}/b/{urllib.parse.quote(bucket, safe='')}/o?{params}"
        r = c.post(url, headers=_headers({"Content-Type": content_type}), content=data)
        if r.status_code >= 300:
            raise GcsError(f"write gs://{bucket}/{name}: {r.status_code} {r.text[:200]}")
        return r.json()


def write_text(bucket: str, name: str, text: str, content_type="text/plain; charset=utf-8"):
    return write_bytes(bucket, name, text.encode("utf-8"), content_type)


def exists(bucket: str, name: str) -> bool:
    with _client() as c:
        url = f"{BASE}/b/{urllib.parse.quote(bucket, safe='')}/o/{urllib.parse.quote(name, safe='')}"
        r = c.get(url, headers=_headers())
        return r.status_code < 300
