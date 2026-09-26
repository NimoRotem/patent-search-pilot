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
import sys
import threading
import time
import traceback

from flask import (Blueprint, abort, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

import accounts
import auth
import db
import docket_files
import iptorch_packages
import observation_actions
import patent_cards
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
#  The hand-kept board of what to do next, read on every page load so that editing the file is
#  the whole deployment. A missing file is a page without a board, never an error.
BOARD_PATH = os.path.join(DATA_DIR, "board.json")

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
    #  The three stages a package goes through, kept apart on purpose: every zip built for the
    #  case (iptorch.com's among them), what was actually submitted to the office, and what the
    #  office itself now shows publicly, as the office words it. See `stages`.
    "iptorch", "submitted", "public", "stage", "built_count", "built_latest", "office_blind",
    #  Designs and marks: what a row of those kinds carries that a patent row does not.
    "kind", "status", "registration", "registration_date", "expiry_date", "publication_date",
    "opposition_start", "opposition_end", "classes", "mark_type", "image", "oppositions",
    "cancellations", "designated", "deferred",
    #  The German register's own event list. It is the evidence behind every German posture and
    #  deadline on this page: "granted 2026-08-20" is an assertion until you can see the R018 and
    #  the B4 it was read off.
    "register_events",
    #  What used to sit in two sections above the table, now on the rows themselves: the hand-kept
    #  action list's entries for the case, a window we missed on it, filings prepared and never
    #  handed over, and, for a row the docket itself does not have, why it is here at all.
    "boards", "missed", "not_filed", "extra", "extra_note",
    #  The first drawing and the abstract, from patent_cards.
    "abstract",
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
    #  A target's guards against namesakes (see observation_refresh.owner_matches): names that
    #  only look like it, and whether a private person with the same surname counts.
    "ALTER TABLE app_observation_targets ADD COLUMN IF NOT EXISTS exclude jsonb NOT NULL "
    "DEFAULT '[]'::jsonb",
    "ALTER TABLE app_observation_targets ADD COLUMN IF NOT EXISTS companies_only boolean NOT NULL "
    "DEFAULT false",
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
    for key in ("assignees", "inventors", "offices", "exclude"):
        v = t.get(key)
        t[key] = list(v) if isinstance(v, (list, tuple)) else []
    t["companies_only"] = bool(t.get("companies_only"))
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


def create_target(user_id, name="", assignees=(), inventors=(), offices=(), lookback=None,
                  exclude=(), companies_only=False):
    """A new target for this person. Raises ValueError with a sentence the form can show."""
    ensure_schema()
    assignees = _clean_names(assignees)
    inventors = _clean_names(inventors)
    exclude = _clean_names(exclude)
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
                 (user_id, name, assignees, inventors, offices, lookback_months, exclude,
                  companies_only)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (user_id, name, json.dumps(assignees), json.dumps(inventors), json.dumps(offices),
             lookback, json.dumps(exclude), bool(companies_only)))
        tid = cur.fetchone()["id"]
    return get_target(user_id, tid)


def update_target(user_id, target_id, name=None, assignees=None, inventors=None, offices=None,
                  lookback=None, exclude=None, companies_only=None):
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
    new_exclude = _clean_names(exclude) if exclude is not None else current["exclude"]
    new_companies = bool(companies_only) if companies_only is not None else current["companies_only"]
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT id FROM app_observation_targets WHERE user_id = %s "
                    "AND lower(name) = lower(%s) AND id <> %s", (user_id, new_name, current["id"]))
        if cur.fetchone():
            raise ValueError("There is already a target called %s." % new_name)
        cur.execute(
            """UPDATE app_observation_targets
                  SET name = %s, assignees = %s, inventors = %s, offices = %s,
                      lookback_months = %s, exclude = %s, companies_only = %s, updated_at = now()
                WHERE user_id = %s AND id = %s""",
            (new_name, json.dumps(new_assignees), json.dumps(new_inventors),
             json.dumps(new_offices), new_lookback, json.dumps(new_exclude), new_companies,
             user_id, current["id"]))
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
    ours, by_key = set(), {}
    for f in filings or []:
        for key in ("target", "application"):
            value = re.sub(r"[^A-Z0-9]", "", str(f.get(key) or "").upper())
            if value:
                ours.add(value)
                by_key.setdefault(value, []).append(f)
    for c in cases:
        keys = {re.sub(r"[^A-Z0-9]", "", str(c.get(k) or "").upper())
                for k in ("publication", "granted_as", "application")}
        mine = bool(keys & ours)
        on_file = []
        for entry in list(c.get("our_submissions") or []) + list(c.get("file_events") or []):
            entry = dict(entry)
            entry["whose"] = "ours" if (mine and entry.get("whose") != "unknown") else "unknown"
            #  THE OFFICE PUT THIS ONE THERE. Both of these lists are read off a file wrapper or a
            #  register, which is what makes them public; the entries added below are our own
            #  receipts, which are not. Nothing downstream can tell them apart without saying so.
            entry["origin"] = "office"
            on_file.append(entry)
        #  OUR OWN RECEIPT IS EVIDENCE UNTIL THE OFFICE PUBLISHES ITS OWN. A file wrapper carries
        #  a third-party paper days after the office took it: two submissions filed and paid on
        #  2026-09-05 were still absent from ODP on the 9th, so this column said nothing had ever
        #  been filed on a case we had filed on twice. A filing of ours whose date the office has
        #  not published yet is shown from our record, and drops out again the moment the wrapper
        #  carries that same date itself.
        seen, mine_rows = {str(e.get("date") or "")[:10] for e in on_file}, []
        for key in sorted(keys - {""}):
            for f in by_key.get(key, []):
                if f not in mine_rows:
                    mine_rows.append(f)
        #  AN ANONYMOUS PAPER ON THE REGISTER, DATED THE DAY WE FILED ONE, IS OURS. An Art. 115
        #  observation is filed without a name and the EPO lists it as nobody's, so the register
        #  step for our own EP filing of 2026-09-05 read "3rd party" on the very row whose
        #  receipt we hold. Only an exact date match claims it, and only when our own record says
        #  that filing went in.
        filed_dates = {str(f.get("filed_on") or "")[:10] for f in mine_rows
                       if f.get("status") in ("filed", "posted")} - {""}
        for entry in on_file:
            if str(entry.get("date") or "")[:10] in filed_dates:
                entry["whose"] = "ours"
                entry["source"] = ("the office lists it without a name; our own receipt for that "
                                   "day says it is ours")
        for f in mine_rows:
            when = str(f.get("filed_on") or "")[:10]
            if not when or when in seen or f.get("status") not in ("filed", "posted"):
                continue
            seen.add(when)
            on_file.append({
                "date": when, "whose": "ours", "origin": "receipt",
                "instrument": f.get("route_label") or "Third-party submission",
                "documents": f.get("references") or 0,
                "source": "our own receipt; the register has not published it yet",
                "evidence": f.get("evidence") or ""})
        on_file.sort(key=lambda e: e.get("date") or "", reverse=True)
        c["on_file"] = on_file
        #  Our own filing records for this case, whatever their state, for `stages`.
        c["our_filings"] = mine_rows
    return cases


