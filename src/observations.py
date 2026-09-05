"""The actions docket: what can still be filed against somebody else's patents, one target at a time.

An observation, an Einwendung, a preissuance submission: three offices, three names, one act. You
put prior art in front of an examiner who is still examining, and it costs almost nothing. The
hard part was never the filing, it is knowing WHICH of a competitor's cases is still open and for
how long, because every office computes that differently and none of them tells you.

So this is a docket, not a report. Each row is one case with every instrument that reaches it
today, the date each door shuts and what has already been put on the file. Alongside it sits the
record of what we have actually filed, which is the part that decays fastest: correspondence says
a submission went in on the 26th, the file wrapper says the 24th, and only one of those is the
date an examiner will see.

A DOCKET IS ABOUT A TARGET

A target is a company, a person, or several of either: the assignee names and inventor names to
search the offices for. Each target owns its own rows, its own refresh and its own last-pulled
date, and the page shows one target at a time. Adding one searches the EPO's published data (EP,
DE and WO) and the USPTO's Open Data Portal for everything those names published in the lookback
window, checks each hit against its own bibliographic record before accepting it, and then reads
the register for each accepted case so that it arrives with a posture and a window rather than a
bare number. See [[observation_refresh]] for the searches.

The shipped GRABO/Schmalz docket is one such target, marked `seeded`, created for `OWNER_EMAIL`
on boot. Rows that predate targets are adopted into one on first boot, so nothing that was on the
page disappears from it.

WHY THE DATA IS PER USER, AND SEEDED FOR EXACTLY ONE ACCOUNT

This app takes public signups. A docket is a list of a named third party's patents with the dates
on which they become harder to attack, annotated with what our counsel thinks and what was
declined; it is the opposite of shareable. Every table here is keyed by `user_id` with a foreign
key onto `app_users`, every query filters on it, and nothing reads a row it was not asked for by
its owner. A new account therefore sees an empty docket and can build its own.

WHAT THE ROUTES TRUST

`auth.current_user()` and nothing else. The app-wide gate also honours a loopback exemption, which
is right for a drafting agent publishing its own work but wrong here: this box has a second tenant
on it, and "a process on the machine" is not "the person who owns this docket".

A COUNTDOWN IS COMPUTED, NEVER STORED

`days_left` used to be baked into the shipped file against the date the sweep ran, so on the day
after a build every number on the page was one day wrong and nothing said so. It is derived here,
on every read, from the deadline and today. The stored value is kept only as the fallback for a
row that has a countdown and no date behind it. See [[observation_refresh]] for the other half:
the deadlines themselves going stale, which is a button rather than an expression.
"""
from __future__ import annotations

import collections
import datetime
import json
import os
import re
import threading
import traceback

