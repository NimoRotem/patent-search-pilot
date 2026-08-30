"""One interactive Claude Code agent per draft, in its own tmux session.

WHY A TERMINAL AND NOT A CHAT BOX
---------------------------------
Drafting an application is file work: narrow claim 1, then check that claim 7 still has antecedent
basis, then fix the numeral table, then re-read the background.  A chat box that sends one prompt
and waits for one answer cannot do that, and the headless ``claude -p`` turn that used to sit
behind it could only ever finish or fail - you could not watch it, steer it mid-thought, or ask it
what it was about to do.  So the drafting agent is now the same thing the operators use all day: a
real Claude Code session, running in a real terminal, with the draft as its working directory.

ONE SESSION PER DRAFT, AND NOTHING SHARED
-----------------------------------------
Every project gets:

  * its own tmux session ``draft-p<id>`` on a PRIVATE tmux server (``-L iptorch-drafts``), so a
    drafting agent never appears in, is never killed by, and can never attach to the operator's
    own ``tmux ls``;
  * its own ``HOME`` and ``CLAUDE_CONFIG_DIR`` under the workspace, so it starts with **blank
    memory**: no auto-memory store, no skills, no plugins, no MCP servers, no other project's
    history, and nothing it writes can reach the operator's home;
  * its own ``CLAUDE.md`` in the workspace root, which is the only standing instruction it has.

The workspace itself is outside ``$HOME`` (see ``draft_workspace``), so the CLAUDE.md walk-up
finds ours and stops.

AUTHENTICATION
--------------
A Claude subscription, not a metered API key.  The long-lived ``sk-ant-oat01-`` setup token is
written into the session's own ``.credentials.json`` with a far-future expiry and an EMPTY refresh
token, which is what makes it safe to run any number of these at once: a setup token has no
refresh state, so there is no rotating credential for two sessions to race (the failure mode that
revokes both).  It is written to the file rather than exported as ``CLAUDE_CODE_OAUTH_TOKEN``
because the environment path makes every session brand itself "Claude API" in its own splash and
``/status``, which reads like metered billing when it is not.

HOW THE AGENT'S WORK REACHES THE PAGE
-------------------------------------
It edits ``draft/*.md`` and then runs ``python3 tools/publish.py``.  That posts to this app over
the loopback interface with a per-project token, the server reads the workspace exactly as the old
turn worker did, and stores a new version.  The page's poller sees ``latest_version_no`` move and
re-renders.  The contract is written out in full in the CLAUDE.md below, because an instruction
the agent cannot find is an instruction that does not exist.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

import draft_agent
import draft_workspace

# A private tmux server. `tmux -L <socket>` is a whole separate server process with its own
# session namespace: the drafting agents cannot collide with, be listed by, or be killed by
# anything the operator does in their own tmux.
TMUX_SOCKET = os.environ.get("DRAFT_TMUX_SOCKET", "iptorch-drafts")
SESSION_PREFIX = "draft-p"

# The pane geometry the agent renders into. tmux sizes a detached session to this and never
# resizes it, so the CLI's own wrapping is stable and the browser's line-rejoining is predictable.
PANE_COLS = int(os.environ.get("DRAFT_TERMINAL_COLS", "132"))
PANE_ROWS = int(os.environ.get("DRAFT_TERMINAL_ROWS", "42"))

# The model a new drafting session starts on. The user switches it from the chip beside Reload,
# which types /model into the pane, exactly as the operators' dashboard does.
DEFAULT_MODEL = os.environ.get("DRAFT_TERMINAL_MODEL", "claude-opus-5")
DEFAULT_EFFORT = os.environ.get("DRAFT_TERMINAL_EFFORT", "high")

MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("claude-opus-5", "Opus 5"),
    ("claude-opus-5[1m]", "Opus 5 - 1M"),
    ("claude-fable-5", "Fable 5"),
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-haiku-4-5", "Haiku 4.5"),
)
EFFORT_CHOICES: tuple[tuple[str, str], ...] = (
    ("low", "Low"), ("medium", "Medium"), ("high", "High"),
    ("xhigh", "xHigh"), ("max", "Max"),
)
MODEL_IDS = frozenset(item[0] for item in MODEL_CHOICES)
EFFORT_IDS = frozenset(item[0] for item in EFFORT_CHOICES)

# Where the subscription token comes from. Its OWN file, deliberately: the drafting agents run on
# the builder4 subscription (ai@nemopowertools.com), and falling back to the box's shared
# `~/.claude/oauth_token` would silently bill a different account and put drafting usage in
# somebody else's ceiling. No token here means no drafting agent, which is a state the page can
# report; the wrong account is a state nobody notices.
TOKEN_FILES = tuple(path for path in (
    os.environ.get("DRAFT_TERMINAL_TOKEN_FILE", ""),
    str(Path.home() / ".claude/oauth_token.drafting"),
    "/home/nimrod_rotem/.claude/oauth_token.drafting",
) if path)

# One year past the epoch of any deploy that will ever run this build. The token itself is what
# expires; this number only has to stop the CLI trying to refresh a credential that has no
# refresh token to refresh with.
_FAR_FUTURE_MS = 1_900_000_000_000

_LOCK = threading.Lock()
_TOKEN_CACHE: tuple[float, str] | None = None
_STABILITY: dict[str, tuple[str, float]] = {}


class TerminalError(RuntimeError):
    """The drafting agent's terminal could not be started or reached."""


# =============================================================================================
# tmux
# =============================================================================================
def _tmux(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", "-L", TMUX_SOCKET, *args],
                          capture_output=True, text=True, timeout=timeout)


def session_name(project_id: int) -> str:
    return f"{SESSION_PREFIX}{int(project_id)}"


