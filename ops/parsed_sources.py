"""Where a fetched publication comes from, and the watermark that makes reading it resumable.

Two sources, one shape. Workstream C writes parsed documents to GCS; the search path's demand
fetch writes them to `sources_docstore`. Both are read here and both go through the same
normaliser, so the claim-count assertion cannot be bypassed by arriving the other way.

Each source is a lexicographic cursor, never a "what is new" query: `fetch(watermark)` returns the
next slice strictly after the watermark and the new watermark to persist. That is what lets the
worker be killed at any point and resume by reading one row of its own progress table, and it is
why the GCS listing uses the API's `startOffset` rather than filtering a full listing client side.
"""
from __future__ import annotations

import json
import os
import zlib

import gcs_lite

#  ONE setting names the bucket, and it is workstream C's. `FULLTEXT_GCS_BUCKET` is what the two
#  running `patents-fulltext-acquire*` units are configured with, so reading it here is what stops
#  this pipeline from politely idling against an address nobody writes to. The interrupted
#  checkpoint defaulted to `nimo-patents-v3`; C has written 13,872 objects to
#  `gs://nimo-patents-fulltext/parsed/` and none at all to the other bucket.
PARSED_BUCKET = (os.environ.get("PARSED_BUCKET") or os.environ.get("FULLTEXT_GCS_BUCKET")
                 or "nimo-patents-fulltext")
PARSED_PREFIX = os.environ.get("PARSED_PREFIX", "parsed/")


class RawDoc(dict):
    """`{source_key, publication_number, payload, watermark}`."""


# --------------------------------------------------------------------------- GCS (workstream C)

class GcsParsedSource:
    """`gs://{bucket}/{prefix}{publication}/doc.json`, per docs/parsed_doc_contract.md.

    Fails soft in every direction that is not a bug in this code: a bucket that does not exist
    yet, a prefix with nothing under it, an object that is not JSON. Workstream C's output does
    not exist until C runs, and a pipeline that crashes because its upstream has not started is a
    pipeline nobody can leave running.
    """

    name = "gcs"

    def __init__(self, bucket=PARSED_BUCKET, prefix=PARSED_PREFIX, gcs=gcs_lite):
        self.bucket, self.prefix, self.gcs = bucket, prefix, gcs

    def fetch(self, watermark, limit=200):
        docs, mark = [], watermark
        try:
            listing = list(self.gcs.list_objects(self.bucket, self.prefix,
                                                 start_after=watermark or None, limit=limit))
        except Exception as exc:                                     # noqa: BLE001
            return [], watermark, str(exc)[:200]
        for obj in listing:
            name = obj["name"]
            mark = name
            if not name.endswith(".json"):
                continue
            try:
                payload = self.gcs.read_json(self.bucket, name)
            except Exception:                                        # noqa: BLE001
                continue
            if not isinstance(payload, dict):
                continue
            pub = (payload.get("publication_number") or _pub_from_name(name, self.prefix) or "")
            docs.append(RawDoc(source_key=f"gcs:{self.bucket}/{name}",
                               publication_number=pub, payload=payload, watermark=name))
        return docs, mark, None


def _pub_from_name(name, prefix):
    rest = name[len(prefix):] if name.startswith(prefix) else name
    return rest.split("/")[0] if "/" in rest else rest.rsplit(".", 1)[0]


# --------------------------------------------------------------------------- sources_docstore

class DocstoreSource:
    """The demand-fetch scratch store (`sql/008`, `src/sources/docstore.py`).

    Ordered by `(updated_at, publication_number)` so the cursor is total: `updated_at` alone is
    not unique and a batch boundary landing inside one timestamp would either skip or repeat the
    rows sharing it.
    """

    name = "docstore"

    def __init__(self, conn):
        self.conn = conn

    def fetch(self, watermark, limit=200):
        ts, _, pub = (watermark or "").partition("\x1e")
        with self.conn.cursor() as cur:
            if ts:
                cur.execute("""SELECT publication_number, title, abstract, claims_z, description_z,
                                      meta, updated_at
                               FROM sources_docstore
                               WHERE (updated_at, publication_number) > (%s::timestamptz, %s)
                               ORDER BY updated_at, publication_number LIMIT %s""",
                            (ts, pub, limit))
            else:
                cur.execute("""SELECT publication_number, title, abstract, claims_z, description_z,
                                      meta, updated_at
                               FROM sources_docstore
                               ORDER BY updated_at, publication_number LIMIT %s""", (limit,))
            rows = cur.fetchall()
        docs, mark = [], watermark
        for r in rows:
            mark = f"{r['updated_at'].isoformat()}\x1e{r['publication_number']}"
            meta = r["meta"] if isinstance(r["meta"], dict) else (json.loads(r["meta"] or "{}"))
            payload = {"publication_number": r["publication_number"],
                       "title": r["title"] or "",
                       "abstract": r["abstract"] or "",
                       "claims": _uz(r["claims_z"]),
                       "description": _uz(r["description_z"]),
                       "meta": meta,
                       "source": meta.get("source") or "docstore",
                       "lang": meta.get("claims_lang") or "en"}
            docs.append(RawDoc(source_key=f"docstore:{r['publication_number']}",
                               publication_number=r["publication_number"],
                               payload=payload, watermark=mark))
        return docs, mark, None


def _uz(b) -> str:
    if not b:
        return ""
    try:
        return zlib.decompress(bytes(b)).decode("utf-8", "replace")
    except Exception:                                                # noqa: BLE001
        return ""


def build(conn, names=None):
    """-> the enabled sources, in the order they should be drained."""
    names = names or [n.strip() for n in
                      os.environ.get("PARSED_SOURCES", "gcs,docstore").split(",") if n.strip()]
    out = []
    for n in names:
        if n == "gcs":
            out.append(GcsParsedSource())
        elif n == "docstore":
            out.append(DocstoreSource(conn))
    return out
