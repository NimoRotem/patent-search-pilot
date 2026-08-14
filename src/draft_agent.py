"""Run Claude Code headlessly as the patent drafting and reviewing agent.

WHY A CODING AGENT DRAFTS A PATENT
----------------------------------
A patent application is a set of interdependent documents — a specification, a numbered claim
set, drawing descriptions and a reference-numeral vocabulary — where a change in one place must
propagate to the others or the application is internally inconsistent.  That is a file-editing
problem, not a single-completion problem.  One model call has to emit the whole application in one
pass and cannot go back to check that claim 7 still has antecedent basis after claim 1 was
narrowed.  An agent with Read/Edit/Grep over a directory can, and does, exactly that.

So the draft lives on disk as a small tree of files (see ``draft_workspace``), the agent edits it
in place, and this module is the bridge: it starts ``claude -p``, streams its events back, and
returns a validated JSON summary of what it did.

ISOLATION — THE PART THAT MATTERS
---------------------------------
This runs on a box whose owner's ``~/CLAUDE.md`` carries pages of infrastructure instructions, and
whose Claude configuration carries skills, plugins and MCP servers.  A drafting agent that
inherited any of that would be worse at drafting and could reach far outside its workspace, so
every run is pinned:

  ``--safe-mode``          no CLAUDE.md discovery, no skills, plugins, hooks or MCP servers
  ``--setting-sources ""`` no user/project/local settings files
  ``--tools ...``          the tool set is an explicit allow-list, not a deny-list
  ``CLAUDE_CONFIG_DIR``    a private config directory, never the operator's own
  ``cwd``                  the project workspace, which is deliberately outside ``$HOME``

The drafting instructions are passed as ``--append-system-prompt`` rather than written into a
CLAUDE.md inside the workspace, because the agent can edit files in the workspace and must not be
able to edit its own instructions.

STRUCTURED RESULT
-----------------
``--json-schema`` makes the CLI expose a StructuredOutput tool and validate the final answer
against the schema, so the caller gets a parsed object instead of prose it has to scrape.  A run
that ends without one is an error we can name, not a parsing accident.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# Model names are aliases on purpose: the CLI resolves an alias to the current model of that tier,
# so a project drafted last month and revised today does not silently jump generations mid-thread
# only because we pinned a dated identifier that has since been retired.
DRAFT_MODEL = os.environ.get("DRAFT_AGENT_MODEL", "opus")
QA_MODEL = os.environ.get("DRAFT_QA_MODEL", "opus")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "")
DRAFT_TIMEOUT = max(120, int(os.environ.get("DRAFT_AGENT_TIMEOUT", "1500")))
QA_TIMEOUT = max(120, int(os.environ.get("DRAFT_QA_TIMEOUT", "900")))
MAX_BUDGET_USD = float(os.environ.get("DRAFT_AGENT_MAX_USD", "12"))

# The lookup helper the agent may run.  Bash is otherwise unusable: the allow-list below is the
# only command auto-approved, and with `--permission-mode acceptEdits` anything else is refused
# rather than queued for a human who is not there.
LOOKUP_COMMAND = "python3 tools/patent_lookup.py"

_DRAFT_TOOLS = "Read,Write,Edit,Glob,Grep,Bash"
_QA_TOOLS = "Read,Glob,Grep,Bash"

_ENV_LOCK = threading.Lock()
_CACHED_TOKEN: tuple[float, str] | None = None
_CACHED_VERSION: tuple[str, str] | None = None


class AgentError(RuntimeError):
    """The agent could not be run, or finished without a usable answer."""


class AgentUnavailable(AgentError):
    """Claude Code is not installed or not authenticated on this host."""


@dataclass
class AgentRun:
    ok: bool = False
    result: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    model: str = ""
    error: str = ""
    cancelled: bool = False
    #  A compact, user-facing trace of what the agent actually did.  Not the raw transcript: that
    #  is written to disk in full and linked, because it is large and mostly uninteresting.
    steps: list[dict[str, Any]] = field(default_factory=list)
    transcript_path: str = ""


def binary() -> str:
    """Absolute path to the Claude Code CLI, or ''.

    The npm-global install is checked BEFORE ``which``: this host carries two of them, and the
    system-wide one at /usr/bin/claude is an older release that PATH happens to find first under
    supervisor. The flags this module depends on (``--safe-mode``, ``--tools``, ``--json-schema``)
    are recent, so silently taking the older binary would fail in a way that reads like a model
    problem rather than a packaging one. ``CLAUDE_BIN`` overrides everything.
    """
    candidates = [CLAUDE_BIN, str(Path.home() / ".npm-global/bin/claude"),
                  shutil.which("claude") or "", "/usr/local/bin/claude", "/usr/bin/claude"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def version(path: str = "") -> str:
    """The CLI's reported version, cached — surfaced so a flag failure is diagnosable."""
    global _CACHED_VERSION
    path = path or binary()
    if not path:
        return ""
    if _CACHED_VERSION and _CACHED_VERSION[0] == path:
        return _CACHED_VERSION[1]
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        reported = (out.stdout or "").strip().split()[0]
    except Exception:                                          # noqa: BLE001
        reported = ""
    _CACHED_VERSION = (path, reported)
    return reported


