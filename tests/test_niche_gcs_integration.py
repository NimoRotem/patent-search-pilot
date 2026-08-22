"""Opt-in live GCS checks using one unique, disposable object prefix."""
from __future__ import annotations

import json
import os
import uuid
from urllib.parse import urlsplit

import pytest


def test_gcs_raw_source_is_write_once_readable_and_sanitized():
    root = str(os.environ.get("NICHE_TEST_GCS_ROOT") or "").strip()
    if not root:
        pytest.skip("NICHE_TEST_GCS_ROOT is not configured")
    parsed_root = urlsplit(root)
    if parsed_root.scheme != "gs" or not parsed_root.netloc:
        raise RuntimeError("NICHE_TEST_GCS_ROOT must be a gs:// URI")

    from google.cloud import storage

    from corpus.niche.storage import GCSObjectStore

    run_prefix = f"{parsed_root.path.strip('/')}/{uuid.uuid4().hex}".strip("/")
    scoped_root = f"gs://{parsed_root.netloc}/{run_prefix}"
    client = storage.Client()
    store = GCSObjectStore(scoped_root, client=client)
    content = b"<patent><claims>vacuum gripper</claims></patent>"

    try:
        first = store.put_raw(
            authority="US",
            publication_number="USQA3001A1",
            provider="qa-provider",
            content=content,
            media_type="application/xml",
            http_status=200,
            headers={"Content-Type": "application/xml", "Authorization": "secret"},
            source_url="https://example.test/patent?api_key=secret&lang=en",
        )
        second = store.put_raw(
            authority="US",
            publication_number="USQA3001A1",
            provider="qa-provider",
            content=content,
            media_type="application/xml",
            http_status=200,
            headers={"Content-Type": "application/xml", "Authorization": "different"},
            source_url="https://example.test/patent?api_key=different&lang=en",
        )

        assert first.uri == second.uri
        assert store.read(first.uri) == content
        metadata = json.loads(store.read(first.metadata_uri))
        assert metadata["http_headers"] == {"content-type": "application/xml"}
        assert metadata["source_url"] == "https://example.test/patent?lang=en"
    finally:
        bucket = client.bucket(parsed_root.netloc)
        for blob in client.list_blobs(parsed_root.netloc, prefix=f"{run_prefix}/"):
            current = bucket.blob(blob.name, generation=blob.generation)
            current.delete(if_generation_match=blob.generation)
