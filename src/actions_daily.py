"""The actions docket's daily check: every followed company asked about again, once a day.

Run by cron on this box (ops/actions_daily.sh). It does exactly what "Refresh from the registers"
does on the page, for every target of every account and for all three kinds, one after another:

- every row's register is read again (USPTO Open Data Portal, EPO Register, INPADOC for the DPMA,
  TMview and the EUIPO for marks and designs). That is what moves a posture, a first rejection, an
  allowance or a grant, and with them the windows and the submissions each row can take, which
  the page computes from those facts on every load;
- our own submissions are looked for on the US file wrappers, which is what moves a row from
  "submitted" to "public";
- the target's assignee and inventor names are searched at every office it tracks, over its own
  lookback, and each new application in those names is added to the list;
- and iptorch.com is swept for packages, so a build made since the last page load is on its row.

WHY A CRON NOW. The button was the only way in, on the argument that the reader knows whether
today is worth the EPO's API budget. In practice a grant sat unseen until somebody happened to
press it, and a nine-month opposition window starts on the day of grant. One sweep a day of a
two-target docket is a few hundred calls; the EPO's fair-use allowance is measured in gigabytes a
week.

Usage, from src/:  ../.venv/bin/python actions_daily.py [--kind patent] [--user 4] [--no-iptorch]
Writes data/observations/daily/<date>.json and latest.json, which the page reads to say when the
last automatic check ran and whether it was clean.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import sys
import time
import traceback
from pathlib import Path

import config  # noqa: F401  loads .env: the office keys and the database
import db
import iptorch_packages
import observation_marks
import observation_refresh
import observations

LOG_DIR = Path(os.environ.get("ACTIONS_DAILY_DIR", str(
    Path(__file__).resolve().parent.parent / "data" / "observations" / "daily")))


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def all_targets(user=None):
    """(user_id, target) for every target on every docket, oldest first."""
    observations.ensure_schema()
    with db.cursor(autocommit=True) as cur:
        if user is None:
            cur.execute("SELECT user_id, id FROM app_observation_targets ORDER BY user_id, id")
        else:
            cur.execute("SELECT user_id, id FROM app_observation_targets WHERE user_id = %s "
                        "ORDER BY id", (user,))
        pairs = [(r["user_id"], r["id"]) for r in cur.fetchall()]
    out = []
    for uid, tid in pairs:
        t = observations.get_target(uid, tid)
        if t:
            out.append((uid, t))
    return out


def check(uid, target, kind):
    """One target, one kind: the same sweep the button starts, merged the same way."""
    started = time.time()
    rows = observations.cases_for(uid, target["id"], kind=kind)
    if kind in ("design", "trademark"):
        res = observation_marks.sweep(rows, target, kind)
    else:
        res = observation_refresh.sweep(rows, target=target)
    counts = observation_refresh.apply_to_user(uid, res, target_id=target["id"], kind=kind,
                                               trigger="daily")
    return {"user_id": uid, "target_id": target["id"], "target": target.get("name") or "",
            "kind": kind, "cases": len(rows) + counts["new"], "updated": counts["updated"],
            "new": counts["new"], "errors": (res.get("errors") or [])[:40],
            "changes": (res.get("changes") or [])[:200], "seconds": round(time.time() - started, 1)}


def run(kinds=observation_marks.KINDS, user=None, iptorch=True):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"started": _now().isoformat(), "ok": True, "targets": [], "failures": [],
               "iptorch": None}
    with open(LOG_DIR / ".lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another daily check is still running; not starting a second", file=sys.stderr)
            return None
        for uid, target in all_targets(user):
            for kind in kinds:
                try:
                    one = check(uid, target, kind)
                    summary["targets"].append(one)
                    print("%s / %s: %d cases, %d updated, %d new, %d errors, %ss"
                          % (one["target"], kind, one["cases"], one["updated"], one["new"],
                             len(one["errors"]), one["seconds"]), flush=True)
                    for line in one["changes"]:
                        print("  " + line, flush=True)
                except Exception as exc:
                    traceback.print_exc()
                    summary["ok"] = False
                    summary["failures"].append({"user_id": uid, "target_id": target["id"],
                                                "target": target.get("name") or "", "kind": kind,
                                                "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])})
        if iptorch:
            try:
                s = iptorch_packages.sync(docket_keys=observations._iptorch_docket_keys)
                summary["iptorch"] = {k: s.get(k) for k in ("seen", "added", "skipped_running",
                                                            "not_on_docket", "errors", "busy")}
                print("iptorch.com packages: %s" % summary["iptorch"], flush=True)
            except Exception as exc:
                traceback.print_exc()
                summary["iptorch"] = {"errors": ["%s: %s" % (type(exc).__name__, str(exc)[:300])]}
        summary["finished"] = _now().isoformat()
        summary["new"] = sum(t["new"] for t in summary["targets"])
        summary["updated"] = sum(t["updated"] for t in summary["targets"])
        summary["read_errors"] = sum(len(t["errors"]) for t in summary["targets"])
        body = json.dumps(summary, ensure_ascii=False, indent=1, default=str)
        for name in ("%s.json" % summary["started"][:10], "latest.json"):
            tmp = LOG_DIR / (name + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, LOG_DIR / name)
    return summary


def latest(log_dir=None):
    """The last daily check as the page shows it, or None when there has never been one."""
    try:
        s = json.loads((Path(log_dir) if log_dir else LOG_DIR).joinpath("latest.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return {"started": s.get("started") or "", "finished": s.get("finished") or "",
            "ok": bool(s.get("ok")), "new": s.get("new") or 0, "updated": s.get("updated") or 0,
            "read_errors": s.get("read_errors") or 0, "failures": len(s.get("failures") or []),
            "iptorch_added": (s.get("iptorch") or {}).get("added") or 0}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kind", action="append", choices=observation_marks.KINDS,
                    help="only this kind (repeatable); default all three")
    ap.add_argument("--user", type=int, help="only this account's targets")
    ap.add_argument("--no-iptorch", action="store_true", help="skip the iptorch.com package sweep")
    args = ap.parse_args(argv)
    s = run(kinds=tuple(args.kind or observation_marks.KINDS), user=args.user,
            iptorch=not args.no_iptorch)
    if s is None:
        return 0
    print("done: %d new cases, %d re-read, %d read errors, %d failed sweeps"
          % (s["new"], s["updated"], s["read_errors"], len(s["failures"])), flush=True)
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
