"""The third-party observation docket: what can still be filed against somebody else's patent.

An observation, an Einwendung, a preissuance submission: three offices, three names, one act. You
put prior art in front of an examiner who is still examining, and it costs almost nothing. The
hard part was never the filing, it is knowing WHICH of a competitor's cases is still open and for
how long, because every office computes that differently and none of them tells you.

So this is a docket, not a report. Each row is one case with the route that reaches it, the date
the door shuts and what was decided about it. Alongside it sits the record of what has actually
been filed, which is the part that decays fastest: correspondence says a submission went in on the
26th, the file wrapper says the 24th, and only one of those is the date an examiner will see.

WHY THE DATA IS PER USER, AND SEEDED FOR EXACTLY ONE ACCOUNT

This app takes public signups. A docket is a list of a named third party's patents with the dates
on which they become harder to attack, annotated with what our counsel thinks and what was
declined; it is the opposite of shareable. Every table here is keyed by `user_id` with a foreign
key onto `app_users`, every query filters on it, and nothing reads a row it was not asked for by
its owner. A new account therefore sees an empty docket and can build its own.

The GRABO/Schmalz docket is seeded once, for `OWNER_EMAIL` only. If that account does not exist
the seed is a no-op rather than an error, so the app boots the same in a test database.

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

import datetime
import json
import os
import re
import threading
import traceback

from flask import (Blueprint, abort, jsonify, render_template, request,
                   send_from_directory)

import auth
import db
import observation_actions
import observation_refresh

bp = Blueprint("observations", __name__)

#  THE ONE ACCOUNT THE SHIPPED DOCKET BELONGS TO. Overridable by env so a staging box can seed a
#  test account instead, but never a list of addresses: seeding two accounts would put the same
#  private docket in two places and there would be no answer to which one is authoritative.
OWNER_EMAIL = os.environ.get("OBSERVATIONS_OWNER_EMAIL", "nimo@rotem.ai")

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("OBSERVATIONS_DATA",
                          os.path.join(_HERE, "..", "data", "observations"))
SEED_PATH = os.path.join(DATA_DIR, "seed.json")
PACKAGE_DIR = os.path.join(DATA_DIR, "packages")

MAX_NOTE_CHARS = 4000
#  What the detail dialog shows. Named explicitly rather than "everything except the row fields",
#  so adding a field to the docket never silently publishes it to the page.
DETAIL_FIELDS = (
    "publication", "granted_as", "title", "title_full", "application", "applicant", "office",
    "baseline_route_label", "register_status", "register_updated", "register_url", "google",
    "pubDate", "priority_date", "six_months", "first_rejection", "deadline", "deadline_kind",
    "days_left", "verified", "counsel_required", "counsel_report", "next_action",
    "superseded_note", "why_new", "family", "priority", "user_state", "user_note",
    #  Added with the live refresh: where the case now stands, what we have already put on its
    #  file, and the instrument table for its office.
    "posture", "grant_published", "opposition_deadline", "scheduled_grant", "decision_on",
    "closing_note", "closing_soon", "allowance", "quayle", "exam_requested", "opposition_pending",
    "our_submissions", "refreshed_at", "refresh_source", "actions",
)
#  The states a person moves a row through by hand. `open` is the absence of a decision, which is
#  why it is the default and why it is not the same thing as `watch`: one has not been looked at,
#  the other has been looked at and parked.
USER_STATES = ("open", "watch", "queued", "filed", "declined", "done")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS app_observation_cases (
         id bigserial PRIMARY KEY,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         publication text NOT NULL,
         payload jsonb NOT NULL,
         user_state text NOT NULL DEFAULT 'open',
         user_note text NOT NULL DEFAULT '',
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (user_id, publication))""",
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
    """CREATE TABLE IF NOT EXISTS app_observation_decisions (
         id bigserial PRIMARY KEY,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         decision_key text NOT NULL,
         payload jsonb NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (user_id, decision_key))""",
    """CREATE TABLE IF NOT EXISTS app_observation_meta (
         user_id bigint PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
         payload jsonb NOT NULL,
         updated_at timestamptz NOT NULL DEFAULT now())""",
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
        _SCHEMA_READY = True


def reset_schema_cache_for_tests() -> None:
    global _SCHEMA_READY
    _SCHEMA_READY = False


