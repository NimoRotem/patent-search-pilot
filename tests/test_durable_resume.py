"""The durability claim, finished: cancellation, per-unit resume, digests and exactly-once.

WHAT WAS OPEN, AND WHAT THESE TESTS CLOSE
-----------------------------------------
`runstore.resume_point` was read and PRINTED. The worker then called `webapp._generate` as one
opaque block, so a run killed after reading 600 of 700 documents read all 700 again, and a run
whose lease was reaped kept issuing provider calls until the pipeline finished on its own and only
then discovered it could not settle. These tests are about the seven things that closes:

  1. a cancellation token the heartbeat sets and every spending call site checks
  2. per-channel retrieval checkpoints, with fusion proven identical after an interruption
  3. per-reference read checkpoints
  4. per-rescue-round checkpoints
  5. a content digest on every checkpointed artifact, so a truncated one is redone not trusted
  6. atomic report publication, and a terminal state that agrees with the report on disk
  7. side effects that happen once per RUN, not once per attempt

WHERE THEY RUN. Most of them use the live durable schema (009 is applied there) on throwaway run
ids under a `test-resume-*` slug, exactly as `test_durable_runs.py` does, and delete everything
they made. The exactly-once tests need `run_side_effects`, which is 013 and is NOT applied to the
live schema and must not be: workstream H applies migrations, once, deliberately. Those tests
create a THROWAWAY SCHEMA of their own inside the same database, apply 009, 012 and 013 into it,
and drop it. That is the smallest thing that gives real Postgres semantics, a primary key really
refusing the second insert, without touching the corpus or the shared `search_runs` table.
"""
import contextlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import db                                                            # noqa: E402
import deep_analysis                                                 # noqa: E402
import deep_rank                                                     # noqa: E402
import claim_rescue                                                  # noqa: E402
import runartifact                                                   # noqa: E402
import runctx                                                        # noqa: E402
import runstore                                                      # noqa: E402
from retrieval import fusion, orchestrator                           # noqa: E402


# =============================================================================== fixtures
@pytest.fixture()
def slug():
    s = f"test-resume-{uuid.uuid4().hex[:10]}"
    yield s
    with db.cursor() as cur:
        cur.execute("DELETE FROM corpus_ingest_queue WHERE requested_by_run IN "
                    "(SELECT run_id FROM search_runs WHERE slug=%s)", (s,))
        cur.execute("DELETE FROM search_runs WHERE slug=%s", (s,))


@pytest.fixture()
def run(slug):
    """A claimed run on the live durable schema. -> (run_id, slug)."""
    rid = runstore.enqueue(slug, {"query": "a vacuum gripper with a sealing lip"}, lane="quick")
    runstore.claim(runstore.worker_id(), lanes=["quick"])
    return rid, slug


@contextlib.contextmanager
def bound(run_id, slug, attempt=1, artifact_root=None, heartbeat=None):
    """A RunContext bound for the duration of a block, unbound whatever happens."""
    ctx = runctx.RunContext(run_id, slug, attempt=attempt, heartbeat=heartbeat,
                            artifact_root=artifact_root)
    runctx.bind(slug, ctx)
    try:
        yield ctx
    finally:
        runctx.unbind(slug)


@pytest.fixture()
def side_effect_schema(monkeypatch):
    """A throwaway SCHEMA carrying 009, 012 and 013, inside the live database.

    013 is deliberately not applied to the live schema. Faking the uniqueness constraint would
    test the fake; this tests Postgres refusing the second insert, which is the whole mechanism.
    """
    name = f"durable_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    real_connect = db.connect
    with db.cursor(autocommit=True) as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
    conn = real_connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path = "{name}"')
            for fn in ("009_durable_runs.sql", "012_run_admission.sql",
                       "013_run_side_effects.sql"):
                cur.execute((Path(ROOT) / "sql" / fn).read_text(encoding="utf-8"))
    finally:
        conn.close()

    def connect(autocommit=False, readonly=False):
        c = real_connect(autocommit=autocommit, readonly=readonly)
        with c.cursor() as cur:
            cur.execute(f'SET search_path = "{name}"')
        if not autocommit:
            c.commit()
        return c

    @contextlib.contextmanager
    def cursor(autocommit=False, readonly=False):
        c = connect(autocommit=autocommit, readonly=readonly)
        try:
            with c.cursor() as cur:
                yield cur
            if not autocommit:
                c.commit()
        finally:
            c.close()

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(db, "cursor", cursor)
    runstore._schema_ready.clear()
    yield name
    runstore._schema_ready.clear()
    c = real_connect(autocommit=True)
    try:
        with c.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    finally:
        c.close()


def _enqueue_admitted(lane="quick"):
    rid = runstore.enqueue(f"sfx-{uuid.uuid4().hex[:8]}", {"query": "q"}, lane=lane)
    runstore.admit_waiting(lane=lane, daily_cap=100, max_concurrent=100)
    return rid


# ======================================================================= 1. cancellation
def test_the_heartbeat_publishes_a_lost_lease_to_the_run_context():
    """DETECTING THE LOSS WAS NEVER THE PROBLEM. `Heartbeat.lost` was already set; nothing
    downstream heard it. The callback is the wire between the heartbeat thread and the threads
    that are spending."""
    hb = runstore.Heartbeat("run-x", "worker-a", interval=99, lease_seconds=99)
    ctx = runctx.RunContext("run-x", "slug-x", heartbeat=hb)
    assert ctx.cancelled is False
    ctx.check_cancelled()                                    # must not raise

    hb._publish_lost()                                       # what the loop does on a False beat

    assert ctx.cancelled is True
    with pytest.raises(runctx.RunCancelled) as exc:
        ctx.check_cancelled("read:US-1-A")
    assert exc.value.reason == "lease lost"
    #  The worker already treats LeaseLost as "drop it, do not settle". A cancelled run needs
    #  exactly that, so it must BE one.
    assert isinstance(exc.value, runstore.LeaseLost)


def test_a_listener_registered_after_the_loss_still_fires():
    """A context built while the worker was still starting must not miss the one edge it exists
    to observe."""
    hb = runstore.Heartbeat("run-y", "worker-a", interval=99, lease_seconds=99)
    hb._publish_lost()
    ctx = runctx.RunContext("run-y", "slug-y", heartbeat=hb)
    assert ctx.cancelled is True


