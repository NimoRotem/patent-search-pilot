"""Progress reporting through the cross-encoder head (src/retrieval.py _rerank_progressive).

A live run showed SSE gaps of 0, 1.9, 4.1, 20.9, 24.1, 18.1, 0 and then 56.4s stuck on
"Reranking 1299 families + grounding the claim chart…" — roughly half the run on one frozen
message. Measured cause: rerank_families made ONE rr.rerank call for the whole 25-passage head,
and the cross-encoder costs ~2.4-3.1 s per passage on this box (best_text is 0.01 s total, so it
is all model time). Scoring in slices lets the loop report where it is.

The slicing must not change any result: the cross-encoder scores each (query, passage) pair
independently, so chunked and unchunked ordering have to match exactly.
"""
import pytest
import retrieval


@pytest.fixture()
def fake_reranker(monkeypatch):
    """Deterministic stand-in: score = passage length, so ordering is predictable."""
    calls = []

    def _fake(query, passages, top_k=None):
        calls.append(len(passages))
        out = sorted(((i, float(len(p))) for i, p in enumerate(passages)), key=lambda t: -t[1])
        return out[:top_k] if top_k else out

    monkeypatch.setattr(retrieval.rr, "rerank", _fake)
    return calls


PASSAGES = ["x" * n for n in range(1, 26)]


def test_chunked_ordering_is_identical_to_one_call(fake_reranker):
    single = retrieval._rerank_progressive("q", PASSAGES, chunk=0)
    chunked = retrieval._rerank_progressive("q", PASSAGES, chunk=5)
    assert [i for i, _ in single] == [i for i, _ in chunked]
    assert [s for _, s in single] == [s for _, s in chunked]


@pytest.mark.parametrize("chunk", [1, 2, 5, 7, 8, 24, 25, 26, 100])
def test_ordering_is_stable_across_every_chunk_size(fake_reranker, chunk):
    ref = retrieval._rerank_progressive("q", PASSAGES, chunk=0)
    got = retrieval._rerank_progressive("q", PASSAGES, chunk=chunk)
    assert [i for i, _ in got] == [i for i, _ in ref]


def test_indices_are_absolute_not_per_slice(fake_reranker):
    """The bug this guards: rr.rerank returns slice-LOCAL indices."""
    out = retrieval._rerank_progressive("q", PASSAGES, chunk=5)
    assert sorted(i for i, _ in out) == list(range(len(PASSAGES)))
    # longest passage is the last one, and must come first
    assert out[0][0] == len(PASSAGES) - 1


def test_progress_is_reported_monotonically_and_completes(fake_reranker):
    seen = []
    retrieval._rerank_progressive("q", PASSAGES, chunk=5, on_progress=lambda d, t: seen.append((d, t)))
    assert seen == [(5, 25), (10, 25), (15, 25), (20, 25), (25, 25)]
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)
    assert seen[-1][0] == seen[-1][1] == len(PASSAGES)


def test_progress_reaches_the_total_on_a_ragged_last_chunk(fake_reranker):
    seen = []
    retrieval._rerank_progressive("q", PASSAGES, chunk=7, on_progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (25, 25), "final progress must report the true total, not an overshoot"
    assert all(d <= t for d, t in seen)


def test_single_call_path_still_reports_completion(fake_reranker):
    seen = []
    retrieval._rerank_progressive("q", PASSAGES, chunk=0, on_progress=lambda d, t: seen.append((d, t)))
    assert seen == [(25, 25)]
    assert fake_reranker == [25], "chunk=0 must stay a single cross-encoder call"


def test_empty_input(fake_reranker):
    assert retrieval._rerank_progressive("q", [], chunk=5) == []


def test_progress_is_optional(fake_reranker):
    assert retrieval._rerank_progressive("q", PASSAGES, chunk=5)   # must not raise without callback


def test_chunking_actually_slices(fake_reranker):
    retrieval._rerank_progressive("q", PASSAGES, chunk=5)
    assert fake_reranker == [5, 5, 5, 5, 5]


# ---- the elapsed-time heartbeat --------------------------------------------------------------
# Slicing the cross-encoder to get per-item counts was measured to double the stage (39.9/43.3 s
# single vs 76.0/83.2 s in chunks of 5), so the shipped fix is a heartbeat: it costs nothing and
# still means the user never watches a static message for a minute.
import time
import webapp


@pytest.fixture()
def job(monkeypatch):
    slug = "hb-test-slug"
    webapp._JOBS.pop(slug, None)
    webapp._set_job(slug, status="running", kind="reranking", msg="Reranking…")
    yield slug
    webapp._stop_stage_heartbeat(slug)
    webapp._JOBS.pop(slug, None)


def test_heartbeat_updates_a_running_rerank_stage(job):
    webapp._start_stage_heartbeat(job, 25, tick=0.05)
    time.sleep(0.25)
    msg = webapp._JOBS[job]["msg"]
    webapp._stop_stage_heartbeat(job)
    assert "elapsed" in msg, f"heartbeat never updated the message: {msg!r}"
    assert webapp._JOBS[job]["detail"].get("refs") == 25


def test_heartbeat_message_actually_changes(job):
    """The whole defect was a message that never changed."""
    webapp._start_stage_heartbeat(job, 25, tick=0.05)
    time.sleep(0.12)
    first = webapp._JOBS[job]["msg"]
    time.sleep(1.1)                      # elapsed seconds are integers; wait for a tick over
    second = webapp._JOBS[job]["msg"]
    webapp._stop_stage_heartbeat(job)
    assert first != second, f"message stayed frozen at {first!r}"


def test_heartbeat_stops_on_request(job):
    webapp._start_stage_heartbeat(job, 25, tick=0.05)
    time.sleep(0.15)
    webapp._stop_stage_heartbeat(job)
    settled = webapp._JOBS[job]["msg"]
    time.sleep(0.3)
    assert webapp._JOBS[job]["msg"] == settled, "heartbeat kept writing after being stopped"


def test_heartbeat_never_resurrects_a_finished_job(job):
    webapp._start_stage_heartbeat(job, 25, tick=0.05)
    webapp._set_job(job, status="done", kind="done", msg="done")
    time.sleep(0.3)
    assert webapp._JOBS[job]["status"] == "done"
    assert webapp._JOBS[job]["msg"] == "done", "heartbeat overwrote a completed job"


def test_heartbeat_does_not_overwrite_a_later_stage(job):
    webapp._start_stage_heartbeat(job, 25, tick=0.05)
    webapp._set_job(job, kind="federating", msg="Searching external patent APIs…")
    time.sleep(0.3)
    assert webapp._JOBS[job]["msg"] == "Searching external patent APIs…"


def test_starting_twice_leaves_only_one_ticker(job):
    webapp._start_stage_heartbeat(job, 25, tick=0.05)
    webapp._start_stage_heartbeat(job, 25, tick=0.05)
    assert len([k for k in webapp._HEARTBEATS if k == job]) == 1
    webapp._stop_stage_heartbeat(job)
    assert job not in webapp._HEARTBEATS
