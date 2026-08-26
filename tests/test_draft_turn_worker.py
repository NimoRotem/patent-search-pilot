from pathlib import Path


def test_dedicated_worker_configures_recovers_and_polls(monkeypatch):
    import draft_turn_worker

    calls = []

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            calls.append(("wait", seconds))
            self.stopped = True

    monkeypatch.setattr(
        draft_turn_worker.draft_studio_service,
        "configure",
        lambda factory: calls.append(("configure", factory)),
    )
    monkeypatch.setattr(
        draft_turn_worker.draft_studio_service,
        "recover_interrupted_turns",
        lambda: calls.append(("recover", None)) or 1,
    )
    monkeypatch.setattr(
        draft_turn_worker.draft_studio_service,
        "process_one",
        lambda: calls.append(("process", None)),
    )

    draft_turn_worker.run(Stop())

    assert calls[0] == ("configure", draft_turn_worker.build_runner)
    assert ("recover", None) in calls
    assert ("process", None) in calls
    assert ("wait", draft_turn_worker.draft_studio_service.POLL_SECONDS) in calls


def test_supervisor_runs_turns_outside_the_web_process():
    root = Path(__file__).resolve().parents[1]
    web = (root / "patent-results.conf").read_text(encoding="utf-8")
    worker = (root / "patent-draft-turn-worker.conf").read_text(encoding="utf-8")

    assert 'DRAFT_TURN_WORKER="0"' in web
    assert "python -m draft_turn_worker" in worker
    assert "autorestart=true" in worker


def test_the_worker_drains_on_every_configured_slot():
    """One slot was a queue every project on the host shared, and one turn could hold all of it."""
    import threading
    import draft_studio_service
    import draft_turn_worker

    stop = threading.Event()
    seen = set()
    claims = threading.Semaphore(0)

    def process_one():
        seen.add(threading.current_thread().name)
        claims.release()
        return None

    original_process, original_configure, original_recover = (
        draft_studio_service.process_one, draft_studio_service.configure,
        draft_studio_service.recover_interrupted_turns)
    original_poll = draft_studio_service.POLL_SECONDS
    draft_studio_service.process_one = process_one
    draft_studio_service.configure = lambda _factory: None
    draft_studio_service.recover_interrupted_turns = lambda: 0
    draft_studio_service.POLL_SECONDS = 0.01
    try:
        runner = threading.Thread(target=draft_turn_worker.run, args=(stop,), daemon=True)
        runner.start()
        for _ in range(40):
            claims.acquire(timeout=2)
            if len(seen) >= draft_studio_service.DRAFT_TURN_WORKERS:
                break
        stop.set()
        runner.join(timeout=5)
    finally:
        draft_studio_service.process_one = original_process
        draft_studio_service.configure = original_configure
        draft_studio_service.recover_interrupted_turns = original_recover
        draft_studio_service.POLL_SECONDS = original_poll

    assert len(seen) == draft_studio_service.DRAFT_TURN_WORKERS


def test_graceful_stop_waits_for_every_inflight_slot(monkeypatch):
    """A deploy must not abandon another slot five seconds after the main slot finishes."""
    import threading
    import draft_studio_service
    import draft_turn_worker

    stop = threading.Event()
    stop.set()
    slots = []
    joins = []

    class Slot:
        def __init__(self, *, target, args, name, daemon):
            self.target = target
            self.args = args
            self.name = name
            self.daemon = daemon
            slots.append(self)

        def start(self):
            return None

        def join(self, timeout=None):
            joins.append(timeout)

    monkeypatch.setattr(draft_studio_service, "configure", lambda _factory: None)
    monkeypatch.setattr(draft_studio_service, "recover_interrupted_turns", lambda: 0)
    monkeypatch.setattr(draft_turn_worker.threading, "Thread", Slot)

    draft_turn_worker.run(stop)

    assert len(slots) == draft_studio_service.DRAFT_TURN_WORKERS - 1
    assert all(slot.target is draft_turn_worker._drain for slot in slots)
    assert all(slot.daemon is False for slot in slots)
    assert joins == [None] * len(slots)