from flask import (Blueprint, abort, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

import auth
import db
import observation_actions
import observation_links
import observation_marks
import observation_refresh

bp = Blueprint("observations", __name__)

#  THE ONE ACCOUNT THE SHIPPED DOCKET BELONGS TO. Overridable by env so a staging box can seed a
#  test account instead, but never a list of addresses: seeding two accounts would put the same
#  private docket in two places and there would be no answer to which one is authoritative.
OWNER_EMAIL = os.environ.get("OBSERVATIONS_OWNER_EMAIL", "nimo@rotem.ai")
#  What the shipped docket's target is called. Its rows are found by the `seeded` flag, never by
#  this name, so renaming the target on the page does not make the next boot seed a second copy.
SEED_TARGET = os.environ.get("OBSERVATIONS_SEED_TARGET", "J. Schmalz GmbH")

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("OBSERVATIONS_DATA",
                          os.path.join(_HERE, "..", "data", "observations"))
SEED_PATH = os.path.join(DATA_DIR, "seed.json")
PACKAGE_DIR = os.path.join(DATA_DIR, "packages")

MAX_NOTE_CHARS = 4000
MAX_TARGETS = 40
MAX_NAMES = 12
MAX_NAME_CHARS = 120
#  The offices a target can be tracked at, and the label the form shows for each. US is the Open
#  Data Portal; the other three are the EPO's published data and registers.
OFFICES = (("EP", "Europe (EPO)"), ("DE", "Germany (DPMA)"),
           ("US", "United States (USPTO)"), ("WO", "PCT (WIPO)"))
OFFICE_CODES = tuple(code for code, _ in OFFICES)
#  How far back a new target's first sweep looks, in months of publication date. Sixty is the
#  long end because a granted patent's later windows (IPR, § 301) never close.
LOOKBACKS = (6, 12, 24, 36, 60)
DEFAULT_LOOKBACK = 36
#  The shipped docket was hand-built and its refresh only ever looked a year back for new cases.
#  Keeping that on the seeded target means the button keeps doing what it did.
SEED_LOOKBACK = 12

#  What the expanded row shows. Named explicitly rather than "everything except the row fields",
#  so adding a field to the docket never silently publishes it to the page.
DETAIL_FIELDS = (
    "publication", "granted_as", "title", "title_full", "application", "applicant", "applicants",
    "inventors", "ipc", "office", "baseline_route_label", "register_status", "register_updated",
    "register_url", "google", "pubDate", "priority_date", "filing_date", "grant_date",
    "patent_number", "six_months", "first_rejection", "deadline", "deadline_kind", "days_left",
    "verified", "counsel_required", "counsel_report", "next_action", "superseded_note", "why_new",
    "found_by", "family", "priority", "user_note", "representative",
    #  Where the case now stands, what we have already put on its file, and the instrument table
    #  for its office.
    "posture", "grant_published", "opposition_deadline", "opposition_opens_est", "scheduled_grant",
    "decision_on", "closing_note", "closing_soon", "allowance", "quayle", "exam_requested",
    "opposition_pending", "our_submissions", "file_events", "on_file", "refreshed_at",
    "refresh_source", "actions",
    #  What has already been built for the case elsewhere: the filing app's packets and this
    #  app's own searches, pinned to the row by number. See observation_links.
    "packages", "package_state", "searches", "search_state",
    #  Designs and marks: what a row of those kinds carries that a patent row does not.
    "kind", "status", "registration", "registration_date", "expiry_date", "publication_date",
    "opposition_start", "opposition_end", "classes", "mark_type", "image", "oppositions",
    "cancellations", "designated", "deferred",
    #  The German register's own event list. It is the evidence behind every German posture and
    #  deadline on this page: "granted 2026-08-20" is an assertion until you can see the R018 and
    #  the B4 it was read off.
    "register_events",
)
#  Kept on the row for anyone who set one before the column left the page; nothing writes it now.
USER_STATES = ("open", "watch", "queued", "filed", "declined", "done")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS app_observation_targets (
         id bigserial PRIMARY KEY,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         name text NOT NULL,
         assignees jsonb NOT NULL DEFAULT '[]'::jsonb,
         inventors jsonb NOT NULL DEFAULT '[]'::jsonb,
         offices jsonb NOT NULL DEFAULT '["EP","DE","US","WO"]'::jsonb,
         lookback_months integer NOT NULL DEFAULT 36,
         seeded boolean NOT NULL DEFAULT false,
         refreshed_at timestamptz,
         refresh jsonb NOT NULL DEFAULT '{}'::jsonb,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (user_id, name))""",
    """CREATE TABLE IF NOT EXISTS app_observation_cases (
         id bigserial PRIMARY KEY,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         target_id bigint REFERENCES app_observation_targets(id) ON DELETE CASCADE,
         publication text NOT NULL,
         payload jsonb NOT NULL,
         user_state text NOT NULL DEFAULT 'open',
         user_note text NOT NULL DEFAULT '',
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now())""",
    #  A database from before targets has the table without the column. Added nullable here and
    #  made NOT NULL only after `_adopt_orphans` has given every existing row a target.
    "ALTER TABLE app_observation_cases ADD COLUMN IF NOT EXISTS target_id bigint "
    "REFERENCES app_observation_targets(id) ON DELETE CASCADE",
    "CREATE INDEX IF NOT EXISTS app_observation_cases_user_idx "
    "ON app_observation_cases (user_id, publication)",
    """CREATE TABLE IF NOT EXISTS app_observation_filings (
         id bigserial PRIMARY KEY,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         filing_key text NOT NULL,
         payload jsonb NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (user_id, filing_key))""",
    """CREATE TABLE IF NOT EXISTS app_observation_meta (
         user_id bigint PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
         payload jsonb NOT NULL,
         updated_at timestamptz NOT NULL DEFAULT now())""",
)
#  After the adoption. The old uniqueness was per user; a publication may now sit on two of one
#  person's targets (a co-applicant's case is on both dockets), so the key gains the target.
_SCHEMA_AFTER = (
    "ALTER TABLE app_observation_cases "
    "DROP CONSTRAINT IF EXISTS app_observation_cases_user_id_publication_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS app_observation_cases_target_pub_key "
    "ON app_observation_cases (user_id, target_id, publication)",
    "ALTER TABLE app_observation_cases ALTER COLUMN target_id SET NOT NULL",
)

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def ensure_schema(force: bool = False) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return
        with db.cursor(autocommit=True) as cur:
            for statement in _SCHEMA:
                cur.execute(statement)
            _adopt_orphans(cur)
            for statement in _SCHEMA_AFTER:
                cur.execute(statement)
        _SCHEMA_READY = True


def reset_schema_cache_for_tests() -> None:
    global _SCHEMA_READY
    _SCHEMA_READY = False


def load_seed():
    with open(SEED_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _owner_id(cur=None):
    """The `app_users` row id for OWNER_EMAIL, or None when that account does not exist."""
    if cur is not None:
        cur.execute("SELECT id FROM app_users WHERE lower(email) = lower(%s)", (OWNER_EMAIL,))
        row = cur.fetchone()
        return row["id"] if row else None
    with db.cursor(autocommit=True) as cur:
        return _owner_id(cur)


# ---------------------------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------------------------

def _target_row(r):
    """A database row -> the dict the page and the sweep use."""
    t = dict(r)
    for key in ("assignees", "inventors", "offices"):
        v = t.get(key)
        t[key] = list(v) if isinstance(v, (list, tuple)) else []
    #  One refresh record per kind. A record written before kinds existed has its `changes`
    #  at the top; read it as the patent one.
    raw = dict(t.get("refresh") or {})
    t["refresh"] = {"patent": raw} if "changes" in raw else raw
    when = t.get("refreshed_at")
    #  To the minute, in UTC, with no offset suffix: "2026-09-05 00:25" is what a reader wants
    #  beside "last pulled", and every box in this fleet keeps UTC.
    if isinstance(when, datetime.datetime):
        if when.tzinfo is not None:
            when = when.astimezone(datetime.timezone.utc)
        t["refreshed_at"] = when.strftime("%Y-%m-%d %H:%M")
    else:
        t["refreshed_at"] = str(when)[:16].replace("T", " ") if when else None
    for key in ("created_at", "updated_at"):
        v = t.get(key)
        if isinstance(v, datetime.datetime):
            t[key] = v.replace(microsecond=0).isoformat()
    t["cases"] = int(t.get("cases") or 0)
    t["designs"] = int(t.get("designs") or 0)
    t["trademarks"] = int(t.get("trademarks") or 0)
    return t


def _clean_names(values):
    """A list of names as typed, one per line or one per item -> deduplicated, capped, clean."""
    if isinstance(values, str):
        values = re.split(r"[\r\n;]+", values)
    out, seen = [], set()
    for v in values or []:
        name = re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).strip()
        name = re.sub(r"\s+", " ", name)[:MAX_NAME_CHARS]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
        if len(out) >= MAX_NAMES:
            break
    return out


def _clean_offices(values):
    if isinstance(values, str):
        values = re.split(r"[\s,]+", values)
    out = [str(v).strip().upper() for v in (values or [])]
    out = [o for o in OFFICE_CODES if o in out]
    return out or list(OFFICE_CODES)


def _clean_lookback(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LOOKBACK
    return n if n in LOOKBACKS else DEFAULT_LOOKBACK


def _ensure_target(cur, user_id, name, assignees=(), inventors=(), seeded=False,
                   lookback=DEFAULT_LOOKBACK):
    """The id of this person's target by name, creating it if it is missing. The SEEDED target
    is matched on its flag, not its name, so it can be renamed without being seeded twice."""
    if seeded:
        cur.execute("SELECT id FROM app_observation_targets WHERE user_id = %s AND seeded",
                    (user_id,))
    else:
        cur.execute("SELECT id FROM app_observation_targets "
                    "WHERE user_id = %s AND lower(name) = lower(%s)", (user_id, name))
    got = cur.fetchone()
    if got:
        return got["id"]
    cur.execute(
        """INSERT INTO app_observation_targets
             (user_id, name, assignees, inventors, offices, lookback_months, seeded)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id, name) DO UPDATE SET updated_at = now()
           RETURNING id""",
        (user_id, name, json.dumps(list(assignees)), json.dumps(list(inventors)),
         json.dumps(list(OFFICE_CODES)), lookback, bool(seeded)))
    return cur.fetchone()["id"]


def _adopt_orphans(cur):
    """Give every row from before targets a target, once.

    The owner's rows are the shipped docket and go under the seeded target. Anybody else's go
    under a target named after whichever applicant their rows mention most, which is the only
    thing the rows themselves say about who they are about. The per-user refresh record moves
    across with them, so "last pulled" does not reset to "never" on the boot that adds targets.
    """
    cur.execute("SELECT user_id, payload->>'applicant' AS applicant "
                "FROM app_observation_cases WHERE target_id IS NULL")
    rows = cur.fetchall()
    if not rows:
        return
    owner = _owner_id(cur)
    by_user = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r["applicant"] or "")
    for uid, applicants in by_user.items():
        if uid == owner:
            name, seeded, lookback = SEED_TARGET, True, SEED_LOOKBACK
        else:
            counts = collections.Counter(
                observation_refresh.normalise_applicant(a) for a in applicants if a)
            counts.pop("", None)
            name = counts.most_common(1)[0][0].title() if counts else "My docket"
            seeded, lookback = False, DEFAULT_LOOKBACK
        tid = _ensure_target(cur, uid, name, assignees=[name], seeded=seeded, lookback=lookback)
        cur.execute("UPDATE app_observation_cases SET target_id = %s "
                    "WHERE user_id = %s AND target_id IS NULL", (tid, uid))
        cur.execute("SELECT payload FROM app_observation_meta WHERE user_id = %s", (uid,))
        got = cur.fetchone()
        meta = dict(got["payload"]) if got else {}
        if meta.get("refreshed_at"):
            record = {"errors": meta.get("refresh_errors") or [],
                      "changes": meta.get("refresh_changes") or [],
                      "counts": meta.get("refresh_counts") or {},
                      "sources": meta.get("refresh_sources") or {},
                      "as_of": meta.get("as_of")}
            cur.execute("UPDATE app_observation_targets "
                        "SET refreshed_at = COALESCE(refreshed_at, %s::timestamptz), refresh = %s "
                        "WHERE id = %s", (meta["refreshed_at"], json.dumps(record), tid))


def targets_for(user_id):
    """Every target this person has, the shipped one first, each with its row counts per kind."""
    ensure_schema()
    with db.cursor(autocommit=True) as cur:
        cur.execute(
            """SELECT t.*,
                      (SELECT count(*) FROM app_observation_cases c WHERE c.target_id = t.id
                          AND COALESCE(c.payload->>'kind', 'patent') = 'patent') AS cases,
                      (SELECT count(*) FROM app_observation_cases c WHERE c.target_id = t.id
                          AND c.payload->>'kind' = 'design') AS designs,
                      (SELECT count(*) FROM app_observation_cases c WHERE c.target_id = t.id
                          AND c.payload->>'kind' = 'trademark') AS trademarks
                 FROM app_observation_targets t
                WHERE t.user_id = %s
                ORDER BY t.seeded DESC, t.created_at, t.id""", (user_id,))
        return [_target_row(r) for r in cur.fetchall()]


def get_target(user_id, target_id):
    ensure_schema()
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return None
    with db.cursor(autocommit=True) as cur:
        cur.execute(
            """SELECT t.*, (SELECT count(*) FROM app_observation_cases c
                              WHERE c.target_id = t.id) AS cases
                 FROM app_observation_targets t
                WHERE t.user_id = %s AND t.id = %s""", (user_id, target_id))
        row = cur.fetchone()
    return _target_row(row) if row else None


def default_target_id(cur, user_id):
    """Where a row with no target of its own belongs: the seeded target, else the first, else a
    fresh one. Only the legacy sweep path reaches this."""
    cur.execute("SELECT id FROM app_observation_targets WHERE user_id = %s "
                "ORDER BY seeded DESC, created_at, id LIMIT 1", (user_id,))
    got = cur.fetchone()
    if got:
        return got["id"]
    return _ensure_target(cur, user_id, "My docket")


def create_target(user_id, name="", assignees=(), inventors=(), offices=(), lookback=None):
    """A new target for this person. Raises ValueError with a sentence the form can show."""
    ensure_schema()
    assignees = _clean_names(assignees)
    inventors = _clean_names(inventors)
    if not assignees and not inventors:
        raise ValueError("Name at least one assignee or one inventor to search for.")
    name = _clean_names([name])[:1]
    name = name[0] if name else (assignees or inventors)[0]
    offices = _clean_offices(offices)
    lookback = _clean_lookback(lookback)
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT count(*) AS n FROM app_observation_targets WHERE user_id = %s",
                    (user_id,))
        if cur.fetchone()["n"] >= MAX_TARGETS:
            raise ValueError("This account already has %d targets." % MAX_TARGETS)
        cur.execute("SELECT id FROM app_observation_targets "
                    "WHERE user_id = %s AND lower(name) = lower(%s)", (user_id, name))
        if cur.fetchone():
            raise ValueError("There is already a target called %s." % name)
        cur.execute(
            """INSERT INTO app_observation_targets
                 (user_id, name, assignees, inventors, offices, lookback_months)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (user_id, name, json.dumps(assignees), json.dumps(inventors), json.dumps(offices),
             lookback))
        tid = cur.fetchone()["id"]
    return get_target(user_id, tid)


