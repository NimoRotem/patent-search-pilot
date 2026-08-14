"""Integration tests for the parallel multi-channel fan-out (document search).

Covers the seams added by the Integrate phase: the document-chunk retrieval channel, the image
channel wrapper, the doc-materials stash roundtrip, the channel-merge splice, and the source-tag /
per-result provenance additions. Hermetic: embeds + LLM are mocked by conftest; the real read-only
Postgres is used for the dense-SQL path (same as the rest of the suite)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import retrieval
import webapp
import webview


def _vec(seed):
    # a plausible 768-d unit-ish vector; content is irrelevant to the wiring under test
    import math
    v = [math.sin(seed + i) for i in range(768)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


# --------------------------------------------------------------------- doc-chunk channel
def test_search_doc_chunks_empty_returns_empty():
    assert retrieval.search_doc_chunks([]) == []
    assert retrieval.search_doc_chunks(None) == []
    assert retrieval.search_doc_chunks([None, []]) == []


def test_search_doc_chunks_dedups_by_family_and_shapes_results():
    """Two chunk vectors fan out, pool per publication, dedup by family. Uses the shared family
    map for family keys so results are one-per-family (family, pid, score) tuples, best-first."""
    fam = webapp.retriever()._fam
    out = retrieval.search_doc_chunks([_vec(1), _vec(2)], weights=[1.0, 0.9],
                                      fam_map=fam, topk=25)
    assert isinstance(out, list)
    assert len(out) <= 25
    fams = [t[0] for t in out]
    assert len(fams) == len(set(fams)), "families must be deduped"
    for fk, pid, sc in out:
        assert isinstance(sc, float)
    # best-first
    scores = [t[2] for t in out]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------- image channel wrapper
def test_image_channel_no_blobs_is_off():
    out = webapp._image_channel([])
    assert out["state"] == "off"
    assert out["families"] == []


def test_image_channel_empty_index_is_none_not_crash(monkeypatch):
    import img_search

    def _raise(*a, **k):
        raise img_search.ImageIndexEmpty("not built")

    monkeypatch.setattr(img_search, "search_by_images", _raise)
    out = webapp._image_channel([b"fake-png-bytes"])
    assert out["state"] == "none"
    assert out["families"] == []


def test_image_channel_failure_surfaces_as_failed(monkeypatch):
    import img_search
    monkeypatch.setattr(img_search, "search_by_images",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = webapp._image_channel([b"x"])
    assert out["state"] == "failed"
    assert "boom" in out["note"]


# --------------------------------------------------------------------- doc-materials stash
def test_stash_and_load_doc_roundtrip():
    import base64
    res = {
        "ok": True,
        "source": "upload",
        "label": "claims.pdf",
        "title": "RFID lifter",
        "full_text": "Complete inventor disclosure with handle, RFID reader and swappable plate.",
        "chunks": [
            {"kind": "claim_own", "independent": True, "text": "a vacuum gripper claim",
             "coord": {"claim_no": 1}, "vector": _vec(3)},
            {"kind": "abstract", "independent": False, "vector": _vec(4)},
            {"kind": "figure_caption", "independent": False, "vector": None},  # no vector -> skipped
        ],
        "figure_images": [{"mime": "image/png", "b64": base64.b64encode(b"PNGDATA").decode()}],
    }
    token = webapp._stash_doc(res)
    assert token
    try:
        loaded = webapp._load_doc_materials(token)
        assert len(loaded["chunk_vecs"]) == 2          # the None-vector chunk dropped
        assert len(loaded["chunk_weights"]) == 2
        assert loaded["chunk_weights"][0] == 1.0        # independent claim -> weight 1.0
        assert loaded["figure_blobs"] == [b"PNGDATA"]
        assert loaded["claims"] == [{"claim_no": 1, "text": "a vacuum gripper claim",
                                      "independent": True}]
        assert loaded["source"] == "upload"
        assert loaded["title"] == "RFID lifter"
        assert loaded["full_text"].startswith("Complete inventor disclosure")
    finally:
        (webapp.DOCSTASH / f"doc-{token}.json").unlink(missing_ok=True)


def test_stash_returns_none_when_nothing_to_search():
    assert webapp._stash_doc({"ok": True, "chunks": [], "figure_images": []}) is None


def test_stash_retains_upload_claims_when_embedding_failed():
    res = {"ok": True, "source": "upload", "label": "claims.txt", "chunks": [
        {"kind": "claim_own", "text": "a claim whose vector failed", "coord": {"claim_no": 1},
         "independent": True, "vector": None},
    ], "figure_images": []}
    token = webapp._stash_doc(res)
    assert token
    try:
        loaded = webapp._load_doc_materials(token)
        assert loaded["chunk_vecs"] == [] and len(loaded["claims"]) == 1
    finally:
        (webapp.DOCSTASH / f"doc-{token}.json").unlink(missing_ok=True)


def test_load_doc_materials_missing_token_is_none():
    assert webapp._load_doc_materials(None) is None
    assert webapp._load_doc_materials("does-not-exist") is None


def test_attach_query_document_is_upload_only():
    rep = {}
    webapp._attach_query_document(rep, {"source": "upload", "label": "claims.pdf",
                                  "title": "RFID lifter", "full_text": "Verbatim disclosure",
                                  "claims": [
        {"claim_no": 1, "text": "a vacuum lifter", "independent": True},
    ]})
    assert rep["query_document"]["label"] == "claims.pdf"
    assert rep["query_document"]["n_claims"] == 1
    assert rep["query_document"]["disclosure_text"] == "Verbatim disclosure"
    untouched = {}
    webapp._attach_query_document(untouched, {"source": "link", "claims": rep["query_document"]["claims"]})
    assert "query_document" not in untouched


# --------------------------------------------------------------------- channel merge splice
def test_merge_channel_records_families_and_splices_unique_finds():
    rep = {"ranked_families": ["A", "B", "C", "D"], "channel_families": {"dense": ["A", "B"]}}
    scored = [("X", 10, 0.9), ("A", 11, 0.8), ("Y", 12, 0.7)]  # X,Y new; A already present
    webapp._merge_channel(rep, "docchunks", scored, head_keep=2, take=8)
    assert set(rep["channel_families"]["docchunks"]) == {"X", "A", "Y"}
    # new finds spliced just below the kept head (pos 0..1), originals preserved, no dups
    rf = rep["ranked_families"]
    assert rf[:2] == ["A", "B"]
    assert "X" in rf and "Y" in rf
    assert len(rf) == len(set(rf))
    assert set(rf) == {"A", "B", "C", "D", "X", "Y"}


def test_merge_channel_noop_on_empty():
    rep = {"ranked_families": ["A"], "channel_families": {}}
    webapp._merge_channel(rep, "image", [])
    assert rep["ranked_families"] == ["A"]
    assert "image" not in rep["channel_families"]


def test_dedup_preserve_order():
    assert webapp._dedup_preserve(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


# --------------------------------------------------------------------- source tags + provenance
def test_source_tags_include_new_channels():
    rep = {"channel_families": {"dense": ["A"], "docchunks": ["A", "B"], "image": ["C"]},
           "image_channel": {"state": "used", "n": 1, "note": ""}}
    tags = webview._source_tags(rep, n_local=3)
    by_id = {t["id"]: t for t in tags}
    assert by_id["local"]["state"] == "used"
    assert by_id["docchunks"]["state"] == "used" and by_id["docchunks"]["n"] == 2
    assert by_id["image"]["state"] == "used" and by_id["image"]["n"] == 1


def test_source_tags_image_not_built_shows_none():
    rep = {"channel_families": {"docchunks": ["A"]},
           "image_channel": {"state": "none", "n": 0, "note": "image index not built yet"}}
    tags = webview._source_tags(rep, n_local=1)
    by_id = {t["id"]: t for t in tags}
    assert by_id["image"]["state"] == "none"


def test_source_tags_preserve_degraded_provider_without_raw_exception():
    rep = {"federation": {"source_status": [{
        "id": "uspto", "label": "USPTO ODP", "state": "used",
        "state_detail": "degraded", "n": 75,
        "note": "404 Client Error for url https://example.invalid/private/provider/path",
    }]}}
    tag = {t["id"]: t for t in webview._source_tags(rep, n_local=2)}["uspto"]
    assert tag["state"] == "degraded" and tag["n"] == 75
    assert tag["why"] == "Partial results: one or more provider queries failed (HTTP 404)."
    assert "example.invalid" not in tag["why"]


def test_serpapi_failure_is_marked_as_fallback_when_other_sources_return_art():
    rep = {"federation": {"source_status": [
        {"id": "serpapi_gpatents", "label": "SerpApi Google Patents", "state": "failed",
         "state_detail": "failed", "n": 0, "reason": "HTTP 429"},
        {"id": "bigquery_gpatents", "label": "BigQuery Google Patents", "state": "used",
         "state_detail": "used", "n": 1963},
    ]}}
    tags = {t["id"]: t for t in webview._source_tags(rep, n_local=50)}
    assert tags["serpapi_gpatents"]["state"] == "degraded"
    assert "fallback" in tags["serpapi_gpatents"]["why"].lower()
    assert tags["bigquery_gpatents"]["state"] == "used"


def test_attach_fed_family_sources_maps_api_provenance(monkeypatch):
    import federation
    rep = {"federation": {"hits": [
        {"pub": "US-1234567-A", "sources": ["pqai", "uspto"]},
        {"pub": "EP-9999999-A1", "sources": ["epo_ops"]},
    ]}}

    # resolve only the first hit to a local family
    def _resolve(self, keys):
        jk = federation.join_key("US-1234567-A")
        return {jk: (42, "FAM42")}

    monkeypatch.setattr(webapp.Retriever, "resolve_pub_numbers", _resolve, raising=True)
    webapp._attach_fed_family_sources(rep)
    assert rep["family_sources"]["FAM42"] == ["pqai", "uspto"]
    assert len(rep["family_sources"]) == 1        # the unresolved EP hit is not attached
