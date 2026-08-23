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


def run(stop_event: threading.Event | None = None) -> None:
    """Recover dead owners, then poll and execute one leased turn at a time."""
    stop = stop_event or threading.Event()
    draft_studio_service.configure(build_runner)
    recovered = draft_studio_service.recover_interrupted_turns()
    print(
        f"[draft-turn-worker] ready, recovered={recovered}, "
        f"poll={draft_studio_service.POLL_SECONDS}s",
        flush=True,
    )
    while not stop.is_set():
        try:
            worked = draft_studio_service.process_one()
        except Exception:  # noqa: BLE001 - a queue or database blip must not stop the daemon
            traceback.print_exc()
            worked = None
        if worked is None:
            stop.wait(draft_studio_service.POLL_SECONDS)


def main() -> None:
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run(stop)


if __name__ == "__main__":
    main()
