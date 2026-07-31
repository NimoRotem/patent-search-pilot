from concurrent.futures import ThreadPoolExecutor
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
