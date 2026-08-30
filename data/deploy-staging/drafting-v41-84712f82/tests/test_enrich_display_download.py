from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import enrich_display


class _ConcurrentResponse:
    status_code = 200

    def __init__(self, barrier):
        self._barrier = barrier

    def iter_content(self, _chunk_size):
        # Both callers have opened their temporary file before either finishes. This reproduced
        # the old shared-<dest>.tmp rename/stat race reliably.
        self._barrier.wait(timeout=2)
        yield b"complete patent payload"
        self._barrier.wait(timeout=2)


def test_download_same_destination_is_concurrency_safe(monkeypatch, tmp_path):
    barrier = threading.Barrier(2)
    monkeypatch.setattr(
        enrich_display.requests,
        "get",
        lambda *_args, **_kwargs: _ConcurrentResponse(barrier),
    )
    dest = tmp_path / "same-patent.pdf"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: enrich_display._download("https://example.test/p.pdf", dest),
                                 range(2)))

    assert outcomes == [True, True]
    assert dest.read_bytes() == b"complete patent payload"
    assert list(tmp_path.glob("*.tmp")) == []


def test_cache_publication_is_atomic_under_concurrent_writers(monkeypatch, tmp_path):
    monkeypatch.setattr(enrich_display, "ENRICHED", tmp_path)
    payloads = [
        {"_display": {"pub": "US-123-A", "marker": "a" * 20_000}},
        {"_display": {"pub": "US-123-A", "marker": "b" * 20_000}},
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda p: enrich_display._write_cache("US-123-A", p), payloads))

    assert outcomes == [True, True]
    cached = enrich_display.load_cached("US-123-A")
    assert cached in payloads
    assert list(tmp_path.glob("*.tmp")) == []


def test_pdf_extractors_use_isolated_work_directories(monkeypatch, tmp_path):
    monkeypatch.setattr(enrich_display, "FIGDIR", tmp_path)
    barrier = threading.Barrier(2)
    workdirs = []

    def fake_run(args, **_kwargs):
        workdirs.append(enrich_display.Path(args[-1]).parent)
        barrier.wait(timeout=2)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(enrich_display.subprocess, "run", fake_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda _: enrich_display.extract_pdf_drawings(tmp_path / "source.pdf", "US-123-A"),
            range(2),
        ))

    assert outcomes == [[], []]
    assert len(set(workdirs)) == 2
    assert all(not path.exists() for path in workdirs)
