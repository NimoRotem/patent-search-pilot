"""Where a job lives on disk.

One directory per job, holding every artefact the pipeline produced and a status file that is
rewritten atomically as it goes. The browser polls rather than holding a request open, because a
figure set takes minutes and a held connection is a connection that dies at the proxy.

A job whose recorded process is gone is closed by whichever worker next looks at it. Jobs run on
daemon threads and do not survive a restart, so without that a killed job would poll for ever.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(os.environ.get("FM_DATA_DIR", os.path.expanduser("~/figuresmaker-data")))
JOBS_DIR = DATA_DIR / "jobs"
MAX_JOBS_KEPT = int(os.environ.get("FM_MAX_JOBS", "400"))

_lock = threading.Lock()


@dataclass
class Step:
    name: str
    state: str = "pending"          # pending | running | done | failed | skipped
    detail: str = ""
    started: float = 0.0
    finished: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["seconds"] = round((self.finished or time.time()) - self.started, 1) \
            if self.started else 0.0
        return out


STEP_NAMES = ("ingest", "sections", "registry", "claims", "plan", "figures", "layout",
              "validate", "export")


@dataclass
class Job:
    id: str
    created: float
    status: str = "queued"          # queued | running | done | failed | cancelled
    error: str = ""
    owner: str = ""
    title: str = ""
    source: str = ""
    pid: int = 0
    steps: list[Step] = field(default_factory=lambda: [Step(name) for name in STEP_NAMES])
    summary: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return JOBS_DIR / self.id

    def step(self, name: str) -> Step:
        for step in self.steps:
            if step.name == name:
                return step
        step = Step(name)
        self.steps.append(step)
        return step

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "created": self.created, "status": self.status,
                "error": self.error, "owner": self.owner, "title": self.title,
                "source": self.source, "pid": self.pid,
                "steps": [s.as_dict() for s in self.steps], "summary": self.summary,
                "options": self.options}


def _ensure() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def new_job(owner: str = "", title: str = "", source: str = "",
            options: Optional[dict] = None) -> Job:
    _ensure()
    job = Job(id=uuid.uuid4().hex[:16], created=time.time(), owner=owner, title=title,
              source=source, pid=os.getpid(), options=options or {})
    job.path.mkdir(parents=True, exist_ok=True)
    save(job)
    _prune()
    return job


def save(job: Job) -> None:
    write_json(job.path / "job.json", job.as_dict())


def load(job_id: str) -> Optional[Job]:
    raw = read_json(JOBS_DIR / job_id / "job.json")
    if not raw:
        return None
    job = Job(id=raw["id"], created=raw.get("created", 0.0), status=raw.get("status", "queued"),
              error=raw.get("error", ""), owner=raw.get("owner", ""),
              title=raw.get("title", ""), source=raw.get("source", ""),
              pid=int(raw.get("pid") or 0), summary=raw.get("summary") or {},
              options=raw.get("options") or {})
    job.steps = [Step(name=s.get("name", ""), state=s.get("state", "pending"),
                      detail=s.get("detail", ""), started=s.get("started", 0.0),
                      finished=s.get("finished", 0.0)) for s in raw.get("steps") or []]
    if job.status == "running" and job.pid and not _alive(job.pid):
        # The process that was running this is gone. Say so rather than poll for ever.
        job.status = "failed"
        job.error = ("the worker running this job is no longer present, most likely because the "
                     "application was restarted. Run it again.")
        save(job)
    return job


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def listing(owner: str = "", limit: int = 60) -> list[dict[str, Any]]:
    _ensure()
    out: list[dict[str, Any]] = []
    for path in sorted(JOBS_DIR.glob("*/job.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        raw = read_json(path)
        if not raw:
            continue
        if owner and raw.get("owner") and raw.get("owner") != owner:
            continue
        out.append({"id": raw.get("id"), "created": raw.get("created"),
                    "status": raw.get("status"), "title": raw.get("title"),
                    "source": raw.get("source"), "summary": raw.get("summary") or {}})
        if len(out) >= limit:
            break
    return out


def _prune() -> None:
    paths = sorted(JOBS_DIR.glob("*/"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[MAX_JOBS_KEPT:]:
        shutil.rmtree(path, ignore_errors=True)


# ------------------------------------------------------------------------------------ files


def write_json(path: Path, payload: Any) -> None:
    write_bytes(path, json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_text(path: Path, body: str) -> None:
    write_bytes(path, body.encode("utf-8"))


def write_bytes(path: Path, blob: bytes) -> None:
    """Atomic: a poller must never read half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        handle = tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False)
        try:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, path)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
