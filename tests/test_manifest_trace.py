"""Run manifests and candidate tracing: the two things whose absence made comparisons invalid."""
import os

import manifest
import trace as tr


# --- manifest ---------------------------------------------------------------------------------
def test_manifest_records_what_decides_a_run(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "MANIFEST_DIR", str(tmp_path))
    m = manifest.start("run-1", subject_id="s", benchmark_version="v1")
    for key in ("run_id", "start_time", "completion_status", "git_commit", "corpus_snapshot",
                "index_snapshot", "model_versions", "prompt_versions", "parameters",
                "feature_flags"):
        assert key in m, key
    assert m["completion_status"] == "running"
    assert os.path.exists(os.path.join(tmp_path, "run-1.json"))


def test_a_run_that_dies_is_not_reported_as_complete(tmp_path, monkeypatch):
    """The manifest is written BEFORE the run, so a crash leaves 'running', never 'completed'."""
    monkeypatch.setattr(manifest, "MANIFEST_DIR", str(tmp_path))
    m = manifest.start("run-2")
    assert manifest.load("run-2")["completion_status"] == "running"
    manifest.finish(m, status="failed", failure_reason="vertex 503")
    got = manifest.load("run-2")
    assert got["completion_status"] == "failed" and "503" in got["failure_reason"]


def test_prompt_edits_are_visible(tmp_path, monkeypatch):
    """A prompt edit is a change of experiment and is otherwise invisible in a diff of results."""
    monkeypatch.setattr(manifest, "MANIFEST_DIR", str(tmp_path))
    a = manifest.prompt_versions()
    assert a, "no prompts hashed"
    import disclosures
    monkeypatch.setattr(disclosures, "_SYS", disclosures._SYS + " extra instruction")
    b = manifest.prompt_versions()
    assert a.get("disclosures._SYS") != b.get("disclosures._SYS")


def test_comparable_refuses_runs_that_moved_underneath_each_other():
    a = {"git_commit": "aaa", "corpus_snapshot": {"publications": 1}, "git_dirty": False,
         "disclosure_list_version": "1"}
    b = dict(a)
    assert manifest.comparable(a, b) == []
    b2 = dict(a, corpus_snapshot={"publications": 2})
    assert any("corpus_snapshot" in d for d in manifest.comparable(a, b2))
    b3 = dict(a, disclosure_list_version="2")
    assert any("disclosure_list_version" in d for d in manifest.comparable(a, b3))
    assert manifest.comparable(a, dict(a, git_dirty=True))


# --- trace ------------------------------------------------------------------------------------
def test_every_candidate_ends_at_exactly_one_stage():
    t = tr.Trace(subject_id="s", slug="x")
    t.seen("F1", retrieval_channel="dense", raw_rank=5)
    t.seen("F2", retrieval_channel="cpc", raw_rank=900)
    t.stage("F1", tr.TOP_50)
    t.stage("F2", tr.SCREEN_REJECTED, "screen 20")
    c = t.counts()
    assert c == {tr.TOP_50: 1, tr.SCREEN_REJECTED: 1}
    assert t.unknown() == []


def test_a_candidate_with_no_stage_is_a_defect_not_a_category():
    t = tr.Trace(slug="x")
    t.seen("F9", retrieval_channel="dense")
    assert t.unknown() == ["F9"], "an unstaged candidate must be reported as UNKNOWN"


def test_channels_accumulate_rather_than_overwrite():
    """A family found by three channels is evidence of agreement; overwriting loses it."""
    t = tr.Trace(slug="x")
    t.seen("F1", retrieval_channel="dense", raw_rank=40)
    t.seen("F1", retrieval_channel="cpc", raw_rank=9)
    t.seen("F1", retrieval_channel="dense")
    row = t.rows()[0]
    assert sorted(row["retrieval_channel"]) == ["cpc", "dense"]
    assert row["raw_rank"] == 9, "keep the BEST rank any channel gave it"


def test_later_stages_overwrite_earlier_ones():
    t = tr.Trace(slug="x")
    t.seen("F1")
    t.stage("F1", tr.SCREEN_REJECTED)
    t.stage("F1", tr.TOP_50)
    assert t.counts() == {tr.TOP_50: 1}


def test_attribute_turns_a_miss_into_a_cause():
    t = tr.Trace(slug="x")
    t.seen("G1"); t.stage("G1", tr.SCREEN_REJECTED)
    t.seen("G2"); t.stage("G2", tr.TOP_50)
    by, tally = tr.attribute(t, ["G1", "G2", "G3"])
    assert by["G1"] == tr.SCREEN_REJECTED and by["G2"] == tr.TOP_50
    assert by["G3"] == tr.NOT_RETRIEVED, "a family never seen was never retrieved"
    assert tally[tr.NOT_RETRIEVED] == 1


def test_trace_writes_one_row_per_family(tmp_path):
    t = tr.Trace(slug="run", enabled=True)
    t.seen("F1"); t.stage("F1", tr.TOP_50)
    t.seen("F2"); t.stage("F2", tr.DEDUPED, "same family as F1")
    p = t.write(str(tmp_path / "run.trace.jsonl"))
    assert p and len(open(p).read().strip().splitlines()) == 2


def test_disabled_trace_costs_nothing_and_writes_nothing(tmp_path):
    t = tr.Trace(slug="run", enabled=False)
    t.seen("F1"); t.stage("F1", tr.TOP_50)
    assert t.rows() == [] and t.write(str(tmp_path / "x.jsonl")) == ""


def test_snapshots_are_cheap_enough_for_the_request_path(monkeypatch):
    """The manifest runs on every search, so an exact count is not affordable.

    The first version ran `count(*) FROM chunks WHERE embedding IS NOT NULL` over 26.6 million
    rows on every run. Two concurrency tests caught it by timing out; nothing else would have,
    because it produced a correct manifest, just minutes late.
    """
    import time as _t
    manifest._snap_cache.update(at=0.0, corpus=None, index=None)
    t0 = _t.time()
    c = manifest.corpus_snapshot()
    first = _t.time() - t0
    assert first < 5.0, f"snapshot took {first:.1f}s on the request path"
    assert "publications_estimate" in c or "error" in c
    t0 = _t.time()
    manifest.corpus_snapshot()
    assert _t.time() - t0 < 0.05, "the second call must come from the cache"


def test_a_report_written_mid_run_is_not_a_finished_run(tmp_path, monkeypatch):
    """_write_report persists DURING a run, so the file existing does not mean the run finished.

    Scoring one mid-flight counts everything the later stages have not reached as NOT_RETRIEVED.
    Measured: reading suction_chuck while it was still running turned 3 delivered references into
    11 missing ones and looked exactly like a regression, which is the most expensive kind of
    false signal because it prompts a hunt for a bug that is not there.
    """
    monkeypatch.setattr(manifest, "MANIFEST_DIR", str(tmp_path))
    m = manifest.start("bench-x-v1-123")
    assert manifest.load("bench-x-v1-123")["completion_status"] == "running"
    manifest.finish(m, status="completed")
    assert manifest.load("bench-x-v1-123")["completion_status"] == "completed"