#  What an office shows of a third party's paper, when it shows nothing, said in the cell rather
#  than left as a dash that reads "not public yet".
OFFICE_BLIND = {"DPMA": "DPMA does not publish these", "WIPO (PCT)": "PATENTSCOPE only"}
#  Except an opposition, which every register records. A filed Einspruch is watched for on the
#  German legal status rather than written off as invisible.
_OPPOSITION = re.compile(r"opposition|opposed|einspruch", re.I)
STAGE_RANK = {"public": 4, "submitted": 3, "handed": 2, "built": 1, "none": 0}


#  Everything the filings table used to print for one filing, carried onto its row's stage 2.
FILING_FIELDS = ("id", "title", "target", "application", "target_owner", "office", "status",
                 "route_label", "filed_on", "handed_on", "handed_times", "handed_note",
                 "handed_again_on", "entered_on", "acknowledged_on", "window_was", "counsel",
                 "references", "references_list", "official_fee_usd", "counsel_fee_usd", "payments",
                 "report", "detail", "outcome", "evidence", "how", "package", "package_note",
                 "packet_note", "packet_url", "public")


def filing_view(f):
    out = {k: f.get(k) for k in FILING_FIELDS if f.get(k) not in (None, "", [])}
    receipts = f.get("receipts")
    out["receipts"] = receipts if isinstance(receipts, list) else (receipts or 0)
    return out


def _keys_of(record, fields):
    return {re.sub(r"[^A-Z0-9]", "", str(record.get(k) or "").upper()) for k in fields} - {""}


def attach_missed(cases, missed):
    """Pin each hand-written missed window to its row, by number, as the filings are."""
    for c in cases:
        keys = _keys_of(c, ("publication", "granted_as", "application"))
        c["missed"] = [dict(m) for m in missed or [] if _keys_of(m, ("target", "application")) & keys]
    return cases


def extra_rows(cases, filings, missed, board):
    """Rows for what the docket has no row for and the page must still show: a filing whose
    patent no target tracks (the Nguyen family), a missed window with no row, and an action on
    the list that names no docket patent. They sit in the same table, marked, and count in none
    of the totals above it."""
    out = []
    attached = {str(f.get("id")) for c in cases for f in c.get("our_filings") or []}
    for f in filings or []:
        if str(f.get("id")) in attached:
            continue
        pub = str(f.get("target") or f.get("application") or f.get("id") or "")
        #  No register sweep reads a patent no target tracks, so the office's side is the hand
        #  check kept on the filing itself.
        seen = f.get("public") or {}
        on_file = [{"date": str(f.get("filed_on") or "")[:10], "whose": "ours", "origin": "office",
                    "instrument": f.get("route_label") or "Third-party submission",
                    "office_source": seen.get("note") or "checked by hand on the office's file",
                    "record_url": seen.get("url") or ""}] if seen.get("state") == "published" else []
        out.append({"publication": pub, "application": f.get("application") or "",
                    "title": f.get("title") or pub, "title_full": f.get("title") or pub,
                    "applicant": f.get("target_owner") or "", "applicant_short": f.get("target_owner") or "",
                    "office": f.get("office") or "", "kind": "patent", "extra": "filing",
                    "extra_note": "Filed by us, on a patent no target on this docket tracks.",
                    "register_url": (f.get("public") or {}).get("url") or "",
                    "our_filings": [f], "on_file": on_file, "actions": [], "can_keys": [],
                    "days_left": None, "sort_key": 99999})
    have_rows = {k for c in cases for k in _keys_of(c, ("publication", "granted_as", "application"))}
    for m in missed or []:
        if _keys_of(m, ("target", "application")) & have_rows:
            continue
        pub = str(m.get("target") or m.get("application") or "")
        out.append({"publication": pub, "application": m.get("application") or "",
                    "title": m.get("title") or pub, "kind": "patent", "extra": "missed",
                    "extra_note": "A window we missed, on a patent this docket has no row for.",
                    "missed": [dict(m)], "actions": [], "can_keys": [], "days_left": None,
                    "sort_key": 99999})
    for e in (board or {}).get("entries") or []:
        if e.get("on_docket"):
            continue
        view = board_view(e)
        out.append({"publication": "action-%s" % e.get("n"), "title": e.get("title") or "",
                    "title_full": e.get("title") or "", "kind": "patent", "extra": "board",
                    "extra_note": ("An action on the list that names no patent on this docket"
                                   + (": " + ", ".join(e.get("pubs")) if e.get("pubs") else "") + "."),
                    "boards": [view], "board": {"n": e.get("n"), "title": e.get("title")},
                    "deadline": view["deadline"], "days_left": view["days_left"],
                    "state": "lapsed" if (view["days_left"] is not None and view["days_left"] < 0) else "open",
                    "actions": [], "can_keys": [],
                    "sort_key": view["days_left"] if view["days_left"] is not None else 99999})
    return out


