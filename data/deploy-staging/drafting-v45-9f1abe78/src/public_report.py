"""Publish one report at a public URL, optionally behind a password, and record who reads it.

WHAT THIS IS FOR
----------------
A finished search is a document you want to put in front of a client, an examiner or opposing
counsel — people who have no account here and should not get one. The existing
`deliverables.create_share` mints an unguessable token at `/shared/<token>`; this is the other
shape of the same need: a STABLE, readable URL at `/public-report/<slug>` that the owner can hand
out, revoke, password-protect, and — the part that does not exist anywhere yet — see the readership
of.

A slug in a URL is guessable in a way a 32-byte token is not, so three things guard it:

  * it is OFF until the owner publishes it, and a slug that was never published 404s exactly like
    one that does not exist — an unpublished report must not be distinguishable from a missing one;
  * revoking is immediate and permanent for that link;
  * an optional password. No username and no signup, because the audience is one person you sent a
    link to, not a user base. It is hashed with the same `werkzeug.security` primitives the real
    accounts use, never stored in the clear, and it is not recoverable — the owner sets a new one.

WHAT IS RECORDED, AND WHY BOTH HALVES
-------------------------------------
Two sources, because neither alone is honest:

  server side   what the request carried: address, forwarded chain, user agent, referrer, languages,
                the modern client hints (`sec-ch-ua*`, which name browser, platform and mobile-ness
                far more reliably than a user-agent string), method, protocol, and the exact time.
                Always present, cannot be blocked, and cannot see the screen.
  client side   what only the page can know: screen and viewport size, colour depth, pixel ratio,
                timezone, full language list, platform, cores, memory, touch, connection type, and
                TIME ON PAGE — which is not a property of a request at all, it is a property of a
                session that has to be measured while it happens and sent as it ends.

The two are joined on a visit id minted server side and handed to the page, so a blocked or failed
beacon leaves a visit row that is incomplete rather than absent. `beacon_ok` says which happened,
because "we know nothing about this visitor's screen" and "this visitor blocked scripts" are
different facts and a dashboard that conflates them is lying.

TIME ON PAGE IS MEASURED, NOT INFERRED. The page sends a heartbeat while it is visible and a final
`sendBeacon` on `pagehide`; the server keeps the largest value it has been told, so a closed laptop
or a killed tab still leaves the last good reading rather than resetting it to zero.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import traceback

from werkzeug.security import check_password_hash, generate_password_hash

import db

#  How long a visit id stays acceptable for enrichment. A beacon that arrives days later is not a
#  reading session, it is a replayed request.
BEACON_WINDOW_HOURS = float(os.environ.get("PUBLIC_BEACON_WINDOW_HOURS", "12"))
MAX_VISITS_SHOWN = int(os.environ.get("PUBLIC_MAX_VISITS", "500"))

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS app_public_reports (
         id bigserial PRIMARY KEY,
         slug text NOT NULL UNIQUE,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         title text NOT NULL DEFAULT '',
         password_hash text,
         published boolean NOT NULL DEFAULT true,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         revoked_at timestamptz)""",
    "CREATE INDEX IF NOT EXISTS app_public_reports_user_idx ON app_public_reports (user_id)",
    #  One row per VIEW, not per viewer. Two visits from one address at different times are two
    #  readings and collapsing them would hide exactly the pattern an owner is looking for.
    """CREATE TABLE IF NOT EXISTS app_public_visits (
         id bigserial PRIMARY KEY,
         slug text NOT NULL,
         visit_key text NOT NULL UNIQUE,
         at timestamptz NOT NULL DEFAULT now(),
         ip text NOT NULL DEFAULT '',
         forwarded_for text NOT NULL DEFAULT '',
         user_agent text NOT NULL DEFAULT '',
         referer text NOT NULL DEFAULT '',
         accept_language text NOT NULL DEFAULT '',
         method text NOT NULL DEFAULT '',
         protocol text NOT NULL DEFAULT '',
         host text NOT NULL DEFAULT '',
         path text NOT NULL DEFAULT '',
         query_string text NOT NULL DEFAULT '',
         headers jsonb NOT NULL DEFAULT '{}'::jsonb,
         unlocked boolean NOT NULL DEFAULT false,
         beacon_ok boolean NOT NULL DEFAULT false,
         seconds_on_page integer NOT NULL DEFAULT 0,
         max_scroll_pct integer NOT NULL DEFAULT 0,
         client jsonb NOT NULL DEFAULT '{}'::jsonb,
         last_seen_at timestamptz)""",
    "CREATE INDEX IF NOT EXISTS app_public_visits_slug_idx ON app_public_visits (slug, at DESC)",
)


