"""Opt-in read-only reconciliation check against an existing Vertex batch job."""
from __future__ import annotations

import json
import os
import unittest


def test_vertex_reconciliation_finds_exactly_one_existing_job_without_posting():
    raw = str(os.environ.get("NICHE_TEST_VERTEX_BATCH_JSON") or "").strip()
    if not raw:
        raise unittest.SkipTest("NICHE_TEST_VERTEX_BATCH_JSON is not configured")

    from corpus.niche.batch import VertexBatchClient

    batch = json.loads(raw)
    required = (
        "submission_key",
        "display_name",
        "input_uri",
        "output_prefix",
        "model",
        "gcp_project",
        "gcp_location",
    )
    if any(not batch.get(name) for name in required):
        raise RuntimeError("Vertex integration batch fixture is incomplete")

    client = VertexBatchClient(
        project=batch["gcp_project"],
        location=batch["gcp_location"],
    )
    matches = client.find_matches(batch)

    assert len(matches) == 1
    assert matches[0]["displayName"] == batch["display_name"]


if __name__ == "__main__":
    test_vertex_reconciliation_finds_exactly_one_existing_job_without_posting()
    print("Vertex reconciliation integration passed")
