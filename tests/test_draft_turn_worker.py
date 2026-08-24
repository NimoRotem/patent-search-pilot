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