def ensure_schema():
    with db.cursor() as cur:
        for stmt in _SCHEMA:
            cur.execute(stmt)


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------
def autopublish(user_id, slug, title="") -> dict:
    """Publish a finished report under the owner's own share password. -> the row, or {}.

    Called when a search completes, so the link exists before anyone asks for it: an owner who
    shares every report should not have to click Publish every time.

    IT DOES NOTHING WITHOUT A SHARE PASSWORD. The link is the access control, and an unguessable
    slug is not a password: publishing automatically with no password would quietly turn every
    finished search into a document anyone holding the URL can read, which is not a default anybody
    chose. No password set means no automatic link, and the account page says so.

    Never raises: a search must not fail over its share link.
    """
    try:
        import accounts
        d = accounts.share_defaults(user_id)
        if not d.get("autopublish") or not d.get("password_hash"):
            return {}
        return publish(user_id, slug, title=title, password_hash=d["password_hash"])
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return {}


def publish(user_id, slug, password=None, title="", clear_password=False,
            password_hash=None) -> dict:
    """Publish (or re-publish) a report. -> the row.

    Re-publishing an existing link keeps its visit history: an owner who changes the password has
    not created a different document, and losing the readership on a password change would be a
    surprising thing for a dashboard to do.
    """
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_public_reports WHERE slug=%s", (slug,))
        row = cur.fetchone()
        if row and row["user_id"] != user_id:
            #  Somebody else already published this report. Distinguishable from "no such report",
            #  because the caller CAN see the report — they reached this route through the access
            #  check — and a bare 404 would tell them their own report does not exist. One link per
            #  report, owned by whoever published it first, and the second person is told so.
            return {"error": "already_published_by_another_user"}
        #  `password_hash` is an ALREADY-HASHED value, which is how the owner's one share password
        #  reaches a report without the plaintext ever being stored or passed around.
        pw = password_hash or None
        if password:
            pw = generate_password_hash(password)
        if row:
            #  The three-way choice is made HERE, not in SQL. A `CASE WHEN %s IS NOT NULL THEN %s`
            #  over a possibly-NULL parameter gives Postgres nothing to infer a type from and it
            #  refuses the statement outright ("could not determine data type of parameter $3") —
            #  which surfaced as "the password was silently never set", because the caller had
            #  already returned a row that looked published.
            new_hash = None if clear_password else (pw if pw else row.get("password_hash"))
            cur.execute(
                """UPDATE app_public_reports
                   SET published=true, revoked_at=NULL, updated_at=now(),
                       title=COALESCE(NULLIF(%s,''), title),
                       password_hash=%s
                   WHERE slug=%s RETURNING *""",
                (title, new_hash, slug))
        else:
            cur.execute(
                """INSERT INTO app_public_reports(slug, user_id, title, password_hash)
                   VALUES (%s,%s,%s,%s) RETURNING *""", (slug, user_id, title, pw))
        return dict(cur.fetchone() or {})


def unpublish(user_id, slug) -> bool:
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("UPDATE app_public_reports SET published=false, revoked_at=now(), "
                    "updated_at=now() WHERE slug=%s AND user_id=%s", (slug, user_id))
        return cur.rowcount > 0


def get(slug) -> dict:
    """The published row, or {} — including when it exists but was revoked.

    A revoked link and a link that never existed must be indistinguishable to a visitor, so this
    returns nothing for both and the caller 404s either way.
    """
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_public_reports WHERE slug=%s AND published=true", (slug,))
        return dict(cur.fetchone() or {})


