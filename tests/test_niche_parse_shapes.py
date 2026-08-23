"""The two defects that were failing 24,178 publications in the live factory on 2026-08-23.

Each test is anchored on a measured failure, not on a hypothetical one:

  AttributeError: 'str' object has no attribute 'get'                    19,403 publications
  ValueError: dictionary update sequence element #0 has length 1         5,056 publications
  RuntimeError: GCS source content hash mismatch                         4,775 publications

The first two came from `merge_parsed` assuming every stored source used the shape this module
writes. The third came from verifying a MUTABLE object against the hash it had when the parse job
was created.
"""
import hashlib

import pytest

from corpus.niche.parse import merge_parsed
from corpus.niche.stream import decode_source_bytes, is_content_addressed


def test_merge_survives_claims_stored_as_plain_strings():
    existing = {"publication_number": "US-1-A", "claims": ["a gripper comprising a suction cup"]}
    incoming = {"publication_number": "US-1-A", "claims": [
        {"number": 1, "text": "a gripper comprising a suction cup and a vacuum pump"},
    ]}
    merged = merge_parsed(existing, incoming)
    assert all(isinstance(claim, dict) for claim in merged["claims"])
    assert merged["claims"][0]["text"].endswith("vacuum pump")


def test_merge_keeps_the_richer_side_when_the_poorer_side_is_strings():
    existing = {"claims": [{"number": 1, "text": "x" * 400}]}
    incoming = {"claims": ["short"]}
    assert merge_parsed(existing, incoming)["claims"][0]["text"] == "x" * 400


def test_merge_normalizes_paragraphs_stored_as_plain_strings():
    merged = merge_parsed(
        {"description_paragraphs": ["the first paragraph"]},
        {"description_paragraphs": ["the first paragraph", "the second paragraph"]},
    )
    paragraphs = merged["description_paragraphs"]
    assert [p["text"] for p in paragraphs] == ["the first paragraph", "the second paragraph"]
    assert all(p["id"] and p["source_location"] for p in paragraphs)


def test_merge_survives_dates_stored_as_a_bare_string():
    merged = merge_parsed({"dates": "20200101"}, {"dates": {"publication_date": "2020-01-01"}})
    assert merged["dates"] == {"publication_date": "2020-01-01"}


def test_merge_survives_source_and_completeness_stored_as_strings():
    merged = merge_parsed(
        {"source": "serpapi", "completeness": "unknown"},
        {"source": {"provider": "epo_ops"}, "completeness": {"has_claims": True}},
    )
    assert merged["source"] == {"provider": "epo_ops"}
    assert merged["completeness"]["has_claims"] is True
    assert merged["completeness"]["has_figures"] is False


def test_a_first_source_is_still_normalized():
    merged = merge_parsed(None, {"claims": ["only a string"], "description_paragraphs": ["p"]})
    assert merged["claims"][0]["number"] == 1
    assert merged["description_paragraphs"][0]["text"] == "p"


def test_raw_objects_are_content_addressed_and_parsed_objects_are_not():
    assert is_content_addressed("gs://b/patents/raw/US-1-A/serpapi/abc123.json.gz")
    assert not is_content_addressed("gs://b/patents/parsed/US-1-A/serpapi.json")


def test_a_rewritten_parsed_object_is_read_not_failed():
    body = b'{"publication_number": "US-1-A"}'
    stale = "sha256:" + hashlib.sha256(b"the bytes this job was created for").hexdigest()
    assert decode_source_bytes(body, "gs://b/patents/parsed/US-1-A/serpapi.json", stale) == body


def test_a_corrupt_raw_object_still_fails_closed():
    stale = "sha256:" + hashlib.sha256(b"the bytes this job was created for").hexdigest()
    with pytest.raises(RuntimeError, match="hash mismatch"):
        decode_source_bytes(b"other", "gs://b/patents/raw/US-1-A/serpapi/abc.json.gz", stale)


def test_a_matching_hash_still_verifies():
    body = b'{"publication_number": "US-1-A"}'
    generation = "sha256:" + hashlib.sha256(body).hexdigest()
    assert decode_source_bytes(
        body, "gs://b/patents/raw/US-1-A/serpapi/abc.json", generation
    ) == body