def stages(cases, have=()):
    """Split what has been done on each case into the three stages a package goes through.

    1. BUILT. Every package made for the case: iptorch.com's zips (one kept per build, see
       iptorch_packages), the filing app's packets and this app's own search zips. Building one
       puts nothing in front of anybody.
    2. SUBMITTED. What went to the office, from our own records: the filing ledger and the filing
       app's receipts. A package handed to the filing app and not filed yet is listed here too,
       flagged, because that is somebody's job today. This is our word, not the office's.
    3. PUBLIC. What the office's own file wrapper or register now shows, exactly as the office
       lists it. Only this is something an examiner or the other side can see.

    These used to share two columns. "On file" mixed our own receipts with the register's
    entries and "Prepared" mixed packets with searches, so a zip built for a case and a paper the
    examiner already has could read the same at a glance.
    """
    for c in cases:
        packets = [p for p in (c.get("packages") or []) if not p.get("demo")]
        zips = [s for s in (c.get("searches") or []) if s.get("concise")]
        c["built_count"] = len(c.get("iptorch") or []) + len(packets) + len(zips)
        dates = ([str(v.get("built_at") or "")[:10] for v in c.get("iptorch") or []]
                 + [str(p.get("created") or "")[:10] for p in packets]
                 + [str(s.get("when") or "")[:10] for s in zips])
        c["built_latest"] = max([d for d in dates if d] or [""])
        subs, packet_ids = [], set()
        #  Prepared and never handed over is stage 1, not 2, but the record of it is kept on the
        #  row: the filings table used to be the only place it was said.
        c["not_filed"] = [filing_view(f) for f in c.get("our_filings") or []
                          if str(f.get("status") or "") not in ("filed", "posted", "handed_not_filed")]
        for f in c.get("our_filings") or []:
            st = str(f.get("status") or "")
            state = "filed" if st in ("filed", "posted") else ("handed" if st == "handed_not_filed" else "")
            if not state:
                continue
            if f.get("packet_id"):
                packet_ids.add(str(f["packet_id"]))
            receipts = f.get("receipts")
            subs.append({
                "state": state, "posted": st == "posted",
                "date": str((f.get("filed_on") if state == "filed" else f.get("handed_on")) or "")[:10],
                "label": (f.get("route_label") or iptorch_packages.INSTRUMENT_LABEL.get(f.get("route"))
                          or f.get("route") or "Submission"),
                "how": f.get("how") or "", "evidence": f.get("evidence") or "",
                "references": f.get("references") or 0,
                "receipts": len(receipts) if isinstance(receipts, list) else (receipts or 0),
                "package": f.get("package") if f.get("package") in have else "",
                "packet_url": f.get("packet_url") or "",
                "filing": filing_view(f),
                "id": f.get("id") or "", "source": "our filing record"})
        for p in packets:
            if p.get("id") in packet_ids or p.get("state") not in ("filed", "handed off"):
                continue
            packet_ids.add(p.get("id"))
            subs.append({
                "state": "filed" if p["state"] == "filed" else "handed",
                "date": str(p.get("filing_date") or p.get("created") or "")[:10],
                "label": p.get("label") or "Packet", "confirmation": p.get("confirmation") or "",
                "receipts": p.get("receipts") or 0, "evidence": p.get("outcome") or "",
                "packet_url": p.get("url") or "", "id": p.get("id") or "", "source": "filing app"})
        for v in c.get("iptorch") or []:
            sent = v.get("sent") or {}
            if not sent or sent.get("id") in packet_ids:
                continue
            packet_ids.add(sent.get("id"))
            there = next((p for p in (c.get("packages") or []) if p.get("id") == sent.get("id")), None)
            subs.append({
                "state": "handed", "date": str(sent.get("at") or "")[:10],
                "label": "Sent from iptorch.com to the filing app",
                "by": sent.get("by") or "", "packet_url": sent.get("url") or "",
                "evidence": ("The filing app shows it as %s, not filed." % there["state"]) if there
                            else "Not filed by the filing app.",
                "id": sent.get("id") or "", "source": "iptorch.com", "version": v.get("stamp")})
        #  The shipped docket marks a few rows filed by hand, from before any of the records
        #  above existed. That is still our word that it went in.
        if c.get("filed") and not any(s["state"] == "filed" for s in subs):
            subs.append({"state": "filed", "date": str(c.get("filed_on") or "")[:10],
                         "label": "Marked filed on the docket", "evidence": "",
                         "source": "docket"})
        #  THE ACTION LIST'S WORD COUNTS TOO. The German opposition faxed on 2026-09-18 has no
        #  filing record, only its entry on the action list, and without this its row read "not
        #  submitted" beside a list that said filed.
        for b in c.get("boards") or []:
            if b.get("state") in ("filed", "posted") and not any(s["state"] == "filed" for s in subs):
                subs.append({"state": "filed", "posted": b["state"] == "posted",
                             "date": b.get("filed_date") or "",
                             "label": "Action %s: %s" % (b.get("n"), b.get("title") or ""),
                             "evidence": b.get("state_label") or "",
                             "package": b.get("package") if b.get("package_available") else "",
                             "id": "action-%s" % b.get("n"), "source": "the action list"})
        subs.sort(key=lambda s: s["date"] or "", reverse=True)
        c["submitted"] = subs
        #  Stage 3 is ONLY what the office put there. Our own receipts sit in `on_file` too, as
        #  evidence until the register catches up, and they belong to stage 2.
        public = [e for e in (c.get("on_file") or []) if e.get("origin") == "office"]
        public.sort(key=lambda e: (e.get("whose") == "ours", e.get("date") or ""), reverse=True)
        c["public"] = public
        if any(e.get("whose") == "ours" for e in public):
            c["stage"] = "public"
        elif any(s["state"] == "filed" for s in subs):
            c["stage"] = "submitted"
        elif subs:
            c["stage"] = "handed"
        elif c["built_count"]:
            c["stage"] = "built"
        else:
            c["stage"] = "none"
        opposed = any(_OPPOSITION.search("%s %s" % (s.get("label"), s.get("id"))) for s in subs
                      if s["state"] == "filed")
        c["office_blind"] = "" if opposed else OFFICE_BLIND.get(c.get("office") or "", "")
    return cases


