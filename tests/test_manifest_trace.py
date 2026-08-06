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
    #  Replay state is part of the contract now. Two arms that saw different external results are
    #  not comparable however identical the code, and two arms that BOTH ran live are not
    #  comparable either: one subject delivered 3 of 11 gold references in one run and 0 of 11 in
    #  another hours later with nothing changed between them.
    frozen = {"mode": "replay", "adapter_version": "1", "normalization_version": "1"}
    a = {"git_commit": "aaa", "corpus_snapshot": {"publications": 1}, "git_dirty": False,
         "disclosure_list_version": "1", "replay": frozen}
    b = dict(a)
    assert manifest.comparable(a, b) == []
    #  live on either side is not a comparison
    assert any("replay" in d for d in
               manifest.comparable(a, dict(a, replay={"mode": "off"})))
    assert any("replay" in d for d in manifest.comparable({**a, "replay": {}}, b))
    #  and a recording made under a different adapter version is a different external world
    assert any("adapter_version" in d for d in manifest.comparable(
        a, dict(a, replay=dict(frozen, adapter_version="2"))))
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


def test_channel_hit_that_fusion_never_ranked_is_not_unknown():
    """A family a channel retrieved and fusion never carried in must name its stage.

    Measured on the v15 dev split: four gold families rested at UNKNOWN and every one of them had
    been retrieved -- by the citation graph, by CPC, by query-by-example -- and was then absent
    from ranked_families entirely. UNKNOWN is defined in this module as a defect rather than a
    category, so a resting UNKNOWN is a bug in the instrument, and an instrument that cannot say
    where a reference died is the one thing this whole funnel exists to prevent.
    """
    import trace as tr
    rep = {
        #  retrieved by two channels; fusion ranked only the second one
        "channel_families": {"citation": ["FAM_DROPPED"], "cpc": ["FAM_RANKED"]},
        "ranked_families": ["FAM_RANKED"],
        "deep_rank": {"n_candidates": 1, "by_pub": {}, "candidate_families": ["FAM_RANKED"],
                      "screen_scores": {}, "order": [], "unread": {}},
    }
    t = tr.from_report(rep, subject_id="s", slug="s")
    by, _ = tr.attribute(t, ["FAM_DROPPED", "FAM_RANKED"])
    assert by["FAM_DROPPED"] == tr.CHANNEL_TRUNCATED, by
    #  and the family fusion DID rank keeps the stage the ranked pass gave it
    assert by["FAM_RANKED"] != tr.CHANNEL_TRUNCATED, by
    assert tr.UNKNOWN not in by.values(), by


def test_rerank_pool_refuses_to_spawn_from_inside_a_spawned_child(monkeypatch):
    """A child must not build its own pool, however careless the script that started it.

    multiprocessing "spawn" re-imports the parent's __main__. A script with its work at module
    level therefore re-runs that work in every child, and the work spawns again. Measured: a
    four-deep tree of interpreters holding a reranker each, roughly 1.3 GB apiece, exhausted 16 GB
    of RAM and 16 GB of swap and froze the host, while several recursive copies wrote the same
    report file. The `if __name__ == "__main__":` guard is the real fix; this is the backstop for
    the next script that forgets it.
    """
    import multiprocessing
    import rerank_pool

    class _FakeParent:
        pid = 4242

    monkeypatch.setattr(multiprocessing, "parent_process", lambda: _FakeParent(), raising=False)
    assert rerank_pool.in_spawned_child() is True
    try:
        rerank_pool._get_pool_locked()
    except RuntimeError as e:
        assert "__main__" in str(e)
    else:
        raise AssertionError("a spawned child was allowed to create a rerank pool")

    #  and the normal case still works: a real parent process is not a child
    monkeypatch.setattr(multiprocessing, "parent_process", lambda: None, raising=False)
    assert rerank_pool.in_spawned_child() is False


def test_eval_entrypoints_guard_their_module_level_work():
    """Every eval script that drives the pipeline must guard its work.

    This is the defect itself, asserted directly: eval/run_one_oracle.py ran ingest and report
    generation at module level while the pipeline spawns a reranker child, so each child re-ran
    the entire arm.
    """
    import os
    import re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("run_one_oracle.py", "run_split.py", "benchmark.py"):
        src = open(os.path.join(here, "eval", name)).read()
        assert re.search(r'if\s+__name__\s*==\s*.__main__.', src), (
            f"eval/{name} has no __main__ guard; multiprocessing spawn will re-run its "
            f"module-level work in every child")


def test_gold_matches_when_the_two_sides_spell_the_family_key_differently():
    """A reference the pipeline DID find must not be reported as never retrieved.

    eval/benchmark_gold.py names a familyless reference `ext:US-3429508-A`; src/external.py names
    the same thing `US-3429508-A`. Measured on the v15 dev split, all 57 references carrying the
    placeholder were attributed to NOT_RETRIEVED, which is what a structural non-match looks like.
    It inflated NOT_RETRIEVED from 29% to 44% and would have made the acquisition experiment
    unable to show a gain at all.
    """
    import trace as tr
    rep = {
        "channel_families": {"pqai": ["US-3429508-A"]},
        "ranked_families": ["US-3429508-A", "12345678"],
        "deep_rank": {"n_candidates": 2, "by_pub": {}, "candidate_families": [],
                      "screen_scores": {}, "order": [], "unread": {}},
    }
    t = tr.from_report(rep, subject_id="s", slug="s")
    by, _ = tr.attribute(t, ["ext:US-3429508-A", "12345678"])
    assert by["ext:US-3429508-A"] != tr.NOT_RETRIEVED, (
        "the pipeline ranked this reference; attribution must not call it never retrieved")
    assert by["12345678"] != tr.NOT_RETRIEVED
    #  and a family genuinely absent from the run is still NOT_RETRIEVED
    by2, _ = tr.attribute(t, ["ext:DE-9999999-A1"])
    assert by2["ext:DE-9999999-A1"] == tr.NOT_RETRIEVED


def test_every_charted_cell_records_which_publication_its_quote_came_from():
    """Evidence must be attributable to a specific publication, not to a family.

    A DOCDB family is the same invention filed in several offices, but the CLAIMS are amended
    between them. Once a readable sibling can stand in for a reference we hold no text for, a
    quote taken from the US member does not prove what the cited DE publication disclosed. The
    field is added before the substitution exists precisely so the substitution cannot ship
    without it.
    """
    import deep_rank
    ref = {"pub": "DE-102011089343-A1", "chars": 5602,
           "features": [{"item": "a sealing lip deflects inward", "verdict": "disclosed",
                         "grounding": "verified", "quote": "the lip deflects inward",
                         "location": "[0031]"}]}
    rar = {"feature_idf": {"a sealing lip deflects inward": 2.0}, "claim_idf": {},
           "feature_df": {"a sealing lip deflects inward": 1}}
    _score, detail = deep_rank.score_reference(ref, rar)
    assert detail["evidence_publication_id"] == "DE-102011089343-A1"
    assert detail["is_proxy_text"] is False
    assert detail["covered"], "expected at least one grounded cell"
    for cell in detail["covered"]:
        assert cell.get("evidence_pub"), f"cell has no evidence_pub: {cell}"

    #  and when a sibling's text stood in, both the id and the proxy flag must say so
    ref["text_source_pub"] = "US-2014008929-A1"
    _score2, detail2 = deep_rank.score_reference(ref, rar)
    assert detail2["evidence_publication_id"] == "US-2014008929-A1"
    assert detail2["is_proxy_text"] is True
