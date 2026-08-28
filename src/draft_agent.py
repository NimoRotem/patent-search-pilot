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
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

#  Imported without a fallback on purpose.  If the spend guard is missing this host must not draft
#  at all: an agent that runs unmetered is exactly the failure this import exists to prevent.
from llm_spend_guard import SpendGuard

# Model names are aliases on purpose: the CLI resolves an alias to the current model of that tier,
# so a project drafted last month and revised today does not silently jump generations mid-thread
# only because we pinned a dated identifier that has since been retired.
DRAFT_MODEL = os.environ.get("DRAFT_AGENT_MODEL", "opus")
QA_MODEL = os.environ.get("DRAFT_QA_MODEL", "opus")

#  The tiers a project may be drafted on, for the same reason the constants above are aliases: the
#  CLI resolves an alias to the current model of that tier, so a project drafted last month and
#  revised today does not silently change generation because a dated identifier was retired.
#  An id that is not on this list never reaches the command line.
MODEL_CHOICES = (
    {"id": "opus", "label": "Opus 5",
     "detail": "The most capable. Slowest and dearest; the default for a first draft."},
    {"id": "fable", "label": "Fable 5",
     "detail": "Strong at long prose. A good choice for description and background work."},
    {"id": "sonnet", "label": "Sonnet 5",
     "detail": "Noticeably faster and cheaper. Fine for wording changes to one section."},
    {"id": "haiku", "label": "Haiku 4.5",
     "detail": "Fastest and cheapest. Small, well-specified edits only."},
)
MODEL_IDS = frozenset(item["id"] for item in MODEL_CHOICES)


def normalize_model(value: Any) -> str:
    """A model id this host will run, or '' meaning the server default for this kind of work."""
    name = str(value or "").strip().lower()
    return name if name in MODEL_IDS else ""


def model_label(value: Any) -> str:
    name = normalize_model(value)
    return next((item["label"] for item in MODEL_CHOICES if item["id"] == name), "")


CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "")
DRAFT_TIMEOUT = max(120, int(os.environ.get("DRAFT_AGENT_TIMEOUT", "1500")))
QA_TIMEOUT = max(120, int(os.environ.get("DRAFT_QA_TIMEOUT", "900")))
MAX_BUDGET_USD = float(os.environ.get("DRAFT_AGENT_MAX_USD", "12"))
#  There is no auth mode any more.  DRAFT_AGENT_TOKEN_FILES lists SUBSCRIPTION credentials in the
#  order to try them, colon separated; a capped one falls through to the next subscription and
#  never to metered billing.  Only a long-lived `claude setup-token` credential belongs on this
#  list: a rotating one presented from two hosts is revoked by reuse detection, taking both.
DEFAULT_TOKEN_FILES = (str(Path.home() / ".claude/oauth_token"),
                       str(Path.home() / ".claude/oauth_token.fallback"))
RATE_LIMIT_RETRIES = max(0, min(int(os.environ.get("DRAFT_AGENT_RATE_LIMIT_RETRIES", "2")), 3))
RATE_LIMIT_RETRY_SECONDS = max(
    1, min(int(os.environ.get("DRAFT_AGENT_RATE_LIMIT_RETRY_SECONDS", "65")), 300))
VERTEX_FALLBACK = os.environ.get(
    "DRAFT_AGENT_VERTEX_FALLBACK", "1").strip().lower() not in {"0", "false", "no", "off"}