def _read_wave_fixtures(monkeypatch, calls, cancel_after=None, ctx=None):
    """A reading wave with the provider replaced by a counter. Everything else is the real code."""
    import evidence
    import llm

    monkeypatch.setattr(evidence, "load_chart", lambda *a, **k: None)
    #  The passage shape is `full_text`'s own, `label` and `coord` included: `_rendered` formats
    #  every passage as "[label] text" and a fixture missing either key raises inside the reading
    #  and is caught as an unreadable reference, so the wave completes having issued no provider
    #  call at all and a cancellation test measures nothing.
    monkeypatch.setattr(deep_analysis, "full_text", lambda pub: {
        "found": True, "chars": 400, "n_claims": 1, "n_paragraphs": 1, "truncated": False,
        "title": pub, "passages": [{"kind": "claim", "no": 1, "coord": 1, "label": "claim 1",
                                    "text": "a vacuum gripper"}]})

    lock = threading.Lock()

    def chat_json(system, user, **kw):
        with lock:
            calls.append(kw.get("tier"))
            n = len(calls)
        if cancel_after is not None and n >= cancel_after and ctx is not None:
            ctx.cancel("lease lost")
        return {}

    monkeypatch.setattr(llm, "chat_json", chat_json)


def test_a_run_whose_lease_is_stolen_mid_read_stops_issuing_provider_calls(monkeypatch, tmp_path,
                                                                          run):
    """THE ONE THE WHOLE ITEM IS ABOUT.

    Twenty-four references, one provider call each. The lease is stolen on the third call. Before
    this, the wave read all twenty-four and the worker discovered at the end that it could not
    settle, having paid for every one of them.
    """
    rid, slug = run
    calls = []
    with bound(rid, slug, artifact_root=tmp_path) as ctx:
        _read_wave_fixtures(monkeypatch, calls, cancel_after=3, ctx=ctx)
        chosen = [{"pub": f"US-{i}-A", "title": f"t{i}", "rank": i, "fam": f"F{i}"}
                  for i in range(24)]
        with pytest.raises(runctx.RunCancelled):
            deep_rank.read_wave(chosen, ["a vacuum gripper"], [], {}, {}, slug, workers=1)
    assert len(calls) <= 4, (
        f"the reading kept spending after the lease was lost: {len(calls)} provider calls")


def test_without_the_guard_the_same_wave_pays_for_every_reference(monkeypatch, tmp_path, run):
    """DEFECT INJECTION for the test above. With the cancellation check removed, the identical
    wave issues a call for all twenty-four references, which is the behaviour being fixed. If this
    test ever passes with the guard in place, the test above is not measuring the guard."""
    rid, slug = run
    calls = []
    with bound(rid, slug, artifact_root=tmp_path) as ctx:
        _read_wave_fixtures(monkeypatch, calls, cancel_after=3, ctx=ctx)
        monkeypatch.setattr(runctx, "check_cancelled", lambda where="": None)
        chosen = [{"pub": f"US-{i}-A", "title": f"t{i}", "rank": i, "fam": f"F{i}"}
                  for i in range(24)]
        deep_rank.read_wave(chosen, ["a vacuum gripper"], [], {}, {}, slug, workers=1)
    assert len(calls) == 24


def test_a_cancelled_reference_leaves_no_read_checkpoint(monkeypatch, tmp_path, run):
    """Cancellation must never leave a half-written artifact. A reference whose reading was cut
    off has no checkpoint at all, so the next attempt reads it rather than resuming onto a
    fragment."""
    rid, slug = run
    calls = []
    chosen = [{"pub": f"US-{i}-A", "title": f"t{i}", "rank": i, "fam": f"F{i}"}
              for i in range(6)]
    with bound(rid, slug, artifact_root=tmp_path) as ctx:
        _read_wave_fixtures(monkeypatch, calls, cancel_after=1, ctx=ctx)
        with pytest.raises(runctx.RunCancelled):
            deep_rank.read_wave(chosen, ["a vacuum gripper"], [], {}, {}, slug, workers=1)

    banked = runstore.substages_done(rid, "read")
    assert len(banked) < len(chosen), "a cancelled wave banked a checkpoint for every reference"
    fp = deep_analysis.checklist_fp(["a vacuum gripper"], [])
    with bound(rid, slug, attempt=2, artifact_root=tmp_path) as ctx2:
        for pub, row in banked.items():
            art = (row or {}).get("artifact")
            if art:
                #  A checkpoint that NAMES an artifact must be the whole artifact. This is the
                #  half-written file the digest exists to catch.
                assert runartifact.verify(art["path"], art["sha256"]), (
                    f"{pub} left a checkpoint whose artifact does not match its digest")
            else:
                #  The reading that never came back leaves a ledger row saying so, and NOTHING a
                #  resume would read instead of paying again. The interruption cut the reading off
                #  at "unavailable", which is a statement about one attempt's luck and must not
                #  become this run's answer for that reference.
                assert (row or {}).get("method") != "llm"
                assert ctx2.reference_payload(pub, fp=fp) is None, (
                    f"{pub} resumed onto a reading that was cut off rather than reading again")


def test_the_retrieval_fan_out_does_not_query_for_a_cancelled_run(run):
    """A run whose lease was reaped while its channels queued behind the concurrency gate must
    stop there, not go and run eight channel queries for somebody else's run."""
    rid, slug = run
    ran = []
    tasks = [(name, (lambda n=name: ran.append(n) or [])) for name in ("dense", "bm25", "cpc")]
    with bound(rid, slug) as ctx:
        ctx.cancel("lease lost")
        with pytest.raises(runctx.RunCancelled):
            orchestrator._run_phase(tasks, 2)
    assert ran == [], f"a cancelled run still queried: {ran}"


