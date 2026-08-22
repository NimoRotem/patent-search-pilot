"""The search worker: a standalone process that claims runs from Postgres and executes them.

    python -m runner.worker                 # any lane
    python -m runner.worker --lanes quick   # only the cheap interactive lane
    python -m runner.worker --reaper-only   # just return expired leases, run nothing

WHY IT IS A SEPARATE PROCESS
----------------------------
A run used to be a daemon thread inside the gunicorn worker, so `supervisorctl restart
patent-results` destroyed whatever was in flight. That has cost two production searches. Under
this design the web app will only write a row; restarting it will be invisible to a running
search. Restarting the worker costs at most the current resumable stage.

INTEGRATION STATUS
------------------
This worker is not enabled, and the route is not cut over: the production web route and status
stream still use the legacy in-process dispatcher, and no Supervisor worker is running. That
cutover is a coordinated step and is deliberately not taken here.

WHAT IS NOW COMPLETE, and was the open half of the durability claim: EVERY RECORDED STAGE IS
CONSULTED ON RESUME. `runstore.resume_point` is no longer read and printed. Each stage reloads its
own checkpoint, at the granularity the money is spent at:

    fuse                webapp._generate reloads the fused candidate set, digest-checked
    retrieve:<channel>  retrieval.orchestrator._run_phase restores a completed channel's hit set
                        from retrieval_hits instead of re-querying it; fusion is identical
    screen              deep_rank reuses the screening scores when they cover the same candidates
    read:<pub>          deep_rank and claim_rescue reload each reference's chart, digest-checked
    rescue:<round>      claim_rescue re-enters at the first round it has not finished
    report              the worker verifies the published report against its recorded digest

and every checkpointed artifact carries a content digest, so a truncated one is treated as absent
and redone rather than trusted.

CANCELLATION. The heartbeat publishes a lost lease to `runctx.RunContext`, and the retrieval
fan-out, every LLM batch in the reading, the enrichment fetches and the rescue rounds all check it
between units of work. A run whose lease is stolen mid-read stops issuing provider calls instead of
spending to the end and then discovering it cannot settle.

THE LOOP
--------
    reap expired leases  ->  claim one run (FOR UPDATE SKIP LOCKED)  ->  heartbeat thread on
    ->  execute with a RunContext bound  ->  verify the published report
    ->  settle + charge in ONE transaction / fail (retry) / release

Two workers never claim the same run: the claim is a single UPDATE ... FROM (SELECT ... FOR UPDATE
SKIP LOCKED LIMIT 1), so the loser of a race skips the locked row instead of waiting on it.

THE CORPUS IS READ ONLY IN HERE. `corpus_guard.arm()` runs before anything else is imported, so
every connection this process opens refuses a write to the live corpus. See
docs/corpus_write_policy.md.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#  BEFORE webapp is imported, for two reasons: importing webapp must not start the web app's own
#  dispatcher (it would run searches in this process by the old in-thread path), and the guard has
#  to be armed before any module opens its first connection.
os.environ.setdefault("PATENTS_NO_STARTUP", "1")

import corpus_guard
import runctx
import runstore

WORKER_ENABLED_ENV = "DURABLE_WORKER_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


class NotEnabled(RuntimeError):
    """The worker was started without an explicit, well-formed opt-in."""


def enabled():
    """Whether this process may execute runs. Off by default, and FAILS CLOSED.

    A worker spends money: provider calls, retrieval, reads. So execution is opt-in, and a value
    that cannot be parsed is refused rather than guessed. Guessing "off" would be survivable;
    guessing "on" would not, and a typo in a unit file must not be the difference. Contrast
    `webapp.durable_runs_enabled`, where an unparseable value falls back to the legacy path
    because refusing to serve the site is the worse failure there.
    """
    raw = str(os.environ.get(WORKER_ENABLED_ENV, "")).strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        raise NotEnabled(
            f"{WORKER_ENABLED_ENV} is not set. Durable execution is opt-in: set it to 1 to let "
            f"this worker claim and run searches.")
    raise NotEnabled(
        f"{WORKER_ENABLED_ENV}={raw!r} is not a recognised boolean. Refusing to start rather "
        f"than guess, because guessing wrong here spends money.")


POLL_SECONDS = float(os.environ.get("RUN_WORKER_POLL", "2.0"))
REAP_SECONDS = float(os.environ.get("RUN_WORKER_REAP", "15.0"))
#  The retention sweep for the raw hit ledger. Hourly, because a day of rows is a day of rows
#  whether it is noticed at 12:00 or 12:59, and a bounded delete an hour is invisible to the box.
PRUNE_SECONDS = float(os.environ.get("RUN_WORKER_PRUNE", "3600.0"))

_stop = False


def _signal(signum, _frame):
    """SIGTERM during a run is a request to stop taking NEW work, not to abandon this one.

    The run itself is safe either way: it is checkpointed, and if this process is SIGKILLed the
    lease expires and another worker resumes it. Draining just avoids paying for that.
    """
    global _stop
    _stop = True
    print(f"[worker] signal {signum}: draining, will not claim another run", flush=True)


def verify_report(run_id, slug, webapp):
    """The published report agrees with a run about to be marked done. -> the digest it verified.

    Raises otherwise. A reader must never see a run marked done whose report is absent, partial or
    not the bytes the run's own checkpoint recorded, and "the file exists" answers none of those:
    the pipeline writes a PARTIAL snapshot to the same path the moment the seed search returns, so
    a run that died after the partial and before the finished report leaves a file that exists, is
    valid JSON, and is not the answer.
    """
    import runartifact

    path = webapp.report_path(slug)
    if not path.exists():
        raise RuntimeError("the pipeline finished without writing a report")
    ck = runstore.completed_stages(run_id).get("report") or {}
    recorded = ck.get("sha256") if isinstance(ck, dict) else None
    if recorded and not runartifact.verify(path, recorded):
        raise RuntimeError(
            f"the report at {path} is not the artifact this run checkpointed; refusing to settle "
            f"it done rather than publish a report nobody produced")
    body = runartifact.read(path, recorded)
    if body is None:
        raise RuntimeError(f"the report at {path} could not be read back as the finished artifact")
    if body.get("partial"):
        raise RuntimeError(
            "the report on disk is still the partial snapshot; a run marked done must not point "
            "at one")
    return recorded or runartifact.digest_of(path)


def execute(run, worker, heartbeat):
    """Run one claimed search. Raises on failure; the caller decides retry vs fail."""
    import webapp  # heavy: imported once, on first run

    inp = run.get("input") or {}
    slug = run["slug"]
    ctx = runctx.RunContext(run["run_id"], slug, attempt=run.get("attempts") or 1,
                            worker=worker, heartbeat=heartbeat,
                            #  Where the per-reference and per-round checkpoint artifacts live.
                            #  Under the reports directory, in a folder named for the RUN, so two
                            #  runs of one slug can never read each other's reading.
                            artifact_root=webapp.REPORTS)
    runctx.bind(slug, ctx)
    try:
        resume_stage, done = runstore.resume_point(run["run_id"])
        if done:
            #  CONSULTED, NOT MERELY PRINTED. Every stage in `done` is reloaded by the stage that
            #  owns it: `fuse` by webapp._generate, `screen` and each `read:<pub>` by deep_rank,
            #  each `retrieve:<channel>` by the retrieval fan-out, each `rescue:<round>` by
            #  claim_rescue. The line below is the operator's view of the same fact.
            print(f"[worker] {run['run_id']} attempt {ctx.attempt}: resuming at "
                  f"{resume_stage} ({len(done)} stage(s) already done: "
                  f"{', '.join(sorted(done))})", flush=True)
        subj = webapp._subject_obj(inp.get("subject")) if inp.get("subject") else None
        webapp._generate(slug, inp.get("query"), subj, inp.get("mode") or "novelty",
                         wide=bool(inp.get("wide")), doc_token=inp.get("doc_token"),
                         search_focus=inp.get("search_focus") or "all_text",
                         depth=inp.get("depth") or "deep")
        try:
            digest = verify_report(run["run_id"], slug, webapp)
        except Exception as exc:
            runstore.progress(run["run_id"], {"kind": "done", "status": "error",
                                              "msg": str(exc)[:300], "done": False})
            raise
        runstore.progress(run["run_id"], {"kind": "done", "status": "done", "msg": "done",
                                          "done": True, "report_sha256": digest})
    finally:
        runctx.unbind(slug)
        runstore.release_shards(run["run_id"])


LANES = ("quick", "deep")


def lane_caps(lane):
    """The configured limits this worker admits against.

    Resolved through `auth.lane_limits`, the same function the web gate uses, so a worker and the
    app can never disagree about what the operator configured. An earlier version invented its own
    variable names and would have quietly admitted against defaults nobody set.
    """
    import auth
    return auth.lane_limits(lane)


def sweep(lanes=None):
    """Admit waiting rows this worker could run. -> {lane: [run_id, ...]}.

    Without this nothing ever re-checks a row that was refused for concurrency or for today's
    budget: it stays unadmitted for ever, because the producer only looks once, at submission.
    A run finishing or a UTC day rolling over is exactly when a waiting row becomes eligible, and
    the worker is the process that notices both.
    """
    out = {}
    for lane in (lanes or LANES):
        caps = lane_caps(lane)
        got = runstore.admit_waiting(lane=lane, **caps)
        if got:
            out[lane] = got
            print(f"[worker] admitted {len(got)} waiting run(s) in lane {lane}", flush=True)
    return out


#  Settled in the SAME transaction as the run's terminal state, so there is no window in which a
#  run is done and its charge is not recorded. `charge` is the debit receipt for the run; a run
#  retried three times produces exactly one of these rows because (run_id, kind) is a primary key.
SETTLE_SIDE_EFFECTS = ("charge",)


def run_once(worker, lanes=None):
    """Claim and execute at most one run. -> the run_id executed, or None."""
    #  BEFORE any sweep or claim. DURABLE_WORKER_ENABLED=1 must never degrade into claiming
    #  unadmitted rows on a 009 database: the real worker refuses instead.
    runstore.require_admission_schema()
    #  And before any spend, for the same reason: a worker that cannot record a side effect
    #  exactly once cannot promise to do it exactly once, and finding that out after an hour of
    #  reading is finding it out too late.
    runstore.require_side_effect_schema()
    sweep(lanes)
    #  Explicit, right after the guard above: no second catalog probe, and the safety of
    #  this call is readable here rather than inferred from a global.
    run = runstore.claim(worker, lanes=lanes, admitted_only=True)
    if not run:
        return None
    rid = run["run_id"]
    print(f"[worker] claimed {rid} (slug={run['slug']} lane={run['lane']} "
          f"attempt={run['attempts']}/{run['max_attempts']})", flush=True)
    hb = runstore.Heartbeat(rid, worker).start()
    t0 = time.time()
    try:
        execute(run, worker, hb)
        settled, claimed = runstore.settle(rid, worker, "done",
                                           side_effects=SETTLE_SIDE_EFFECTS,
                                           attempt=int(run.get("attempts") or 1))
        if not settled:
            raise runstore.LeaseLost(
                f"{rid} could not be settled because worker ownership was lost")
        if "charge" in claimed:
            print(f"[worker] {rid} charged once, on attempt {run.get('attempts')}", flush=True)
        else:
            print(f"[worker] {rid} was already charged by an earlier attempt; not charging again",
                  flush=True)
    except runctx.RunCancelled as exc:
        #  A cancelled run is a lease we no longer hold, detected by the pipeline itself rather
        #  than at the settle. Logged apart from the generic case because it is the good outcome:
        #  it means the reading stopped instead of spending to the end of a run somebody else owns.
        print(f"[worker] {rid}: cancelled after {time.time() - t0:.0f}s ({exc.reason}); the "
              f"pipeline stopped spending and the run was NOT settled", flush=True)
        return rid
    except runstore.LeaseLost:
        #  Somebody else owns this run now. Do NOT settle it: that would overwrite their state.
        print(f"[worker] {rid}: lease lost after {time.time() - t0:.0f}s, dropping it", flush=True)
        return rid
    except Exception as exc:
        traceback.print_exc()
        state = runstore.fail(rid, worker, f"{type(exc).__name__}: {exc}")
        print(f"[worker] {rid} FAILED after {time.time() - t0:.0f}s -> {state}", flush=True)
        return rid
    finally:
        hb.stop()
    print(f"[worker] {rid} done in {time.time() - t0:.0f}s", flush=True)
    return rid


def loop(lanes=None, poll=None, once=False):
    worker = runstore.worker_id()
    poll = float(poll or POLL_SECONDS)
    print(f"[worker] {worker} up, lanes={lanes or 'any'}, poll={poll}s, "
          f"lease={runstore.LEASE_SECONDS}s", flush=True)
    runstore.ensure_schema()
    last_reap = 0.0
    #  Starts "now", not at zero: the first sweep is an hour in, so a worker restarted in a loop
    #  cannot turn the retention sweep into a hot loop against the live table.
    last_prune = time.time()
    while not _stop:
        try:
            if time.time() - last_reap > REAP_SECONDS:
                last_reap = time.time()
                runstore.reap()
                runstore.reap_shards()
            #  RETENTION, on its own much slower clock. While a run is alive its rows in
            #  `retrieval_hits` ARE its per-channel checkpoint; once it has settled they are
            #  evidence with a retention. One bounded delete an hour, so the ledger of a box
            #  running fifty deep searches a day does not grow without end and no single
            #  statement holds a lock on the live table for long. See runstore.prune_hits.
            if time.time() - last_prune > PRUNE_SECONDS:
                last_prune = time.time()
                n = runstore.prune_hits()
                if n:
                    print(f"[worker] pruned {n} retrieval hit row(s) from runs settled more "
                          f"than {runstore.HITS_TTL_DAYS} day(s) ago", flush=True)
            if run_once(worker, lanes) is None:
                if once:
                    return
                time.sleep(poll)
            elif once:
                return
        except KeyboardInterrupt:
            return
        except Exception:
            #  The store being briefly unreachable is not a reason to exit and have supervisor
            #  restart-loop us. Back off and try again.
            traceback.print_exc()
            time.sleep(min(30.0, poll * 5))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lanes", nargs="*", default=None,
                    help="restrict to these lanes (quick / deep). Default: any.")
    ap.add_argument("--poll", type=float, default=None)
    ap.add_argument("--once", action="store_true", help="claim at most one run, then exit")
    ap.add_argument("--reaper-only", action="store_true",
                    help="return expired leases and exit; execute nothing")
    args = ap.parse_args(argv)

    try:
        enabled()
    except NotEnabled as exc:
        #  Before ANY side effect, including the reaper, which mutates run state and is therefore
        #  not a read-only escape hatch.
        print(f"[worker] refusing to start: {exc}", file=sys.stderr, flush=True)
        return 2

    corpus_guard.arm("search worker")
    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    if args.reaper_only:
        runstore.ensure_schema()
        print(runstore.reap(), runstore.reap_shards(), flush=True)
        return 0
    loop(lanes=args.lanes, poll=args.poll, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