def docket_keys(user_id):
    """Every publication key on any of this person's docket rows, all targets and kinds."""
    keys = set()
    with db.cursor(autocommit=True) as cur:
        cur.execute("""SELECT publication, payload->>'granted_as' AS g, payload->>'patent_number' AS p
                         FROM app_observation_cases WHERE user_id = %s""", (user_id,))
        for r in cur.fetchall():
            for v in (r["publication"], r["g"], r["p"]):
                keys |= observation_links.pub_keys(v)
    keys.discard("")
    return keys


def refresh_public(filings, cases):
    """Whether the OFFICE now shows each of our filings, as against our own receipt for it.

    A RECEIPT IS NOT A PUBLIC RECORD. Ours proves we filed and pays for nothing else; the file
    wrapper or the register is what an examiner, an applicant or a court will see, and it appears
    days later. The July submission was public the same day, the 2026-09-05 ones were still
    invisible five days on, and reading "filed" as "on the record" is how a docket ends up
    asserting something the other side cannot see. So the state is recomputed here from whatever
    the last register sweep actually found, and the line stored on the filing is only what
    somebody last checked by hand: a hand-verified `published` is never downgraded by a sweep
    that has not run since.
    """
    by_key = {}
    for c in cases:
        keys = observation_links.pub_keys(c.get("publication"))
        keys |= observation_links.pub_keys(c.get("granted_as"))
        keys |= {observation_links.app_key(c.get("application"))}
        for k in keys - {""}:
            by_key.setdefault(k, c)
    for f in filings:
        stored = dict(f.get("public") or {})
        keys = observation_links.pub_keys(f.get("target"))
        keys |= {observation_links.app_key(f.get("application"))}
        case = None
        for k in sorted(keys - {""}):
            case = case or by_key.get(k)
        when = str(f.get("filed_on") or "")[:10]
        hit = None
        for entry in ((case or {}).get("on_file") or []):
            if str(entry.get("date") or "")[:10] == when and entry.get("origin") == "office":
                hit = entry
                break
        if hit:
            stored["state"] = "published"
            stored["as_of"] = (case or {}).get("refreshed_at") or stored.get("checked")
            stored["documents"] = hit.get("documents") or stored.get("documents")
            stored["evidence"] = hit.get("evidence") or stored.get("evidence") or ""
            stored["url"] = stored.get("url") or (case or {}).get("register_url") or ""
        elif stored.get("state") != "published":
            stored["state"] = stored.get("state") or ("not_yet" if when else "")
            if case:
                stored["as_of"] = case.get("refreshed_at") or stored.get("checked")
                stored["url"] = stored.get("url") or case.get("register_url") or ""
        if stored:
            #  A filing whose case is not on this docket at all, the Nguyen family for one, has
            #  no sweep to date it from. What somebody last checked by hand is then the date.
            stored.setdefault("as_of", stored.get("checked"))
            f["public"] = stored
    return filings


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
    #  WHAT IS NOT FILED COMES FIRST. A packet handed to a filing agent and never uploaded is
    #  the one row here that is somebody's job today; everything filed is history.
    order = {"handed_not_filed": 0, "prepared_not_filed": 1, "posted": 2, "filed": 3}
    rows.sort(key=lambda f: (order.get(f.get("status"), 4), f.get("filed_on") or "9999"))
    return rows


def meta_for(user_id):
    ensure_schema()
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT payload FROM app_observation_meta WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    return dict(row["payload"]) if row else {}


#  What a board entry's state means for the eye: red for a finished packet nobody has filed,
#  amber for work that has to be written, green for what is done, grey for what is only watched.
BOARD_STATE_CLASS = {"handed": "todo", "nothing": "prep", "part": "prep",
                     "filed": "done", "posted": "done", "monitor": "watch"}


def load_board():
    """The board file, or nothing at all when there is none. Never raises: a malformed board
    must cost the reader a panel, not the docket."""
    try:
        with open(BOARD_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


BOARD_FIELDS = ("n", "title", "deadline", "days_left", "urgency", "state", "state_label",
                "state_class", "due_note", "cost", "what", "verify", "links", "packet_url",
                "packet_url2", "package", "package_available", "package2", "package2_available")


def board_view(item):
    """One action-list entry as its row's panel shows it. `filed_date` is read off the state
    line ("Faxed 2026-09-18 ..."), because the list keeps a due date and no filing date."""
    out = {k: item.get(k) for k in BOARD_FIELDS}
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(item.get("state_label") or ""))
    out["filed_date"] = m.group(0) if (m and item.get("state") in ("filed", "posted")) else ""
    return out


