"""What one search cost: how long it ran, how many model calls, how many tokens.

WHY THIS MODULE EXISTS. The numbers were all there WHILE a search ran and none of them survived it.
`_job_tokens` reports live tokens from a process-global counter minus the job's baseline, and the
job dies with the process; `llm.usage_session()` is opened only by the retrieval agent, and its own
docstring says a ContextVar scope does not cross into a ThreadPoolExecutor, so the seven calls it
records are the agent's and not the reading's. A finished report therefore carried `llm_usage:
{calls: 7}` for a run that had just read four hundred documents in full.

So the run's own window is measured once, at the end, and written down.

HOW HONEST THE TOKEN NUMBER IS. It is the process-global counter's delta across this run's window.
This app runs several searches concurrently in one gunicorn worker, so when another run overlapped,
that run's tokens are inside this number. That is recorded as `shared_process` and said on the page
rather than papered over: a cost attributed to the wrong search is worse than a cost marked
approximate.

FOR REPORTS WRITTEN BEFORE THIS EXISTED, nothing is invented. The wall clock comes from the saved
search row, the stage timings and the document counts come from the report itself, and the token
figure is reported as not recorded. An estimate dressed as a measurement is the one thing a cost
page must not do.
"""
from __future__ import annotations

import json
import os
import traceback

VERSION = 2
#  A model call costs roughly this many tokens on the reading tier, measured across the runs of
#  2026-08-20 (200-260 M tokens for 400-odd documents read in full). Used ONLY to say "roughly n
#  calls" when calls were not counted but tokens were, and always labelled as derived.
_TOKENS_PER_CALL = 90_000


def path_for(reports_dir, slug):
    return os.path.join(str(reports_dir), "%s.stats.json" % slug)


def record(reports_dir, slug, *, seconds, tokens, calls=0, prompt_tokens=0, completion_tokens=0,
           shared_process=False, report=None):
    """Write the run's own measurement. Never raises: a search must not fail over its receipt."""
    data = {
        "version": VERSION,
        "measured": True,
        "seconds": round(float(seconds or 0), 1),
        "tokens": int(tokens or 0),
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "calls": int(calls or 0),
        #  True when another search was running in this worker during the window, which means the
        #  token figure is an upper bound for this search rather than its own cost.
        "shared_process": bool(shared_process),
    }
    data.update(_from_report(report or {}))
    try:
        p = path_for(reports_dir, slug)
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, p)                 # atomic: a half-written receipt reads as garbage
    except Exception:
        traceback.print_exc()
    return data


def _from_report(rep):
    """The parts a finished report already knows: what it searched, read and spent time on."""
    qd = (rep or {}).get("query_document") or {}
    dr = (rep or {}).get("deep_rank") or {}
    ext = (rep or {}).get("external") or {}
    agent = (rep or {}).get("llm_usage") or {}
    out = {
        "subject_pub": (qd.get("publication_number") or qd.get("label")
                        or (rep or {}).get("subject") or ""),
        "subject_title": qd.get("title") or "",
        "subject_source": qd.get("source") or "",
        "n_claims": len(qd.get("claims") or []),
        "mode": (rep or {}).get("mode") or "",
        "depth": (rep or {}).get("depth") or dr.get("depth") or "",
        "rounds": (rep or {}).get("rounds"),
        "n_families": (rep or {}).get("n_families"),
        "screened": dr.get("screened"),
        "read_in_full": dr.get("read_in_full"),
        "charted": dr.get("charted"),
        "chars_read": dr.get("chars_read"),
        "screen_seconds": dr.get("screen_seconds"),
        "chart_seconds": dr.get("chart_seconds"),
        "deep_seconds": dr.get("seconds"),
        "external_queries": ext.get("n_queries"),
        "external_seconds": ext.get("elapsed"),
        #  The retrieval agent's OWN calls. Named for what it is: this is not the run's total, and
        #  labelling it "llm_usage" on the report is what made a 400-document read look like seven
        #  calls.
        "agent_calls": agent.get("calls"),
        "agent_tokens": (int(agent.get("prompt_tokens") or 0)
                         + int(agent.get("completion_tokens") or 0)) or None,
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def load(reports_dir, slug, report=None, seconds=None):
    """The receipt for `slug`, measured if one was written, derived from the report if not.

    `report` is only read when there is no receipt, so the common path costs one small file.
    """
    p = path_for(reports_dir, slug)
    if os.path.exists(p):
        try:
            with open(p) as fh:
                got = json.load(fh)
            if seconds and not got.get("seconds"):
                got["seconds"] = round(float(seconds), 1)
            return got
        except Exception:
            traceback.print_exc()
    out = {"version": VERSION, "measured": False, "shared_process": False}
    out.update(_from_report(report or {}))
    if seconds:
        out["seconds"] = round(float(seconds), 1)
    elif out.get("deep_seconds"):
        #  Better than nothing and clearly labelled: the reading stage's own clock is most of a
        #  deep run, but it is not the whole run.
        out["seconds_partial"] = out["deep_seconds"]
    return out


def summarise(st):
    """One line for a list row, and the caveats that have to travel with the numbers.

    -> {"time", "calls", "tokens", "notes": [...]}, every field a string or None.
    """
    st = st or {}
    notes = []
    time_s = None
    if st.get("seconds"):
        time_s = _hms(st["seconds"])
    elif st.get("seconds_partial"):
        time_s = _hms(st["seconds_partial"]) + " reading"
        notes.append("Only the reading stage was timed for this run; the whole run was longer.")

    tokens = calls = None
    if st.get("measured") and st.get("tokens"):
        tokens = _si(st["tokens"])
        if st.get("calls"):
            calls = "{:,}".format(st["calls"])
        else:
            calls = "~%s" % "{:,}".format(max(1, round(st["tokens"] / _TOKENS_PER_CALL)))
            notes.append("Calls are derived from the token count, not counted directly.")
        if st.get("shared_process"):
            notes.append("Another search was running in the same worker during this one, so the "
                         "token figure is an upper bound for this search rather than its own cost.")
    else:
        notes.append("Tokens were not recorded for this run: the counter was live-only until "
                     "2026-08-21, so only searches run after that carry a measured cost.")
        if st.get("agent_calls"):
            calls = "%s (query agent only)" % "{:,}".format(st["agent_calls"])
    return {"time": time_s, "calls": calls, "tokens": tokens, "notes": notes}


def _hms(sec):
    sec = int(float(sec or 0))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return "%dh %02dm" % (h, m)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


def _si(n):
    n = int(n or 0)
    for unit, div in (("B", 10 ** 9), ("M", 10 ** 6), ("k", 10 ** 3)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return str(n)
