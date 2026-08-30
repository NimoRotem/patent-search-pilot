"""The drafting agent's terminal: its isolation, its credential, and its way back to the page.

Almost none of this needs tmux or a Claude binary, and the parts that do are exercised through a
recorded fake, because what actually breaks here is never "does tmux work". It is the four things
below, each of which failed silently the first time:

  * a tmux target of the wrong SHAPE. ``-t =name`` is a session target and tmux refuses it
    wherever a pane is wanted; send-keys returns non-zero, nothing is typed, and the page shows a
    terminal sitting at a shell prompt for ever with no error anywhere;
  * an unanswered first-run dialog. Without ``hasTrustDialogAccepted`` for that exact directory
    the CLI opens on "Do you trust the files in this folder?" and waits;
  * a credential with refresh state in it, which is what revokes two agents at once;
  * a CLAUDE.md that does not say how to publish, which makes every edit invisible to the person
    watching.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import draft_terminal  # noqa: E402


TOKEN = "sk-ant-oat01-" + "x" * 40


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_terminal, "_TOKEN_CACHE", None)
    monkeypatch.setenv("DRAFT_TERMINAL_TOKEN", TOKEN)
    monkeypatch.setenv("WEBAPP_PORT", "8631")
    directory = tmp_path / "p15"
    directory.mkdir()
    return directory


# =============================================================================================
# The tmux target, which is the thing that fails without saying so
# =============================================================================================
def test_a_pane_target_carries_the_session_and_the_window():
    """``=name`` alone is refused wherever a pane is wanted; the trailing colon is what fixes it.

    Worth its own test because the failure is silent: `capture-pane -t =draft-p15` prints
    "can't find pane" to a stderr nobody reads, `send-keys` types nothing, and every symptom
    downstream looks like the agent being slow to start.
    """
    assert draft_terminal.session_name(15) == "draft-p15"
    assert draft_terminal._target(15) == "=draft-p15:"
    assert draft_terminal._target(15).startswith("=")     # exact match, never a prefix
    assert draft_terminal._target(15).endswith(":")       # ...and resolves to its one pane


def test_every_tmux_call_is_on_the_private_server(monkeypatch):
    """The drafting agents live on their own tmux server and cannot touch the operator's."""
    seen = []

    class Result:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(draft_terminal.subprocess, "run",
                        lambda argv, **_kwargs: seen.append(argv) or Result())
    draft_terminal.exists(15)
    draft_terminal.capture_recent(15, 10)
    draft_terminal.interrupt(15)
    assert seen, "nothing ran"
    for argv in seen:
        assert argv[:3] == ["tmux", "-L", draft_terminal.TMUX_SOCKET]


# =============================================================================================
# What a new draft's agent is given
# =============================================================================================
def test_install_writes_the_instructions_the_tools_and_the_credential(workspace):
    draft_terminal.install(workspace, 15)

    claude_md = (workspace / "CLAUDE.md").read_text(encoding="utf-8")
    #  THE contract. An agent that edits draft/ and never publishes has done nothing anybody can
    #  see, so the command has to be in its standing instructions, not in a prompt it may not get.
    assert "python3 tools/publish.py" in claude_md
    assert "draft/" in claude_md
    assert "prior_art/INDEX.md" in claude_md
    #  And it has to be told that it does not draw, or it will try.
    assert "You do not draw" in claude_md
    assert "figures/" in claude_md

    assert (workspace / "tools" / "publish.py").exists()
    assert (workspace / "tools" / "publish.py").stat().st_mode & 0o111