def board_for(cases, today=None, have=None):
    """The board with its countdowns computed for today, and every docket row it names marked.

    THE BOARD IS HAND-KEPT AND THE DOCKET IS A SWEEP. They answer different questions: the sweep
    knows what each register said this morning, the board says which ten of two hundred rows are
    worth somebody's week and what is already done about them. Neither derives from the other, so
    they are pinned together by publication number, the same way a packet is.

    A COUNTDOWN IS COMPUTED HERE TOO. `due` is a date in the file and nothing else; the days are
    counted on every read, for the same reason the rows are.
    """
    today = today or datetime.date.today()
    data = load_board()
    entries = data.get("entries")
    if not isinstance(entries, list):
        return {}
    #  One row per number, so a case that two entries name is chipped by the first, which is the
    #  more urgent one: the file is written in the order the work has to be done.
    by_key = {}
    for c in cases:
        for field in ("publication", "granted_as"):
            for key in observation_links.pub_keys(c.get(field)):
                by_key.setdefault(key, c)
    out = []
    for raw in entries:
        item = dict(raw)
        due = observation_actions._date(item.get("due"))
        item["deadline"] = due.isoformat() if due else None
        n = (due - today).days if due else None
        item["days_left"] = n
        item["urgency"] = ("lapsed" if n is not None and n < 0 else
                           "urgent" if n is not None and n <= 14 else
                           "closing" if n is not None and n <= 30 else
                           "soon" if n is not None and n <= 90 else "open")
        item["state_class"] = BOARD_STATE_CLASS.get(item.get("state"), "watch")
        for name in ("package", "package2"):
            item[name + "_available"] = bool(item.get(name)) and have is not None \
                and item[name] in have
        rows = 0
        view = board_view(item)
        for pub in item.get("pubs") or []:
            for key in observation_links.pub_keys(pub):
                case = by_key.get(key)
                if case is not None:
                    case.setdefault("board", {"n": item["n"], "title": item["title"],
                                              "urgency": item["urgency"]})
                    #  Every entry that names the row, not only the first: DE 10 2021 119 687 B4
                    #  is both the opposition that was filed and a case still to work.
                    mine = case.setdefault("boards", [])
                    if not any(b["n"] == view["n"] for b in mine):
                        mine.append(dict(view))
                    rows += 1
                    break
        item["on_docket"] = rows
        out.append(item)
    data = dict(data)
    data["entries"] = out
    return data


#  THE TOP OF THE PAGE IS EVERY COMPANY AT ONCE. Whichever target the table below shows, the strip
#  above it lists what shuts within this many days on ANY target and ANY kind (patent, design,
#  trademark), plus the dated items on the action list.
URGENT_DAYS = int(os.environ.get("ACTIONS_URGENT_DAYS", "15"))
#  The strip reads every target and kind on every load, which with sixteen targets doubled the
#  page time. The OTHER dockets' cards are kept for a few minutes, keyed on the day and on when
#  that docket was last read from the registers, so a refresh or a new day recomputes them.
URGENT_TTL = int(os.environ.get("ACTIONS_URGENT_TTL", "600"))
_URGENT_CACHE = {}
_URGENT_LOCK = threading.Lock()


def _soonest(c, horizon):
    """(days, instrument, fee, more) for the first door this row shuts within `horizon`, or None."""
    live = [a for a in c.get("actions") or [] if a.get("status") in ("open", "closing")
            and not a.get("weak") and a.get("days_left") is not None and 0 <= a["days_left"] <= horizon]
    live.sort(key=lambda a: a["days_left"])
    n = c.get("days_left")
    if live and (n is None or n < 0 or live[0]["days_left"] <= n):
        a = live[0]
        return a["days_left"], a.get("instrument") or a.get("stage_label") or "", a.get("fee") or "", len(live) - 1
    if n is not None and 0 <= n <= horizon and c.get("state") != "lapsed":
        head = c.get("action_headline") or {}
        return n, head.get("label") or "window closes", head.get("fee") or "", max(len(live) - 1, 0)
    return None


def _us_app(c):
    m = re.search(r"patentcenter\.uspto\.gov/applications/(\d{6,9})", str(c.get("register_url") or ""))
    if m:
        return m.group(1)
    if c.get("office") == "USPTO" and c.get("application"):
        return re.sub(r"\D", "", str(c["application"]))
    return ""


def _card(c, t, kind, days, what, fee, more, here):
    v0 = (c.get("iptorch") or [None])[0]
    return {"kind": kind, "tab": kind, "target_id": t["id"], "company": t.get("name") or "",
            "office": c.get("office") or "", "pub": c.get("publication") or "",
            "number": c.get("granted_as") or c.get("registration") or c.get("publication") or "",
            "title": c.get("title") or "", "days": days, "date": c.get("deadline") or "",
            "what": what, "fee": fee, "more": more, "here": here,
            "built": c.get("built_count") or 0, "stage": c.get("stage") or "none",
            "boards": [b.get("n") for b in c.get("boards") or []],
            "us_app": _us_app(c) if kind == "patent" else "",
            "office_url": "" if _us_app(c) else (c.get("register_url") or ""),
            "package": {"slug": v0["slug"], "stamp": v0["stamp"]} if v0 else None,
            "context": (c.get("closing_note") or c.get("next_action") or "")[:400]}