def test_a_channel_cancelled_while_it_waits_for_the_gate_does_not_query(run):
    """The check inside the gate, not only before the submit. With a bound of one, the second
    channel is still waiting when the first one's work loses the lease."""
    rid, slug = run
    ran = []
    with bound(rid, slug) as ctx:
        def first():
            ran.append("dense")
            ctx.cancel("lease lost")
            return []

        def second():
            ran.append("bm25")
            return []

        with pytest.raises(runctx.RunCancelled):
            orchestrator._run_phase([("dense", first), ("bm25", second)], 1)
    assert ran == ["dense"], f"the queued channel queried after the lease was lost: {ran}"


def test_the_worker_does_not_settle_a_cancelled_run(monkeypatch):
    """A cancelled run belongs to somebody else now. Settling it would overwrite their state."""
    from runner import worker

    monkeypatch.setattr(worker.runstore, "require_admission_schema", lambda: None)
    monkeypatch.setattr(worker.runstore, "require_side_effect_schema", lambda: None)
    monkeypatch.setattr(worker, "sweep", lambda lanes=None: {})
    row = {"run_id": "run-c", "slug": "slug-c", "lane": "quick", "attempts": 2, "max_attempts": 3}
    monkeypatch.setattr(runstore, "claim", lambda *a, **k: row)
    monkeypatch.setattr(runstore, "Heartbeat",
                        lambda *a, **k: type("H", (), {"start": lambda s: s,
                                                       "stop": lambda s: None})())
    settled, failed = [], []
    monkeypatch.setattr(runstore, "settle",
                        lambda *a, **k: settled.append(a) or (True, ["charge"]))
    monkeypatch.setattr(runstore, "fail", lambda *a, **k: failed.append(a) or "queued")

    def execute(*_a, **_k):
        raise runctx.RunCancelled("stopped", reason="lease lost")

    monkeypatch.setattr(worker, "execute", execute)
    assert worker.run_once("worker-a", lanes=["quick"]) == "run-c"
    assert settled == [], "the worker settled a run it no longer owns"
    assert failed == [], "the worker marked a stolen run failed, which is another worker's row"


# ============================================================ 2. per-channel retrieval resume
def _channel(name, hits, ran):
    def fn():
        ran.append(name)
        return hits
    return fn


def test_a_resumed_run_re_runs_only_the_channels_that_did_not_finish(run):
    """A worker killed after six of nine channels used to re-run all nine. The local channel alone
    measured 958 s."""
    rid, slug = run
    dense = [(11, 0.9), (12, 0.8), (13, 0.7)]
    bm25 = [(12, 4.1), (14, 3.2)]
    cpc = [(15, 1.0)]

    ran = []
    with bound(rid, slug, attempt=1) as ctx:
        tasks = [("dense", _channel("dense", dense, ran)),
                 ("bm25", _channel("bm25", bm25, ran)),
                 ("cpc", (lambda: (_ for _ in ()).throw(RuntimeError("the cpc channel died"))))]
        first = orchestrator._run_phase(tasks, 3)
    assert sorted(ran) == ["bm25", "dense"]
    assert first["cpc"] == [], "an optional channel that failed must fail soft, not raise"

    ran2 = []
    with bound(rid, slug, attempt=2):
        tasks = [("dense", _channel("dense", dense, ran2)),
                 ("bm25", _channel("bm25", bm25, ran2)),
                 ("cpc", _channel("cpc", cpc, ran2))]
        second = orchestrator._run_phase(tasks, 3)
    assert ran2 == ["cpc"], (
        f"a resumed run re-ran channels it had already paid for: {ran2}")
    assert second["dense"] == dense
    assert second["bm25"] == bm25
    assert second["cpc"] == cpc


def test_fusion_after_an_interruption_produces_the_same_ranking(run):
    """THE INVARIANT THAT MAKES THE CHECKPOINT SAFE. Restoring a channel is only allowed if the
    answer is the same one. RRF consumes rank order and nothing else, so the restored list has to
    be the same publications in the same order."""
    rid, slug = run
    dense = [(101, 0.91), (102, 0.83), (103, 0.55), (104, 0.51)]
    bm25 = [(103, 9.1), (101, 6.0), (105, 2.2)]
    cpc = [(105, 1.0), (102, 1.0)]

    with bound(rid, slug, attempt=1):
        uninterrupted = orchestrator._run_phase(
            [("dense", _channel("dense", dense, [])),
             ("bm25", _channel("bm25", bm25, [])),
             ("cpc", _channel("cpc", cpc, []))], 3)
    expected = fusion.rrf(uninterrupted)

    with bound(rid, slug, attempt=2):
        resumed = orchestrator._run_phase(
            [("dense", (lambda: pytest.fail("dense was re-run"))),
             ("bm25", (lambda: pytest.fail("bm25 was re-run"))),
             ("cpc", (lambda: pytest.fail("cpc was re-run")))], 3)
    assert list(resumed) == list(uninterrupted), "channel ORDER changed, which reorders RRF ties"
    assert fusion.rrf(resumed) == expected


def test_a_channel_hit_ledger_that_disagrees_with_its_checkpoint_is_run_again(run):
    """The digest rule, on a checkpoint that lives in rows rather than a file. A channel whose
    ledger was half written is a silently SHORT retrieval, which looks like a bad result rather
    than a broken one."""
    rid, slug = run
    dense = [(1, 0.9), (2, 0.8), (3, 0.7)]
    with bound(rid, slug, attempt=1):
        orchestrator._run_phase([("dense", _channel("dense", dense, []))], 1)

    with db.cursor() as cur:                       # lose one row, as a truncated write would
        cur.execute("DELETE FROM retrieval_hits WHERE run_id=%s AND channel='dense' "
                    "AND publication_number='3'", (rid,))

    ran = []
    with bound(rid, slug, attempt=2):
        out = orchestrator._run_phase([("dense", _channel("dense", dense, ran))], 1)
    assert ran == ["dense"], "a short hit ledger was reloaded as though it were whole"
    assert out["dense"] == dense


# ================================================================ 3. per-reference read resume
def _chart(pub):
    return {"pub": pub, "title": pub, "found": True, "method": "llm", "chars": 4000,
            "features": [{"item": "a sealing lip", "verdict": "disclosed",
                          "grounding": "verified", "quote": "a sealing lip 12"}],
            "claims": [], "refuted": 0, "seconds": 1.0}


