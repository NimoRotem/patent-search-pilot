"""What this application has put through the models, added up while it happens.

WHY IT WAS NOT VISIBLE.  Every part of this product measured its own spend and none of them added
up.  A headless turn wrote its tokens onto the turn row (022).  The interactive drafting agent, now
the main way a draft gets written, wrote nothing anywhere: it is a Claude Code session in a tmux
pane, and the only record of what it spent is its own transcript.  On one real project that
transcript held 470 million cache-read tokens and 888 thousand output tokens for a single session,
which is $392 at published Opus rates, and no page in this product could tell you that.  The
drawing inspections and the filing reviewer were invisible in the same way.

WHAT THIS DOES.  Two ways in, one way out.

  SCAN      Claude Code writes an exact ``usage`` block on every assistant message of every
            session, in JSONL under the configuration directory it was given. Every agent this
            product runs has a private configuration directory under the draft's own workspace, so
            the transcripts are findable per project. A scan remembers how many bytes of each file
            it has counted and reads only what is new, which makes it cheap enough to run on the
            terminal's one-second poll and is what makes the number move while you watch.
  RECORD    Usage that never went through Claude Code - the Vertex drawing inspections - is
            reported by the code that spent it.

DOLLARS ARE A METERED EQUIVALENT.  The drafting agent runs on a subscription, so nothing here is
billed per token. The figure is what these tokens would cost at published API rates, which is the
only number that compares an agent turn with a drawing inspection, and the only one that tells you
when a session has started re-reading its whole context every round. Every surface that shows it
says so.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Iterable, Mapping

import db

#  Where a source's usage came from, in the order a person reads them.
SOURCES = (
    ("terminal", "Drafting agent"),
    ("turn", "Headless drafting and review"),
    ("review", "Consistency review"),
    ("research", "Re-search"),
    ("quick_art", "Quick prior-art pass"),
    ("figures", "Drawing inspection"),
    ("filing_qa", "Filing review"),
)
SOURCE_LABELS = dict(SOURCES)

#  A scan is cheap but not free, and the terminal polls once a second. This is how often a poll is
#  allowed to look at the disk for one project.
MIN_SCAN_SECONDS = float(os.environ.get("DRAFT_USAGE_SCAN_SECONDS", "4"))
#  A single read of one transcript. A drafting session's file reaches tens of megabytes over a day,
#  and the first scan of an old project should not hold a request thread while it reads all of it.
MAX_READ_BYTES = 24 * 1024 * 1024

_LAST_SCAN: dict[int, float] = {}
_SCANNING: set[int] = set()
_LOCK = threading.Lock()
_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()

_TOKEN_KEYS = (
    ("tokens_input", "input_tokens"),
    ("tokens_output", "output_tokens"),
    ("tokens_cache_write", "cache_creation_input_tokens"),
    ("tokens_cache_read", "cache_read_input_tokens"),
)


def ensure_schema(force: bool = False) -> None:
    global _SCHEMA_READY
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return
        path = Path(__file__).resolve().parents[1] / "sql" / "024_draft_usage.sql"
        try:
            with db.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
            _SCHEMA_READY = True
        except Exception:                                   # noqa: BLE001 - never break a page
            traceback.print_exc()


# =============================================================================================
# Pricing
# =============================================================================================
def price(model: str, tokens: Mapping[str, int]) -> float:
    """What these tokens would cost at published API rates.

    Delegates to the fleet's spend guard, which is where the rate table lives and which prices an
    unrecognised model at the most expensive tier we run, so a model nobody listed reads high
    rather than free.
    """
    try:
        from llm_spend_guard import price as _price
        return float(_price(model or "", {
            "input_tokens": int(tokens.get("tokens_input") or 0),
            "output_tokens": int(tokens.get("tokens_output") or 0),
            "cache_read_input_tokens": int(tokens.get("tokens_cache_read") or 0),
            "cache_creation_input_tokens": int(tokens.get("tokens_cache_write") or 0),
        }))
    except Exception:                                       # noqa: BLE001
        return 0.0


# =============================================================================================
# Where the transcripts are
# =============================================================================================
def transcript_roots(project_id: int) -> list[tuple[str, Path]]:
    """Every configuration directory an agent of this project has been given, with what it is.

    Each entry is a ``projects/`` directory, inside which Claude Code makes one folder per working
    directory and one JSONL per session.
    """
    try:
        import draft_workspace
        root = draft_workspace.root()
    except Exception:                                       # noqa: BLE001
        return []
    workspace = root / f"p{int(project_id)}"
    out = [
        #  The interactive drafting agent. It is given a whole private HOME and its
        #  CLAUDE_CONFIG_DIR is `.claude` INSIDE it, so its transcripts are one level deeper than
        #  the headless runs'. Getting this wrong reads as an agent that spent nothing, which is
        #  the most expensive thing on the page.
        ("terminal", workspace / ".agent-home" / ".claude" / "projects"),
        #  The headless turn and review runs share one home beside the workspaces, and
        #  `draft_agent` points CLAUDE_CONFIG_DIR straight at it.
        ("turn", root / ".agent-home" / "projects"),
        #  The filing reviewer, one level down so its configuration starts empty.
        ("filing_qa", root / "filing-qa" / ".agent-home" / "projects"),
    ]
    return [(source, path) for source, path in out if path.is_dir()]


def _project_folders(root: Path, project_id: int) -> list[Path]:
    """The per-working-directory folders under a ``projects/`` root that belong to this draft.

    Claude Code encodes the working directory into the folder name by replacing every non-word
    character with a hyphen, so the draft's own workspace ends in ``-p8``. Matching on that suffix
    keeps p8 and p18 apart, which a substring match would not.
    """
    want = re.compile(r"(?:^|-)p%d$" % int(project_id))
    out = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and want.search(entry.name):
            out.append(entry)
    return out


# =============================================================================================
# Counting
# =============================================================================================
def _sum_usage(handle: Any, start: int) -> dict[str, Any]:
    """Read the new part of one transcript and total the usage in it, per model."""
    handle.seek(start)
    raw = handle.read(MAX_READ_BYTES)
    #  Stop at the last complete line: a session being written to right now ends mid-line, and
    #  the tail of it is counted on the next scan rather than dropped or double-counted.
    cut = raw.rfind(b"\n")
    if cut < 0:
        return {"read": 0, "models": {}}
    body = raw[:cut + 1]
    models: dict[str, dict[str, int]] = {}
    for line in body.split(b"\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        message = event.get("message")
        if not isinstance(message, Mapping):
            continue
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            continue
        model = str(message.get("model") or "")
        #  A synthetic message is the CLI's own placeholder for an error or an interruption. It
        #  carries a zero usage block and no model, and counting it as a call inflates the count.
        if not model or model.startswith("<"):
            continue
        bucket = models.setdefault(model, {"calls": 0})
        bucket["calls"] += 1
        for column, key in _TOKEN_KEYS:
            bucket[column] = bucket.get(column, 0) + int(usage.get(key) or 0)
    return {"read": len(body), "models": models}


def scan(project_id: int, *, force: bool = False, background: bool = True) -> bool:
    """Fold everything new in this project's transcripts into the ledger.

    ALWAYS OFF THE REQUEST THREAD by default. A steady scan is milliseconds, because it reads only
    the bytes that arrived since the last one. The FIRST scan of an old project is not: project 8
    on this database had fifty transcripts holding 1.3 billion tokens and took 27 seconds to read
    them, and a poll that hangs for 27 seconds is a broken page. So the caller gets whatever the
    ledger already holds and the number catches up a moment later, which is what a counter is for.

    Rate limited per project, because the page asks once a second and the answer only changes as
    fast as a model can answer.
    """
    project_id = int(project_id)
    now = time.monotonic()
    with _LOCK:
        if not force and now - _LAST_SCAN.get(project_id, 0.0) < MIN_SCAN_SECONDS:
            return False
        if project_id in _SCANNING:
            return False
        _LAST_SCAN[project_id] = now
        _SCANNING.add(project_id)
    if not background:
        _scan_now(project_id)
        return True
    threading.Thread(target=_scan_now, args=(project_id,),
                     name=f"draft-usage-{project_id}", daemon=True).start()
    return True


def _scan_now(project_id: int) -> None:
    try:
        ensure_schema()
        for source, root in transcript_roots(project_id):
            for folder in _project_folders(root, project_id):
                for path in sorted(folder.glob("*.jsonl")):
                    _scan_file(project_id, source, path)
    except Exception:                                       # noqa: BLE001 - never break a page
        traceback.print_exc()
    finally:
        with _LOCK:
            _SCANNING.discard(project_id)


def _scan_file(project_id: int, source: str, path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    key = str(path)
    with db.cursor() as cur:
        cur.execute("SELECT model,bytes_read FROM app_draft_usage "
                    "WHERE project_id=%s AND source=%s AND path=%s",
                    (project_id, source, key))
        rows = cur.fetchall() or []
    counted = max((int(row.get("bytes_read") or 0) for row in rows), default=0)
    if counted >= size:
        return
    try:
        with path.open("rb") as handle:
            summary = _sum_usage(handle, counted)
    except OSError:
        return
    if not summary["read"]:
        return
    read_to = counted + int(summary["read"])
    for model, counts in summary["models"].items():
        _upsert(project_id, source, key, model, counts, bytes_read=read_to)
    if not summary["models"]:
        #  Nothing billable in this stretch, but the bytes are counted so the next scan does not
        #  read them again. A row with no model carries the offset for the file.
        _upsert(project_id, source, key, "", {"calls": 0}, bytes_read=read_to)
    else:
        with db.cursor() as cur:
            cur.execute("UPDATE app_draft_usage SET bytes_read=%s,updated_at=now() "
                        "WHERE project_id=%s AND source=%s AND path=%s",
                        (read_to, project_id, source, key))


def _upsert(project_id: int, source: str, path: str, model: str, counts: Mapping[str, int],
            *, bytes_read: int = 0, usd: float | None = None) -> None:
    tokens = {column: int(counts.get(column) or 0) for column, _key in _TOKEN_KEYS}
    usd = price(model, tokens) if usd is None else float(usd)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_draft_usage "
            "(project_id,source,path,model,bytes_read,calls,tokens_input,tokens_output,"
            " tokens_cache_read,tokens_cache_write,usd) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (project_id,source,path,model) DO UPDATE SET "
            "bytes_read=GREATEST(app_draft_usage.bytes_read,EXCLUDED.bytes_read),"
            "calls=app_draft_usage.calls+EXCLUDED.calls,"
            "tokens_input=app_draft_usage.tokens_input+EXCLUDED.tokens_input,"
            "tokens_output=app_draft_usage.tokens_output+EXCLUDED.tokens_output,"
            "tokens_cache_read=app_draft_usage.tokens_cache_read+EXCLUDED.tokens_cache_read,"
            "tokens_cache_write=app_draft_usage.tokens_cache_write+EXCLUDED.tokens_cache_write,"
            "usd=app_draft_usage.usd+EXCLUDED.usd,updated_at=now()",
            (project_id, source, path, model, int(bytes_read), int(counts.get("calls") or 0),
             tokens["tokens_input"], tokens["tokens_output"], tokens["tokens_cache_read"],
             tokens["tokens_cache_write"], round(usd, 4)))


def record(project_id: int, *, source: str, model: str, usage: Mapping[str, Any] | None = None,
           tokens: Mapping[str, int] | None = None, usd: float | None = None,
           calls: int = 1) -> None:
    """Report usage that never went through a Claude Code transcript.

    ``usage`` takes an Anthropic-shaped block, which is what both providers are normalised to
    here. Failure is swallowed on purpose: a counter must never be the reason a drawing
    inspection or a filing build fails.
    """
    try:
        ensure_schema()
        counts = dict(tokens or {})
        if usage:
            for column, key in _TOKEN_KEYS:
                counts[column] = int(counts.get(column) or 0) + int(usage.get(key) or 0)
        counts["calls"] = int(calls)
        #  A caller that knows what its call actually cost says so: the guard's rate for a model
        #  it has never seen is a deliberate ceiling, not a measurement.
        _upsert(project_id, str(source), "", str(model or ""), counts, usd=usd)
    except Exception:                                       # noqa: BLE001
        traceback.print_exc()


# =============================================================================================
# Reading it back
# =============================================================================================
def _blank() -> dict[str, Any]:
    return {"calls": 0, "tokens_input": 0, "tokens_output": 0, "tokens_cache_read": 0,
            "tokens_cache_write": 0, "tokens_total": 0, "usd": 0.0}


def totals(project_id: int) -> dict[str, Any]:
    """Everything this project has spent, in total, by source and by model."""
    ensure_schema()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT source,model,calls,tokens_input,tokens_output,tokens_cache_read,"
                "tokens_cache_write,usd,updated_at FROM app_draft_usage "
                "WHERE project_id=%s", (int(project_id),))
            rows = cur.fetchall() or []
    except Exception:                                       # noqa: BLE001
        traceback.print_exc()
        rows = []

    total = _blank()
    by_source: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    updated = 0.0
    for row in rows:
        source = str(row.get("source") or "")
        model = str(row.get("model") or "")
        for bucket in (total,
                       by_source.setdefault(source, {**_blank(), "source": source,
                                                     "label": SOURCE_LABELS.get(source, source)}),
                       by_model.setdefault(model, {**_blank(), "model": model})):
            bucket["calls"] += int(row.get("calls") or 0)
            for column, _key in _TOKEN_KEYS:
                bucket[column] += int(row.get(column) or 0)
            bucket["usd"] += float(row.get("usd") or 0)
        stamp = row.get("updated_at")
        if stamp is not None:
            updated = max(updated, stamp.timestamp())
    for bucket in [total] + list(by_source.values()) + list(by_model.values()):
        bucket["tokens_total"] = sum(bucket[column] for column, _key in _TOKEN_KEYS)
        bucket["usd"] = round(bucket["usd"], 4)
    order = {key: index for index, (key, _label) in enumerate(SOURCES)}
    return {
        **total,
        "by_source": sorted((item for item in by_source.values() if item["tokens_total"]),
                            key=lambda item: order.get(item["source"], 99)),
        "by_model": sorted((item for item in by_model.values() if item["tokens_total"]),
                           key=lambda item: -item["tokens_total"]),
        "updated_at": updated,
        "basis": "Metered-equivalent at published API rates. The drafting agent runs on a "
                 "subscription, so this is what the work would have cost, not what was charged.",
    }


def refresh(project_id: int, *, force: bool = False) -> dict[str, Any]:
    """Ask for a scan, then report what is known now. What the page asks for.

    The scan is not waited on: see `scan`. The answer is therefore up to one scan behind, which
    for a counter is the difference between a number that moves and a page that stalls.
    """
    scan(project_id, force=force)
    out = totals(project_id)
    out["scanning"] = int(project_id) in _SCANNING
    return out


def summary_line(usage: Mapping[str, Any]) -> str:
    return f"{compact(usage.get('tokens_total') or 0)} tokens, ${float(usage.get('usd') or 0):,.2f}"


def compact(value: Any) -> str:
    """1,240 -> 1.2k, 470,510,446 -> 471M. A token count is read, not audited."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(number) >= limit:
            trimmed = number / limit
            return f"{trimmed:.1f}{suffix}" if trimmed < 10 else f"{trimmed:.0f}{suffix}"
    return f"{number:,.0f}"


def totals_for(project_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """One query for a list page, rather than one per row."""
    ids = [int(value) for value in project_ids]
    if not ids:
        return {}
    ensure_schema()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT project_id,SUM(calls) AS calls,"
                "SUM(tokens_input+tokens_output+tokens_cache_read+tokens_cache_write) AS tokens,"
                "SUM(usd) AS usd FROM app_draft_usage WHERE project_id = ANY(%s) "
                "GROUP BY project_id", (ids,))
            rows = cur.fetchall() or []
    except Exception:                                       # noqa: BLE001
        traceback.print_exc()
        return {}
    return {int(row["project_id"]): {"tokens_total": int(row.get("tokens") or 0),
                                     "calls": int(row.get("calls") or 0),
                                     "usd": round(float(row.get("usd") or 0), 4)}
            for row in rows}