def load_seed():
    with open(SEED_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _owner_id():
    """The `app_users` row id for OWNER_EMAIL, or None when that account does not exist."""
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT id FROM app_users WHERE lower(email) = lower(%s)", (OWNER_EMAIL,))
        row = cur.fetchone()
    return row["id"] if row else None


def seed_owner(force: bool = False) -> int:
    """Load the shipped docket into the owner's rows. Idempotent, and additive by default.

    `force=False` inserts rows the owner does not have and refreshes the register facts on rows
    they do, WITHOUT touching `user_state` or `user_note`: the file on disk is authoritative about
    what the register said, the person is authoritative about what they decided. `force=True`
    additionally resets those two, which is only for a test fixture.

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
        for row in seed.get("rows", []):
            pub = row.get("publication") or ""
            if not pub:
                continue
            if force:
                cur.execute(
                    """INSERT INTO app_observation_cases (user_id, publication, payload)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, publication) DO UPDATE
                         SET payload = EXCLUDED.payload, user_state = 'open', user_note = '',
                             updated_at = now()""",
                    (uid, pub, json.dumps(row)))
            else:
                cur.execute(
                    """INSERT INTO app_observation_cases (user_id, publication, payload)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, publication) DO UPDATE
                         SET payload = EXCLUDED.payload, updated_at = now()
                       WHERE COALESCE(app_observation_cases.payload->>'refreshed_at', '')
                             <= %s""",
                    (uid, pub, json.dumps(row), seed_as_of))
            n += 1
        for f in seed.get("filings", []):
            cur.execute(
                """INSERT INTO app_observation_filings (user_id, filing_key, payload)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, filing_key) DO UPDATE
                     SET payload = EXCLUDED.payload, updated_at = now()""",
                (uid, f["id"], json.dumps(f)))
        for d in seed.get("decisions", []):
            cur.execute(
                """INSERT INTO app_observation_decisions (user_id, decision_key, payload)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, decision_key) DO UPDATE
                     SET payload = EXCLUDED.payload, updated_at = now()""",
                (uid, d["id"], json.dumps(d)))
        meta = {k: seed.get(k) for k in ("as_of", "generated", "source_note", "changes", "missed")}
        #  `missed` and `changes` are hand-written and belong to the file; `as_of` and the refresh
        #  record belong to whichever pull was last. Merge rather than replace, or a restart tells
        #  the reader the docket was last checked in August when it was checked this morning.
        cur.execute("SELECT payload FROM app_observation_meta WHERE user_id = %s", (uid,))
        got = cur.fetchone()
        if got and (dict(got["payload"]).get("refreshed_at") or ""):
            merged = dict(got["payload"])
            merged["missed"] = meta.get("missed")
            merged["changes"] = meta.get("changes")
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
    row["sort_key"] = n if n is not None else (1 if row.get("closing_soon") else 9999)
    return row


def cases_for(user_id, today=None):
    ensure_schema()
    today = today or datetime.date.today()
    with db.cursor(autocommit=True) as cur:
        cur.execute(
            """SELECT publication, payload, user_state, user_note
                 FROM app_observation_cases WHERE user_id = %s""", (user_id,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        row = dict(r["payload"])
        row["user_state"] = r["user_state"]
        row["user_note"] = r["user_note"]
        #  What can still be filed against this one, evaluated for today. Cheap enough to do on
        #  every read, and the alternative is a stored answer that rots exactly like the count did.
        #  BEFORE `recount`, which now takes the row's countdown from whichever of these is open.
        row["actions"] = observation_actions.actions_for(row, today)
        row["action_headline"] = observation_actions.headline(row, today)
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


def decisions_for(user_id):
    ensure_schema()
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT payload FROM app_observation_decisions WHERE user_id = %s", (user_id,))
        rows = [dict(r["payload"]) for r in cur.fetchall()]
    rank = {"open_and_dated": 0, "unanswered": 1, "declined": 2, "answered_in_principle": 3,
            "moot_for_now": 4}
    rows.sort(key=lambda d: (rank.get(d.get("status"), 9), d.get("asked_on") or ""))
    return rows


def meta_for(user_id):
    ensure_schema()
    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT payload FROM app_observation_meta WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    return dict(row["payload"]) if row else {}


def set_case(user_id, publication, state=None, note=None):
    """Update the two fields a person owns. Refuses a row that is not theirs, by construction:
    the WHERE clause carries the user id, so a mismatched publication updates nothing."""
    ensure_schema()
    sets, params = [], []
    if state is not None:
        if state not in USER_STATES:
            raise ValueError("unknown state")
        sets.append("user_state = %s")
        params.append(state)
    if note is not None:
        clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(note))[:MAX_NOTE_CHARS]
        sets.append("user_note = %s")
        params.append(clean)
    if not sets:
        return False
    sets.append("updated_at = now()")
    params.extend([user_id, publication])
    with db.cursor(autocommit=True) as cur:
        cur.execute("UPDATE app_observation_cases SET %s WHERE user_id = %%s AND publication = %%s"
                    % ", ".join(sets), params)
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


@bp.route("/observations")
def observations_page():
    user = _user()
    try:
        cases = cases_for(user["id"])
        filings = filings_for(user["id"])
        decisions = decisions_for(user["id"])
        meta = meta_for(user["id"])
    except Exception:
        traceback.print_exc()
        cases, filings, decisions, meta = [], [], [], {}
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
    counts["submitted"] = sum(1 for c in cases if c.get("our_submissions") or c.get("filed"))
    #  Every case where something can be filed TODAY, whatever the office calls it.
    counts["actionable"] = sum(
        1 for c in cases
        if (c.get("action_headline") or {}).get("status") in ("open", "closing"))
    #  The act-now band, computed here rather than in the template: Jinja's one-argument
    #  `selectattr('days_left')` is a truthiness test, so it silently drops the case that closes
    #  TODAY, which is the one row nobody can afford to lose.
    #  Live windows first, the nearest last chance at the top. Then ONLY the recently lapsed: a
    #  door that shut a year ago is history, not an action, and five of them were crowding out the
    #  three that still matter. The '666 row earns its place here while its art is still being
    #  redeployed to the German and European members of the same family.
    live = sorted((c for c in cases
                   if c.get("days_left") is not None and 0 <= c["days_left"] < 45),
                  key=lambda c: c["days_left"])
    just_missed = sorted((c for c in cases
                          if c.get("days_left") is not None and -30 <= c["days_left"] < 0),
                         key=lambda c: c["days_left"], reverse=True)
    act = live + just_missed
    #  THE POPUP'S DATA, TRIMMED. The table row carries what you scan by; everything else lives
    #  in the dialog and is fetched from this map by publication number. Sending the whole case
    #  list again would double a 108-row page for the sake of fields only one row shows at a time,
    #  so only the fields the dialog actually renders are serialised.
    detail = {c["publication"]: {k: c.get(k) for k in DETAIL_FIELDS} for c in cases}
    return render_template("observations.html", cases=cases, filings=filings,
                           decisions=decisions, meta=meta, counts=counts, act=act,
                           detail=detail, states=USER_STATES,
                           stages=observation_actions.STAGES,
                           today=datetime.date.today().isoformat())


@bp.route("/api/observations/case", methods=["POST"])
def api_observation_case():
    user = _user()
    auth.require_csrf()
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    pub = (body.get("publication") or "").strip()
    if not pub:
        return jsonify({"ok": False, "error": "publication is required"}), 400
    try:
        ok = set_case(user["id"], pub, state=body.get("state"), note=body.get("note"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)[:160]}), 500
    if not ok:
        return jsonify({"ok": False, "error": "not on your docket"}), 404
    return jsonify({"ok": True})


@bp.route("/api/observations/refresh", methods=["POST"])
def api_observation_refresh():
    """Go and ask the offices again. One job per person, running in the background.

    The work is a hundred-odd HTTP calls to three registers and it takes the best part of a
    minute, which is too long to hold a request open and far too long to hold a browser on a
    spinner with nothing to read. So the button starts a job and the page polls this route's GET
    twin for the count and the case it is on.
    """
    user = _user()
    auth.require_csrf()
    try:
        rows = cases_for(user["id"])
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)[:160]}), 500
    started = observation_refresh.start(user["id"], rows)
    st = observation_refresh.state(user["id"])
    if not started:
        return jsonify({"ok": True, "already_running": True, "state": st})
    return jsonify({"ok": True, "already_running": False, "state": st})


@bp.route("/api/observations/refresh", methods=["GET"])
def api_observation_refresh_state():
    user = _user()
    return jsonify({"ok": True, "state": observation_refresh.state(user["id"])})


@bp.route("/observations/package/<path:name>")
def observation_package(name):
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