def update_target(user_id, target_id, name=None, assignees=None, inventors=None, offices=None,
                  lookback=None):
    """Change what a target searches for. Rows already found are kept: a sweep adds, it never
    removes, so narrowing the names does not silently empty a docket."""
    ensure_schema()
    current = get_target(user_id, target_id)
    if not current:
        return None
    new_assignees = _clean_names(assignees) if assignees is not None else current["assignees"]
    new_inventors = _clean_names(inventors) if inventors is not None else current["inventors"]
    if not new_assignees and not new_inventors:
        raise ValueError("Keep at least one assignee or one inventor.")
    new_name = current["name"]
    if name is not None:
        cleaned = _clean_names([name])
        if cleaned:
            new_name = cleaned[0]
    new_offices = _clean_offices(offices) if offices is not None else current["offices"]
    new_lookback = _clean_lookback(lookback) if lookback is not None else current["lookback_months"]
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT id FROM app_observation_targets WHERE user_id = %s "
                    "AND lower(name) = lower(%s) AND id <> %s", (user_id, new_name, current["id"]))
        if cur.fetchone():
            raise ValueError("There is already a target called %s." % new_name)
        cur.execute(
            """UPDATE app_observation_targets
                  SET name = %s, assignees = %s, inventors = %s, offices = %s,
                      lookback_months = %s, updated_at = now()
                WHERE user_id = %s AND id = %s""",
            (new_name, json.dumps(new_assignees), json.dumps(new_inventors),
             json.dumps(new_offices), new_lookback, user_id, current["id"]))
    return get_target(user_id, current["id"])