def urgent_items(user_id, targets, current=None, filings=None, have=(), with_iptorch=False,
                 horizon=URGENT_DAYS):
    """What shuts within `horizon` days on every target and kind this person watches.

    -> {"items": [card], "anyday": [card], "horizon": n}. `current` is (target_id, kind, rows)
    for the docket the page is already showing, so its rows are not read twice. Rows of every
    other docket are read the same way the page reads its own, then given the same stages.
    """
    today = datetime.date.today()
    items, anyday, acted = [], [], {}
    for t in targets or []:
        for kind in observation_marks.KINDS:
            here = bool(current and current[0] == t["id"] and current[1] == kind)
            key = None
            if not here:
                stamp = ((t.get("refresh") or {}).get(kind) or {}).get("refreshed_at") or ""
                key = (user_id, t["id"], kind, today.isoformat(), str(stamp),
                       str(t.get("refreshed_at")), horizon, bool(with_iptorch))
                with _URGENT_LOCK:
                    hit = _URGENT_CACHE.get(key)
                if hit and time.time() - hit[0] < URGENT_TTL:
                    items.extend(dict(i) for i in hit[1])
                    anyday.extend(dict(i) for i in hit[2])
                    continue
            n_items, n_any = len(items), len(anyday)
            if here:
                rows = current[2]
            else:
                try:
                    rows = cases_for(user_id, t["id"], today=today, kind=kind)
                    attribute_filings(rows, filings or [])
                    if with_iptorch and kind == "patent":
                        iptorch_packages.attach(rows)
                    if t.get("seeded") and kind == "patent":
                        board_for(rows, today=today, have=set(have))
                    stages(rows, have)
                except Exception:
                    traceback.print_exc()
                    continue
            for c in rows:
                if c.get("extra"):
                    continue
                hit = _soonest(c, horizon)
                if hit:
                    items.append(_card(c, t, kind, hit[0], hit[1], hit[2], hit[3], here))
                elif c.get("days_left") is None and c.get("closing_soon") and c.get("state") != "lapsed":
                    head = c.get("action_headline") or {}
                    anyday.append(_card(c, t, kind, None, head.get("label") or "could close any day",
                                        head.get("fee") or "", 0, here))
                #  The action list keeps its own dates (confirm a fee, post an original), which are
                #  not register windows and can be the sooner of the two.
                for b in c.get("boards") or []:
                    n = b.get("days_left")
                    if n is not None and 0 <= n <= horizon and not (hit and hit[0] == n):
                        #  One card per action, however many patents it names.
                        key = (t["id"], b.get("n"))
                        if key in acted:
                            acted[key]["more"] += 1
                            continue
                        card = _card(c, t, kind, n, "Action %s: %s" % (b.get("n"), b.get("title") or ""),
                                     "", 0, here)
                        acted[key] = card
                        card.update(kind="action", date=b.get("deadline") or "",
                                    context=(b.get("due_note") or b.get("what") or "")[:400])
                        items.append(card)
            if key is not None:
                with _URGENT_LOCK:
                    _URGENT_CACHE[key] = (time.time(), [dict(i) for i in items[n_items:]],
                                          [dict(i) for i in anyday[n_any:]])
    items.sort(key=lambda i: (i["days"], i["kind"] != "action"))
    anyday.sort(key=lambda i: (i["company"], i["kind"], i["number"]))
    return {"items": items, "anyday": anyday, "horizon": horizon,
            "companies": len({i["target_id"] for i in items})}


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
    """Whose docket this request is for. A named account, never the loopback exemption.

    Normally that is the signed-in account. A GUEST account (outside counsel, given this page
    and nothing else) carries `docket_user_id` naming the account whose docket it works on, and
    is handed that row instead: every query below keys on `user["id"]`, so a guest returning
    their own id would be shown an empty docket, which is indistinguishable from a wiped one.
    What a guest may not do is change the docket's SHAPE; see `_no_guests`.
    """
    user = auth.current_user()
    if not user:
        if request.path.startswith("/api") or request.headers.get("Accept", "").startswith(
                "application/json"):
            abort(401)
        abort(403)
    host_id = auth.docket_owner_id()
    if host_id and host_id != int(user["id"]):
        host = accounts.get_user(host_id)
        if not host or not host.get("is_active"):
            abort(403)
        return host
    return user