def status_for_owner(user_id, slug) -> dict:
    """What the owner's report page needs to render the Export control."""
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_public_reports WHERE slug=%s AND user_id=%s",
                    (slug, user_id))
        row = dict(cur.fetchone() or {})
        if not row:
            return {"published": False, "has_password": False, "visits": 0}
        cur.execute("SELECT count(*) n FROM app_public_visits WHERE slug=%s", (slug,))
        n = (cur.fetchone() or {}).get("n") or 0
    return {"published": bool(row.get("published")),
            "has_password": bool(row.get("password_hash")),
            "created_at": row.get("created_at"), "visits": int(n)}


def check_password(slug, password) -> bool:
    row = get(slug)
    if not row:
        return False
    if not row.get("password_hash"):
        return True
    return check_password_hash(row["password_hash"], password or "")


def needs_password(slug) -> bool:
    return bool((get(slug) or {}).get("password_hash"))


# ---------------------------------------------------------------------------
# recording a reading
# ---------------------------------------------------------------------------
#  Headers worth keeping verbatim. The `sec-ch-ua*` family is the modern, structured answer to the
#  questions a user-agent string answers badly, and browsers that send it are telling the truth
#  about themselves in a way UA sniffing never did.
_KEEP_HEADERS = (
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-ch-ua-platform-version",
    "sec-ch-ua-arch", "sec-ch-ua-model", "sec-ch-ua-bitness", "sec-ch-ua-full-version-list",
    "dnt", "sec-gpc", "accept", "accept-encoding", "sec-fetch-site", "sec-fetch-mode",
    "sec-fetch-dest", "upgrade-insecure-requests", "via", "cf-ipcountry", "x-real-ip",
    "x-forwarded-proto", "x-forwarded-host", "priority", "cache-control",
)


def client_ip(req) -> tuple:
    """(the address we can actually stand behind, the whole forwarded chain).

    Delegates to `auth.client_ip`, which already solved this properly: X-Forwarded-For is entirely
    forgeable, so it is only consulted when the TCP peer is our own reverse proxy, and it takes the
    right ELEMENT of the chain given nginx appends rather than replaces. Reimplementing that here
    would be a second, worse copy of a rule that has to be identical in both places — the rate
    limiter and this log must agree about who a caller is.

    The raw chain is stored alongside it regardless, because it is what lets somebody later tell a
    real reader from our own health check.
    """
    chain = (req.headers.get("X-Forwarded-For") or "").strip()
    try:
        import auth
        return (auth.client_ip() or ""), chain
    except Exception:
        #  Outside a request context, or auth unavailable: fall back to the peer, never to an
        #  unverified header.
        return (req.remote_addr or ""), chain


def record_visit(slug, req, unlocked=False) -> str:
    """One row for this view. -> the visit key the page will beacon back against."""
    ensure_schema()
    key = secrets.token_urlsafe(18)
    ip, chain = client_ip(req)
    keep = {}
    for h in _KEEP_HEADERS:
        v = req.headers.get(h)
        if v:
            keep[h] = str(v)[:400]
    try:
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO app_public_visits
                     (slug, visit_key, ip, forwarded_for, user_agent, referer, accept_language,
                      method, protocol, host, path, query_string, headers, unlocked)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (slug, key, ip[:120], chain[:400], (req.headers.get("User-Agent") or "")[:600],
                 (req.headers.get("Referer") or "")[:600],
                 (req.headers.get("Accept-Language") or "")[:200],
                 req.method, req.environ.get("SERVER_PROTOCOL", "")[:20],
                 (req.host or "")[:200], (req.path or "")[:300],
                 (req.query_string or b"").decode("utf-8", "replace")[:300],
                 json.dumps(keep), bool(unlocked)))
    except Exception:
        #  A reader must never be turned away because the log failed. This runs in the request
        #  path of a page somebody was sent a link to.
        import traceback
        traceback.print_exc()
        return ""
    return key