def delete_target(user_id, target_id):
    """Remove a target and every row under it. The seeded target is refused: the next boot
    would only put it back, and its rows carry counsel's annotations."""
    ensure_schema()
    current = get_target(user_id, target_id)
    if not current:
        return False, "No such target."
    if current["seeded"]:
        return False, "The shipped docket cannot be removed."
    with db.cursor(autocommit=True) as cur:
        cur.execute("DELETE FROM app_observation_targets WHERE user_id = %s AND id = %s",
                    (user_id, current["id"]))
        return cur.rowcount > 0, ""


# ---------------------------------------------------------------------------------------------
# the shipped docket
# ---------------------------------------------------------------------------------------------

def seed_owner(force: bool = False) -> int:
    """Load the shipped docket into the owner's seeded target. Idempotent, and additive by default.

    `force=False` inserts rows the owner does not have and refreshes the register facts on rows
    they do, WITHOUT touching `user_note`: the file on disk is authoritative about what the
    register said, the person is authoritative about what they wrote. `force=True` additionally
    resets the note, which is only for a test fixture.

    A LIVE REFRESH OUTRANKS THE SHIPPED FILE. `init_app` seeds on every boot, so once the button
    had pulled the registers a single `supervisorctl restart` put the August snapshot back over
    the top of it and silently un-did the refresh. A row that carries `refreshed_at` is therefore
    left alone unless the file on disk is itself newer than that pull.
    """
    ensure_schema()
    uid = _owner_id()
    if not uid:
        return 0
    seed = load_seed()
    seed_as_of = str(seed.get("as_of") or "")
    n = 0
    with db.cursor(autocommit=True) as cur:
        tid = _ensure_target(cur, uid, SEED_TARGET, assignees=[SEED_TARGET], seeded=True,
                             lookback=SEED_LOOKBACK)
        for row in seed.get("rows", []):
            pub = row.get("publication") or ""
            if not pub:
                continue
            if force:
                cur.execute(
                    """INSERT INTO app_observation_cases (user_id, target_id, publication, payload)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, target_id, publication) DO UPDATE
                         SET payload = EXCLUDED.payload, user_state = 'open', user_note = '',
                             updated_at = now()""",
                    (uid, tid, pub, json.dumps(row)))
            else:
                cur.execute(
                    """INSERT INTO app_observation_cases (user_id, target_id, publication, payload)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, target_id, publication) DO UPDATE
                         SET payload = EXCLUDED.payload, updated_at = now()
                       WHERE COALESCE(app_observation_cases.payload->>'refreshed_at', '')
                             <= %s""",
                    (uid, tid, pub, json.dumps(row), seed_as_of))
            n += 1
        for f in seed.get("filings", []):
            cur.execute(
                """INSERT INTO app_observation_filings (user_id, filing_key, payload)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, filing_key) DO UPDATE
                     SET payload = EXCLUDED.payload, updated_at = now()""",
                (uid, f["id"], json.dumps(f)))
        #  `missed` and `changes` are hand-written and belong to the file. The refresh record
        #  belongs to the target now, so the file never overwrites it.
        meta = {k: seed.get(k) for k in ("as_of", "generated", "source_note", "changes", "missed")}
        cur.execute("SELECT payload FROM app_observation_meta WHERE user_id = %s", (uid,))
        got = cur.fetchone()
        if got:
            merged = dict(got["payload"])
            merged.update({k: v for k, v in meta.items() if k in ("missed", "changes", "generated")})
            merged.setdefault("as_of", meta.get("as_of"))
            merged.setdefault("source_note", meta.get("source_note"))
            meta = merged
        cur.execute(
            """INSERT INTO app_observation_meta (user_id, payload) VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()""",
            (uid, json.dumps(meta)))
    return n


