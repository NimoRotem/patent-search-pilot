from pathlib import Path


def test_dedicated_worker_configures_recovers_and_polls(monkeypatch):
    """The calling thread supervises the slots rather than taking one itself, so a stop is acted
    on immediately instead of after whatever fifteen-minute turn that thread happened to be in."""
    import threading

    import draft_turn_worker

    calls = []
    stop = threading.Event()
    ran = threading.Event()

    def process_one():
        calls.append(("process", None))
        ran.set()
        return None

    monkeypatch.setattr(draft_turn_worker.draft_studio_service, "configure",
                        lambda factory: calls.append(("configure", factory)))
    monkeypatch.setattr(draft_turn_worker.draft_studio_service, "recover_interrupted_turns",
                        lambda: calls.append(("recover", None)) or 1)
    monkeypatch.setattr(draft_turn_worker.draft_studio_service, "process_one", process_one)
    monkeypatch.setattr(draft_turn_worker.draft_studio_service, "POLL_SECONDS", 0.01)

    thread = threading.Thread(target=draft_turn_worker.run, args=(stop,), daemon=True)
    thread.start()
    assert ran.wait(10), "no slot ever asked for a turn"
    stop.set()
    thread.join(timeout=15)

    assert not thread.is_alive(), "the worker did not stop when it was asked to"
    assert calls[0] == ("configure", draft_turn_worker.build_runner)
    assert ("recover", None) in calls
    assert ("process", None) in calls


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


def test_a_slot_that_dies_is_put_back(capsys):
    """The pool was found at one of five, hours after start, with an empty error log: a slot can go
    away without raising anything anybody sees, so something has to notice and replace it."""
    import threading

    import draft_studio_service
    import draft_turn_worker

    stop = threading.Event()
    killed = threading.Event()
    ticked = threading.Semaphore(0)

    def process_one():
        #  The first slot to arrive takes itself out of the pool, exactly once.
        if not killed.is_set():
            killed.set()
            raise SystemExit("this slot is going away")
        ticked.release()
        return None

    original = (draft_studio_service.process_one, draft_studio_service.configure,
                draft_studio_service.recover_interrupted_turns, draft_studio_service.POLL_SECONDS)
    supervise = draft_turn_worker.SUPERVISE_SECONDS
    #  A slot leaving is the event under test, so its exit is expected rather than a test failure.
    hook = threading.excepthook
    threading.excepthook = lambda _args: None
    draft_studio_service.process_one = process_one
    draft_studio_service.configure = lambda _factory: None
    draft_studio_service.recover_interrupted_turns = lambda: 0
    draft_studio_service.POLL_SECONDS = 0.01
    draft_turn_worker.SUPERVISE_SECONDS = 0.05
    try:
        runner = threading.Thread(target=draft_turn_worker.run, args=(stop,), daemon=True)
        runner.start()
        assert killed.wait(10), "the test never removed a slot"
        for _ in range(50):                    # let the supervisor notice and put it back
            ticked.acquire(timeout=0.2)
        stop.set()
        runner.join(timeout=15)
    finally:
        (draft_studio_service.process_one, draft_studio_service.configure,
         draft_studio_service.recover_interrupted_turns,
         draft_studio_service.POLL_SECONDS) = original
        draft_turn_worker.SUPERVISE_SECONDS = supervise
        threading.excepthook = hook

    printed = capsys.readouterr().out
    assert "was not alive; restarting it" in printed, (
        "a slot went away and nothing put it back:\n" + printed[-800:])
    assert "exited: SystemExit" in printed, "the reason a slot left was never reported"
