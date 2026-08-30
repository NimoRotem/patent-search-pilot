"""Run durable patent drafting turns outside the web process.

Search deployments restart gunicorn. Drafting turns can spend many minutes in text and drawing
review, so they must have a separate process lifetime. Postgres leases remain the authority and
keep ownership safe across local process boundaries without changing the worker contract.
"""
from __future__ import annotations

import signal
import threading
import traceback

import draft_studio
import draft_studio_service
import drafting

#  How often the supervisor looks at its slots. Short enough that a lost one is back quickly,
#  long enough that an idle pool costs nothing.
SUPERVISE_SECONDS = 5.0


def build_runner() -> draft_studio.TurnRunner:
    """Construct one turn runner from the same durable repositories used by the web app."""
    return draft_studio.TurnRunner(
        draft_studio.StudioRepository(), drafting.DraftingRepository())


def _drain(stop: threading.Event, slot: int = 0) -> None:
    """Claim and run turns until asked to stop. One of these per worker slot."""
    reason = "stop requested"
    try:
        while not stop.is_set():
            try:
                worked = draft_studio_service.process_one()
            except Exception:  # noqa: BLE001 - a queue or database blip must not stop the daemon
                traceback.print_exc()
                worked = None
            if worked is None:
                stop.wait(draft_studio_service.POLL_SECONDS)
    except BaseException as exc:  # noqa: BLE001 - say why a slot went away, then let it go
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        raise
    finally:
        #  A slot that disappears silently is how the pool decayed from five to one over an
        #  afternoon with nothing in any log to say so.
        print(f"[draft-turn-worker] slot {slot} exited: {reason}", flush=True)


def run(stop_event: threading.Event | None = None) -> None:
    """Recover dead owners, then drain the queue on ``DRAFT_TURN_WORKERS`` slots.

    ONE SLOT WAS A QUEUE EVERY PROJECT SHARED. A drawing repair that spends hours in
    "drawing and inspecting figures" held every other application on the host behind it, and the
    page told their owners the agent would pick their turn up in a moment. Postgres is still the
    authority for who owns what: claiming is FOR UPDATE SKIP LOCKED and a partial unique index
    allows one active turn PER PROJECT, so more slots run more APPLICATIONS side by side and can
    never run one of them twice.
    """
    stop = stop_event or threading.Event()
    draft_studio_service.configure(build_runner)
    recovered = draft_studio_service.recover_interrupted_turns()
    slots = draft_studio_service.DRAFT_TURN_WORKERS
    print(
        f"[draft-turn-worker] ready, recovered={recovered}, slots={slots}, "
        f"poll={draft_studio_service.POLL_SECONDS}s",
        flush=True,
    )
    #  EVERY slot is a thread and this thread supervises them, rather than the calling thread
    #  taking a slot itself. Two reasons, both learned the hard way. A slot can go away without
    #  raising anything a log would show - the pool was found at one of five, hours after start,
    #  with an empty error log - so something has to notice and put it back. And a caller that is
    #  itself running a fifteen-minute turn cannot act on a stop request until that turn ends,
    #  which made shutdown as slow as the longest drafting run.
    workers: list[threading.Thread] = []

    def spawn(slot: int) -> threading.Thread:
        thread = threading.Thread(target=_drain, args=(stop, slot),
                                  name=f"draft-turn-slot-{slot}", daemon=True)
        thread.start()
        return thread

    workers = [spawn(slot) for slot in range(1, max(1, slots) + 1)]
    #  is_set() decides, not the return of wait(). A stop object whose wait() returns None is
    #  perfectly ordinary and spun this loop for ever when the condition was written the other way.
    while not stop.is_set():
        stop.wait(SUPERVISE_SECONDS)
        if stop.is_set():
            break
        for index, thread in enumerate(workers):
            if not thread.is_alive():
                print(f"[draft-turn-worker] slot {index + 1} was not alive; restarting it",
                      flush=True)
                workers[index] = spawn(index + 1)
    for thread in workers:
        thread.join(timeout=5)


def main() -> None:
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run(stop)


if __name__ == "__main__":
    main()