# ---------------------------------------------------------------------------------------------
# per-user reads
# ---------------------------------------------------------------------------------------------

def recount(row, today=None):
    """Recompute the countdown and the urgency band, in place.

    THE COUNTDOWN BELONGS TO THE INSTRUMENT THAT IS OPEN, not to whatever date the last sweep
    happened to store. Those are the same thing on most rows and different on the ones that
    matter: a US case under an Ex parte Quayle action carried 2026-08-26 as its deadline, so the
    table printed "closed, 9 days ago" beside an instrument the very same page reported as still
    open, because a Quayle action is not a rejection and 1.290 had not in fact shut. Whenever
    something can still be filed, that instrument's window IS the row's window. Only when nothing
    is open does the stored date stand, and then it is a record of when the door shut.

    The bands are the ones the table colours by and the filter selects on, so they are defined
    once here rather than three times over in Jinja, JavaScript and the sweep script.
    """
    today = today or datetime.date.today()
    head = row.get("action_headline") or {}
    if head.get("status") in ("open", "closing"):
        row["deadline"] = head.get("deadline")
        row["days_left"] = head.get("days_left")
        #  NOT `closing_soon` here. An Art. 115 observation and a § 43(3) Einwendung are open
        #  with no deadline for years at a stretch; flagging every one of them as urgent would
        #  make the flag mean nothing. Only the sweep sets it, and only on a window that has run
        #  out of dates rather than one that never had any.
    deadline = observation_actions._date(row.get("deadline"))
    if deadline:
        row["days_left"] = (deadline - today).days
    elif row.get("deadline"):
        pass                      # a deadline we cannot parse: keep whatever count came with it
    else:
        row["days_left"] = None
    n = row.get("days_left")
    if row.get("filed"):
        row["state"] = "filed"
    #  A REFUSED OR WITHDRAWN CASE IS LAPSED EVEN THOUGH NOTHING EXPIRED. Four German
    #  applications were refused with the refusal final: no deadline ever passed on them, so the
    #  date arithmetic put them in the same band as a live application nobody has examined yet.
    #  The register's own posture outranks the calendar here.
    elif (row.get("posture") or "").lower() == "lapsed":
        row["state"] = "lapsed"
    elif row.get("missed") or (n is not None and n < 0):
        row["state"] = "lapsed"
    elif n is None:
        row["state"] = "closing" if row.get("closing_soon") else "open"
    elif n <= 14:
        row["state"] = "urgent"
    elif n <= 30:
        row["state"] = "closing"
    elif n <= 90:
        row["state"] = "soon"
    else:
        row["state"] = "open"
    #  A WINDOW WITH NO DATE LEFT TO COUNT TO IS NOT THE LEAST URGENT ROW ON THE PAGE. A 1.290
    #  window past its six-month date with no rejection, and an EP application under a Rule 71(3)
    #  intention to grant, both shut the next time the examiner touches the file. Sorting them
    #  with the undated majority buried them ninety rows down. `1` puts them immediately after
    #  anything that closes today and ahead of everything with a date in the future.
    row["sort_key"] = n if n is not None else (
        1 if (row.get("closing_soon") and row["state"] != "lapsed") else 9999)
    return row


def attribute_filings(cases, filings):
    """Say which of the third-party papers already on a file are OURS, and which merely exist.

    The office does not tell you. A 37 CFR 1.290 submission in a file wrapper is a third party's,
    and an Art. 115 observation on the European Register may be anonymous; neither carries "filed
    by GRABO". What we do have is our own record of what we filed and against what, so the two
    are matched on the target and the page says which of the two it is rather than assuming.
    A submission we cannot tie to our own record is still worth showing: somebody else has put
    art in front of this examiner, and that changes what is worth adding.
    """
    ours = set()
    for f in filings or []:
        for key in ("target", "application"):
            value = re.sub(r"[^A-Z0-9]", "", str(f.get(key) or "").upper())
            if value:
                ours.add(value)
    for c in cases:
        keys = {re.sub(r"[^A-Z0-9]", "", str(c.get(k) or "").upper())
                for k in ("publication", "granted_as", "application")}
        mine = bool(keys & ours)
        on_file = []
        for entry in list(c.get("our_submissions") or []) + list(c.get("file_events") or []):
            entry = dict(entry)
            entry["whose"] = "ours" if (mine and entry.get("whose") != "unknown") else "unknown"
            on_file.append(entry)
        on_file.sort(key=lambda e: e.get("date") or "", reverse=True)
        c["on_file"] = on_file
    return cases