VERTEX_AGENT_MODEL = os.environ.get(
    "DRAFT_AGENT_VERTEX_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro"
VERTEX_AGENT_ROUNDS = max(
    4, min(int(os.environ.get("DRAFT_AGENT_VERTEX_ROUNDS", "40")), 80))
VERTEX_AGENT_TOOL_CALLS = max(
    20, min(int(os.environ.get("DRAFT_AGENT_VERTEX_TOOL_CALLS", "180")), 400))
VERTEX_AGENT_SLOTS = max(
    1, min(int(os.environ.get("DRAFT_AGENT_VERTEX_SLOTS", "2")), 4))
VERTEX_CALL_TIMEOUT_MS = max(
    30_000, min(int(os.environ.get("DRAFT_AGENT_VERTEX_CALL_TIMEOUT_MS", "300000")), 600_000))

# The lookup helper the agent may run.  Bash is otherwise unusable: the allow-list below is the
# only command auto-approved, and with `--permission-mode acceptEdits` anything else is refused
# rather than queued for a human who is not there.
LOOKUP_COMMAND = "python3 tools/patent_lookup.py"

_DRAFT_TOOLS = "Read,Write,Edit,Glob,Grep,Bash"
_QA_TOOLS = "Read,Glob,Grep,Bash"

_ENV_LOCK = threading.Lock()
_CACHED_TOKEN: tuple[float, tuple[str, ...], tuple[str, ...]] | None = None
_CACHED_VERSION: tuple[str, str] | None = None
#  The metered ceiling. Vertex is the only route left that reaches it.  Per app, per UTC day, raised only by a deliberate `llm-spend override`.
SPEND_APP = (os.environ.get("LLM_SPEND_APP", "patent-search-pilot").strip()
             or "patent-search-pilot")
_SPEND = SpendGuard(SPEND_APP)
_VERTEX_CLIENT_LOCAL = threading.local()
_VERTEX_AGENT_LANE = threading.BoundedSemaphore(VERTEX_AGENT_SLOTS)


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
    #  What the run actually put through the models. Cost alone hides the shape of the spend: a
    #  turn whose context is re-sent on every repair round reads tens of millions of cached tokens
    #  and looks cheap per call while being ruinous in aggregate.
    tokens: dict[str, int] = field(default_factory=lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
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


def token_files() -> list[Path]:
    """The subscription credential files this host may present, in priority order."""
    configured = os.environ.get("DRAFT_AGENT_TOKEN_FILES", "").strip()
    if configured:
        return [Path(item).expanduser() for item in configured.split(":") if item.strip()]
    primary = os.environ.get("CLAUDE_OAUTH_TOKEN_FILE", "").strip()
    names = (primary,) + DEFAULT_TOKEN_FILES[1:] if primary else DEFAULT_TOKEN_FILES
    return [Path(name).expanduser() for name in names]


def subscription_tokens() -> list[str]:
    """Every subscription credential this host can present, cached briefly.

    Read from disk rather than the process environment because the web tier is started by
    supervisor with a deliberately small environment, and because a token rotated on disk should
    be picked up without a restart.  A file that is absent is a host with one subscription, which
    is the normal case and not a misconfiguration.
    """
    global _CACHED_TOKEN
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    paths = token_files()
    asked = (env_token,) + tuple(str(item) for item in paths)
    with _ENV_LOCK:
        if _CACHED_TOKEN and _CACHED_TOKEN[2] == asked and time.time() - _CACHED_TOKEN[0] < 300:
            return list(_CACHED_TOKEN[1])
        out: list[str] = []
        if env_token:
            out.append(env_token)
        for path in paths:
            try:
                token = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if token and token not in out:
                out.append(token)
        _CACHED_TOKEN = (time.time(), tuple(out), asked)
        return list(out)


def _oauth_token() -> str:
    """The credential a run reaches for first."""
    tokens = subscription_tokens()
    return tokens[0] if tokens else ""


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
    tokens = subscription_tokens()
    if not tokens:
        return {"ok": False,
                "reason": ("No Claude subscription is configured on this host. Metered API "
                           "billing is deliberately not a fallback."),
                "binary": path, "auth": False, "auth_mode": "subscription",
                "subscriptions": 0}
    #  Report the ceiling too.  A page that offers a conversation this host will refuse to pay for
    #  is the same defect as one that offers a conversation it cannot authenticate.
    spend = _SPEND.status()
    return {"ok": True, "reason": "", "binary": path, "auth": True,
            "auth_mode": "subscription", "subscriptions": len(tokens),
            "version": version(path),
            "draft_model": DRAFT_MODEL, "qa_model": QA_MODEL,
            "models": [dict(item) for item in MODEL_CHOICES],
            "spend": {"app": spend.app, "day": spend.day, "spent_usd": round(spend.spent_usd, 2),
                      "cap_usd": spend.cap_usd, "metered": False,
                      "at_cap": not spend.allowed, "degraded": spend.degraded}}


def config_dir(root: Path) -> Path:
    """A private Claude configuration directory shared by every run on this host."""
    out = Path(root) / ".agent-home"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _environment(cfg_dir: Path, *, token: str = "") -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token or _oauth_token()
    #  UNCONDITIONAL.  An ANTHROPIC_API_KEY left in the service environment is preferred by the CLI
    #  over the subscription token, and that is exactly how this host spent half a billion metered
    #  tokens in an afternoon while the page still said it was on the subscription.
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


def _final_error(final: Mapping[str, Any]) -> str:
    """Keep the CLI's specific failure instead of collapsing it to its generic subtype."""
    errors = final.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
        detail = " / ".join(str(item).strip() for item in errors if str(item).strip())
        if detail:
            return detail
    elif str(errors or "").strip():
        return str(errors).strip()
    result = final.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    return str(final.get("subtype") or "error")


def _run_once(*, workspace: Path, prompt: str, system_prompt: str, schema: Mapping[str, Any],
              session_id: str = "", resume: bool = False, model: str = "",
              tools: str = _DRAFT_TOOLS, timeout: int = DRAFT_TIMEOUT,
              transcript: Path | None = None,
              allowed_bash: Sequence[str] = (LOOKUP_COMMAND,),
              on_event: Callable[[Mapping[str, Any]], None] | None = None,
              cancel: threading.Event | None = None,
              max_budget_usd: float = MAX_BUDGET_USD,
              token: str = "") -> AgentRun:
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
        argv, cwd=str(workspace), env=_environment(cfg, token=token),
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
    usage = final.get("usage") or {}
    out.tokens = {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
    }
    out.session_id = str(final.get("session_id") or session_id)
    payload = final.get("result")
    out.text = payload if isinstance(payload, str) else ""
    if final.get("is_error"):
        out.error = _final_error(final)[:2000]
        return out
    parsed = _parse_result(payload)
    if parsed is None:
        out.error = ("The drafting agent finished without returning the required structured "
                     "answer.")
        return out
    out.result = parsed
    out.ok = True
    return out


def _provider_quota_error(error: str) -> bool:
    """Recognize a durable account ceiling, including the provider's current wording."""
    text = str(error or "").lower()
    return bool(
        re.search(r"\b(?:weekly|monthly|usage) limits?\b", text) or
        "specified api usage limits" in text or
        ("reached" in text and "usage" in text and "limit" in text) or
        ("hit your" in text and "limit" in text and "reset" in text))


def _vertex_client():
    """One Vertex client per worker thread, using the VM service account."""
    key = (
        os.environ.get("GCP_PROJECT", "nimo-gpt"),
        os.environ.get("VERTEX_LOCATION", "us-central1"),
    )
    if getattr(_VERTEX_CLIENT_LOCAL, "key", None) != key:
        from google import genai
        _VERTEX_CLIENT_LOCAL.client = genai.Client(
            vertexai=True, project=key[0], location=key[1])
        _VERTEX_CLIENT_LOCAL.key = key
    return _VERTEX_CLIENT_LOCAL.client


_VERTEX_READ_ROOTS = frozenset({
    "input", "prior_art", "draft", "figures", "review", "tools",
})
_VERTEX_WRITE_ROOTS = frozenset({"draft", "figures"})
_VERTEX_TEXT_SUFFIXES = frozenset({
    ".md", ".txt", ".json", ".jsonl", ".csv", ".tsv", ".py", ".xml", ".html",
})


def _workspace_path(workspace: Path, value: Any, *, write: bool = False) -> Path:
    """Resolve a model path inside the narrow drafting workspace."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or "\x00" in raw:
        raise ValueError("Use a non-empty workspace-relative path.")
    root = Path(workspace).resolve()
    candidate = (root / raw).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("The path leaves the drafting workspace.") from exc
    if not relative.parts or relative.parts[0] not in _VERTEX_READ_ROOTS:
        raise ValueError("The path is outside the drafting workspace's allowed directories.")
    if write and relative.parts[0] not in _VERTEX_WRITE_ROOTS:
        raise ValueError("Only draft/ and figures/ may be edited.")
    return candidate


def _safe_glob(workspace: Path, pattern: Any) -> list[Path]:
    raw = str(pattern or "**/*").strip().replace("\\", "/") or "**/*"
    if len(raw) > 240 or raw.count("**") > 4:
        raise ValueError("The glob is too broad or too long.")
    if raw.startswith("/") or any(part == ".." for part in Path(raw).parts):
        raise ValueError("The glob must stay inside the drafting workspace.")
    root = Path(workspace).resolve()
    paths = []
    for candidate in root.glob(raw):
        try:
            relative = candidate.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if relative.parts and relative.parts[0] in _VERTEX_READ_ROOTS:
            paths.append(candidate)
    return sorted(paths, key=lambda item: item.as_posix())


def _vertex_tool_declarations(types, *, schema: Mapping[str, Any], tools: str,
                              allowed_bash: Sequence[str]):
    allowed = {item.strip() for item in str(tools or "").split(",") if item.strip()}

    def declaration(name: str, description: str, properties: Mapping[str, Any],
                    required: Sequence[str] = ()):
        return types.FunctionDeclaration(
            name=name,
            description=description,
            parameters_json_schema={
                "type": "object", "properties": dict(properties),
                "required": list(required), "additionalProperties": False,
            },
        )

    declarations = []
    if "Glob" in allowed:
        declarations.append(declaration(
            "list_files", "List files or directories matching a workspace-relative glob.",
            {"pattern": {"type": "string", "description": "For example **/*.md"}},
            ["pattern"]))
    if "Read" in allowed:
        declarations.append(declaration(
            "read_file",
            "Read a text file by line range. Reading an image attaches its pixels for inspection.",
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 2000},
            }, ["path"]))
    if "Grep" in allowed:
        declarations.append(declaration(
            "grep_files", "Search text files for a case-insensitive literal string.",
            {
                "pattern": {"type": "string"},
                "file_glob": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
            }, ["pattern"]))
    if "Write" in allowed:
        declarations.append(declaration(
            "write_file", "Create or replace one filing file under draft/ or figures/.",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"]))
    if "Edit" in allowed:
        declarations.append(declaration(
            "replace_text", "Replace exact text in one file under draft/ or figures/.",
            {
                "path": {"type": "string"}, "old_text": {"type": "string"},
                "new_text": {"type": "string"}, "replace_all": {"type": "boolean"},
            }, ["path", "old_text", "new_text"]))
    if ("Bash" in allowed and
            any(str(command).strip() == LOOKUP_COMMAND for command in allowed_bash)):
        declarations.append(declaration(
            "patent_lookup",
            "Run the workspace's exact local patent lookup with publication numbers and flags.",
            {"arguments": {
                "type": "array", "items": {"type": "string"},
                "minItems": 1, "maxItems": 41,
            }}, ["arguments"]))
    declarations.append(types.FunctionDeclaration(
        name="submit_result",
        description=("Return the required structured result only after all requested reading, "
                     "editing, and verification are complete."),
        parameters_json_schema=dict(schema) if schema else {
            "type": "object", "additionalProperties": True,
        },
    ))
    return declarations


def _schema_problem(value: Any, schema: Mapping[str, Any], path: str = "result") -> str:
    """Small recursive validator for the JSON Schema subset used by drafting results."""
    if not schema:
        return "" if isinstance(value, Mapping) else f"{path} must be an object."
    if "enum" in schema and value not in schema.get("enum", ()):
        return f"{path} is not one of the allowed values."
    expected = schema.get("type")
    type_ok = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(str(expected), True)
    if not type_ok:
        return f"{path} must have type {expected}."
    if expected == "object":
        properties = schema.get("properties") or {}
        for name in schema.get("required") or ():
            if name not in value:
                return f"{path}.{name} is required."
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                return f"{path} has an unexpected property: {min(extra)}."
        for name, child in properties.items():
            if name in value and isinstance(child, Mapping):
                problem = _schema_problem(value[name], child, f"{path}.{name}")
                if problem:
                    return problem
    elif expected == "array":
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            return f"{path} has too many items."
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            return f"{path} has too few items."
        child = schema.get("items")
        if isinstance(child, Mapping):
            for index, item in enumerate(value):
                problem = _schema_problem(item, child, f"{path}[{index}]")
                if problem:
                    return problem
    return ""


def _vertex_tool(workspace: Path, name: str, arguments: Mapping[str, Any], *, writable: bool):
    """Execute one declared workspace tool and return JSON plus optional visual parts."""
    root = Path(workspace).resolve()
    attachments: list[tuple[bytes, str]] = []
    if name == "list_files":
        items = []
        for path in _safe_glob(root, arguments.get("pattern"))[:500]:
            relative = path.resolve().relative_to(root).as_posix()
            items.append(relative + ("/" if path.is_dir() else ""))
        return {"ok": True, "paths": items, "truncated": len(items) >= 500}, attachments

    if name == "read_file":
        path = _workspace_path(root, arguments.get("path"))
        if not path.is_file():
            raise ValueError("The requested file does not exist.")
        mime_type = mimetypes.guess_type(path.name)[0] or ""
        if mime_type.startswith("image/"):
            data = path.read_bytes()
            if len(data) > 12_000_000:
                raise ValueError("The image is too large to inspect.")
            attachments.append((data, mime_type))
            return {
                "ok": True, "path": path.relative_to(root).as_posix(),
                "mime_type": mime_type, "bytes": len(data), "pixels_attached": True,
            }, attachments
        if path.suffix.lower() not in _VERTEX_TEXT_SUFFIXES:
            raise ValueError("This file type is not readable by the drafting agent.")
        if path.stat().st_size > 2_000_000:
            raise ValueError("The text file is too large to read in one agent tool call.")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(arguments.get("start_line") or 1))
        count = max(1, min(int(arguments.get("line_count") or 500), 2000))
        selected = lines[start - 1:start - 1 + count]
        return {
            "ok": True, "path": path.relative_to(root).as_posix(),
            "start_line": start, "end_line": start + len(selected) - 1,
            "total_lines": len(lines), "content": "\n".join(selected),
            "truncated": start - 1 + len(selected) < len(lines),
        }, attachments

    if name == "grep_files":
        needle = str(arguments.get("pattern") or "").strip().casefold()
        if not needle or len(needle) > 500:
            raise ValueError("The search string must contain 1 to 500 characters.")
        limit = max(1, min(int(arguments.get("max_results") or 100), 200))
        matches = []
        for path in _safe_glob(root, arguments.get("file_glob") or "**/*"):
            if not path.is_file() or path.suffix.lower() not in _VERTEX_TEXT_SUFFIXES:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_no, line in enumerate(lines, 1):
                if needle in line.casefold():
                    matches.append({
                        "path": path.resolve().relative_to(root).as_posix(),
                        "line": line_no, "text": line[:1000],
                    })
                    if len(matches) >= limit:
                        return {"ok": True, "matches": matches, "truncated": True}, attachments
        return {"ok": True, "matches": matches, "truncated": False}, attachments

    if name in {"write_file", "replace_text"}:
        if not writable:
            raise ValueError("This review run has no write permission.")
        path = _workspace_path(root, arguments.get("path"), write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == "write_file":
            content = str(arguments.get("content") or "")
            if len(content) > 400_000:
                raise ValueError("The file exceeds the workspace file limit.")
            path.write_text(content, encoding="utf-8")
            return {
                "ok": True, "path": path.relative_to(root).as_posix(),
                "characters": len(content),
            }, attachments
        if not path.is_file():
            raise ValueError("The file to edit does not exist.")
        old = str(arguments.get("old_text") or "")
        new = str(arguments.get("new_text") or "")
        if not old:
            raise ValueError("old_text must not be empty.")
        text = path.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if not occurrences:
            raise ValueError("old_text was not found exactly in the file.")
        replace_all = bool(arguments.get("replace_all"))
        if occurrences > 1 and not replace_all:
            raise ValueError("old_text occurs more than once; make it unique or set replace_all.")
        changed = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        if len(changed) > 400_000:
            raise ValueError("The edited file exceeds the workspace file limit.")
        path.write_text(changed, encoding="utf-8")
        return {
            "ok": True, "path": path.relative_to(root).as_posix(),
            "replacements": occurrences if replace_all else 1,
        }, attachments

    if name == "patent_lookup":
        raw_arguments = arguments.get("arguments")
        if not isinstance(raw_arguments, list) or not raw_arguments:
            raise ValueError("arguments must be a non-empty list.")
        values = [str(item).strip() for item in raw_arguments[:41]]
        allowed_value = re.compile(r"(?:--claims|--check|-c|[A-Za-z0-9][A-Za-z0-9.\-/]{0,48})")
        if any(not allowed_value.fullmatch(item) for item in values):
            raise ValueError("The lookup contains an unsupported argument.")
        tool = _workspace_path(root, "tools/patent_lookup.py")
        completed = subprocess.run(
            [sys.executable, str(tool), *values], cwd=str(root), capture_output=True,
            text=True, timeout=90, check=False)
        output = ((completed.stdout or "") + (completed.stderr or ""))[:120_000]
        return {
            "ok": completed.returncode == 0, "exit_code": completed.returncode,
            "output": output,
        }, attachments

    raise ValueError(f"Unsupported tool: {name}")


def _vertex_generate(client, *, model: str, contents, config, deadline: float,
                     cancel: threading.Event | None):
    """Call Vertex with bounded retries for errors that are safe to repeat."""
    last_error: Exception | None = None
    for attempt in range(3):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Stopped at your request.")
        if time.monotonic() >= deadline:
            raise TimeoutError("The Vertex drafting fallback reached its time limit.")
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:
            last_error = exc
            if attempt >= 2 or not _transient_provider_error(str(exc)):
                raise
            delay = 2 * (attempt + 1)
            if not _wait_for_rate_limit_retry(delay, cancel=cancel):
                raise InterruptedError("Stopped at your request.") from exc
    raise RuntimeError(str(last_error or "Vertex request failed."))


def _run_vertex_once(*, workspace: Path, prompt: str, system_prompt: str,
                     schema: Mapping[str, Any], session_id: str = "", resume: bool = False,
                     model: str = "", tools: str = _DRAFT_TOOLS,
                     timeout: int = DRAFT_TIMEOUT, transcript: Path | None = None,
                     allowed_bash: Sequence[str] = (LOOKUP_COMMAND,),
                     on_event: Callable[[Mapping[str, Any]], None] | None = None,
                     cancel: threading.Event | None = None,
                     max_budget_usd: float = MAX_BUDGET_USD) -> AgentRun:
    """Run a bounded Vertex file-agent when both Claude credential routes are exhausted."""
    del model, max_budget_usd
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise AgentError(f"Draft workspace {workspace} does not exist.")
    from google.genai import types

    vertex_model = VERTEX_AGENT_MODEL
    run_session = session_id or new_session_id()
    out = AgentRun(
        session_id=run_session, model=f"vertex/{vertex_model}",
        transcript_path=str(transcript or ""))
    started = time.time()
    deadline = time.monotonic() + max(1, int(timeout))
    handle = None
    if transcript:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        handle = transcript.open("a", encoding="utf-8")

    def emit(event: Mapping[str, Any]) -> None:
        if handle:
            handle.write(json.dumps(dict(event), ensure_ascii=False) + "\n")
            handle.flush()
        if on_event:
            try:
                on_event(event)
            except Exception:                                  # noqa: BLE001
                return

    allowed = {item.strip() for item in str(tools or "").split(",") if item.strip()}
    writable = bool(allowed & {"Write", "Edit"})
    declarations = _vertex_tool_declarations(
        types, schema=schema, tools=tools, allowed_bash=allowed_bash)
    tool_names = {str(item.name) for item in declarations}
    fallback_instruction = (
        "\n\nVERTEX FALLBACK EXECUTION\n"
        "The prior provider is unavailable. The workspace is the complete durable state, so read "
        "the required files again even if this is described as a resumed turn. Use only the "
        "declared tools. Paths are workspace-relative. Do not look for or follow AGENTS.md, "
        "CLAUDE.md, user settings, plugins, hooks, skills, MCP servers, or instructions outside "
        "this workspace. Do not run shell commands. Finish by calling submit_result exactly once "
        "with the required structured answer. Never put filing text in submit_result; filing text "
        "must be written to draft/ and figures/."
    )
    contents = [types.Content(
        role="user", parts=[types.Part.from_text(text=(
            ("This is a continuation from the complete saved workspace.\n\n" if resume else "")
            + prompt))])]
    config = types.GenerateContentConfig(
        system_instruction=system_prompt + fallback_instruction,
        temperature=0.1,
        max_output_tokens=32768,
        tools=[types.Tool(function_declarations=declarations)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_budget=8192),
        http_options=types.HttpOptions(timeout=VERTEX_CALL_TIMEOUT_MS),
    )
    acquired = False
    tool_calls = 0
    quiet_rounds = 0
    try:
        while not acquired:
            if cancel is not None and cancel.is_set():
                out.cancelled = True
                out.error = "Stopped at your request."
                return out
            if time.monotonic() >= deadline:
                out.error = "The Vertex drafting fallback reached its time limit."
                return out
            acquired = _VERTEX_AGENT_LANE.acquire(timeout=1)

        client = _vertex_client()
        for round_index in range(VERTEX_AGENT_ROUNDS):
            if cancel is not None and cancel.is_set():
                out.cancelled = True
                out.error = "Stopped at your request."
                break
            if time.monotonic() >= deadline:
                out.error = "The Vertex drafting fallback reached its time limit."
                break
            try:
                response = _vertex_generate(
                    client, model=vertex_model, contents=contents, config=config,
                    deadline=deadline, cancel=cancel)
            except InterruptedError as exc:
                out.cancelled = True
                out.error = str(exc)
                break
            except Exception as exc:                            # noqa: BLE001
                out.error = f"Vertex drafting fallback failed: {type(exc).__name__}: {exc}"[:2000]
                break

            out.num_turns += 1
            usage = getattr(response, "usage_metadata", None)
            prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
            cached_tokens = int(
                getattr(usage, "cached_content_token_count", 0) or 0) if usage else 0
            output_tokens = int(
                getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
            out.tokens["input"] += max(0, prompt_tokens - cached_tokens)
            out.tokens["cache_read"] += cached_tokens
            out.tokens["output"] += output_tokens

            candidates = list(getattr(response, "candidates", None) or ())
            content = getattr(candidates[0], "content", None) if candidates else None
            parts = list(getattr(content, "parts", None) or ()) if content else []
            if parts:
                # Vertex accepts only user/model history roles. Normalize provider candidates and
                # never replay an empty candidate, which otherwise poisons the next tool round.
                contents.append(types.Content(role="model", parts=parts))
            calls = [getattr(part, "function_call", None) for part in parts]
            calls = [call for call in calls if call is not None]
            text_parts = [str(getattr(part, "text", "") or "").strip() for part in parts]
            response_text = "\n".join(item for item in text_parts if item)
            if response_text:
                out.steps.append({"kind": "say", "text": response_text[:4000]})
                emit({"type": "vertex_text", "round": round_index + 1,
                      "text": response_text[:4000]})

            if not calls:
                parsed = _parse_result(response_text)
                problem = _schema_problem(parsed, schema) if parsed is not None else ""
                if parsed is not None and not problem:
                    out.result = parsed
                    out.text = json.dumps(parsed, ensure_ascii=False)
                    out.ok = True
                    break
                quiet_rounds += 1
                if quiet_rounds >= 3:
                    out.error = ("The Vertex drafting fallback finished without returning the "
                                 "required structured answer.")
                    break
                contents.append(types.Content(
                    role="user", parts=[types.Part.from_text(text=(
                        "Continue the task. When every required check or edit is complete, call "
                        "submit_result with the exact structured answer. Do not answer in prose."))]))
                continue

            quiet_rounds = 0
            response_parts = []
            submitted = None
            submit_problem = ""
            for call in calls:
                name = str(getattr(call, "name", "") or "")
                arguments = dict(getattr(call, "args", None) or {})
                tool_calls += 1
                if tool_calls > VERTEX_AGENT_TOOL_CALLS:
                    out.error = "The Vertex drafting fallback exceeded its tool-call limit."
                    break
                if name == "submit_result":
                    submit_problem = _schema_problem(arguments, schema)
                    if not submit_problem:
                        submitted = arguments
                        out.steps.append({"kind": "tool", "tool": name,
                                          "detail": "structured result"})
                        emit({"type": "vertex_tool", "name": name, "ok": True})
                        continue
                    result = {"ok": False, "error": submit_problem}
                elif name not in tool_names:
                    result = {"ok": False, "error": f"Unsupported tool: {name}"}
                else:
                    try:
                        result, attachments = _vertex_tool(
                            workspace, name, arguments, writable=writable)
                    except Exception as exc:                    # noqa: BLE001
                        result, attachments = {
                            "ok": False, "error": f"{type(exc).__name__}: {exc}"[:1200],
                        }, []
                    detail = str(arguments.get("path") or arguments.get("pattern") or name)[:240]
                    out.steps.append({"kind": "tool", "tool": name, "detail": detail})
                    emit({"type": "vertex_tool", "name": name,
                          "detail": detail, "ok": bool(result.get("ok"))})
                    response_parts.append(types.Part.from_function_response(
                        name=name, response=dict(result)))
                    for data, mime_type in attachments:
                        response_parts.append(types.Part.from_bytes(
                            data=data, mime_type=mime_type))
                    continue
                response_parts.append(types.Part.from_function_response(
                    name=name, response=dict(result)))
            if out.error:
                break
            if submitted is not None:
                out.result = dict(submitted)
                out.text = json.dumps(out.result, ensure_ascii=False)
                out.ok = True
                break
            if response_parts:
                contents.append(types.Content(role="user", parts=response_parts))
            elif submit_problem:
                contents.append(types.Content(
                    role="user", parts=[types.Part.from_text(text=(
                        "submit_result did not match the required schema: " + submit_problem))]))
        else:
            out.error = "The Vertex drafting fallback exceeded its model-round limit."
    finally:
        if acquired:
            _VERTEX_AGENT_LANE.release()
        if handle:
            handle.close()
        out.duration_ms = int((time.time() - started) * 1000)
    return out


def _with_vertex_fallback(common: Mapping[str, Any], previous: AgentRun) -> AgentRun:
    if (not VERTEX_FALLBACK or previous.ok or previous.cancelled or
            not (_provider_quota_error(previous.error) or
                 _credential_dead_error(previous.error))):
        return previous
    state = _SPEND.status()
    if not state.degraded and not state.allowed:
        _SPEND.log(f"refused the Vertex fallback: ${state.spent_usd:.2f} of ${state.cap_usd:.2f} "
                   f"spent today across {state.calls} calls")
        return _merge_attempts(
            previous, _spend_refusal(state),
            "Every Claude subscription was unavailable and the metered fallback is at its "
            "daily ceiling, so nothing further was spent.")
    vertex = _run_vertex_once(**common)
    _SPEND.record(usd=vertex.cost_usd, model=VERTEX_AGENT_MODEL, route="vertex",
                  detail=f"turns={vertex.num_turns}")
    return _merge_attempts(
        previous, vertex,
        "Every Claude subscription on this host was unavailable, so the run continued through "
        "the isolated Vertex drafting agent from the complete saved workspace.")


def _subscription_limit_error(error: str) -> bool:
    return _provider_quota_error(error)


def _credential_dead_error(error: str) -> bool:
    """A credential this host can no longer present at all, as opposed to one out of quota.

    Both are reasons to move to the next subscription and neither is a reason to fail the turn,
    but they are not the same thing and the report must not call one the other. This exists
    because a mirrored rotating credential is revoked the moment its own host refreshes it, and a
    fallback that answers 401 was otherwise ending turns that had a working rung left.
    """
    text = str(error or "").lower()
    return bool(
        "has been revoked" in text or
        "failed to authenticate" in text or
        ("401" in text and ("oauth" in text or "token" in text or "authenticat" in text)))


def _rate_limit_error(error: str) -> bool:
    text = str(error or "").lower()
    return bool(
        re.search(r"(?:^|\D)429(?:\D|$)", text) or
        "rate limit" in text or
        "tokens per minute" in text or
        "too many requests" in text)


def _transient_provider_error(error: str) -> bool:
    """Recognize provider failures that are safe to repeat with the same workspace."""
    text = str(error or "").lower()
    return bool(
        _rate_limit_error(text) or
        re.search(r"(?:^|\D)(?:500|502|503|504|529)(?:\D|$)", text) or
        any(phrase in text for phrase in (
            "overloaded", "service unavailable", "temporarily unavailable",
            "server-side issue", "internal server error", "connection lost",
            "connection reset", "connection closed", "unexpected eof", "broken pipe",
            "network error", "no response parts", "image_recitation")))


def _wait_for_rate_limit_retry(seconds: int,
                               cancel: threading.Event | None = None) -> bool:
    """Wait through a provider window while still honoring a user cancellation."""
    deadline = time.monotonic() + max(0, int(seconds))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        interval = min(1.0, remaining)
        if cancel is not None:
            if cancel.wait(interval):
                return False
        else:
            time.sleep(interval)


def _missing_session_error(error: str) -> bool:
    text = str(error or "").lower()
    return "session" in text and any(
        phrase in text for phrase in ("not found", "no conversation", "does not exist"))


def _merge_attempts(previous: AgentRun, current: AgentRun, message: str) -> AgentRun:
    current.cost_usd += previous.cost_usd
    current.duration_ms += previous.duration_ms
    current.num_turns += previous.num_turns
    for key in ("input", "output", "cache_read", "cache_write"):
        current.tokens[key] = current.tokens.get(key, 0) + previous.tokens.get(key, 0)
    current.steps = previous.steps + [{"kind": "system", "text": message}] + current.steps
    return current


def _spend_refusal(state: Any) -> AgentRun:
    """The run the guard returns instead of a metered call it will not pay for.

    Vertex is now the ONLY route on this host that spends money, so this is the ceiling on it.
    Unlike the subscription cascade above it, there is nothing further to fall through to, so this
    is a plain failure: the turn keeps its draft and says what it would have cost.
    """
    out = AgentRun()
    out.error = (
        f"Refused by the local spend guard: {SPEND_APP} has spent ${state.spent_usd:.2f} of its "
        f"${state.cap_usd:.2f} daily metered limit (UTC). Raise it deliberately with: "
        f"llm-spend override {SPEND_APP} --usd N --hours N --reason '...'")
    return out


def _run_once_booked(common: Mapping[str, Any], *, token: str = "",
                      **overrides: Any) -> AgentRun:
    """One CLI run. A subscription run costs no money, so it is booked for VISIBILITY only.

    The cost the CLI reports for a subscription run is what the same work would have cost at list
    price. Recording it is what makes "a week of the plan in a few hours" a number somebody can
    see on the page before the week is gone, instead of afterwards.
    """
    run = _run_once(**{**common, **overrides}, token=token)
    _SPEND.record(usd=0.0, model=str(common.get("model") or DRAFT_MODEL),
                  route="subscription",
                  detail=f"list=${run.cost_usd:.2f} turns={run.num_turns} "
                         f"session={run.session_id[:12]}")
    return run


def _run_with_rate_limit_retries(common: Mapping[str, Any], *, token: str = "") -> AgentRun:
    """Retry transient provider failures without weakening the session boundary."""
    current = _run_once_booked(common, token=token)
    for retry_index in range(RATE_LIMIT_RETRIES):
        cancel = common.get("cancel")
        if (current.ok or current.cancelled or not _transient_provider_error(current.error) or
                (cancel is not None and cancel.is_set())):
            return current
        delay = RATE_LIMIT_RETRY_SECONDS * (retry_index + 1)
        if not _wait_for_rate_limit_retry(delay, cancel=cancel):
            current.cancelled = True
            current.error = "Stopped at your request."
            return current
        retry_session = current.session_id or str(common.get("session_id") or "")
        retry_resume = bool(common.get("resume") and retry_session)
        if not retry_resume:
            retry_session = new_session_id()
        retried = _run_once_booked(
            common, token=token, session_id=retry_session, resume=retry_resume)
        current = _merge_attempts(
            current, retried,
            f"The provider returned a temporary error or rate limit, so the run waited "
            f"{delay} seconds and retried automatically.")
    return current


def run(*, workspace: Path, prompt: str, system_prompt: str, schema: Mapping[str, Any],
        session_id: str = "", resume: bool = False, model: str = "", tools: str = _DRAFT_TOOLS,
        timeout: int = DRAFT_TIMEOUT, transcript: Path | None = None,
        allowed_bash: Sequence[str] = (LOOKUP_COMMAND,),
        on_event: Callable[[Mapping[str, Any]], None] | None = None,
        cancel: threading.Event | None = None,
        max_budget_usd: float = MAX_BUDGET_USD) -> AgentRun:
    """Run on a subscription. If it is capped, try the NEXT subscription, and never a bill.

    On 2026-08-26 at 20:33 UTC the subscription here reported "You have hit your weekly limit,
    resets Aug 31" and this function did exactly what it had been configured to do: it moved every
    subsequent run onto the ANTHROPIC_API_KEY sitting in .env, each one carrying more than 600,000
    tokens of resumed context, and nothing in the product said so. That route is gone. What
    remains is a list of subscriptions tried in order, and then Vertex, which has its own daily
    ceiling and is the only thing on this host that can still spend money.
    """
    common = {
        "workspace": workspace, "prompt": prompt, "system_prompt": system_prompt,
        "schema": schema, "session_id": session_id, "resume": resume, "model": model,
        "tools": tools, "timeout": timeout, "transcript": transcript,
        "allowed_bash": allowed_bash, "on_event": on_event, "cancel": cancel,
        "max_budget_usd": max_budget_usd,
    }
    tokens = subscription_tokens()
    if not tokens:
        raise AgentUnavailable(
            "No Claude subscription is configured on this host. Metered API billing is "
            "deliberately not a fallback: configure DRAFT_AGENT_TOKEN_FILES.")

    merged: AgentRun | None = None
    for index, token in enumerate(tokens):
        #  A session opened under one credential is not resumable under another, so every
        #  subscription after the first starts fresh from the complete saved workspace.
        attempt_common = dict(common)
        if index:
            attempt_common.update(session_id=new_session_id(), resume=False)
        current = _run_with_rate_limit_retries(attempt_common, token=token)
        if (attempt_common["resume"] and not current.ok and not current.cancelled and
                _missing_session_error(current.error) and
                (cancel is None or not cancel.is_set())):
            fresh = _run_with_rate_limit_retries(
                {**attempt_common, "session_id": new_session_id(), "resume": False}, token=token)
            current = _merge_attempts(
                current, fresh,
                "The prior conversation session was unavailable, so the run continued from the "
                "complete workspace in a fresh session.")

        if merged is None:
            merged = current
        else:
            reason = ("could not be presented at all (the credential is dead or revoked)"
                      if _credential_dead_error(merged.error) else "was out of quota")
            merged = _merge_attempts(
                merged, current,
                f"Subscription {index} {reason}, so the run continued on subscription "
                f"{index + 1} of {len(tokens)} from the complete saved workspace.")

        if merged.ok or merged.cancelled or (cancel is not None and cancel.is_set()):
            return merged
        if not (_subscription_limit_error(merged.error) or
                _credential_dead_error(merged.error)):
            #  A malformed answer or a bad workspace is not a reason to spend another account's
            #  weekly quota on exactly the same work.
            return _with_vertex_fallback(common, merged)

    assert merged is not None
    ending = ("could not be presented" if _credential_dead_error(merged.error)
              else "is out of quota")
    merged.steps = merged.steps + [{
        "kind": "system",
        "text": (f"Every configured subscription ({len(tokens)}) {ending}. Metered API billing "
                 f"is deliberately not a fallback, so nothing was billed. The draft is saved and "
                 f"the turn can be run again when a plan window reopens.")}]
    return _with_vertex_fallback(common, merged)


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