def test_the_publish_tool_imports_nothing_that_needs_installing(workspace):
    """It runs under whatever python3 the agent's shell finds, which is the system one.

    The workspace has no virtualenv and the agent has no way to make one. A single
    ``import requests`` here would make publishing fail on a box where the app itself works
    perfectly, and the failure would read as "the server refused my draft".
    """
    draft_terminal.install(workspace, 15)
    source = (workspace / "tools" / "publish.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported


def test_the_agent_home_is_private_and_starts_empty(workspace):
    draft_terminal.install(workspace, 15)
    home = draft_terminal.agent_home(workspace)

    assert home == workspace / ".agent-home"
    #  Nothing that would give this agent another project's memory, another box's skills, or the
    #  operator's own instructions.
    assert not (home / ".claude" / "projects").exists()
    assert not (home / ".claude" / "CLAUDE.md").exists()
    assert not (home / ".claude" / "plugins").exists()
    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["includeCoAuthoredBy"] is False


def test_the_trust_dialog_is_answered_before_it_is_asked(workspace):
    """Keyed on the workspace path, because that is what the CLI keys it on.

    Without it the session opens on a question, waits for an answer nobody is there to give, and
    reports itself perfectly healthy while doing nothing at all.
    """
    draft_terminal.install(workspace, 15)
    config = json.loads(
        (draft_terminal.agent_home(workspace) / ".claude" / ".claude.json").read_text("utf-8"))
    assert config["hasCompletedOnboarding"] is True
    assert config["projects"][str(workspace)]["hasTrustDialogAccepted"] is True


def test_the_credential_has_no_refresh_state(workspace):
    """A setup token, written whole, with nothing that can rotate.

    This is the difference between running one drafting agent and running twenty. A rotating
    credential copied into several sessions is refreshed by whichever reaches its expiry first;
    the next one to present the superseded token has it revoked, and both die. A setup token has
    no refresh state at all, so there is nothing to race.
    """
    draft_terminal.install(workspace, 15)
    stored = json.loads((draft_terminal.agent_home(workspace) / ".claude" /
                         ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]
    assert stored["accessToken"] == TOKEN
    assert stored["refreshToken"] == ""
    assert stored["expiresAt"] > 1_800_000_000_000
    assert stored["subscriptionType"] == "max"


def test_the_credential_file_is_not_world_readable(workspace):
    draft_terminal.install(workspace, 15)
    path = draft_terminal.agent_home(workspace) / ".claude" / ".credentials.json"
    assert path.stat().st_mode & 0o077 == 0


def test_the_drafting_token_is_never_taken_from_the_shared_file(monkeypatch):
    """Its OWN file, or nothing.

    The drafting agents run on a different subscription from the box's own sessions. Falling back
    to ~/.claude/oauth_token would bill somebody else's plan and put drafting usage inside their
    ceiling, and nothing in the product would ever say so. No token is a state the page reports;
    the wrong account is a state nobody notices.
    """
    assert not any(path.endswith("/.claude/oauth_token") for path in draft_terminal.TOKEN_FILES)
    assert any(path.endswith("oauth_token.drafting") for path in draft_terminal.TOKEN_FILES)


# =============================================================================================
# The way back to the page
# =============================================================================================
def test_the_publish_token_is_per_project_and_stable(workspace, tmp_path):
    draft_terminal.install(workspace, 15)
    first = draft_terminal.publish_credentials(workspace)
    assert first["token"]
    assert first["url"] == "http://127.0.0.1:8631/api/drafts/15/workspace/publish"

    #  Re-installing must not mint a new one: the tool the agent already has in front of it holds
    #  the old value, and rotating under it turns every publish into a 403.
    draft_terminal.install(workspace, 15)
    assert draft_terminal.publish_credentials(workspace)["token"] == first["token"]

    other = tmp_path / "p16"
    other.mkdir()
    draft_terminal.install(other, 16)
    assert draft_terminal.publish_credentials(other)["token"] != first["token"]


def test_verify_publish_token_rejects_everything_but_the_right_one(workspace):
    draft_terminal.install(workspace, 15)
    token = draft_terminal.publish_credentials(workspace)["token"]
    assert draft_terminal.verify_publish_token(workspace, token)
    assert not draft_terminal.verify_publish_token(workspace, token + "x")
    assert not draft_terminal.verify_publish_token(workspace, "")
    assert not draft_terminal.verify_publish_token(workspace, None)


# =============================================================================================
# Availability, which the page shows verbatim
# =============================================================================================
def test_availability_names_the_missing_piece(monkeypatch):
    monkeypatch.setattr(draft_terminal.shutil, "which", lambda _name: None)
    assert draft_terminal.availability() == {
        "ok": False, "reason": "tmux is not installed on this server."}

    monkeypatch.setattr(draft_terminal.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(draft_terminal.draft_agent, "binary", lambda: "")
    assert "Claude Code CLI is not installed" in draft_terminal.availability()["reason"]

    monkeypatch.setattr(draft_terminal.draft_agent, "binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(draft_terminal, "subscription_token", lambda: "")
    assert "subscription token" in draft_terminal.availability()["reason"]

    monkeypatch.setattr(draft_terminal, "subscription_token", lambda: TOKEN)
    monkeypatch.setattr(draft_terminal.draft_agent, "version", lambda _path="": "2.1.250")
    available = draft_terminal.availability()
    assert available["ok"] is True
    assert [item["id"] for item in available["models"]][0] == "claude-opus-5"
    assert [item["id"] for item in available["efforts"]] == [
        "low", "medium", "high", "xhigh", "max"]


def test_only_a_model_this_host_runs_reaches_the_command_line():
    assert draft_terminal.normalize_model("claude-opus-5") == "claude-opus-5"
    assert draft_terminal.normalize_model("; rm -rf /") == ""
    assert draft_terminal.normalize_model("gpt-4") == ""
    assert draft_terminal.normalize_effort("MAX") == "max"
    assert draft_terminal.normalize_effort("ludicrous") == ""


# =============================================================================================
# Reading the pane
# =============================================================================================
class FakeTmux:
    """Answers the handful of tmux calls the tail path makes, and records the rest."""

    def __init__(self, *, history=0, height=42, screen="", scrollback=""):
        self.history, self.height = history, height
        self.screen, self.scrollback = screen, scrollback
        self.calls = []

    def __call__(self, *args, **_kwargs):
        self.calls.append(list(args))
        verb = args[0]
        out = ""
        if verb == "has-session":
            return _Result(0, "")
        if verb == "display-message":
            fmt = args[-1]
            if fmt == "#{pane_width}":
                out = "132"
            elif fmt.startswith("#{history_size}"):
                out = f"{self.history}:{self.height}"
            else:
                out = "claude"
        elif verb == "capture-pane":
            out = self.scrollback if "-J" in args else self.screen
        return _Result(0, out)


class _Result:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


def test_tail_returns_a_full_capture_on_the_first_read(monkeypatch):
    fake = FakeTmux(history=8, scrollback="one\ntwo\nthree")
    monkeypatch.setattr(draft_terminal, "_tmux", fake)
    out = draft_terminal.tail(15, known_lines=0)
    assert out["mode"] == "full" and out["raw"] == "one\ntwo\nthree"
    assert out["pane_width"] == 132


def test_tail_reports_nothing_when_neither_the_scrollback_nor_the_screen_moved(monkeypatch):
    fake = FakeTmux(history=8, screen="same")
    monkeypatch.setattr(draft_terminal, "_tmux", fake)
    digest = draft_terminal.visible_hash(15)
    assert draft_terminal.tail(15, known_lines=50, last_hash=digest)["mode"] == "none"


def test_a_tui_repaint_forces_a_full_capture(monkeypatch):
    """The scrollback can sit still for a whole turn while the screen changes completely.

    Claude Code repaints its alternate screen in place, so "has the buffer grown" is not the
    question. Without the visible-screen hash the page would show a frozen terminal for minutes
    and then everything at once.
    """
    fake = FakeTmux(history=8, screen="now different", scrollback="a\nb")
    monkeypatch.setattr(draft_terminal, "_tmux", fake)
    out = draft_terminal.tail(15, known_lines=50, last_hash="a-hash-from-before")
    assert out["mode"] == "full"


def test_tail_asks_for_a_delta_with_an_overlap_to_splice_on(monkeypatch):
    fake = FakeTmux(history=100, scrollback="new lines")
    monkeypatch.setattr(draft_terminal, "_tmux", fake)
    out = draft_terminal.tail(15, known_lines=120)
    assert out["mode"] == "delta" and out["overlap"] == 5
    capture = next(call for call in fake.calls if call[0] == "capture-pane" and "-J" in call)
    #  (142 - 120) + 5: the rows that are new, plus the overlap the client checks before it
    #  splices. Without the overlap two unrelated screens get glued together on a resync.
    assert capture[capture.index("-S") + 1] == "-27"


def test_nothing_is_captured_from_a_session_that_is_not_there(monkeypatch):
    monkeypatch.setattr(draft_terminal, "exists", lambda _pid: False)
    out = draft_terminal.tail(15, known_lines=0)
    assert out["mode"] == "none" and out["exists"] is False


# =============================================================================================
# Typing into it
# =============================================================================================
def test_a_short_message_is_typed_and_submitted(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(draft_terminal, "_tmux", fake)
    draft_terminal.send(15, "narrow claim 1")
    typed = [call for call in fake.calls if call[0] == "send-keys"]
    assert typed[0][-2:] == ["-l", "narrow claim 1"]
    assert typed[1][-1] == "Enter"


def test_a_multi_line_message_goes_through_a_paste_buffer(monkeypatch):
    """``send-keys -l`` delivers a real newline, and the CLI submits on it.

    So a two-paragraph instruction would arrive as its first line, with the rest typed into the
    composer of the turn it just started. The paste buffer keeps it whole, and bracketed paste is
    turned off first so the CLI does not park it behind a "[Pasted text]" preview and swallow the
    Enter.
    """
    fake = FakeTmux()
    monkeypatch.setattr(draft_terminal, "_tmux", fake)
    monkeypatch.setattr(draft_terminal.time, "sleep", lambda _seconds: None)
    draft_terminal.send(15, "first line\nsecond line")
    verbs = [call[0] for call in fake.calls]
    assert "set-buffer" in verbs and "paste-buffer" in verbs
    assert "delete-buffer" in verbs           # the buffer is not left on the server
    assert not any(call[0] == "send-keys" and "-l" in call for call in fake.calls)


def test_an_empty_message_is_not_sent(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(draft_terminal, "_tmux", fake)
    assert draft_terminal.send(15, "   \n  ") is False
    assert not any(call[0] == "send-keys" for call in fake.calls)


def test_only_known_keys_reach_send_keys(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(draft_terminal, "_tmux", fake)
    assert draft_terminal.send_keys(15, ["Escape", "q"]) == ["Escape", "q"]
    with pytest.raises(draft_terminal.TerminalError):
        draft_terminal.send_keys(15, ["C-x; rm -rf /"])


# =============================================================================================
# What it is doing
# =============================================================================================
def test_esc_to_interrupt_is_the_strongest_busy_signal(monkeypatch):
    monkeypatch.setattr(draft_terminal, "exists", lambda _pid: True)
    monkeypatch.setattr(draft_terminal, "_display", lambda _pid, _fmt: "claude")
    monkeypatch.setattr(draft_terminal, "_tmux", FakeTmux(
        screen="\n".join(["Working on the claims", "", "  esc to interrupt"])))
    assert draft_terminal.activity(15)["status"] == "busy"


def test_a_bare_composer_is_idle(monkeypatch):
    monkeypatch.setattr(draft_terminal, "exists", lambda _pid: True)
    monkeypatch.setattr(draft_terminal, "_display", lambda _pid, _fmt: "claude")
    monkeypatch.setattr(draft_terminal, "_tmux", FakeTmux(screen="Done.\n\n❯ \n"))
    assert draft_terminal.activity(15)["status"] == "idle"


def test_a_pane_back_at_its_shell_is_stopped_not_idle(monkeypatch):
    """The difference the Restart button depends on.

    An agent that exited leaves a perfectly healthy tmux session sitting at a bash prompt. Calling
    that "idle" is how a person waits ten minutes for a reply from a process that is not running.
    """
    monkeypatch.setattr(draft_terminal, "exists", lambda _pid: True)
    monkeypatch.setattr(draft_terminal, "_display", lambda _pid, _fmt: "bash")
    monkeypatch.setattr(draft_terminal, "_tmux", FakeTmux(screen="nimo@box:~/p15$ "))
    state = draft_terminal.activity(15)
    assert state["status"] == "stopped"
    assert "Restart" in state["detail"]


def test_no_session_reads_as_stopped(monkeypatch):
    monkeypatch.setattr(draft_terminal, "exists", lambda _pid: False)
    assert draft_terminal.activity(15)["status"] == "stopped"
