"""Opt-in Tantivy smoke test for the isolated build image."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


def test_tantivy_commit_replace_and_delete_cycle():
    if os.environ.get("NICHE_TEST_TANTIVY") != "1":
        raise unittest.SkipTest("NICHE_TEST_TANTIVY is not enabled")

    import tantivy

    from corpus.niche.tantivy_build import (
        TantivyIndexer,
        ensure_index_generation,
        open_index,
    )

    class Repository:
        def __init__(self):
            self.rows = []
            self.deletions = []
            self.marked = []

        def next_batch(self, _limit):
            rows, self.rows = self.rows, []
            return rows

        def next_deletions(self, _limit):
            return list(self.deletions)

        def complete_deletions(self, keys):
            assert keys == self.deletions
            self.deletions = []

        def mark_indexed(self, rows):
            self.marked.extend(rows)

    def row(text, content_hash):
        return {
            "chunk_id": "qa-chunk",
            "corpus_release": "niche_full_v1_qa",
            "publication_id": "USQA4001A1",
            "family_id": "QA-FAMILY-4",
            "chunk_kind": "claim_own",
            "claim_number": 1,
            "language": "en",
            "text": text,
            "source_location": "claim:1",
            "content_hash": content_hash,
        }

    with tempfile.TemporaryDirectory(prefix="niche-tantivy-qa-") as directory:
        path = Path(directory)
        generation = ensure_index_generation(path)
        assert ensure_index_generation(path) == generation
        index = open_index(path, tantivy)
        repository = Repository()
        indexer = TantivyIndexer(
            repository,
            index,
            tantivy,
            heap_size=50_000_000,
            threads=1,
        )

        repository.rows = [row("vacuum gripper", "hash-one")]
        assert indexer.index_batch(10) == 1
        assert index.searcher().num_docs == 1

        repository.rows = [row("pneumatic lifting device", "hash-two")]
        assert indexer.index_batch(10) == 1
        assert index.searcher().num_docs == 1
        assert repository.marked[-1]["content_hash"] == "hash-two"

        repository.deletions = ["niche_full_v1_qa\x1fqa-chunk"]
        assert indexer.index_batch(10) == 1
        assert index.searcher().num_docs == 0


if __name__ == "__main__":
    test_tantivy_commit_replace_and_delete_cycle()
    print("tantivy integration passed")
