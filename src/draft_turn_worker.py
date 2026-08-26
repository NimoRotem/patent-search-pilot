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


def build_runner() -> draft_studio.TurnRunner:
    """Construct one turn runner from the same durable repositories used by the web app."""
    return draft_studio.TurnRunner(
        draft_studio.StudioRepository(), drafting.DraftingRepository())


def _drain(stop: threading.Event) -> None:
    """Claim and run turns until asked to stop. One of these per worker slot."""
    while not stop.is_set():
        try:
            worked = draft_studio_service.process_one(stop_event=stop)
        except Exception:  # noqa: BLE001 - a queue or database blip must not stop the daemon
            traceback.print_exc()
            worked = None
        if worked is None:
            stop.wait(draft_studio_service.POLL_SECONDS)


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
    #  The calling thread is the first slot, so SIGTERM still reaches the process directly. The
    #  other slots are non-daemon threads and are joined without a deadline: every claimed turn
    #  must finish checkpointing before Supervisor is allowed to replace this process.
    extra = [threading.Thread(target=_drain, args=(stop,), name=f"draft-turn-slot-{index}",
                              daemon=False)
             for index in range(2, max(1, slots) + 1)]
    for thread in extra:
        thread.start()
    _drain(stop)
    for thread in extra:
        thread.join()


def main() -> None:
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run(stop)


if __name__ == "__main__":
    main()