def test_a_resumed_read_wave_re_reads_only_what_is_missing(monkeypatch, tmp_path, run):
    """150 to 700 whole documents is by far the most expensive thing the system does, and a
    restart re-read every one of them."""
    rid, slug = run
    chosen = [{"pub": f"US-{i}-A", "title": f"t{i}", "rank": i, "fam": f"F{i}"} for i in range(8)]
    features, claims = ["a sealing lip"], []

    read = []
    monkeypatch.setattr(deep_analysis, "analyse_reference",
                        lambda pub, *a, **k: read.append(pub) or _chart(pub))

    with bound(rid, slug, attempt=1, artifact_root=tmp_path):
        first = deep_rank.read_wave(chosen[:5], features, claims, {}, {}, slug, workers=2)
    assert sorted(read) == sorted(r["pub"] for r in chosen[:5])

    read.clear()
    with bound(rid, slug, attempt=2, artifact_root=tmp_path):
        second = deep_rank.read_wave(chosen, features, claims, {}, {}, slug, workers=2)
    assert sorted(read) == sorted(r["pub"] for r in chosen[5:]), (
        f"the resumed wave re-read references it had already banked: {sorted(read)}")
    assert [c["pub"] for c in second] == [r["pub"] for r in chosen]
    #  and the reloaded charts are the charts, not stubs
    by_pub = {c["pub"]: c for c in second}
    assert by_pub["US-0-A"]["features"] == first[0]["features"]


def test_a_read_checkpoint_for_a_different_checklist_is_not_reused(monkeypatch, tmp_path, run):
    """A chart answers a question. A resumed run built on a different disclosure list is asking a
    different one, and reusing the answer would put a verdict against a requirement nobody
    charted."""
    rid, slug = run
    chosen = [{"pub": "US-9-A", "title": "t", "rank": 1, "fam": "F"}]
    read = []
    monkeypatch.setattr(deep_analysis, "analyse_reference",
                        lambda pub, *a, **k: read.append(pub) or _chart(pub))

    with bound(rid, slug, attempt=1, artifact_root=tmp_path):
        deep_rank.read_wave(chosen, ["a sealing lip"], [], {}, {}, slug, workers=1)
    read.clear()
    with bound(rid, slug, attempt=2, artifact_root=tmp_path):
        deep_rank.read_wave(chosen, ["a bracing structure"], [], {}, {}, slug, workers=1)
    assert read == ["US-9-A"], "a chart was reused across a change of checklist"