def _oauth_token() -> str:
    """The long-lived subscription token, cached briefly.

    Read from disk rather than the process environment because the web tier is started by
    supervisor with a deliberately small environment, and because a token rotated on disk should
    be picked up without a restart.
    """
    global _CACHED_TOKEN
    with _ENV_LOCK:
        if _CACHED_TOKEN and time.time() - _CACHED_TOKEN[0] < 300:
            return _CACHED_TOKEN[1]
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        if not token:
            path = Path(os.environ.get("CLAUDE_OAUTH_TOKEN_FILE",
                                       str(Path.home() / ".claude/oauth_token")))
            try:
                token = path.read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
        _CACHED_TOKEN = (time.time(), token)
        return token


def availability() -> dict[str, Any]:
    """Whether an agent run can be attempted, and if not, precisely why.

    The drafting page asks this before offering the conversation.  A product that accepts a
    request it cannot serve and reports the failure four minutes later is worse than one that says
    up front that its drafting agent is not configured on this host.
    """
    path = binary()
    if not path:
        return {"ok": False, "reason": "The Claude Code CLI is not installed on this host.",
                "binary": "", "auth": False}
    token = _oauth_token()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not token and not api_key:
        return {"ok": False, "reason": "No Claude subscription token or API key is configured.",
                "binary": path, "auth": False}
    return {"ok": True, "reason": "", "binary": path, "auth": True,
            "version": version(path), "draft_model": DRAFT_MODEL, "qa_model": QA_MODEL}