def filings_on(cases, filings, everything=False):
    """The filings that belong on THIS docket: those whose target is one of its rows. With
    `everything`, all of them, which is right for the shipped docket whose filings predate the
    rows they were matched to (one names a parent application rather than a publication)."""
    if everything:
        return list(filings or [])
    keys = set()
    for c in cases:
        for k in ("publication", "granted_as", "application"):
            value = re.sub(r"[^A-Z0-9]", "", str(c.get(k) or "").upper())
            if value:
                keys.add(value)
    out = []
    for f in filings or []:
        mine = {re.sub(r"[^A-Z0-9]", "", str(f.get(k) or "").upper())
                for k in ("target", "application")} - {""}
        if mine & keys:
            out.append(f)
    return out


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def can_file_options(cases):
    """Every instrument that can be filed today on at least one row, for the filter, and the
    keys each row answers to.

    Two groups: what is OPEN today, and what is worth CHECKING because the instrument exists but
    turns on a fact the register does not put on the page (an opposition already pending, an
    AIA date). A weak entry, one nobody would actually use, is in neither: it is on the row's
    own table for completeness and would only pad the filter.
    """
    opts = {}
    stage_order = {stage: i for i, (stage, _) in enumerate(
        observation_actions.STAGES + observation_marks.TM_STAGES + observation_marks.DESIGN_STAGES)}
    for c in cases:
        keys = set()
        for a in c.get("actions") or []:
            if a.get("weak"):
                continue
            if a["status"] in ("open", "closing"):
                group = "open"
            elif a["status"] == "conditional":
                group = "check"
            else:
                continue
            key = "%s/%s/%s" % (group, a["stage"], _slug(a["instrument"]))
            keys.add(key)
            o = opts.setdefault(key, {"key": key, "group": group, "stage": a["stage"],
                                      "stage_label": a["stage_label"],
                                      "instrument": a["instrument"], "statute": a["statute"],
                                      "count": 0})
        for key in keys:
            opts[key]["count"] += 1
        c["can_keys"] = sorted(keys)
    order = {"open": 0, "check": 1}
    return sorted(opts.values(), key=lambda o: (order.get(o["group"], 9),
                                                stage_order.get(o["stage"], 99),
                                                o["instrument"]))


_DATE_FIELDS = ("filing_date", "pubDate", "priority_date", "grant_date", "grant_published",
                "register_updated", "opposition_deadline", "scheduled_grant", "decision_on",
                "registration_date", "expiry_date", "publication_date", "opposition_start",
                "opposition_end")
#  ", 72293 Glatten, DE": the postal address the German register appends to every name.
_ADDRESS = re.compile(r",\s*\d{4,}.*$")
_IPC = re.compile(r"\s*([A-H]\d\d[A-Z])\s*(\d+)\s*/\s*(\d+)")


def _people(value):
    """Names as the offices hand them over -> a list of names and nothing else.

    The DPMA sweep stored "Stockburger, Ralf, 72293 Glatten, DE; Hofer, Frank, 72172 Sulz, DE"
    as ONE string, and a template that joins a string joins its letters: the table printed
    "S, t, o, c, k" under a title. A list is returned as it is; a string is split on the
    register's separators and each name loses its address.
    """
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    out = []
    for part in re.split(r"\s*[;|]\s*", str(value or "")):
        part = _ADDRESS.sub("", part).strip(" ,")
        if part:
            out.append(part)
    return out


def _tidy_dates(row):
    """The sweep stores 20241119 from one office and 2024-11-19 from another, and the shipped
    file stores people as prose. The page prints one spelling of each."""
    for key in _DATE_FIELDS:
        d = observation_actions._date(row.get(key))
        if d:
            row[key] = d.isoformat()
    row["inventors"] = _people(row.get("inventors"))
    if not isinstance(row.get("applicants"), list):
        row["applicants"] = _people(row.get("applicant"))
    row["applicant_short"] = "; ".join(_people(row.get("applicant")))
    m = _IPC.match(str(row.get("ipc") or ""))
    if m:
        row["ipc"] = "%s %s/%s" % (m.group(1), m.group(2), m.group(3))
    return row


def cases_for(user_id, target_id, today=None, kind="patent"):
    """One target's rows of one kind: patents (the default and the rows from before kinds
    existed), designs or trademarks. Each kind has its own instrument table."""
    ensure_schema()
    today = today or datetime.date.today()
    kind = kind if kind in observation_marks.KINDS else "patent"
    with db.cursor(autocommit=True) as cur:
        cur.execute(
            """SELECT publication, payload, user_state, user_note
                 FROM app_observation_cases
                WHERE user_id = %s AND target_id = %s
                  AND COALESCE(payload->>'kind', 'patent') = %s""",
            (user_id, target_id, kind))
        rows = cur.fetchall()
    out = []
    for r in rows:
        row = dict(r["payload"])
        row["user_state"] = r["user_state"]
        row["user_note"] = r["user_note"]
        row.setdefault("kind", kind)
        _tidy_dates(row)
        #  What can still be filed against this one, evaluated for today. Cheap enough to do on
        #  every read, and the alternative is a stored answer that rots exactly like the count did.
        #  BEFORE `recount`, which now takes the row's countdown from whichever of these is open.
        if kind == "patent":
            row["actions"] = observation_actions.actions_for(row, today)
            row["action_headline"] = observation_actions.headline(row, today)
        else:
            row["actions"] = observation_marks.actions_for(row, today)
            row["action_headline"] = observation_marks.headline(row, today)
        recount(row, today)
        out.append(row)
    #  Soonest deadline first, undated cases last. `sort_key` is already the day count.
    out.sort(key=lambda r: (r.get("sort_key") if r.get("sort_key") is not None else 99999,
                            r.get("publication") or ""))
    return out


def filings_for(user_id):
    ensure_schema()
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT payload FROM app_observation_filings WHERE user_id = %s", (user_id,))
        rows = [dict(r["payload"]) for r in cur.fetchall()]
    order = {"prepared_not_filed": 0, "filed": 1}
    rows.sort(key=lambda f: (order.get(f.get("status"), 2), f.get("filed_on") or "9999"))
    return rows


def meta_for(user_id):
    ensure_schema()
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT payload FROM app_observation_meta WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    return dict(row["payload"]) if row else {}


def set_note(user_id, target_id, publication, note):
    """The one field a person owns on a row. Refuses a row that is not theirs, by construction:
    the WHERE clause carries the user id, so a mismatched publication updates nothing."""
    ensure_schema()
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(note or ""))[:MAX_NOTE_CHARS]
    with db.cursor(autocommit=True) as cur:
        cur.execute("UPDATE app_observation_cases SET user_note = %s, updated_at = now() "
                    "WHERE user_id = %s AND target_id = %s AND publication = %s",
                    (clean, user_id, target_id, publication))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------------------------