#  Client fields accepted from the beacon. An allow-list, because this endpoint takes JSON from
#  anybody with the link and writes it to a column an owner will read.
_CLIENT_FIELDS = (
    "screen_w", "screen_h", "avail_w", "avail_h", "viewport_w", "viewport_h", "color_depth",
    "pixel_ratio", "timezone", "timezone_offset", "languages", "language", "platform",
    "user_agent", "vendor", "hardware_concurrency", "device_memory", "max_touch_points",
    "connection", "downlink", "rtt", "save_data", "cookies_enabled", "do_not_track",
    "referrer", "page_load_ms", "prefers_dark", "prefers_reduced_motion", "orientation",
    "is_bot_hint", "ua_brands", "ua_platform", "ua_mobile", "history_length", "webdriver",
)


def _clean_client(payload) -> dict:
    out = {}
    for k in _CLIENT_FIELDS:
        if k not in (payload or {}):
            continue
        v = payload[k]
        if isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x)[:80] for x in v][:20]
        else:
            out[k] = str(v)[:300]
    return out


def record_beacon(visit_key, payload) -> bool:
    """Enrich a visit with what only the page could know. Idempotent; called many times.

    `seconds_on_page` and `max_scroll_pct` take the LARGEST value ever reported, never the latest.
    A heartbeat arrives while the page is open and a final beacon fires on pagehide, but the final
    one is exactly the delivery most likely to be lost — so a later, smaller number must not be
    allowed to overwrite an earlier, larger one.
    """
    if not visit_key or not isinstance(payload, dict):
        return False
    client = _clean_client(payload)
    try:
        secs = max(0, min(int(payload.get("seconds_on_page") or 0), 86400))
    except (TypeError, ValueError):
        secs = 0
    try:
        scroll = max(0, min(int(payload.get("max_scroll_pct") or 0), 100))
    except (TypeError, ValueError):
        scroll = 0
    try:
        with db.cursor() as cur:
            cur.execute(
                f"""UPDATE app_public_visits
                    SET beacon_ok=true,
                        client = client || %s::jsonb,
                        seconds_on_page = GREATEST(seconds_on_page, %s),
                        max_scroll_pct  = GREATEST(max_scroll_pct, %s),
                        last_seen_at = now()
                    WHERE visit_key=%s
                      AND at > now() - interval '{BEACON_WINDOW_HOURS} hours'""",
                (json.dumps(client), secs, scroll, visit_key))
            return cur.rowcount > 0
    except Exception:
        import traceback
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# what the owner sees
# ---------------------------------------------------------------------------
_BOT = re.compile(r"bot|crawl|spider|slurp|curl|wget|python-requests|headless|monitor|uptime|"
                  r"preview|facebookexternalhit|whatsapp|slack|discord|telegram", re.I)


def looks_automated(visit) -> bool:
    """A best guess, and labelled as one wherever it is shown.

    A link pasted into a chat app is fetched by that app's unfurler before any human sees it, and
    counting that as a reading is how a share dashboard reports an audience that was never there.
    `webdriver` and a missing beacon are the other two tells.
    """
    if _BOT.search(str(visit.get("user_agent") or "")):
        return True
    if (visit.get("client") or {}).get("webdriver"):
        return True
    return not visit.get("beacon_ok") and int(visit.get("seconds_on_page") or 0) == 0


def visits(user_id, slug, limit=MAX_VISITS_SHOWN) -> list:
    """Every recorded reading of this link, newest first. Owner only."""
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM app_public_reports WHERE slug=%s AND user_id=%s",
                    (slug, user_id))
        if not cur.fetchone():
            return []
        cur.execute("SELECT * FROM app_public_visits WHERE slug=%s ORDER BY at DESC LIMIT %s",
                    (slug, int(limit)))
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["automated"] = looks_automated(r)
    return rows


def summary(rows) -> dict:
    """Headline numbers, with the automated ones separated rather than silently included."""
    human = [r for r in rows if not r.get("automated")]
    secs = [int(r.get("seconds_on_page") or 0) for r in human]
    read = [s for s in secs if s > 0]
    return {
        "views": len(rows),
        "human_views": len(human),
        "automated_views": len(rows) - len(human),
        "distinct_ips": len({r.get("ip") for r in human if r.get("ip")}),
        "with_beacon": sum(1 for r in human if r.get("beacon_ok")),
        "median_seconds": (sorted(read)[len(read) // 2] if read else 0),
        "total_seconds": sum(read),
        "last_at": (rows[0].get("at") if rows else None),
    }