def _no_guests():
    """Refuse a guest the routes that define the docket rather than work it.

    Adding, renaming and deleting a target is the owner's decision about what is watched, and a
    guest's writes land on the OWNER's rows (see `_user`), so a deletion here would be somebody
    else's docket disappearing. Notes, refreshes and downloads stay open: that is the work the
    page was shared for.
    """
    if auth.current_scope():
        abort(403)


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
    all_filings = []
    if target:
        try:
            cases = cases_for(uid, target["id"], kind=kind)
            filings = filings_for(uid)
            all_filings = list(filings)
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
    #  Every package our own accounts built on iptorch.com, kept here one zip per build. Only on
    #  the owner's docket (a guest is handed that docket, so counsel sees them too), and a sync
    #  is asked for in the background so a package built a minute ago shows on the next load.
    iptorch_on = iptorch_packages.visible_to(user)
    unmatched = []
    if iptorch_on:
        iptorch_packages.start_background()
        iptorch_packages.kick()
        try:
            iptorch_packages.attach(cases)
            if kind == "patent":
                unmatched = iptorch_packages.unmatched(docket_keys(uid))
        except Exception:
            traceback.print_exc()
    seeded = bool(target and target.get("seeded"))
    refresh_public(filings, cases)
    filings = filings_on(cases, filings, everything=seeded)
    missed = list(meta.get("missed") or []) if seeded else []
    #  A design's stored view becomes an image URL the row and the panel can show. A mark's
    #  image, when TMview gave one, is an absolute URL already.
    if kind == "design":
        for c in cases:
            if observation_marks.image_file(c.get("publication")):
                c["image"] = url_for("observations.action_image", publication=c["publication"])
    #  A patent is read by its first drawing and two lines of its abstract (patent_cards), kept
    #  once per publication. The ones not read yet are fetched in the background, so the next
    #  load has them; the daily check fills the rest.
    if kind == "patent":
        cards = patent_cards.load()
        for c in cases:
            card = cards.get(c.get("publication")) or {}
            if card.get("abstract"):
                c["abstract"] = card["abstract"]
            if card.get("image") and observation_marks.image_file(c.get("publication")):
                c["image"] = url_for("observations.action_image", publication=c["publication"])
        if "pytest" not in sys.modules:
            patent_cards.kick([{"publication": c.get("publication"), "application": c.get("application")}
                               for c in cases if c.get("publication")])
    #  Which package files actually exist on disk, so the page never offers a dead download.
    have = set(os.listdir(PACKAGE_DIR)) if os.path.isdir(PACKAGE_DIR) else set()
    for f in filings:
        f["package_available"] = bool(f.get("package")) and f["package"] in have
    #  ONE TABLE. The hand-kept action list, the filings table and the missed windows used to be
    #  three sections above the docket; each is now pinned to the rows it is about, before the
    #  stages are worked out, because an action that says "filed" is stage 2 on its row. Only on
    #  the shipped docket and only for patents: the list is written about this target's cases.
    board = board_for(cases, have=have) if (seeded and kind == "patent") else {}
    attach_missed(cases, missed)
    #  What has no row of its own (a filing on a patent no target tracks, an action naming no
    #  docket patent) becomes a marked row at the end of the same table, never a separate list.
    extras = extra_rows(cases, filings, missed, board) if kind == "patent" else []
    stages(cases + extras, have)
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
    counts["board"] = sum(1 for c in cases if c.get("board"))
    counts["stage_built"] = sum(1 for c in cases if c.get("built_count"))
    counts["stage_submitted"] = sum(1 for c in cases if c.get("stage") in ("submitted", "public"))
    counts["stage_public"] = sum(1 for c in cases if c.get("stage") == "public")
    can_file = can_file_options(cases)
    #  The strip at the top: what shuts within URGENT_DAYS on every target and every kind.
    try:
        urgent = urgent_items(uid, targets, current=(target["id"], kind, cases) if target else None,
                              filings=all_filings, have=have, with_iptorch=iptorch_on)
    except Exception:
        traceback.print_exc()
        urgent = {"items": [], "anyday": [], "horizon": URGENT_DAYS, "companies": 0}
    #  THE EXPANDED ROW'S DATA, TRIMMED. The table row carries what you scan by; everything else
    #  is built on demand from this map by publication number. Only the fields the panel
    #  actually renders are serialised.
    detail = {c["publication"]: {k: c.get(k) for k in DETAIL_FIELDS} for c in cases + extras}
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
    #  The daily check (actions_daily.py on cron): when it last ran and whether it was clean.
    try:
        import actions_daily
        daily = actions_daily.latest()
    except Exception:
        traceback.print_exc()
        daily = None
    if kind == "patent":
        matrix, matrix_offices = observation_actions.reference_matrix(), observation_actions.REFERENCE_OFFICES
    else:
        matrix, matrix_offices = observation_marks.reference_matrix(kind)
    #  Filings and the hand-written missed list are patent things; the other two kinds have
    #  neither yet.
    if kind != "patent":
        filings, missed = [], []
    return render_template("actions.html", cases=cases + extras, n_extra=len(extras), urgent=urgent,
                           board=board,
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
                           daily=daily, daily_at=os.environ.get("ACTIONS_DAILY_AT", "05:10 UTC"),
                           iptorch_on=iptorch_on, iptorch_unmatched=unmatched,
                           iptorch_sync=iptorch_packages.status() if iptorch_on else {},
                           iptorch_public=iptorch_packages.IPTORCH_PUBLIC,
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


def _flag(value):
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _many(body, key):
    v = body.get(key)
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


@bp.route("/api/actions/targets", methods=["POST"])
def api_target_create():
    """Add a target and start finding its cases. The page follows the job by polling the refresh
    state for the new target's id, exactly as it does for a refresh."""
    _no_guests()
    user = _user()
    auth.require_csrf()
    body = _body()
    try:
        target = create_target(user["id"], name=_one(body, "name") or "",
                               assignees=_many(body, "assignees"),
                               inventors=_many(body, "inventors"),
                               offices=_many(body, "offices"),
                               lookback=_one(body, "lookback_months"),
                               exclude=_many(body, "exclude"),
                               companies_only=_flag(_one(body, "companies_only")))
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
    _no_guests()
    user = _user()
    auth.require_csrf()
    body = _body()
    try:
        target = update_target(user["id"], target_id, name=_one(body, "name"),
                               assignees=_many(body, "assignees") if "assignees" in body else None,
                               inventors=_many(body, "inventors") if "inventors" in body else None,
                               offices=_many(body, "offices") if "offices" in body else None,
                               lookback=_one(body, "lookback_months"),
                               exclude=_many(body, "exclude") if "exclude" in body else None,
                               companies_only=(_flag(_one(body, "companies_only"))
                                               if "companies_only" in body else None))
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
    _no_guests()
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


def _allowed_packages(user_id):
    """Every filed package this person's docket references: their filing records, and on the
    shipped docket the action list too, which names the German opposition's zip and nothing
    else does."""
    wanted = {f.get("package") for f in filings_for(user_id) if f.get("package")}
    try:
        if any(t.get("seeded") for t in targets_for(user_id)):
            for e in load_board().get("entries") or []:
                wanted |= {e.get(k) for k in ("package", "package2") if e.get(k)}
    except Exception:
        traceback.print_exc()
    return wanted


@bp.route("/actions/package/<path:name>")
def action_package(name):
    """One filed package, as a zip. Served only to somebody whose own docket references it."""
    user = _user()
    if "/" in name or "\\" in name or name.startswith("."):
        abort(404)
    wanted = _allowed_packages(user["id"])
    if name not in wanted:
        abort(404)
    if not os.path.isfile(os.path.join(PACKAGE_DIR, name)):
        abort(404)
    return send_from_directory(PACKAGE_DIR, name, as_attachment=True)


@bp.route("/actions/iptorch/<slug>/<stamp>.zip")
def action_iptorch_zip(slug, stamp):
    """One kept iptorch.com package, the zip exactly as it was when that build finished.

    Only to the docket it is shown on: the owner's, and a guest working on it."""
    user = _user()
    if not iptorch_packages.visible_to(user):
        abort(404)
    found = iptorch_packages.zip_path(slug, stamp)
    if not found:
        abort(404)
    path, meta = found
    name = meta.get("download_name") or path.name
    #  The build time goes in the saved file's name, or two builds of one package land in the
    #  Downloads folder under one name and the second silently replaces the first.
    stem, dot, ext = name.rpartition(".")
    name = "%s_%s.%s" % (stem or name, stamp, ext or "zip")
    return send_from_directory(str(path.parent), path.name, as_attachment=True,
                               download_name=name, max_age=0)


# ---------------------------------------------------------------------------------------------
# the viewer: what is inside a package, and the USPTO file, without leaving the page
# ---------------------------------------------------------------------------------------------
#  Each source answers two routes: `files` (JSON, the list) and `file` or `doc` (one of them,
#  inline, or as a download with ?download=1). Every one repeats the check its zip download makes,
#  so the viewer can never show a file the reader could not have downloaded.

def _iptorch_found(slug, stamp):
    user = _user()
    if not iptorch_packages.visible_to(user):
        abort(404)
    found = iptorch_packages.zip_path(slug, stamp)
    if not found:
        abort(404)
    return found


def _package_path(name):
    user = _user()
    if "/" in name or "\\" in name or name.startswith("."):
        abort(404)
    wanted = _allowed_packages(user["id"])
    path = os.path.join(PACKAGE_DIR, name)
    if name not in wanted or not os.path.isfile(path):
        abort(404)
    return path


def _zip_listing(path, title, subtitle, zip_url, file_base):
    try:
        files = docket_files.list_zip(path)
    except Exception as exc:
        return jsonify({"ok": False, "error": "This zip could not be opened (%s)." % type(exc).__name__}), 500
    return jsonify({"ok": True, "title": title, "subtitle": subtitle, "zip_url": zip_url,
                    "file_base": file_base, "files": files})


def _zip_member(path, member):
    data = docket_files.read_member(path, member)
    if data is None:
        abort(404)
    return docket_files.respond(data, member, download=bool(request.args.get("download")))


@bp.route("/actions/view/iptorch/<slug>/<stamp>/files")
def action_view_iptorch(slug, stamp):
    path, meta = _iptorch_found(slug, stamp)
    base = "%s/actions/view/iptorch/%s/%s/file/" % (request.script_root, slug, stamp)
    title = "%s  ·  %s" % (meta.get("subject") or slug, iptorch_packages.INSTRUMENT_LABEL.get(
        meta.get("instrument") or "", meta.get("label") or "iptorch.com package"))
    sub = "Built %s UTC by %s" % (str(meta.get("built_at") or "")[:16].replace("T", " "),
                                  iptorch_packages.who(meta))
    return _zip_listing(path, title, sub,
                        "%s/actions/iptorch/%s/%s.zip" % (request.script_root, slug, stamp), base)


@bp.route("/actions/view/iptorch/<slug>/<stamp>/file/<path:member>")
def action_view_iptorch_file(slug, stamp, member):
    path, _ = _iptorch_found(slug, stamp)
    return _zip_member(path, member)


@bp.route("/actions/view/package/<name>/files")
def action_view_package(name):
    path = _package_path(name)
    return _zip_listing(path, name, "The package as filed or handed over",
                        "%s/actions/package/%s" % (request.script_root, name),
                        "%s/actions/view/package/%s/file/" % (request.script_root, name))


@bp.route("/actions/view/package/<name>/file/<path:member>")
def action_view_package_file(name, member):
    return _zip_member(_package_path(name), member)


def _uspto_app(app):
    """The application, only when it is on the reader's own docket: this app spends its USPTO key
    on the cases it follows, not on whatever number somebody types into a URL."""
    user = _user()
    app = docket_files.app_number(app)
    if not app:
        abort(404)
    with db.cursor(autocommit=True) as cur:
        cur.execute("""SELECT 1 FROM app_observation_cases
                        WHERE user_id = %s
                          AND (regexp_replace(COALESCE(payload->>'application', ''), '[^0-9]', '', 'g') = %s
                               OR payload->>'register_url' LIKE %s)
                        LIMIT 1""", (user["id"], app, "%%/applications/%s%%" % app))
        if not cur.fetchone():
            abort(404)
    return app


@bp.route("/actions/view/uspto/<app>/files")
def action_view_uspto(app):
    app = _uspto_app(app)
    try:
        docs = docket_files.uspto_documents(app, fresh=bool(request.args.get("fresh")))
    except observation_refresh.OdpUnavailable as exc:
        return jsonify({"ok": False, "error": "The USPTO did not answer (%s). Try again in a "
                                              "minute." % exc}), 502
    pretty = "%s/%s,%s" % (app[:2], app[2:5], app[5:]) if len(app) == 8 else app
    files = [{"name": "%s  %s" % (d["date"], d["description"] or d["code"]), "key": d["id"],
              "kind": "pdf" if d["has_pdf"] else "download", "label": d["code"],
              "date": d["date"], "code": d["code"], "direction": d["direction"],
              "pages": d["pages"], "has_pdf": d["has_pdf"]} for d in docket_files.public_list(docs)]
    return jsonify({"ok": True, "title": "US %s: the USPTO file wrapper" % pretty,
                    "subtitle": "%d documents, newest first, read live from the USPTO Open Data "
                                "Portal. Patent Center's own page for this file loads empty for "
                                "anyone not signed in to USPTO.gov." % len(files),
                    "zip_url": "", "office_url": "https://patentcenter.uspto.gov/applications/%s/ifw/docs" % app,
                    "file_base": "%s/actions/view/uspto/%s/doc/" % (request.script_root, app),
                    "file_suffix": ".pdf", "files": files})


@bp.route("/actions/view/uspto/<app>/doc/<doc_id>.pdf")
def action_view_uspto_doc(app, doc_id):
    app = _uspto_app(app)
    try:
        data = docket_files.uspto_pdf(app, doc_id)
    except observation_refresh.OdpUnavailable as exc:
        return jsonify({"ok": False, "error": "The USPTO did not answer (%s)." % exc}), 502
    if not data:
        abort(404)
    return docket_files.respond(data, "US%s_%s.pdf" % (app, doc_id),
                                download=bool(request.args.get("download")))


def _iptorch_docket_keys():
    """Every publication key on the dockets that show iptorch packages, for the package sync."""
    ids = []
    for email in iptorch_packages.DOCKET_OWNERS:
        u = accounts.get_user_by_email(email)
        if u:
            ids.append(int(u["id"]))
    keys = set()
    for uid in ids:
        keys |= docket_keys(uid)
    return keys


def init_app(app):
    """Register the docket. Called before `auth.init_app`, like every other blueprint here."""
    app.register_blueprint(bp)
    try:
        seed_owner()
    except Exception:
        #  A docket that cannot seed must not stop the search product booting.
        traceback.print_exc()
    #  The iptorch.com package copier runs whether or not anybody opens the page: a package
    #  rebuilt twice while nobody looked would otherwise lose its first version. Never under a
    #  test run, which must not reach a real database or a real iptorch.
    #  Which patents are on the docket, so a package ANY iptorch account builds for one of them is
    #  kept, not only our own accounts'.
    iptorch_packages.DOCKET_KEYS = _iptorch_docket_keys
    if "pytest" not in sys.modules:
        iptorch_packages.start_background()
    return app