def test_a_truncated_read_artifact_is_read_again_rather_than_trusted(monkeypatch, tmp_path, run):
    """THE DIGEST'S REASON FOR EXISTING, defect-injected by writing a corrupt artifact.

    A worker SIGKILLed during a write, or a disk that filled, leaves a file that is PRESENT and
    half a chart. Reloading it would put a fragment of one reference's evidence into a finished
    report and say nothing about it.
    """
    rid, slug = run
    chosen = [{"pub": "US-7-A", "title": "t", "rank": 1, "fam": "F"}]
    read = []
    monkeypatch.setattr(deep_analysis, "analyse_reference",
                        lambda pub, *a, **k: read.append(pub) or _chart(pub))

    with bound(rid, slug, attempt=1, artifact_root=tmp_path) as ctx:
        deep_rank.read_wave(chosen, ["a sealing lip"], [], {}, {}, slug, workers=1)
        art = (ctx.substage_payload("read", "US-7-A") or {})["artifact"]

    #  Sound checkpoint first: without corruption the resume genuinely does not re-read.
    read.clear()
    with bound(rid, slug, attempt=2, artifact_root=tmp_path):
        deep_rank.read_wave(chosen, ["a sealing lip"], [], {}, {}, slug, workers=1)
    assert read == []

    blob = Path(art["path"]).read_bytes()
    Path(art["path"]).write_bytes(blob[:len(blob) // 2])          # a half-written artifact

    read.clear()
    with bound(rid, slug, attempt=3, artifact_root=tmp_path):
        again = deep_rank.read_wave(chosen, ["a sealing lip"], [], {}, {}, slug, workers=1)
    assert read == ["US-7-A"], "a truncated artifact was trusted instead of being redone"
    assert again[0]["features"], "the redone reading did not produce a chart"


# ================================================================== 4. per-rescue-round resume
def _rescue_call(charts, claim_items, calls, **kw):
    return claim_rescue.run(
        charts, claim_items, ["a sealing lip"], {"a sealing lip": "gasket, Dichtlippe"},
        subject=None, mode="novelty", retriever=None, brief="a vacuum gripper", title="gripper",
        description="", exclude_pubs=set(), exclude_families=set(),
        enrich=lambda chosen: calls.append("enrich"), ledger=None, emit=None)


@pytest.fixture()
def rescue_fakes(monkeypatch):
    """Everything around the rounds replaced by counters. The rounds themselves are the real code."""
    calls = []
    monkeypatch.setattr(claim_rescue, "LOCAL_ENOUGH", 1)
    monkeypatch.setattr(claim_rescue, "plan", lambda claims, **kw: {
        c["label"]: {"concept": "a sealing lip", "other_words": [], "counts_as": "",
                     "queries": ["a sealing lip"], "hint": "a sealing lip"} for c in claims})
    monkeypatch.setattr(claim_rescue, "find_candidates",
                        lambda *a, **k: calls.append("search") or
                        [{"pub": "US-R1-A", "fam": "FR1", "title": "rescued one",
                          "for_claim": "claim 7"}])
    monkeypatch.setattr(claim_rescue, "second_look",
                        lambda ref, labels, hints, texts: calls.append(f"look:{ref['pub']}") or 0)
    monkeypatch.setattr(deep_analysis, "analyse_reference",
                        lambda pub, *a, **k: calls.append(f"read:{pub}") or _chart(pub))
    return calls


def test_a_resumed_rescue_does_not_repeat_a_completed_round(rescue_fakes, tmp_path, run):
    """The measured rescue took 1h51m. Repeating a finished round of it is the largest avoidable
    cost in a resumed run after the reading itself."""
    rid, slug = run
    calls = rescue_fakes
    charts = [_chart("US-A-A")]
    claim_items = [{"label": "claim 7", "text": "wherein the seal is annular", "claim_no": 7,
                    "independent": False}]

    with bound(rid, slug, attempt=1, artifact_root=tmp_path):
        new_charts, summary = _rescue_call(charts, claim_items, calls)
    assert summary["ran"] is True
    assert [r["round"] for r in summary["rounds"]] == ["reread", "search", "enrich", "read", "narrow"]
    assert all(r["resumed"] is False for r in summary["rounds"])
    first = list(calls)
    assert "search" in first and "read:US-R1-A" in first

    calls.clear()
    charts2 = [_chart("US-A-A")]
    with bound(rid, slug, attempt=2, artifact_root=tmp_path):
        new2, summary2 = _rescue_call(charts2, claim_items, calls)
    assert [r["round"] for r in summary2["rounds"]] == ["reread", "search", "enrich", "read", "narrow"]
    assert all(r["resumed"] is True for r in summary2["rounds"]), summary2["rounds"]
    assert calls == [], f"a resumed rescue repeated completed rounds: {calls}"
    assert [c["pub"] for c in new2] == [c["pub"] for c in new_charts]


def test_a_resumed_rescue_does_not_pay_to_enrich_the_same_candidates_twice(rescue_fakes, tmp_path,
                                                                          run):
    """THE ONE ROUND THAT SPENT OUTSIDE EVERY CHECKPOINT.

    `enrich` is `deep_rank._enrich_missing_text`: a paid provider fetch per rescued candidate with
    no readable text. It sat between the search round and the reading round, outside both, so a
    resumed run reloaded its candidates from the search checkpoint and then went and bought the
    text for all of them again. Two restarts, three enrichments.

    Defect-injected: with the enrich round's checkpoint lookup forced to miss, the identical resume
    enriches a second time, which is the behaviour being fixed.
    """
    rid, slug = run
    calls = rescue_fakes
    claim_items = [{"label": "claim 7", "text": "wherein the seal is annular", "claim_no": 7,
                    "independent": False}]
    with bound(rid, slug, attempt=1, artifact_root=tmp_path):
        _rescue_call([_chart("US-A-A")], claim_items, calls)
    assert calls.count("enrich") == 1, calls

    calls.clear()
    with bound(rid, slug, attempt=2, artifact_root=tmp_path):
        _, summary = _rescue_call([_chart("US-A-A")], claim_items, calls)
    assert calls.count("enrich") == 0, "a resumed rescue enriched the same candidates again"
    assert {r["round"]: r["resumed"] for r in summary["rounds"]}["enrich"] is True

    #  DEFECT INJECTION. Only the enrich round's own checkpoint is taken away; every other round
    #  still resumes. If this does not enrich again, the assertion above is not measuring it.
    calls.clear()
    real = runctx.round_payload
    with bound(rid, slug, attempt=3, artifact_root=tmp_path):
        try:
            runctx.round_payload = (lambda parent, key, fp="":
                                    None if key == "enrich" else real(parent, key, fp=fp))
            _rescue_call([_chart("US-A-A")], claim_items, calls)
        finally:
            runctx.round_payload = real
    assert calls.count("enrich") == 1, (
        "without its checkpoint the enrichment did not repeat, so the test above proves nothing")


def test_an_enrichment_for_a_different_candidate_set_is_not_reused(rescue_fakes, tmp_path, run):
    """The receipt is keyed on the candidates, not on the rescue. A resumed run whose search found
    a different set must enrich that set, or those candidates stay thin and unreadable while the
    run reports the enrichment as done."""
    rid, slug = run
    calls = rescue_fakes
    seen = []
    ctx1 = runctx.RunContext(rid, slug, attempt=1, artifact_root=tmp_path)
    assert ctx1.round_payload("rescue", "enrich", fp="fp-a:set-one") is None
    ctx1.round_done("rescue", "enrich", {"candidates": 3}, fp="fp-a:set-one")
    assert ctx1.round_payload("rescue", "enrich", fp="fp-a:set-one") is not None
    assert ctx1.round_payload("rescue", "enrich", fp="fp-a:set-two") is None, (
        "an enrichment receipt for one candidate set was reused for another")
    assert claim_rescue._cands_fp([{"pub": "B"}, {"pub": "A"}]) == \
        claim_rescue._cands_fp([{"pub": "A"}, {"pub": "B"}]), "order changed the candidate key"
    assert claim_rescue._cands_fp([{"pub": "A"}]) != claim_rescue._cands_fp([{"pub": "A"},
                                                                            {"pub": "B"}])
    assert (seen, calls) == ([], [])


def test_a_corrupt_rescue_round_artifact_makes_the_round_run_again(rescue_fakes, tmp_path, run):
    """Same rule as everywhere else: a checkpoint whose artifact does not match its digest is
    treated as absent, and the round is paid for again rather than resumed onto a fragment."""
    rid, slug = run
    calls = rescue_fakes
    claim_items = [{"label": "claim 7", "text": "wherein the seal is annular", "claim_no": 7,
                    "independent": False}]
    with bound(rid, slug, attempt=1, artifact_root=tmp_path) as ctx:
        _rescue_call([_chart("US-A-A")], claim_items, calls)
        row = ctx.substage_payload("rescue", "search")
    art = row["artifact"]
    Path(art["path"]).write_text('{"cands": [{"pub": "US-R1-A", "fam"')      # a half-written file

    calls.clear()
    with bound(rid, slug, attempt=2, artifact_root=tmp_path):
        _, summary = _rescue_call([_chart("US-A-A")], claim_items, calls)
    rounds = {r["round"]: r for r in summary["rounds"]}
    assert rounds["search"]["resumed"] is False, "a truncated round checkpoint was trusted"
    assert "search" in calls
    assert rounds["reread"]["resumed"] is True, "an intact round was needlessly re-run"


# ====================================================================== 5. artifact digests
def test_an_artifact_is_published_atomically_and_carries_its_digest(tmp_path):
    path = tmp_path / "thing.json"
    sha = runartifact.write(path, {"a": [1, 2, 3]})
    assert runartifact.verify(path, sha)
    assert runartifact.read(path, sha) == {"a": [1, 2, 3]}
    assert list(tmp_path.iterdir()) == [path], "a temp file was left beside the artifact"


def test_a_digest_that_does_not_match_reads_as_absent(tmp_path):
    path = tmp_path / "thing.json"
    sha = runartifact.write(path, {"a": 1})
    path.write_text('{"a": 2}')
    assert runartifact.read(path, sha) is None
    assert runartifact.verify(path, sha) is False
    #  and an unparseable file is the same answer, not an exception
    path.write_text('{"a": ')
    assert runartifact.read(path, None) is None


def test_a_failed_serialisation_leaves_the_previous_artifact_intact(tmp_path):
    """The publish is a rename of a complete file. A payload that cannot be serialised must never
    truncate what is already there."""
    path = tmp_path / "thing.json"
    sha = runartifact.write(path, {"a": 1})

    class Boom:
        def __repr__(self):
            raise ValueError("no")

    with pytest.raises(Exception):                              # noqa: B017
        runartifact.write(path, {"a": Boom()})
    assert runartifact.read(path, sha) == {"a": 1}
    assert list(tmp_path.iterdir()) == [path]


# ================================================================ 6. atomic report publication
def test_the_report_is_never_observable_half_written(tmp_path):
    """`report_path(slug)` is read by the page, the exporters and the worker's own completion
    check, and it is rewritten several times during a run. A reader must never catch one
    mid-write."""
    import webapp

    path = tmp_path / "adhoc-x.json"
    stop = threading.Event()
    seen, bad = [], []

    def reader():
        while not stop.is_set():
            try:
                blob = path.read_text()
            except OSError:
                continue
            try:
                seen.append(json.loads(blob)["n"])
            except Exception:                                    # noqa: BLE001
                bad.append(blob[:80])

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for n in range(60):
            webapp._write_json_atomic(path, {"n": n, "pad": "x" * 200000})
    finally:
        stop.set()
        t.join(timeout=5)
    assert not bad, f"a reader saw a half-written report: {bad[:2]}"
    assert seen, "the reader never managed to read the report at all"


def test_a_run_is_not_settled_done_without_a_report(tmp_path, monkeypatch, run):
    import webapp
    from runner import worker

    rid, slug = run
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    with pytest.raises(RuntimeError, match="without writing a report"):
        worker.verify_report(rid, slug, webapp)


def test_a_run_is_not_settled_done_when_the_report_is_still_partial(tmp_path, monkeypatch, run):
    """THE STATE THIS EXISTS FOR. The pipeline writes a PARTIAL snapshot to the same path the
    moment the seed search returns, so a run that died after it leaves a file that exists and is
    valid JSON. `.exists()` calls that finished."""
    import webapp
    from runner import worker

    rid, slug = run
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    with bound(rid, slug):
        sha = webapp._write_report(slug, {"partial": True, "ranked_families": []})
        runstore.substage(rid, "x", "y")            # keep the run row alive for the FK
        runctx.checkpoint(slug, "report", {"report_path": str(webapp.report_path(slug)),
                                           "sha256": sha, "partial": True})
    with pytest.raises(RuntimeError, match="partial"):
        worker.verify_report(rid, slug, webapp)


def test_a_run_is_not_settled_done_on_a_report_it_did_not_checkpoint(tmp_path, monkeypatch, run):
    """The terminal state and the bytes on disk have to be the same fact. A report replaced or
    truncated after the checkpoint is not the report this run produced."""
    import webapp
    from runner import worker

    rid, slug = run
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    with bound(rid, slug):
        sha = webapp._write_report(slug, {"partial": False, "ranked_families": ["F1"]})
        runctx.checkpoint(slug, "report", {"report_path": str(webapp.report_path(slug)),
                                          "sha256": sha, "partial": False})
        assert worker.verify_report(rid, slug, webapp) == sha        # sound, first
        webapp.report_path(slug).write_text('{"ranked_families": []}')
        with pytest.raises(RuntimeError, match="not the artifact"):
            worker.verify_report(rid, slug, webapp)


def test_a_truncated_fused_checkpoint_is_treated_as_absent(tmp_path, monkeypatch, run):
    """The retrieval phase is the longest in the run and its checkpoint is a file. A fragment of
    it parses as JSON often enough to matter, and screening, reading and publishing on a candidate
    set nobody produced is worse than paying for the retrieval twice."""
    import webapp

    rid, slug = run
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    with bound(rid, slug) as ctx:
        path = webapp._fused_checkpoint_path(slug)
        sha = webapp._write_json_atomic(path, {"ranked_families": ["F1", "F2"]})
        ctx.checkpoint("fuse", {"report_path": str(path), "sha256": sha, "budget": None})
        assert runartifact.read(path, sha) == {"ranked_families": ["F1", "F2"]}
        path.write_text('{"ranked_families": ["F1"')
        assert runartifact.read(path, sha) is None, "a truncated fused checkpoint was trusted"


# =================================================================== 7. exactly-once side effects
def test_a_run_retried_three_times_debits_once(side_effect_schema):
    """Charging is per RUN. The attempt that already charged is usually a different process on a
    different day, so the only thing both attempts can see is the row."""
    rid = _enqueue_admitted()
    owners = []
    for attempt in (1, 2, 3):
        worker = runstore.worker_id()
        got = runstore.claim(worker, lanes=["quick"])
        assert got is not None and got["run_id"] == rid
        settled, claimed = runstore.settle(rid, worker, "done", side_effects=("charge",),
                                           attempt=attempt)
        assert settled is True
        owners.append(claimed)
        if attempt < 3:                     # put it back for another attempt, as the reaper does
            with db.cursor() as cur:
                cur.execute("UPDATE search_runs SET status='queued', finished_at=NULL "
                            "WHERE run_id=%s", (rid,))
    assert owners == [["charge"], [], []], f"the charge was claimed more than once: {owners}"
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM run_side_effects WHERE run_id=%s AND kind='charge'",
                    (rid,))
        assert cur.fetchone()["n"] == 1


def test_the_completion_mail_is_claimed_once_per_run(side_effect_schema):
    """`runctx.once` is what stands between three attempts and three 'your search is ready'
    messages."""
    rid = _enqueue_admitted()
    slug = runstore.get(rid)["slug"]
    sent = []
    for attempt in (1, 2, 3):
        with bound(rid, slug, attempt=attempt):
            if runctx.once(slug, "notify_complete"):
                sent.append(attempt)
    assert sent == [1], f"the completion mail would have been sent {len(sent)} times"


def test_with_no_durable_run_bound_the_side_effect_still_happens(side_effect_schema):
    """The gold set, the benchmark and warm_reports call the same pipeline with no worker. Without
    a run there are no attempts to be exactly-once across, and they must behave as they always
    did."""
    assert runctx.once("a-slug-nobody-bound", "notify_complete") is True


def test_settling_and_charging_commit_together(side_effect_schema):
    """One transaction, or there is a window in which a run is terminal and its charge is not
    recorded, and a worker that dies inside it either charges twice or never."""
    rid = _enqueue_admitted()
    mine = runstore.worker_id()
    runstore.claim(mine, lanes=["quick"])
    #  Somebody else takes the run: the settle must change nothing at all.
    settled, claimed = runstore.settle(rid, "an-impostor", "done", side_effects=("charge",))
    assert (settled, claimed) == (False, [])
    assert runstore.get(rid)["status"] == "running"
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM run_side_effects WHERE run_id=%s", (rid,))
        assert cur.fetchone()["n"] == 0, "a run nobody settled was charged"


def test_a_side_effect_ledger_failure_never_lets_the_effect_fire(side_effect_schema, monkeypatch):
    """A side effect whose ledger is unreachable must NOT happen: performing it is how it happens
    twice. The caller sees a DurabilityError rather than a False that reads as 'already done'."""
    rid = _enqueue_admitted()
    slug = runstore.get(rid)["slug"]
    monkeypatch.setattr(runstore, "claim_side_effect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("db down")))
    with bound(rid, slug) as ctx:
        with pytest.raises(runctx.DurabilityError):
            ctx.once("notify_complete")


def test_the_side_effect_migration_is_replay_safe(side_effect_schema):
    """Applied twice must be a no-op, like every other IF NOT EXISTS migration in the ledger."""
    sql = (Path(ROOT) / "sql" / "013_run_side_effects.sql").read_text(encoding="utf-8")
    with db.cursor(autocommit=True) as cur:
        cur.execute(sql)
        cur.execute(sql)


def test_the_worker_refuses_a_database_without_the_side_effect_ledger(side_effect_schema,
                                                                     monkeypatch):
    """Before any spend. Finding out after an hour of reading that the charge cannot be recorded
    exactly once is finding out too late."""
    from runner import worker

    with db.cursor(autocommit=True) as cur:
        cur.execute("DROP TABLE run_side_effects")
    runstore._schema_ready.clear()
    executed = []
    monkeypatch.setattr(worker, "execute", lambda *a, **k: executed.append(1))
    monkeypatch.setattr(worker, "sweep", lambda lanes=None: {})
    with pytest.raises(RuntimeError, match="013_run_side_effects.sql"):
        worker.run_once("worker-1", lanes=["quick"])
    assert not executed, "the worker executed against a database with no side-effect ledger"


def test_the_capability_probe_is_never_cached(side_effect_schema):
    """Same rule as admission: a cached answer is keyed by nothing, and one process that retargets
    its connection would reuse a 013 answer against a database that does not have the table."""
    assert runstore.side_effects_capable() is True
    with db.cursor(autocommit=True) as cur:
        cur.execute("DROP TABLE run_side_effects")
    assert runstore.side_effects_capable() is False, "a stale capability answer survived"


# =============================================== 8. restarting the web app under the cutover
#
#  THE POINT OF THE WHOLE CUTOVER, and the one place it was still false. Both reconcilers below
#  run at gunicorn startup. Both were written when "nothing is running in this process" meant
#  "nothing is running anywhere". After the cutover a restart of `patent-results` leaves a search
#  executing in the WORKER, with a partial report on disk and a saved-search row still saying
#  `running`, and both of them read that as an interrupted run.
#
#  These live in this file rather than in `test_run_cutover.py` because that file's fixtures need
#  a local Postgres in Docker, which the patents VM does not have: all 99 of its tests skip here.
#  A throwaway schema in the live database is what actually runs on the box the deploy happens on.

@pytest.fixture()
def recovery_schema(side_effect_schema):
    """The durable schema plus the accounts schema, in one throwaway schema, with the recovery
    pass's two destructive side effects replaced by counters."""
    import accounts as acc
    ddl = (Path(ROOT) / "sql" / "003_app_accounts.sql").read_text(encoding="utf-8")
    with db.cursor(autocommit=True) as cur:
        cur.execute(ddl)
    prev = acc._SCHEMA_READY
    try:
        acc.ensure_schema(force=True)
        yield side_effect_schema
    finally:
        acc._SCHEMA_READY = prev


@pytest.fixture()
def recovery_env(monkeypatch, recovery_schema, tmp_path):
    import webapp
    failed, mailed = [], []
    monkeypatch.setenv(webapp.DURABLE_RUNS_ENV, "1")
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(webapp.auth, "accounts_enabled", lambda app_: True)
    monkeypatch.setattr(webapp.accounts, "mark_search_failed", lambda s: failed.append(s))
    monkeypatch.setattr(webapp.notifications, "queue_search_failure",
                        lambda s, reason=None: mailed.append(("failure", s)))
    monkeypatch.setattr(webapp.notifications, "queue_search_completion",
                        lambda s: mailed.append(("completion", s)))
    return {"failed": failed, "mailed": mailed, "tmp": tmp_path, "webapp": webapp}


def _user(email):
    with db.cursor() as cur:
        cur.execute("INSERT INTO app_users (email, full_name, password_hash) "
                    "VALUES (%s,%s,'x') RETURNING id", (email, email.split("@")[0]))
        return cur.fetchone()["id"]


def _saved_search_running(slug, user_id):
    """A saved search left `running` by the process the restart killed, old enough to be past the
    recovery grace period."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO app_saved_searches (user_id, slug, query, mode, status) "
                    "VALUES (%s,%s,'a vacuum gripper','novelty','running')", (user_id, slug))
        cur.execute("UPDATE app_saved_searches SET updated_at = now() - interval '10 minutes' "
                    "WHERE slug=%s", (slug,))


def _running_durable_run(slug):
    rid = runstore.enqueue(slug, {"query": "a vacuum gripper"}, lane="deep")
    runstore.admit_waiting(lane="deep", daily_cap=100, max_concurrent=100)
    runstore.claim(runstore.worker_id(), lanes=["deep"], admitted_only=True)
    assert runstore.get(rid)["status"] == "running", "the fixture did not leave a running run"
    return rid


def test_a_restart_does_not_fail_a_search_the_worker_is_still_running(recovery_env):
    """RESTARTING patent-results MUST BE INVISIBLE TO A RUNNING SEARCH.

    Before this, restarting the web app marked the live run's saved search FAILED and mailed the
    person "the search was interrupted and could not be resumed", while the worker went on reading
    documents for it and published the finished report half an hour later.
    """
    webapp = recovery_env["webapp"]
    slug = "recovery-live"
    _saved_search_running(slug, _user("recovery-live@example.com"))
    _running_durable_run(slug)
    (recovery_env["tmp"] / f"{slug}.json").write_text(json.dumps({"partial": True}))

    got = webapp.recover_interrupted_searches()

    assert got["still_running"] == 1, got
    assert got["failed"] == 0, got
    assert recovery_env["failed"] == [], "a live durable run was marked failed by a restart"
    assert recovery_env["mailed"] == [], "a live durable run was mailed a failure by a restart"


def test_without_the_liveness_check_the_same_restart_fails_the_live_run(recovery_env, monkeypatch):
    """DEFECT INJECTION for the test above. With the liveness answer forced to "settled", the
    identical restart marks the running search failed and mails the user. If it does not, the test
    above is measuring nothing."""
    webapp = recovery_env["webapp"]
    slug = "recovery-injected"
    _saved_search_running(slug, _user("recovery-injected@example.com"))
    _running_durable_run(slug)
    (recovery_env["tmp"] / f"{slug}.json").write_text(json.dumps({"partial": True}))

    monkeypatch.setattr(webapp, "_durable_liveness", lambda s: False)
    got = webapp.recover_interrupted_searches()

    assert got["failed"] == 1, got
    assert recovery_env["failed"] == [slug]
    assert recovery_env["mailed"] == [("failure", slug)]


def test_an_unreadable_run_store_is_not_read_as_nothing_is_running(recovery_env, monkeypatch):
    """UNKNOWN is not FALSE. A store that cannot be reached is not evidence that the worker
    stopped, and the next act of this branch is destructive."""
    webapp = recovery_env["webapp"]
    slug = "recovery-unknown"
    _saved_search_running(slug, _user("recovery-unknown@example.com"))
    (recovery_env["tmp"] / f"{slug}.json").write_text(json.dumps({"partial": True}))

    monkeypatch.setattr(webapp, "_durable_liveness", lambda s: None)
    got = webapp.recover_interrupted_searches()

    assert got["still_running"] == 1 and recovery_env["failed"] == [], got


def test_a_genuinely_interrupted_search_is_still_settled_under_the_flag(recovery_env):
    """The guard must not turn the recovery pass off. A slug with no live durable run is settled
    exactly as it always was."""
    webapp = recovery_env["webapp"]
    slug = "recovery-dead"
    _saved_search_running(slug, _user("recovery-dead@example.com"))
    (recovery_env["tmp"] / f"{slug}.json").write_text(json.dumps({"partial": True}))

    got = webapp.recover_interrupted_searches()

    assert got["failed"] == 1 and recovery_env["failed"] == [slug], got


def test_a_finished_report_is_still_completed_under_the_flag(recovery_env):
    webapp = recovery_env["webapp"]
    slug = "recovery-done"
    _saved_search_running(slug, _user("recovery-done@example.com"))
    (recovery_env["tmp"] / f"{slug}.json").write_text(json.dumps({"partial": False, "ranked": []}))

    got = webapp.recover_interrupted_searches()

    assert got["completed"] == 1 and recovery_env["failed"] == [], got
    assert recovery_env["mailed"] == [("completion", slug)]


def test_with_the_flag_off_the_recovery_pass_never_consults_the_run_store(recovery_env,
                                                                         monkeypatch):
    """The legacy regime is untouched: with the flag off nothing asks the durable store, and an
    interrupted search is settled as it always was."""
    webapp = recovery_env["webapp"]
    monkeypatch.setenv(webapp.DURABLE_RUNS_ENV, "0")
    slug = "recovery-legacy"
    _saved_search_running(slug, _user("recovery-legacy@example.com"))
    (recovery_env["tmp"] / f"{slug}.json").write_text(json.dumps({"partial": True}))

    def _boom(_s):
        raise AssertionError("the legacy path consulted the durable run store")

    monkeypatch.setattr(webapp, "_durable_liveness", _boom)
    got = webapp.recover_interrupted_searches()
    assert got["failed"] == 1 and recovery_env["failed"] == [slug], got


def test_the_boot_requeue_does_not_delete_a_live_runs_partial_report(recovery_env, monkeypatch):
    """`run_queue.requeue_orphans` calls `_drop_partial_report` for every queue row a dead process
    left running. The durable worker is not a dead process, and that partial is the only thing the
    page has to show for an hour of work."""
    webapp = recovery_env["webapp"]
    slug = "requeue-live"
    _running_durable_run(slug)
    p = recovery_env["tmp"] / f"{slug}.json"
    p.write_text(json.dumps({"partial": True}))

    webapp._drop_partial_report(slug)
    assert p.exists(), "a restart deleted the partial report of a search still being run"

    #  DEFECT INJECTION: with the liveness answer forced to settled, the same call deletes it.
    monkeypatch.setattr(webapp, "_durable_liveness", lambda s: False)
    webapp._drop_partial_report(slug)
    assert not p.exists(), "the drop is not gated on liveness at all"


def test_the_boot_requeue_still_clears_a_partial_with_no_live_run(recovery_env):
    webapp = recovery_env["webapp"]
    p = recovery_env["tmp"] / "requeue-dead.json"
    p.write_text(json.dumps({"partial": True}))
    webapp._drop_partial_report("requeue-dead")
    assert not p.exists()
