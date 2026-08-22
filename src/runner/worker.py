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
This worker is not enabled. The current web route and status stream still use the legacy
in-process dispatcher. They must be cut over to runstore before the Supervisor template is
enabled, and every recorded stage must be consulted on resume before the durability claim is
complete.

THE LOOP
--------
    reap expired leases  ->  claim one run (FOR UPDATE SKIP LOCKED)  ->  heartbeat thread on
    ->  execute with a RunContext bound  ->  finish / fail (retry) / release

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

POLL_SECONDS = float(os.environ.get("RUN_WORKER_POLL", "2.0"))
REAP_SECONDS = float(os.environ.get("RUN_WORKER_REAP", "15.0"))

_stop = False


def _signal(signum, _frame):
    """SIGTERM during a run is a request to stop taking NEW work, not to abandon this one.

    The run itself is safe either way: it is checkpointed, and if this process is SIGKILLed the
    lease expires and another worker resumes it. Draining just avoids paying for that.
    """
    global _stop
    _stop = True
    print(f"[worker] signal {signum}: draining, will not claim another run", flush=True)


def execute(run, worker, heartbeat):
    """Run one claimed search. Raises on failure; the caller decides retry vs fail."""
    import webapp  # heavy: imported once, on first run

    inp = run.get("input") or {}
    slug = run["slug"]
    ctx = runctx.RunContext(run["run_id"], slug, attempt=run.get("attempts") or 1,
                            worker=worker, heartbeat=heartbeat)
    runctx.bind(slug, ctx)
    try:
        resume_stage, done = runstore.resume_point(run["run_id"])
        if done:
            print(f"[worker] {run['run_id']} attempt {ctx.attempt}: resuming at "
                  f"{resume_stage} ({len(done)} stage(s) already done: "
                  f"{', '.join(sorted(done))})", flush=True)
        subj = webapp._subject_obj(inp.get("subject")) if inp.get("subject") else None
        webapp._generate(slug, inp.get("query"), subj, inp.get("mode") or "novelty",
                         wide=bool(inp.get("wide")), doc_token=inp.get("doc_token"),
                         search_focus=inp.get("search_focus") or "all_text",
                         depth=inp.get("depth") or "deep")
        ok = webapp.report_path(slug).exists()
        runstore.progress(run["run_id"], {"kind": "done", "status": "done" if ok else "error",
                                          "msg": "done" if ok else "no report was produced",
                                          "done": ok})
        if not ok:
            raise RuntimeError("the pipeline finished without writing a report")
    finally:
        runctx.unbind(slug)
        runstore.release_shards(run["run_id"])


def run_once(worker, lanes=None):
    """Claim and execute at most one run. -> the run_id executed, or None."""
    run = runstore.claim(worker, lanes=lanes)
    if not run:
        return None
    rid = run["run_id"]
    print(f"[worker] claimed {rid} (slug={run['slug']} lane={run['lane']} "
          f"attempt={run['attempts']}/{run['max_attempts']})", flush=True)
    hb = runstore.Heartbeat(rid, worker).start()
    t0 = time.time()
    try:
        execute(run, worker, hb)
        if not runstore.finish(rid, worker, "done"):
            raise runstore.LeaseLost(
                f"{rid} could not be settled because worker ownership was lost")
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
    while not _stop:
        try:
            if time.time() - last_reap > REAP_SECONDS:
                last_reap = time.time()
                runstore.reap()
                runstore.reap_shards()
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