def config_dir(root: Path) -> Path:
    """A private Claude configuration directory shared by every run on this host."""
    out = Path(root) / ".agent-home"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _environment(cfg_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
    token = _oauth_token()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        # A stale ANTHROPIC_API_KEY in the service environment would be preferred over the
        # subscription token and would bill (or 401) against an account we did not intend.
        env.pop("ANTHROPIC_API_KEY", None)
    env.setdefault("CI", "1")
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def new_session_id() -> str:
    return str(uuid.uuid4())


def _summarize_event(event: Mapping[str, Any], steps: list[dict[str, Any]]) -> None:
    """Fold one stream event into the short, user-facing trace."""
    kind = event.get("type")
    if kind == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            btype = block.get("type")
            if btype == "thinking":
                thought = str(block.get("thinking") or "").strip()
                if thought:
                    steps.append({"kind": "thinking", "text": thought[:4000]})
            elif btype == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    steps.append({"kind": "say", "text": text[:4000]})
            elif btype == "tool_use":
                name = str(block.get("name") or "")
                if name == "StructuredOutput":
                    continue
                steps.append({"kind": "tool", "tool": name,
                              "detail": _tool_detail(name, block.get("input") or {})})
    elif kind == "result" and event.get("is_error"):
        steps.append({"kind": "error", "text": str(event.get("result") or "")[:2000]})


def _tool_detail(name: str, payload: Mapping[str, Any]) -> str:
    """One readable line per tool call — the file touched, not the whole payload."""
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return _relative(str(payload.get("file_path") or ""))
    if name == "Bash":
        return str(payload.get("command") or "")[:200]
    if name == "Grep":
        return f"{payload.get('pattern', '')!s:.80} in {_relative(str(payload.get('path') or '.'))}"
    if name == "Glob":
        return str(payload.get("pattern") or "")[:120]
    return json.dumps(payload, ensure_ascii=False)[:200]


def _relative(path: str) -> str:
    """Workspace-relative path, so a transcript never leaks the server's directory layout."""
    if not path:
        return ""
    for marker in ("/draft/", "/prior_art/", "/input/", "/figures/", "/review/", "/tools/"):
        index = path.find(marker)
        if index >= 0:
            return path[index + 1:]
    return Path(path).name


def run(*, workspace: Path, prompt: str, system_prompt: str, schema: Mapping[str, Any],
        session_id: str = "", resume: bool = False, model: str = "", tools: str = _DRAFT_TOOLS,
        timeout: int = DRAFT_TIMEOUT, transcript: Path | None = None,
        allowed_bash: Sequence[str] = (LOOKUP_COMMAND,),
        on_event: Callable[[Mapping[str, Any]], None] | None = None,
        cancel: threading.Event | None = None,
        max_budget_usd: float = MAX_BUDGET_USD) -> AgentRun:
    """One agent turn inside ``workspace``.

    ``resume`` continues the project's own thread so the agent remembers the decisions it already
    explained to the user; a review pass passes ``resume=False`` with a fresh id precisely so that
    it does NOT inherit the drafter's reasoning and re-approve it.
    """
    binary_path = binary()
    if not binary_path:
        raise AgentUnavailable("The Claude Code CLI is not installed on this host.")
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise AgentError(f"Draft workspace {workspace} does not exist.")
    session_id = session_id or new_session_id()

    cfg = config_dir(workspace.parent)
    argv = [
        binary_path, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--safe-mode", "--setting-sources", "",
        "--tools", tools,
        "--permission-mode", "acceptEdits",
        "--model", model or DRAFT_MODEL,
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--append-system-prompt", system_prompt,
        "--max-budget-usd", f"{max_budget_usd:g}",
    ]
    for command in allowed_bash:
        argv += ["--allowedTools", f"Bash({command}:*)"]
    argv += (["--resume", session_id] if resume else ["--session-id", session_id])

    handle = None
    if transcript:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        handle = transcript.open("a", encoding="utf-8")

    out = AgentRun(session_id=session_id, model=model or DRAFT_MODEL,
                   transcript_path=str(transcript or ""))
    started = time.time()
    process = subprocess.Popen(
        argv, cwd=str(workspace), env=_environment(cfg),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, start_new_session=True)
    stderr_tail: list[str] = []

    def _drain_stderr() -> None:
        for line in process.stderr:                      # type: ignore[union-attr]
            stderr_tail.append(line.rstrip()[:400])
            del stderr_tail[:-40]

    watcher = threading.Thread(target=_drain_stderr, name="draft-agent-stderr", daemon=True)
    watcher.start()

    #  A WATCHDOG THREAD, not a check inside the read loop.  The loop only wakes when a line
    #  arrives, so a run that wedges with no output would sit past its deadline for ever — and a
    #  user pressing Stop would be told the turn was cancelled while the model kept working and
    #  kept costing a shared four-core box.  Killing the process closes stdout, which is what ends
    #  the loop below.
    stopped = {"reason": ""}
    finished = threading.Event()

    def _watch() -> None:
        deadline = started + timeout
        while not finished.wait(1.0):
            if cancel is not None and cancel.is_set():
                stopped["reason"] = "cancelled"
                _terminate(process, grace=5)
                return
            if time.time() > deadline:
                stopped["reason"] = "timeout"
                _terminate(process, grace=5)
                return

    watchdog = threading.Thread(target=_watch, name="draft-agent-watchdog", daemon=True)
    watchdog.start()

    final: dict[str, Any] = {}
    try:
        for line in process.stdout:                      # type: ignore[union-attr]
            if handle:
                handle.write(line)
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            _summarize_event(event, out.steps)
            if event.get("type") == "result":
                final = event
            if on_event:
                try:
                    on_event(event)
                except Exception:                        # noqa: BLE001 - progress must never fail a run
                    pass
    finally:
        finished.set()
        if process.poll() is None:
            _terminate(process, grace=3)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        watcher.join(timeout=2)
        watchdog.join(timeout=3)
        if handle:
            handle.close()

    out.duration_ms = int((time.time() - started) * 1000)
    if stopped["reason"] == "timeout":
        out.error = f"The drafting agent exceeded its {timeout}s time limit."
        return out
    if stopped["reason"] == "cancelled":
        out.error = "Stopped at your request."
        out.cancelled = True
        return out
    if not final:
        detail = " / ".join(stderr_tail[-3:]) or f"exit code {process.returncode}"
        out.error = f"The drafting agent produced no result ({detail})."
        return out

    out.cost_usd = float(final.get("total_cost_usd") or 0.0)
    out.num_turns = int(final.get("num_turns") or 0)
    out.session_id = str(final.get("session_id") or session_id)
    payload = final.get("result")
    out.text = payload if isinstance(payload, str) else ""
    if final.get("is_error"):
        out.error = (out.text or str(final.get("subtype") or "error"))[:2000]
        return out
    parsed = _parse_result(payload)
    if parsed is None:
        out.error = ("The drafting agent finished without returning the required structured "
                     "answer.")
        return out
    out.result = parsed
    out.ok = True
    return out


def _terminate(process: subprocess.Popen, *, grace: int) -> None:
    """Stop the CLI and everything it started.

    ``start_new_session=True`` above puts the run in its own process group so this kills the whole
    group.  Killing only the parent leaves the model request and any child running, which on a
    four-core shared box is how a cancelled draft keeps costing CPU for another ten minutes.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.2)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _parse_result(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, Mapping):
        return dict(payload)
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    if not text:
        return None
    # The CLI validates against the schema and returns the object as a JSON string.  A model that
    # wrapped it in a fence anyway is a cheap repair, not a reason to throw the turn away.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return dict(value) if isinstance(value, dict) else None


def strings(value: Any, *, limit: int = 40, chars: int = 4000) -> list[str]:
    """Normalise a model-supplied list of strings, tolerating a single string."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable):
        return []
    out = []
    for item in value:
        text = (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)).strip()
        if text:
            out.append(text[:chars])
        if len(out) >= limit:
            break
    return out