def _user():
    """A named account, never the loopback exemption. See the module docstring."""
    user = auth.current_user()
    if not user:
        if request.path.startswith("/api") or request.headers.get("Accept", "").startswith(
                "application/json"):
            abort(401)
        abort(403)
    return user


def _pick(targets, wanted):
    """The target the URL asks for, else the first one there is."""
    try:
        wanted = int(wanted)
    except (TypeError, ValueError):
        wanted = None
    for t in targets:
        if t["id"] == wanted:
            return t
    return targets[0] if targets else None


@bp.route("/actions")
def actions_page():
    user = _user()
    uid = user["id"]
    try:
        targets = targets_for(uid)
    except Exception:
        traceback.print_exc()
        targets = []
    target = _pick(targets, request.args.get("target"))
    #  Patents, designs or trademarks: one docket at a time, each with its own instruments.
    kind = (request.args.get("kind") or "patent").strip().lower()
    if kind not in observation_marks.KINDS:
        kind = "patent"
    cases, filings, meta = [], [], {}
    if target:
        try:
            cases = cases_for(uid, target["id"], kind=kind)
            filings = filings_for(uid)
            meta = meta_for(uid)
        except Exception:
            traceback.print_exc()
            cases, filings, meta = [], [], {}
    #  Which of the papers on each office file we can prove are ours. Needs both lists, so it
    #  happens here rather than in `cases_for`, which only ever sees one of them. Then only the
    #  filings that belong on this docket, and only the shipped docket carries the hand-written
    #  list of windows that were missed.
    attribute_filings(cases, filings)
    #  What the filing app has already built and what this app has already searched, per case.
    #  Pinned by application and publication number, never by family: the packet for the US
    #  member says nothing about the German one.
    observation_links.attach(cases, uid)
    seeded = bool(target and target.get("seeded"))
    filings = filings_on(cases, filings, everything=seeded)
    missed = list(meta.get("missed") or []) if seeded else []
    #  A design's stored view becomes an image URL the row and the panel can show. A mark's
    #  image, when TMview gave one, is an absolute URL already.
    if kind == "design":
        for c in cases:
            if observation_marks.image_file(c.get("publication")):
                c["image"] = url_for("observations.action_image", publication=c["publication"])
    #  Which package files actually exist on disk, so the page never offers a dead download.
    have = set(os.listdir(PACKAGE_DIR)) if os.path.isdir(PACKAGE_DIR) else set()
    for f in filings:
        f["package_available"] = bool(f.get("package")) and f["package"] in have
    #  THE CHIPS AND THE FILTER MUST AGREE. The urgency select offers "14 days or less" and "90
    #  days or less", which are nested bands; the chips used to be counted from the mutually
    #  exclusive `state` buckets and so reported a smaller number than the filter then showed.
    #  Count the bands the reader is actually offered.
    counts = {"total": len(cases)}
    for c in cases:
        counts[c.get("state", "open")] = counts.get(c.get("state", "open"), 0) + 1
    live = [c["days_left"] for c in cases if c.get("days_left") is not None and c["days_left"] >= 0]
    counts["within_14"] = sum(1 for n in live if n <= 14)
    counts["within_90"] = sum(1 for n in live if n <= 90)
    counts["submitted"] = sum(1 for c in cases if c.get("on_file") or c.get("filed"))
    #  Every case where something can be filed TODAY, whatever the office calls it.
    counts["actionable"] = sum(
        1 for c in cases
        if (c.get("action_headline") or {}).get("status") in ("open", "closing"))
    counts.update(observation_links.summary(cases))
    can_file = can_file_options(cases)
    #  THE EXPANDED ROW'S DATA, TRIMMED. The table row carries what you scan by; everything else
    #  is built on demand from this map by publication number. Only the fields the panel
    #  actually renders are serialised.
    detail = {c["publication"]: {k: c.get(k) for k in DETAIL_FIELDS} for c in cases}
    #  HOW OLD THE REGISTER FACTS ARE, said out loud. The countdowns are computed and cannot go
    #  stale, but the deadlines they count to can: a patent that granted last week opens a nine
    #  month opposition window this page knows nothing about until somebody presses the button.
    refresh = dict((target or {}).get("refresh", {}).get(kind) or {})
    refreshed_at = (target or {}).get("refreshed_at") if kind == "patent" else \
        str(refresh.get("refreshed_at") or "")[:16].replace("T", " ") or None
    stale_days = None
    if refreshed_at:
        pulled = observation_actions._date(str(refreshed_at)[:10])
        if pulled:
            stale_days = (datetime.date.today() - pulled).days
    job = observation_refresh.state(uid, target["id"], kind) if target else {}
    if kind == "patent":
        matrix, matrix_offices = observation_actions.reference_matrix(), observation_actions.REFERENCE_OFFICES
    else:
        matrix, matrix_offices = observation_marks.reference_matrix(kind)
    #  Filings and the hand-written missed list are patent things; the other two kinds have
    #  neither yet.
    if kind != "patent":
        filings, missed = [], []
    return render_template("actions.html", cases=cases, filings=filings, missed=missed,
                           meta=meta, counts=counts, detail=detail, can_file=can_file,
                           targets=targets, target=target, offices=OFFICES,
                           lookbacks=LOOKBACKS, default_lookback=DEFAULT_LOOKBACK,
                           job=job, stages=observation_actions.STAGES,
                           kind=kind, kinds=observation_marks.KINDS,
                           kind_label=observation_marks.KIND_LABEL,
                           refresh=refresh, refreshed_at=refreshed_at,
                           filing_url=observation_links.FILING_URL,
                           matrix=matrix, matrix_offices=matrix_offices,
                           stale_days=stale_days,
                           today=datetime.date.today().isoformat())