def _target(project_id: int) -> str:
    """An EXACT-match target that also resolves to the session's one pane.

    ``=name`` alone is a session target and tmux refuses it wherever a pane is wanted
    ("can't find pane"); the trailing colon says "this session's current window", which is where
    the only pane lives. Getting this wrong is silent: send-keys returns non-zero, nothing is
    typed, and the terminal simply sits at a shell prompt for ever.
    """
    return f"={session_name(project_id)}:"


def exists(project_id: int) -> bool:
    try:
        return _tmux("has-session", "-t", _target(project_id), timeout=5).returncode == 0
    except Exception:                                          # noqa: BLE001 - tmux is optional
        return False


def _display(project_id: int, fmt: str) -> str:
    try:
        out = _tmux("display-message", "-t", _target(project_id), "-p", fmt, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:                                          # noqa: BLE001
        return ""


def pane_width(project_id: int) -> int:
    value = _display(project_id, "#{pane_width}")
    try:
        return int(value) or PANE_COLS
    except ValueError:
        return PANE_COLS


def pane_position(project_id: int) -> int:
    """Total scrollback lines, so the client can ask for a delta instead of the whole buffer."""
    value = _display(project_id, "#{history_size}:#{pane_height}")
    try:
        history, height = value.split(":")
        return int(history) + int(height)
    except (ValueError, AttributeError):
        return 0


def capture_full(project_id: int) -> str:
    try:
        out = _tmux("capture-pane", "-t", _target(project_id), "-p", "-J", "-S", "-",
                    timeout=15)
        return out.stdout if out.returncode == 0 else ""
    except Exception:                                          # noqa: BLE001
        return ""


def capture_recent(project_id: int, lines: int = 80) -> str:
    try:
        out = _tmux("capture-pane", "-t", _target(project_id), "-p", "-J",
                    "-S", f"-{max(1, int(lines))}", timeout=10)
        return out.stdout if out.returncode == 0 else ""
    except Exception:                                          # noqa: BLE001
        return ""


def visible_hash(project_id: int) -> str:
    """Fingerprint of the VISIBLE pane.

    Claude Code repaints its TUI in place, so scrollback length can sit still while the screen
    changes completely. Without this the page would show a frozen terminal during a long turn.
    """
    try:
        out = _tmux("capture-pane", "-t", _target(project_id), "-p", timeout=5)
        if out.returncode == 0:
            return hashlib.md5(out.stdout.encode("utf-8", "replace")).hexdigest()
    except Exception:                                          # noqa: BLE001
        pass
    return ""


# =============================================================================================
# The credential and the private agent home
# =============================================================================================
def subscription_token() -> str:
    """The long-lived ``sk-ant-oat01-`` setup token, cached briefly.

    Read from disk rather than the process environment: the web tier starts under supervisor with
    a deliberately small environment, and a token replaced on disk should be picked up without a
    restart. Only a setup token is accepted - a rotating access token copied here would be
    refreshed by whichever session got there first and revoked for all the others.
    """
    global _TOKEN_CACHE
    with _LOCK:
        if _TOKEN_CACHE and time.time() - _TOKEN_CACHE[0] < 300:
            return _TOKEN_CACHE[1]
        token = os.environ.get("DRAFT_TERMINAL_TOKEN", "").strip()
        if not token:
            for candidate in TOKEN_FILES:
                try:
                    value = Path(candidate).read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if value.startswith("sk-ant-oat01-"):
                    token = value
                    break
        _TOKEN_CACHE = (time.time(), token)
        return token


def agent_home(workspace: Path) -> Path:
    """This draft's private HOME. Fresh, empty, and never the operator's."""
    return Path(workspace) / ".agent-home"


def _write_credentials(home: Path) -> bool:
    token = subscription_token()
    if not token:
        return False
    config = home / ".claude"
    config.mkdir(parents=True, exist_ok=True)
    payload = {"claudeAiOauth": {
        "accessToken": token,
        # EMPTY on purpose. A refresh token here is what starts the rotation war: two sessions
        # refresh the same credential, the second one presents a superseded token, and the server
        # revokes it. A setup token needs no refresh, so there is nothing to race.
        "refreshToken": "",
        "expiresAt": _FAR_FUTURE_MS,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max"}}
    path = config / ".credentials.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return True


def _write_claude_config(home: Path, workspace: Path) -> None:
    """Answer, in advance, every first-run question nobody is there to answer.

    The trust dialog is the one that matters: without ``hasTrustDialogAccepted`` for this exact
    directory the CLI opens on "Do you trust the files in this folder?" and waits, so the page
    shows a terminal that looks alive and will never do anything. Keyed on the workspace path
    because that is what the CLI keys it on.
    """
    path = home / ".claude" / ".claude.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}
    existing.setdefault("hasCompletedOnboarding", True)
    existing.setdefault("bypassPermissionsModeAccepted", True)
    existing.setdefault("numStartups", 1)
    existing.setdefault("theme", "dark")
    projects = existing.setdefault("projects", {})
    if not isinstance(projects, dict):
        projects = existing["projects"] = {}
    entry = projects.setdefault(str(workspace), {})
    if not isinstance(entry, dict):
        entry = projects[str(workspace)] = {}
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("hasClaudeMdExternalIncludesApproved", True)
    entry.setdefault("hasClaudeMdExternalIncludesWarningShown", True)
    entry.setdefault("allowedTools", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    path.chmod(0o600)


def _write_settings(home: Path) -> None:
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "includeCoAuthoredBy": False,
        "env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
    }, indent=2), encoding="utf-8")


def publish_credentials(workspace: Path) -> dict[str, str]:
    """The loopback URL and token the workspace's publish tool posts with.

    Kept in the agent's own home rather than in the tree it edits, so a careless ``rm -rf draft``
    cannot take the agent's ability to publish with it.
    """
    path = agent_home(workspace) / "publish.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(stored, dict) and stored.get("token"):
            return {"url": str(stored.get("url") or ""), "token": str(stored["token"])}
    except (OSError, ValueError):
        pass
    return {}


def _ensure_publish_credentials(workspace: Path, project_id: int) -> dict[str, str]:
    existing = publish_credentials(workspace)
    port = os.environ.get("WEBAPP_PORT", "8631")
    url = f"http://127.0.0.1:{port}/api/drafts/{int(project_id)}/workspace/publish"
    token = existing.get("token") or secrets.token_urlsafe(32)
    home = agent_home(workspace)
    home.mkdir(parents=True, exist_ok=True)
    path = home / "publish.json"
    path.write_text(json.dumps({"url": url, "token": token, "project_id": int(project_id)}),
                    encoding="utf-8")
    path.chmod(0o600)
    return {"url": url, "token": token}


def verify_publish_token(workspace: Path, token: str) -> bool:
    stored = publish_credentials(workspace).get("token") or ""
    if not stored or not token:
        return False
    return secrets.compare_digest(stored, str(token))


# =============================================================================================
# What the agent is told, and the one command that puts its work on the page
# =============================================================================================
_CLAUDE_MD = '''# You are the drafting agent for this patent application

This directory **is** the application. Everything in it was put here by the drafting product; the
person you are talking to is the inventor or their attorney, reading your work on a web page while
you type.

## The one rule that matters

**Editing `draft/` changes nothing anyone can see. Running `python3 tools/publish.py` is what puts
your work on the page.** Until you run it, the person watching still sees the previous version.
Publish when you have finished a coherent change - not after every single edit, and never at the
end of a turn you have left half-done.

```
python3 tools/publish.py                       # publish the current draft/ tree
python3 tools/publish.py -m "narrowed claim 1"  # ...with a note for the History tab
python3 tools/publish.py --check                # validate without publishing
```

It prints the new version number, or the exact reason it refused. A refusal is a real defect in
the files - fix it and run it again rather than working around it.

## The layout

| Path | What it is |
| --- | --- |
| `input/disclosure.md` | The inventor's own words. **The only authority for what the invention is.** |
| `input/brief.md` | Working title, applicant, named inventors, filing notes. |
| `input/materials/` | Documents the user uploaded that are not prior art. |
| `prior_art/INDEX.md` | Every reference you may cite, and the exact citation key for each. |
| `prior_art/<PUB>.md` | One file per reference: abstract, claims, and why the search returned it. |
| `draft/01-title.md` … `draft/10-abstract.md` | **The application.** One file per section, body text only, no heading line. |
| `draft/numerals.md` | The reference-numeral table. One row per part. |
| `figures/` | One Markdown file per sheet: what it shows and which numerals appear on it. Sheets the user has uploaded sit beside them as `rendered-*.png`, and you can open those. |
| `review/previous-qa.md` | What the reviewer found last time. Fix all of it. |
| `tools/` | `publish.py` (above), `figure_check.py` and `patent_lookup.py` (below). |

Write the section files as **body text only**. `draft/09-claims.md` holds the numbered claims and
nothing else. Do not create files in `draft/` other than the ten sections and `numerals.md`:
publish deletes them and refuses the run.

**A figure file is named after its own heading.** Open it with `# FIG. 3 - enlarged section
through the magnet array` and publish will place it at
`figures/FIG-3-ENLARGED-SECTION-THROUGH-THE-MAGNET-ARRAY.md` - the heading, uppercased, every run
of non-alphanumerics turned into a single hyphen, cut at 60 characters. You do not have to compute
that: write the heading, name the file whatever is convenient, and publish renames it. Two files
with the same heading is a real conflict and one of them is dropped.

## Drawings

**You do not draw.** There is no image generation in this product any more. The user uploads
finished sheets from the Drawings tab, and any sheet they have uploaded is sitting in `figures/`
as a PNG you can open and read.

**The sheet is the authority and the text is what moves.** You own the drawing *text*:
`figures/FIG-N.md`, the Brief Description of the Drawings (`draft/07-drawings.md`), the figure
cross-references in the detailed description, and the numeral table. When a sheet arrives, open
it, read what is on it, and change the text to match it. Not the other way round.

```
python3 tools/figure_check.py        # what is on every sheet, and where the text disagrees
```

That prints, per sheet, the views it carries, every reference numeral and what its lead line
lands on, any words printed on the drawing, and then the specific places where the specification
and the sheets contradict each other. Run it after every upload and before every publish.

Six things it looks for, because each one reached a real filing:

- **A view with no number.** A magnified circle, a second arrangement beside the first, anything
  with the word OR between it and its neighbour: that is a separate view under 37 CFR 1.84(u).
  Give it its own number, `FIG. 2A`, and its own sentence in the Brief Description.
- **A claimed feature that no sheet shows.** 37 CFR 1.83(a). If the claims recite a port, the
  port has a numeral and appears in a view. Read your own independent claims against the sheets.
- **A cross-reference pointing at the wrong view.** "As shown in FIG. 3" in a paragraph about a
  part only FIG. 2 shows. These appear every time a figure is split or renumbered.
- **A numeral the drawing and the text disagree about.** The check reads the sheet against your
  numeral table and says which numerals do not agree with it.
- **Hedging.** A drawing description says what a numbered view *is*, in the present tense.
  Nothing in a specification "may illustrate" anything, and no sentence describes a view that has
  no number.
- **A numeral on a sheet that the description never mentions**, or the reverse.

If the application needs a view the user has not supplied, describe what is needed and say
plainly in your reply that the sheet is still missing. Never write a description of a view that
does not exist.

## Citing prior art

Cite only what is in `prior_art/INDEX.md`, only with its key, and only in the form `[REF:US-1234567-B2]`.
Never invent a citation and never describe a reference beyond what its own file says. To check what
a publication actually contains before you write a limitation around it:

```
python3 tools/patent_lookup.py US-9108319-B2 --claims
python3 tools/patent_lookup.py --check US-1111111-A1 EP-2222222-A1
```

## How the person reads your work

The page beside this terminal has tabs. **Draft** shows the published sections. **Review** shows
the mechanical checks and the reviewer's findings. **Sources** shows the prior art and the uploads.
**History** shows every version with the note you passed to `publish.py`. All of them update by
themselves once you publish; none of them can see an unpublished edit.

So: work in the files, keep your replies here short and specific about what you changed and why,
and publish.

## One housekeeping fact

This session is closed after several hours with nothing happening in it, because it holds real
memory on a shared machine. Nothing published is affected and the workspace stays where it is; a
new agent opens on the next message. It does mean an edit you never published is an edit you lose,
which is the other reason to publish a change once it is coherent rather than at the very end.
'''

_PUBLISH_TOOL = '''#!/usr/bin/env python3
"""Put the current draft/ tree on the page.

    python3 tools/publish.py                        publish
    python3 tools/publish.py -m "narrowed claim 1"  publish with a change note
    python3 tools/publish.py --check                validate only, publish nothing

Prints the new version number, or the reason it refused. Standard library only, so it runs under
whatever python3 this box happens to have.
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDENTIALS = os.path.join(HERE, ".agent-home", "publish.json")


def main(argv):
    note, check = "", False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-m", "--message", "--note"):
            index += 1
            note = argv[index] if index < len(argv) else ""
        elif arg in ("--check", "-n", "--dry-run"):
            check = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            note = (note + " " + arg).strip()
        index += 1

    try:
        with open(CREDENTIALS, "r", encoding="utf-8") as handle:
            credentials = json.load(handle)
    except (OSError, ValueError) as exc:
        print("Cannot publish: %s is unreadable (%s)." % (CREDENTIALS, exc))
        return 2

    body = json.dumps({"note": note[:400], "check": bool(check)}).encode("utf-8")
    request = urllib.request.Request(
        credentials["url"], data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Draft-Agent-Token": credentials["token"]})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error") or detail
        except ValueError:
            pass
        print("REFUSED (%s): %s" % (exc.code, detail[:2000]))
        return 1
    except Exception as exc:                                    # noqa: BLE001 - report, do not trace
        print("Cannot reach the drafting server: %s: %s" % (type(exc).__name__, exc))
        return 1

    for problem in payload.get("problems") or []:
        print("  - %s" % problem)
    if payload.get("checked"):
        print("Checked only, nothing published. %s" % (payload.get("message") or ""))
        return 0 if payload.get("ok") else 1
    if not payload.get("ok"):
        print("REFUSED: %s" % (payload.get("error") or "unknown"))
        return 1
    print("Published version %s. The page will show it within a few seconds."
          % payload.get("version_no"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


_FIGURE_CHECK_TOOL = '''#!/usr/bin/env python3
"""Read the uploaded drawing sheets and say where the specification disagrees with them.

    python3 tools/figure_check.py            what is on every sheet, and every disagreement
    python3 tools/figure_check.py --json     the same thing as JSON

The sheets are inspected once each and the reading is cached on the image itself, so running this
again after a text edit costs nothing. Standard library only.
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDENTIALS = os.path.join(HERE, ".agent-home", "publish.json")


def main(argv):
    want_json = "--json" in argv
    try:
        with open(CREDENTIALS, "r", encoding="utf-8") as handle:
            credentials = json.load(handle)
    except (OSError, ValueError) as exc:
        print("Cannot reach the drawing check: %s is unreadable (%s)." % (CREDENTIALS, exc))
        return 2
    url = credentials["url"].replace("/workspace/publish", "/workspace/figures")
    request = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"Content-Type": "application/json",
                 "X-Draft-Agent-Token": credentials["token"]})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print("REFUSED (%s): %s" % (exc.code, detail[:1000]))
        return 1
    except Exception as exc:                                    # noqa: BLE001
        print("Cannot reach the drafting server: %s: %s" % (type(exc).__name__, exc))
        return 1

    if want_json:
        print(json.dumps(payload, indent=1))
        return 0
    if not payload.get("ok"):
        print("REFUSED: %s" % (payload.get("error") or "unknown"))
        return 1
    report = payload.get("report") or {}
    sheets = report.get("sheets") or []
    if not sheets:
        print("No drawing sheet has been uploaded to this draft yet.")
    for sheet in sheets:
        print("=" * 78)
        print("SHEET %s" % (sheet.get("label") or "(unlabelled upload)"))
        for view in sheet.get("views") or []:
            print("  view %-10s %-22s numerals: %s"
                  % (view.get("legend") or "?", (view.get("kind") or "")[:22],
                     ", ".join(view.get("numerals") or []) or "none"))
        for view in sheet.get("unnumbered_views") or []:
            print("  VIEW WITH NO NUMBER: %s  numerals: %s"
                  % (view.get("looks_like") or "a separate picture",
                     ", ".join(view.get("numerals") or []) or "none"))
        for item in sheet.get("numerals") or []:
            flag = "" if item.get("agrees_with_the_table") in ("yes", "unclear", "not_declared") \
                else "   <-- does not agree with the numeral table"
            lines = "" if int(item.get("lead_lines") or 1) < 2 \
                else "  [%s lead lines]" % item.get("lead_lines")
            print("    %-6s %s%s%s" % (item.get("value"), (item.get("points_at") or "")[:70],
                                       lines, flag))
        words = sheet.get("words_printed_on_the_sheet") or []
        if words:
            print("  words printed on the sheet: %s" % "; ".join(w for w in words if w)[:300])
        if sheet.get("reference_numeral_key_printed"):
            print("  a reference numeral key is printed on this sheet")
        if sheet.get("divider_rules"):
            print("  %s divider rule(s) drawn between views" % sheet.get("divider_rules"))

    findings = report.get("findings") or []
    print()
    print("=" * 78)
    print("%d disagreement(s) between the sheets and the text. Verdict: %s"
          % (len(findings), report.get("verdict")))
    print("=" * 78)
    for finding in findings:
        print()
        print("[%s] %s" % (finding.get("severity", "").upper(), finding.get("title")))
        print("  rule: %s   in: %s" % (finding.get("rule"), finding.get("where")))
        if finding.get("detail"):
            print("  %s" % finding["detail"])
    if not findings:
        print("\\nNothing. The specification and the sheets agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def install(workspace: Path, project_id: int) -> dict[str, Any]:
    """Lay down everything the agent needs that is not the draft itself.

    Idempotent: called on every session start, because a workspace is a cache that can be rebuilt
    from Postgres at any time and must come back complete.
    """
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "CLAUDE.md").write_text(_CLAUDE_MD, encoding="utf-8")
    tools = workspace / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "publish.py").write_text(_PUBLISH_TOOL, encoding="utf-8")
    (tools / "publish.py").chmod(0o755)
    (tools / "figure_check.py").write_text(_FIGURE_CHECK_TOOL, encoding="utf-8")
    (tools / "figure_check.py").chmod(0o755)

    home = agent_home(workspace)
    home.mkdir(parents=True, exist_ok=True)
    authenticated = _write_credentials(home)
    _write_claude_config(home, workspace)
    _write_settings(home)
    credentials = _ensure_publish_credentials(workspace, project_id)
    return {"authenticated": authenticated, "publish_url": credentials["url"]}


# =============================================================================================
# Availability
# =============================================================================================
def availability() -> dict[str, Any]:
    """Whether a drafting terminal can be started here, and if not, precisely why."""
    if not shutil.which("tmux"):
        return {"ok": False, "reason": "tmux is not installed on this server."}
    binary = draft_agent.binary()
    if not binary:
        return {"ok": False, "reason": "The Claude Code CLI is not installed on this server."}
    if not subscription_token():
        return {"ok": False, "binary": binary,
                "reason": "No Claude subscription token is configured for the drafting agent."}
    return {"ok": True, "reason": "", "binary": binary, "version": draft_agent.version(binary),
            "models": [{"id": mid, "label": label} for mid, label in MODEL_CHOICES],
            "efforts": [{"id": eid, "label": label} for eid, label in EFFORT_CHOICES],
            "default_model": DEFAULT_MODEL, "default_effort": DEFAULT_EFFORT}


def normalize_model(value: Any) -> str:
    name = str(value or "").strip()
    return name if name in MODEL_IDS else ""


def normalize_effort(value: Any) -> str:
    name = str(value or "").strip().lower()
    return name if name in EFFORT_IDS else ""


# =============================================================================================
# Lifecycle
# =============================================================================================
def _claude_running(project_id: int) -> bool:
    command = _display(project_id, "#{pane_current_command}").lower()
    return command in ("claude", "node", "claude.exe")


def _launch(project_id: int, workspace: Path, *, model: str) -> None:
    """Type the launch line into the pane, exactly as a person would."""
    binary = draft_agent.binary()
    if not binary:
        raise TerminalError("The Claude Code CLI is not installed on this server.")
    launch = " ".join([
        _shell_quote(binary),
        "--dangerously-skip-permissions",
        "--model", _shell_quote(model or DEFAULT_MODEL),
        "--session-id", _shell_quote(str(uuid.uuid4())),
    ])
    target = _target(project_id)
    # C-u first: if anything was already half-typed at the prompt, the launch line would be
    # appended to it and the shell would run neither.
    _tmux("send-keys", "-t", target, "C-u")
    _tmux("send-keys", "-t", target, "-l", launch)
    _tmux("send-keys", "-t", target, "Enter")
    threading.Thread(target=_clear_trust_prompt, args=(project_id,),
                     name=f"draft-trust-{project_id}", daemon=True).start()


_TRUST_PROMPT = "Yes, I trust this folder"


def _clear_trust_prompt(project_id: int, *, attempts: int = 12) -> bool:
    """Answer the folder-trust dialog if it appears anyway.

    ``.claude.json`` is seeded so it should not, but a config that gets wiped or a CLI that moves
    the key would otherwise leave the session parked on a question for ever, looking alive. The
    dialog opens with "No, exit" selected, so the answer is Down then Enter.
    """
    for _ in range(max(1, attempts)):
        time.sleep(1.0)
        if not exists(project_id):
            return False
        visible = capture_recent(project_id, 40)
        if _TRUST_PROMPT not in visible:
            if _claude_running(project_id) and "❯" in visible:
                return False                       # up and at its composer: nothing to answer
            continue
        target = _target(project_id)
        _tmux("send-keys", "-t", target, "Down")
        time.sleep(0.4)
        _tmux("send-keys", "-t", target, "Enter")
        return True
    return False


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def ensure(project_id: int, workspace: Path, *, model: str = "",
           effort: str = "") -> dict[str, Any]:
    """Make sure this draft has a live agent, and return what the page needs to render it."""
    available = availability()
    if not available.get("ok"):
        raise TerminalError(available["reason"])
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise TerminalError("The draft workspace has not been built yet.")
    installed = install(workspace, project_id)
    if not installed["authenticated"]:
        raise TerminalError("No Claude subscription token is configured for the drafting agent.")

    home = agent_home(workspace)
    created = False
    if not exists(project_id):
        #  Set BEFORE the session exists. `history-limit` is a session option that is read when a
        #  pane is created, so setting it afterwards leaves that pane on the 2000-line default and
        #  silently truncates the scrollback of a long drafting session. Global on this socket,
        #  which is private to the drafting agents.
        _tmux("set-option", "-g", "history-limit", "20000")
        #  Without this the CLI prints a line into the pane telling the reader to edit
        #  ~/.tmux.conf, which is advice for somebody sitting at a terminal, not for somebody
        #  reading a patent draft in a browser. Our server, so we simply turn it on.
        _tmux("set-option", "-g", "focus-events", "on")
        command = [
            "new-session", "-d", "-s", session_name(project_id), "-c", str(workspace),
            "-x", str(PANE_COLS), "-y", str(PANE_ROWS),
            # A brand-new environment rather than whatever the web tier happens to hold. HOME is
            # the workspace's own, so the agent starts with no memory, no skills, no plugins and
            # no way to write into the operator's home.
            "-e", f"HOME={home}",
            "-e", f"CLAUDE_CONFIG_DIR={home / '.claude'}",
            "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            "-e", "TERM=xterm-256color",
            "-e", "PATH=/usr/local/bin:/usr/bin:/bin",
            # A stale key in the service environment would be preferred over the subscription and
            # bill an account we did not intend.
            "-e", "ANTHROPIC_API_KEY=",
            "-e", "CLAUDE_CODE_OAUTH_TOKEN=",
        ]
        result = _tmux(*command)
        if result.returncode != 0:
            raise TerminalError(result.stderr.strip() or "tmux refused to create the session.")
        created = True

    if created or not _claude_running(project_id):
        _launch(project_id, workspace, model=normalize_model(model) or DEFAULT_MODEL)
        if normalize_effort(effort):
            threading.Timer(12.0, lambda: _quiet(set_effort, project_id,
                                                 normalize_effort(effort))).start()
    return state(project_id, workspace)


def _quiet(fn, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:                                          # noqa: BLE001 - best effort only
        pass


def restart(project_id: int, workspace: Path, *, model: str = "") -> dict[str, Any]:
    """Kill the agent and start a clean one, keeping the draft and the workspace."""
    kill(project_id)
    return ensure(project_id, workspace, model=model)


def reset(project_id: int, workspace: Path, *, model: str = "") -> dict[str, Any]:
    """A brand-new agent with brand-new memory: the private home is deleted with the session."""
    kill(project_id)
    shutil.rmtree(agent_home(Path(workspace)), ignore_errors=True)
    return ensure(project_id, workspace, model=model)


# =============================================================================================
# Not leaving them running
# =============================================================================================
#  AN IDLE AGENT IS 350 MB THAT NOBODY IS USING. One per draft, never exiting on its own, on a box
#  the house rules say livelocks silently when memory runs out: twenty parked sessions is seven
#  gigabytes held for conversations that ended hours ago. So a session whose composer has been
#  empty and whose screen has not moved for this long is closed. Nothing is lost that was
#  published, the workspace stays where it is, and the next message opens a new agent.
IDLE_REAP_SECONDS = max(
    600, int(float(os.environ.get("DRAFT_TERMINAL_IDLE_HOURS", "6")) * 3600))
REAP_SWEEP_SECONDS = max(60, int(os.environ.get("DRAFT_TERMINAL_REAP_SWEEP", "600")))
_IDLE_SINCE: dict[str, tuple[str, float]] = {}
_REAPER: threading.Thread | None = None


def sessions() -> list[str]:
    try:
        out = _tmux("list-sessions", "-F", "#{session_name}", timeout=5)
        if out.returncode != 0:
            return []
    except Exception:                                          # noqa: BLE001
        return []
    return [line.strip() for line in out.stdout.splitlines()
            if line.strip().startswith(SESSION_PREFIX)]


def reap_idle(max_idle_seconds: float | None = None) -> list[str]:
    """Close every drafting agent that has been sitting still with nothing to do.

    BUSY IS NOT IDLE and a still screen is not enough on its own: a turn that is thinking paints a
    ticking counter, and a turn waiting on a slow tool call may paint nothing at all for a minute.
    So a session is only a candidate when ``activity`` reports it idle AND its visible screen is
    byte-identical to the one seen a whole sweep ago.
    """
    #  `is None`, not `or`: a caller passing 0 means "anything still since the last sweep", and
    #  falling back to the six-hour default there would make every test of this pass by not
    #  reaping at all.
    limit = IDLE_REAP_SECONDS if max_idle_seconds is None else float(max_idle_seconds)
    closed = []
    now = time.time()
    live = set()
    for name in sessions():
        live.add(name)
        try:
            project_id = int(name[len(SESSION_PREFIX):])
        except ValueError:
            continue
        if activity(project_id).get("status") == "busy":
            _IDLE_SINCE.pop(name, None)
            continue
        digest = visible_hash(project_id)
        previous = _IDLE_SINCE.get(name)
        if not previous or previous[0] != digest:
            _IDLE_SINCE[name] = (digest, now)
            continue
        if now - previous[1] >= limit:
            kill(project_id)
            _IDLE_SINCE.pop(name, None)
            closed.append(name)
    for gone in set(_IDLE_SINCE) - live:
        _IDLE_SINCE.pop(gone, None)
    return closed


def start_reaper() -> None:
    """One sweeper for the process. Never raises: a reaper that dies takes nothing with it."""
    global _REAPER
    with _LOCK:
        if _REAPER is not None and _REAPER.is_alive():
            return

        def loop() -> None:
            while True:
                time.sleep(REAP_SWEEP_SECONDS)
                try:
                    reap_idle()
                except Exception:                              # noqa: BLE001
                    pass

        _REAPER = threading.Thread(target=loop, name="draft-terminal-reaper", daemon=True)
        _REAPER.start()


def kill(project_id: int) -> bool:
    if not exists(project_id):
        return False
    _tmux("kill-session", "-t", _target(project_id))
    _STABILITY.pop(session_name(project_id), None)
    return True


# =============================================================================================
# Typing into it
# =============================================================================================
def send(project_id: int, text: str) -> bool:
    """Send a message the way a person types it, including multi-line pastes.

    ``send-keys -l`` delivers a literal newline, which the CLI submits on - so a multi-line
    message would be truncated at its first line. Anything long or multi-line goes through a
    paste buffer instead, with bracketed paste turned off first so the CLI does not swallow the
    Enter behind a "[Pasted text]" preview.
    """
    if not exists(project_id):
        raise TerminalError("The drafting agent is not running.")
    body = str(text or "")
    if not body.strip():
        return False
    target = _target(project_id)
    if len(body) > 200 or "\n" in body:
        _tmux("send-keys", "-t", target, "-H",
              "1b", "5b", "3f", "32", "30", "30", "34", "6c")          # ESC [ ? 2 0 0 4 l
        time.sleep(0.15)
        _tmux("set-buffer", "-b", f"draft{int(project_id)}", "--", body)
        _tmux("paste-buffer", "-b", f"draft{int(project_id)}", "-t", target)
        _tmux("delete-buffer", "-b", f"draft{int(project_id)}")
        time.sleep(max(0.8, min(5.0, len(body) / 1500)))
        _tmux("send-keys", "-t", target, "Enter")
        time.sleep(0.4)
        tail = capture_recent(project_id, 6)
        if "Pasted text" in tail or "[Pasted" in tail:
            _tmux("send-keys", "-t", target, "Enter")
    else:
        _tmux("send-keys", "-t", target, "-l", body)
        _tmux("send-keys", "-t", target, "Enter")
    return True


ALLOWED_KEYS = frozenset({
    "Escape", "Enter", "Space", "Tab", "BSpace", "Up", "Down", "Left", "Right",
    "C-c", "C-d", "C-l", "C-u", "PageUp", "PageDown", "Home", "End",
})


def send_keys(project_id: int, keys: Sequence[str]) -> list[str]:
    if not exists(project_id):
        raise TerminalError("The drafting agent is not running.")
    target = _target(project_id)
    sent = []
    for key in list(keys)[:12]:
        key = str(key)
        if key not in ALLOWED_KEYS and not (len(key) == 1 and key.isprintable()):
            raise TerminalError(f"That key is not allowed: {key[:20]}")
        _tmux("send-keys", "-t", target, key)
        sent.append(key)
    return sent


def interrupt(project_id: int) -> bool:
    if not exists(project_id):
        return False
    _tmux("send-keys", "-t", _target(project_id), "Escape")
    return True


def _slash(project_id: int, command: str) -> None:
    """Run one of the CLI's own slash commands by typing it, the way a person would.

    Three deliberate steps. ``C-u`` clears whatever is in the composer, because a slash command
    appended to a half-typed message runs neither. The wait is for the CLI's autocomplete popup:
    it opens as soon as ``/`` is typed and it takes the Enter for itself, so pressing Enter too
    early selects a completion instead of submitting the line. And the second wait lets the
    command land before the next one is typed - two of these back to back is how a `/effort`
    silently did nothing while its `/model` was still rendering.
    """
    target = _target(project_id)
    _tmux("send-keys", "-t", target, "C-u")
    _tmux("send-keys", "-t", target, "-l", command)
    time.sleep(0.9)
    _tmux("send-keys", "-t", target, "Enter")
    time.sleep(0.6)


#  The CLI asks before a change it cannot undo cheaply - raising the effort level re-reads the
#  whole conversation, so it wants a yes. The list opens with the affirmative selected.
_CONFIRM_OPTION_RE = re.compile(r"^\s*[❯›>]\s*1\.\s+\S")


def _confirm_if_prompted(project_id: int, *, attempts: int = 4) -> bool:
    """Answer a numbered confirmation the CLI put up, and only that.

    Never a blind Enter: an Enter into an empty composer sends an empty message, and an Enter into
    a composer somebody is typing into sends THEIR half-written message. The pane has to be
    showing a choice list with its first option selected before this touches anything.
    """
    for _ in range(max(1, attempts)):
        time.sleep(0.6)
        visible = capture_recent(project_id, 24)
        if any(_CONFIRM_OPTION_RE.match(line) for line in visible.splitlines()):
            _tmux("send-keys", "-t", _target(project_id), "Enter")
            time.sleep(0.5)
            return True
    return False


def set_model(project_id: int, model: str) -> str:
    name = normalize_model(model)
    if not name:
        raise TerminalError("That is not a model this server will run.")
    if not exists(project_id) or not _claude_running(project_id):
        raise TerminalError("The drafting agent is not running - restart it first.")
    _slash(project_id, "/model " + name)
    _confirm_if_prompted(project_id)
    time.sleep(1.2)
    tail = capture_recent(project_id, 10).lower()
    if "unknown model" in tail or "invalid model" in tail or "not a valid model" in tail:
        raise TerminalError(f"Claude Code rejected the model id '{name}'.")
    if "not available for your account" in tail and name.endswith("[1m]"):
        base = name[: -len("[1m]")]
        _slash(project_id, "/model " + base)
        time.sleep(1.2)
        return base
    return name


def set_effort(project_id: int, effort: str) -> str:
    level = normalize_effort(effort)
    if not level:
        raise TerminalError("That is not a reasoning effort this server will run.")
    if not exists(project_id) or not _claude_running(project_id):
        raise TerminalError("The drafting agent is not running - restart it first.")
    _slash(project_id, "/effort " + level)
    #  Raising the effort invalidates the conversation cache, so the CLI asks first and parks the
    #  pane on the question. Without this the switch reports success and the agent sits waiting
    #  for an answer nobody is there to give.
    _confirm_if_prompted(project_id)
    return level


# =============================================================================================
# What it is doing right now
# =============================================================================================
_RE_SPINNER = re.compile(
    r"[✻✽✳✢·✶✻*⚒⚙◐◓◑◒⣾⣽⣻⢿⡿⣟⣯⣷]\s+\w+(?:…|\.{2,3})")
_RE_RUNNING_TASK = re.compile(r"^[⎿\s]*◼")
_RE_IDLE_PROMPT = re.compile(r"^[❯➜]\s*$")
_RE_SHELL_PROMPT = re.compile(r"[$#]\s*$")


def activity(project_id: int) -> dict[str, str]:
    """busy / idle / stopped, read the same way the operators' dashboard reads it.

    The strongest signal is the CLI's own "esc to interrupt"; a spinner is next; a bare composer
    with neither is idle. A pane whose content has not moved for twenty seconds is idle whatever
    the text looks like, which catches the cases the patterns miss.
    """
    if not exists(project_id):
        return {"status": "stopped", "detail": "No drafting agent is running."}
    command = _display(project_id, "#{pane_current_command}").lower()
    try:
        out = _tmux("capture-pane", "-t", _target(project_id), "-p", timeout=5)
        visible = out.stdout if out.returncode == 0 else ""
    except Exception:                                          # noqa: BLE001
        visible = ""
    lines = visible.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    bottom = "\n".join(lines[-6:])
    window = lines[-25:]

    if "esc to interrupt" in bottom:
        return {"status": "busy", "detail": "Working"}
    for line in window:
        stripped = line.strip()
        if _RE_RUNNING_TASK.match(stripped):
            return {"status": "busy", "detail": "Running a task"}
        if _RE_SPINNER.search(stripped):
            return {"status": "busy",
                    "detail": "Thinking" if "thinking" in stripped.lower() else "Working"}

    digest = hashlib.md5(visible.encode("utf-8", "replace")).hexdigest()
    key = session_name(project_id)
    previous = _STABILITY.get(key)
    if previous and previous[0] == digest:
        static_for = time.time() - previous[1]
    else:
        _STABILITY[key] = (digest, time.time())
        static_for = 0.0
    if command in ("claude", "node", "claude.exe"):
        if any(_RE_IDLE_PROMPT.match(line.strip()) for line in lines[-6:]) or static_for >= 20:
            return {"status": "idle", "detail": ""}
        return {"status": "busy", "detail": "Working"}
    if command in ("bash", "sh", "zsh"):
        return {"status": "stopped", "detail": "The agent exited; Restart brings it back."}
    return {"status": "busy", "detail": command}


def state(project_id: int, workspace: Path | None = None) -> dict[str, Any]:
    """Everything the page needs about the session, without capturing its contents."""
    live = exists(project_id)
    out: dict[str, Any] = {
        "session": session_name(project_id),
        "exists": live,
        "running": live and _claude_running(project_id),
        "pane_width": pane_width(project_id) if live else PANE_COLS,
        "pane_total": pane_position(project_id) if live else 0,
    }
    out.update(activity(project_id) if live
               else {"status": "stopped", "detail": "No drafting agent is running."})
    if workspace is not None:
        out["workspace"] = str(workspace)
    return out


def tail(project_id: int, *, known_lines: int = 0, last_hash: str = "") -> dict[str, Any]:
    """The delta read behind the terminal's one-second poll.

    Three answers: a full capture (first load, or the scrollback moved backwards), a delta with a
    small overlap the client uses to splice, or nothing at all. A TUI repaint moves no scrollback,
    so a changed visible hash forces a full capture; without that the pane would look frozen for
    the whole of a long turn.
    """
    if not exists(project_id):
        return {"mode": "none", "exists": False, "total_lines": 0, "pane_total": 0,
                "pane_width": PANE_COLS, "visible_hash": ""}
    total = pane_position(project_id)
    digest = visible_hash(project_id)
    width = pane_width(project_id)
    base = {"exists": True, "pane_total": total, "pane_width": width, "visible_hash": digest}

    if known_lines <= 0 or known_lines > total:
        raw = capture_full(project_id)
        return {**base, "mode": "full", "raw": raw, "total_lines": len(raw.split("\n"))}
    if total <= known_lines:
        if last_hash and digest and last_hash != digest:
            raw = capture_full(project_id)
            return {**base, "mode": "full", "raw": raw, "total_lines": len(raw.split("\n"))}
        return {**base, "mode": "none", "total_lines": known_lines}
    overlap = 5
    raw = capture_recent(project_id, (total - known_lines) + overlap)
    return {**base, "mode": "delta", "raw": raw, "total_lines": total, "overlap": overlap}


# =============================================================================================
# The workspace the agent works in
# =============================================================================================
def sync_figure_images(workspace: Path, project_id: int, user_id: int) -> int:
    """Put every uploaded sheet in the workspace, so the agent can actually look at it.

    ``rendered-<LABEL>.png`` is the filename the workspace already reserves for a drawing's
    pixels, so this does not widen what ``draft_workspace`` will tolerate in ``figures/``.
    """
    directory = Path(workspace) / "figures"
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("rendered-*.png"):
        stale.unlink(missing_ok=True)
    written = 0
    try:
        import draft_figures
        for item in draft_figures.listing(int(project_id), int(user_id)):
            _mime, data = draft_figures.png_bytes(int(item["id"]), int(user_id))
            if not data:
                continue
            slug = re.sub(r"[^A-Za-z0-9]+", "-",
                          str(item.get("figure_label") or "")).strip("-").upper()
            if not slug:
                slug = f"FIG-{item['id']}"
            (directory / f"rendered-{slug}.png").write_bytes(data)
            written += 1
    except Exception:                                          # noqa: BLE001 - never break a launch
        pass
    return written


def section_files(sections: Mapping[str, str]) -> dict[str, str]:
    """The section body of each draft file, for a caller that wants to write them itself."""
    return {name: str(sections.get(key) or "")
            for key, name, _heading in draft_workspace.SECTION_FILES}