@bp.route("/observations")
def observations_redirect():
    """The page's old name. Links to it exist in mail, in counsel's notes and in two nginx
    configurations, so it keeps answering."""
    return redirect(url_for("observations.actions_page", **request.args.to_dict()), 301)


@bp.route("/observations/package/<path:name>")
def observation_package_redirect(name):
    return redirect(url_for("observations.action_package", name=name), 301)


def _body():
    return request.get_json(silent=True) or request.form.to_dict(flat=False) or {}


def _one(body, key):
    v = body.get(key)
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _many(body, key):
    v = body.get(key)
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


@bp.route("/api/actions/targets", methods=["POST"])
def api_target_create():
    """Add a target and start finding its cases. The page follows the job by polling the refresh
    state for the new target's id, exactly as it does for a refresh."""
    user = _user()
    auth.require_csrf()
    body = _body()
    try:
        target = create_target(user["id"], name=_one(body, "name") or "",
                               assignees=_many(body, "assignees"),
                               inventors=_many(body, "inventors"),
                               offices=_many(body, "offices"),
                               lookback=_one(body, "lookback_months"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)[:160]}), 500
    observation_refresh.start(user["id"], [], target=target)
    return jsonify({"ok": True, "target": target,
                    "state": observation_refresh.state(user["id"], target["id"])})


@bp.route("/api/actions/targets/<int:target_id>", methods=["POST"])
def api_target_update(target_id):
    user = _user()
    auth.require_csrf()
    body = _body()
    try:
        target = update_target(user["id"], target_id, name=_one(body, "name"),
                               assignees=_many(body, "assignees") if "assignees" in body else None,
                               inventors=_many(body, "inventors") if "inventors" in body else None,
                               offices=_many(body, "offices") if "offices" in body else None,
                               lookback=_one(body, "lookback_months"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)[:160]}), 500
    if not target:
        return jsonify({"ok": False, "error": "no such target"}), 404
    return jsonify({"ok": True, "target": target})


@bp.route("/api/actions/targets/<int:target_id>/delete", methods=["POST"])
def api_target_delete(target_id):
    user = _user()
    auth.require_csrf()
    try:
        ok, why = delete_target(user["id"], target_id)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)[:160]}), 500
    if not ok:
        return jsonify({"ok": False, "error": why or "not removed"}), 400
    return jsonify({"ok": True})


@bp.route("/api/actions/case", methods=["POST"])
def api_action_case():
    user = _user()
    auth.require_csrf()
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    pub = (body.get("publication") or "").strip()
    if not pub:
        return jsonify({"ok": False, "error": "publication is required"}), 400
    try:
        tid = int(body.get("target_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "target_id is required"}), 400
    try:
        ok = set_note(user["id"], tid, pub, body.get("note"))
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)[:160]}), 500
    if not ok:
        return jsonify({"ok": False, "error": "not on your docket"}), 404
    return jsonify({"ok": True})


@bp.route("/api/actions/refresh", methods=["POST"])
def api_action_refresh():
    """Go and ask the offices again about one target. One job per target, in the background.

    The work is a hundred-odd HTTP calls to three registers and it takes the best part of a
    minute, which is too long to hold a request open and far too long to hold a browser on a
    spinner with nothing to read. So the button starts a job and the page polls this route's GET
    twin for the count and the case it is on.
    """
    user = _user()
    auth.require_csrf()
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    target = get_target(user["id"], body.get("target_id"))
    if not target:
        return jsonify({"ok": False, "error": "no such target"}), 404
    kind = str(body.get("kind") or "patent").lower()
    if kind not in observation_marks.KINDS:
        return jsonify({"ok": False, "error": "unknown kind"}), 400
    try:
        rows = cases_for(user["id"], target["id"], kind=kind)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)[:160]}), 500
    started = observation_refresh.start(user["id"], rows, target=target, kind=kind)
    st = observation_refresh.state(user["id"], target["id"], kind)
    return jsonify({"ok": True, "already_running": not started, "state": st})


@bp.route("/api/actions/refresh", methods=["GET"])
def api_action_refresh_state():
    user = _user()
    try:
        tid = int(request.args.get("target"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "target is required"}), 400
    kind = str(request.args.get("kind") or "patent").lower()
    return jsonify({"ok": True, "state": observation_refresh.state(user["id"], tid, kind)})


@bp.route("/actions/image/<path:publication>")
def action_image(publication):
    """The stored first view of a design on the reader's docket. Served from disk, a day of
    caching, and only for a publication that is on one of their own targets."""
    user = _user()
    if "/" in publication or "\\" in publication or publication.startswith("."):
        abort(404)
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT 1 FROM app_observation_cases WHERE user_id = %s AND publication = %s LIMIT 1",
                    (user["id"], publication))
        if not cur.fetchone():
            abort(404)
    path = observation_marks.image_file(publication)
    if not path:
        abort(404)
    resp = send_from_directory(str(path.parent), path.name, max_age=86400)
    return resp


@bp.route("/actions/package/<path:name>")
def action_package(name):
    """One filed package, as a zip. Served only to somebody whose own docket references it."""
    user = _user()
    if "/" in name or "\\" in name or name.startswith("."):
        abort(404)
    wanted = {f.get("package") for f in filings_for(user["id"]) if f.get("package")}
    if name not in wanted:
        abort(404)
    if not os.path.isfile(os.path.join(PACKAGE_DIR, name)):
        abort(404)
    return send_from_directory(PACKAGE_DIR, name, as_attachment=True)


def init_app(app):
    """Register the docket. Called before `auth.init_app`, like every other blueprint here."""
    app.register_blueprint(bp)
    try:
        seed_owner()
    except Exception:
        #  A docket that cannot seed must not stop the search product booting.
        traceback.print_exc()
    return app
