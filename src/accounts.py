"""Named users, saved reports and durable notification state.

The retrieval corpus already lives in Postgres.  These small, namespaced tables keep the product
layer durable without introducing a second database or a per-process JSON file that would race
under gunicorn.  Schema creation is lazy: importing the web app remains possible in test/dev
environments where Postgres is unavailable, while the first account operation safely runs the
idempotent migration.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

import db


EMAIL_RE = re.compile(r"^[^\s@]{1,128}@[^\s@]{1,190}\.[^\s@]{2,63}$")
VALID_FOCUS = {"all_text", "claims"}
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_DUMMY_HASH = generate_password_hash("this-password-is-never-valid")


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS app_users (
         id bigserial PRIMARY KEY, email text NOT NULL, full_name text NOT NULL,
         password_hash text NOT NULL, is_admin boolean NOT NULL DEFAULT false,
         is_active boolean NOT NULL DEFAULT true,
         session_version integer NOT NULL DEFAULT 1 CHECK (session_version > 0),
         email_on_completion boolean NOT NULL DEFAULT true,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(), last_login_at timestamptz,
         UNIQUE (email))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS app_users_email_lower_uq ON app_users (lower(email))",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS organization text NOT NULL DEFAULT ''",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS default_applicant text NOT NULL DEFAULT ''",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS default_inventors text NOT NULL DEFAULT ''",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS preferred_jurisdiction text NOT NULL DEFAULT 'US'",
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS session_version integer NOT NULL DEFAULT 1",
    """CREATE TABLE IF NOT EXISTS app_saved_searches (
         id bigserial PRIMARY KEY,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         slug text NOT NULL, query text NOT NULL DEFAULT '', title text,
         mode text NOT NULL DEFAULT 'novelty', search_focus text NOT NULL DEFAULT 'all_text',
         subject text, status text NOT NULL DEFAULT 'running', saved boolean NOT NULL DEFAULT true,
         notify_email boolean NOT NULL DEFAULT true,
         notification_status text NOT NULL DEFAULT 'not_requested',
         created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
         completed_at timestamptz, last_viewed_at timestamptz, UNIQUE (user_id, slug))""",
    "CREATE INDEX IF NOT EXISTS app_saved_searches_user_updated_idx ON app_saved_searches (user_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS app_saved_searches_slug_idx ON app_saved_searches (slug)",
    """CREATE TABLE IF NOT EXISTS app_report_flags (
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         slug text NOT NULL, publication_number text NOT NULL,
         flag text NOT NULL DEFAULT '', note text NOT NULL DEFAULT '',
         updated_at timestamptz NOT NULL DEFAULT now(),
         PRIMARY KEY (user_id,slug,publication_number))""",
    "CREATE INDEX IF NOT EXISTS app_report_flags_search_idx ON app_report_flags (user_id,slug)",
    """CREATE TABLE IF NOT EXISTS app_mail_outbox (
         id bigserial PRIMARY KEY, dedupe_key text NOT NULL UNIQUE,
         user_id bigint REFERENCES app_users(id) ON DELETE SET NULL, search_slug text,
         to_email text NOT NULL, kind text NOT NULL, subject text NOT NULL, body_text text NOT NULL,
         status text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0,
         last_error text, next_attempt_at timestamptz NOT NULL DEFAULT now(),
         created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz)""",
    "CREATE INDEX IF NOT EXISTS app_mail_outbox_pending_idx ON app_mail_outbox (status, next_attempt_at, id)",
    """CREATE TABLE IF NOT EXISTS app_password_reset_tokens (
         id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         token_hash text NOT NULL UNIQUE, expires_at timestamptz NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now(), used_at timestamptz)""",
    "CREATE INDEX IF NOT EXISTS app_password_reset_user_idx ON app_password_reset_tokens (user_id, created_at DESC)",
)


def ensure_schema(force: bool = False):
    """Create/upgrade the small app tables once per process; every statement is idempotent."""
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


def _email(value: str) -> str:
    value = (value or "").strip().lower()
    if len(value) > 254 or not EMAIL_RE.match(value):
        raise ValueError("Enter a valid email address.")
    return value


def _password(value: str):
    if len(value or "") < 10:
        raise ValueError("Use at least 10 characters for the password.")
    if len(value) > 256:
        raise ValueError("Password is too long.")


def _name(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    if not (2 <= len(value) <= 120):
        raise ValueError("Enter your name.")
    return value


def public_user(row):
    if not row:
        return None
    out = dict(row)
    out.pop("password_hash", None)
    return out


def create_user(email: str, full_name: str, password: str):
    ensure_schema()
    email, full_name = _email(email), _name(full_name)
    _password(password)
    password_hash = generate_password_hash(password)
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO app_users(email,full_name,password_hash) VALUES (%s,%s,%s) "
                "RETURNING *", (email, full_name, password_hash))
            return public_user(cur.fetchone())
    except Exception as exc:
        # psycopg's exact exception class is deliberately not exposed to the route.  The indexed
        # lower(email) constraint is the authoritative duplicate check under concurrent signups.
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise ValueError("An account with that email already exists.") from None
        raise


def get_user(user_id):
    if not user_id:
        return None
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_users WHERE id=%s", (int(user_id),))
        return public_user(cur.fetchone())


def get_user_by_email(email: str):
    try:
        email = _email(email)
    except ValueError:
        return None
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_users WHERE lower(email)=%s", (email,))
        return cur.fetchone()


def authenticate(email: str, password: str):
    """Constant-work password check; inactive accounts never authenticate."""
    row = get_user_by_email(email)
    encoded = row.get("password_hash") if row else _DUMMY_HASH
    ok = check_password_hash(encoded, password or "")
    if not row or not ok or not row.get("is_active"):
        return None
    with db.cursor() as cur:
        cur.execute("UPDATE app_users SET last_login_at=now() WHERE id=%s", (row["id"],))
    return public_user(row)


def update_profile(user_id, *, full_name: str, email_on_completion: bool,
                   organization=None, default_applicant=None, default_inventors=None,
                   preferred_jurisdiction=None):
    ensure_schema()
    full_name = _name(full_name)
    organization = None if organization is None else re.sub(r"\s+", " ", organization.strip())[:180]
    default_applicant = (None if default_applicant is None else
                         re.sub(r"\s+", " ", default_applicant.strip())[:240])
    default_inventors = None if default_inventors is None else default_inventors.strip()[:2000]
    jurisdiction = None
    if preferred_jurisdiction is not None:
        jurisdiction = preferred_jurisdiction.strip().upper()
        if jurisdiction not in ("US", "EP", "WO"):
            raise ValueError("Choose US, EP or PCT/WO as the preferred drafting jurisdiction.")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET full_name=%s,email_on_completion=%s,"
            "organization=COALESCE(%s,organization),default_applicant=COALESCE(%s,default_applicant),"
            "default_inventors=COALESCE(%s,default_inventors),"
            "preferred_jurisdiction=COALESCE(%s,preferred_jurisdiction),updated_at=now() "
            "WHERE id=%s RETURNING *", (full_name, bool(email_on_completion), organization,
                                         default_applicant, default_inventors, jurisdiction,
                                         int(user_id)))
        return public_user(cur.fetchone())


def account_activity(user_id):
    """Small profile/dashboard counters; one indexed aggregate, no report-file scan."""
    ensure_schema()
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*)::int AS searches,"
            "count(*) FILTER (WHERE saved)::int AS saved,"
            "count(*) FILTER (WHERE status='complete')::int AS completed,"
            "count(*) FILTER (WHERE notification_status='sent')::int AS emailed "
            "FROM app_saved_searches WHERE user_id=%s", (int(user_id),))
        return dict(cur.fetchone())


def change_password(user_id, current_password: str, new_password: str):
    ensure_schema()
    _password(new_password)
    with db.cursor() as cur:
        cur.execute("SELECT password_hash FROM app_users WHERE id=%s FOR UPDATE", (int(user_id),))
        row = cur.fetchone()
        if not row or not check_password_hash(row["password_hash"], current_password or ""):
            raise ValueError("Current password is incorrect.")
        cur.execute(
            "UPDATE app_users SET password_hash=%s,session_version=session_version+1,"
            "updated_at=now() WHERE id=%s RETURNING *",
            (generate_password_hash(new_password), int(user_id)))
        return public_user(cur.fetchone())


def list_users():
    ensure_schema()
    with db.cursor() as cur:
        cur.execute(
            "SELECT u.id,u.email,u.full_name,u.is_admin,u.is_active,u.email_on_completion,"
            "u.created_at,u.last_login_at,count(s.id)::int AS searches,"
            "count(s.id) FILTER (WHERE s.saved)::int AS saved_searches "
            "FROM app_users u LEFT JOIN app_saved_searches s ON s.user_id=u.id "
            "GROUP BY u.id ORDER BY u.created_at DESC")
        return [dict(r) for r in cur.fetchall()]


def update_user_role(user_id, *, is_admin=None, is_active=None):
    """Admin mutation with a last-admin guard. Accounts are deactivated, never deleted."""
    ensure_schema()
    user_id = int(user_id)
    with db.cursor() as cur:
        # Lock the complete active-admin set in a stable order. Locking only the target row lets
        # two concurrent demotions each observe another administrator and remove both.
        cur.execute("SELECT id FROM app_users WHERE is_admin AND is_active ORDER BY id FOR UPDATE")
        active_admin_ids = {int(item["id"]) for item in cur.fetchall()}
        cur.execute("SELECT * FROM app_users WHERE id=%s FOR UPDATE", (user_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("User not found.")
        removing_admin = row["is_admin"] and (is_admin is False or is_active is False)
        if removing_admin and row.get("is_active") and len(active_admin_ids) <= 1:
            raise ValueError("Keep at least one active named administrator.")
        new_admin = row["is_admin"] if is_admin is None else bool(is_admin)
        new_active = row["is_active"] if is_active is None else bool(is_active)
        cur.execute(
            "UPDATE app_users SET is_admin=%s,is_active=%s,updated_at=now() WHERE id=%s RETURNING *",
            (new_admin, new_active, user_id))
        return public_user(cur.fetchone())


def record_search(user_id, slug: str, query: str, mode: str, search_focus: str,
                  subject=None, *, notify_email=True, status="running", saved=False):
    ensure_schema()
    focus = search_focus if search_focus in VALID_FOCUS else "all_text"
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO app_saved_searches
               (user_id,slug,query,title,mode,search_focus,subject,status,saved,notify_email,
                notification_status,updated_at,completed_at)
               VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,now(),
                       CASE WHEN %s='complete' THEN now() ELSE NULL END)
               ON CONFLICT (user_id,slug) DO UPDATE SET
                 query=EXCLUDED.query,mode=EXCLUDED.mode,search_focus=EXCLUDED.search_focus,
                 subject=EXCLUDED.subject,status=EXCLUDED.status,
                 saved=(app_saved_searches.saved OR EXCLUDED.saved),
                 notify_email=EXCLUDED.notify_email,
                 notification_status=CASE WHEN EXCLUDED.notify_email THEN 'pending' ELSE 'not_requested' END,
                 updated_at=now(),completed_at=EXCLUDED.completed_at
               RETURNING *""",
            (int(user_id), slug, query, mode, focus, subject, status, bool(saved),
             bool(notify_email), "pending" if notify_email else "not_requested", status))
        return dict(cur.fetchone())


def mark_search_viewed(user_id, slug):
    if not user_id:
        return
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("UPDATE app_saved_searches SET last_viewed_at=now() WHERE user_id=%s AND slug=%s",
                    (int(user_id), slug))


def get_search(user_id, slug):
    if not user_id:
        return None
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_saved_searches WHERE user_id=%s AND slug=%s",
                    (int(user_id), slug))
        row = cur.fetchone()
        return dict(row) if row else None


def list_searches(user_id=None, *, saved_only=False, limit=300, all_users=False):
    ensure_schema()
    where, params = [], []
    if not all_users:
        where.append("s.user_id=%s")
        params.append(int(user_id))
    if saved_only:
        where.append("s.saved")
    sql = (
        "SELECT s.*,u.email,u.full_name FROM app_saved_searches s JOIN app_users u ON u.id=s.user_id "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY s.updated_at DESC LIMIT %s")
    params.append(max(1, min(int(limit), 1000)))
    with db.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def set_search_saved(user_id, slug, saved: bool, title=None):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE app_saved_searches SET saved=%s,title=COALESCE(%s,title),updated_at=now() "
            "WHERE user_id=%s AND slug=%s RETURNING *",
            (bool(saved), (title or "").strip()[:180] or None, int(user_id), slug))
        row = cur.fetchone()
        if not row:
            raise ValueError("Search is not in this account.")
        return dict(row)


def set_search_notification(user_id, slug, enabled: bool):
    """Change the wait-vs-email choice while a search is running or after it completed.

    A message that was already sent is never made pending again merely by toggling the checkbox.
    The caller queues an immediate completion message when enabling an already-complete search.
    """
    ensure_schema()
    with db.cursor() as cur:
        cur.execute(
            """UPDATE app_saved_searches SET notify_email=%s,
                 notification_status=CASE
                   WHEN notification_status='sent' THEN 'sent'
                   WHEN %s THEN 'pending' ELSE 'not_requested' END,
                 updated_at=now()
               WHERE user_id=%s AND slug=%s RETURNING *""",
            (bool(enabled), bool(enabled), int(user_id), slug))
        row = cur.fetchone()
        if not row:
            raise ValueError("Search is not in this account.")
        return dict(row)


def can_access_search(user_id, slug) -> bool:
    return get_search(user_id, slug) is not None


def mark_search_complete(slug):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE app_saved_searches SET status='complete',completed_at=COALESCE(completed_at,now()),"
            "updated_at=now() WHERE slug=%s RETURNING *", (slug,))
        return [dict(r) for r in cur.fetchall()]


def mark_search_failed(slug):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("UPDATE app_saved_searches SET status='failed',updated_at=now() WHERE slug=%s",
                    (slug,))


def load_report_flags(user_id, slug):
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT publication_number,flag,note FROM app_report_flags "
                    "WHERE user_id=%s AND slug=%s", (int(user_id), slug))
        return {r["publication_number"]: {"flag": r["flag"], "note": r["note"]}
                for r in cur.fetchall()}


def save_report_flag(user_id, slug, publication_number, *, flag=None, note=None):
    ensure_schema()
    has_flag, has_note = flag is not None, note is not None
    flag = "" if flag is None else str(flag)[:20]
    if flag not in ("", "relevant", "maybe", "not"):
        raise ValueError("Invalid triage flag.")
    note = "" if note is None else str(note)[:4000]
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO app_report_flags(user_id,slug,publication_number,flag,note)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (user_id,slug,publication_number) DO UPDATE SET
                 flag=CASE WHEN %s THEN EXCLUDED.flag ELSE app_report_flags.flag END,
                 note=CASE WHEN %s THEN EXCLUDED.note ELSE app_report_flags.note END,
                 updated_at=now() RETURNING flag,note""",
            (int(user_id), slug, publication_number, flag, note, has_flag, has_note))
        return dict(cur.fetchone())


def enqueue_mail(*, to_email, kind, subject, body_text, dedupe_key,
                 user_id=None, search_slug=None):
    ensure_schema()
    to_email = _email(to_email)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO app_mail_outbox
               (dedupe_key,user_id,search_slug,to_email,kind,subject,body_text)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (dedupe_key) DO UPDATE SET
                 to_email=EXCLUDED.to_email,subject=EXCLUDED.subject,body_text=EXCLUDED.body_text,
                 status=CASE WHEN app_mail_outbox.status IN ('sent','sending')
                             THEN app_mail_outbox.status ELSE 'pending' END,
                 next_attempt_at=CASE WHEN app_mail_outbox.status IN ('sent','sending')
                                      THEN app_mail_outbox.next_attempt_at ELSE now() END,
                 last_error=CASE WHEN app_mail_outbox.status IN ('sent','sending')
                                 THEN app_mail_outbox.last_error ELSE NULL END
               RETURNING *""",
            (dedupe_key, int(user_id) if user_id else None, search_slug, to_email, kind,
             subject[:240], body_text))
        return dict(cur.fetchone())


def queue_completion_notifications(slug, report_url):
    """Durably queue one completion message per opted-in user/search pair."""
    rows = mark_search_complete(slug)
    queued = []
    for row in rows:
        if not row.get("notify_email") or row.get("notification_status") == "sent":
            continue
        user = get_user(row["user_id"])
        if not user or not user.get("is_active"):
            continue
        query = re.sub(r"\s+", " ", row.get("query") or "").strip()
        preview = query[:180] + ("…" if len(query) > 180 else "")
        mail = enqueue_mail(
            to_email=user["email"], user_id=user["id"], search_slug=slug,
            kind="search_complete", dedupe_key=f"search-complete:{user['id']}:{slug}",
            subject="Your patent prior-art search is ready",
            body_text=(f"Hello {user['full_name']},\n\nYour prior-art search is ready.\n\n"
                       f"Search: {preview}\n\nOpen the report:\n{report_url}\n\n"
                       "The report includes ranked references, grounded relevance explanations, "
                       "claim grids and a background-built top-50 full-text archive.\n"))
        queued.append(mail)
        notification_status = "sent" if mail.get("status") == "sent" else "queued"
        with db.cursor() as cur:
            cur.execute("UPDATE app_saved_searches SET notification_status=%s "
                        "WHERE user_id=%s AND slug=%s",
                        (notification_status, user["id"], slug))
    return queued


def create_password_reset(email: str, ttl_minutes=60):
    """Return (opaque_token, public_user), or (None, None) without revealing account existence."""
    row = get_user_by_email(email)
    if not row or not row.get("is_active"):
        return None, None
    token = secrets.token_urlsafe(36)
    digest = hashlib.sha256(token.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(minutes=max(10, min(ttl_minutes, 240)))
    with db.cursor() as cur:
        cur.execute("INSERT INTO app_password_reset_tokens(user_id,token_hash,expires_at) "
                    "VALUES (%s,%s,%s)", (row["id"], digest, expires))
    return token, public_user(row)


def reset_password(token: str, new_password: str):
    ensure_schema()
    _password(new_password)
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM app_password_reset_tokens WHERE token_hash=%s AND used_at IS NULL "
            "AND expires_at>now() FOR UPDATE", (digest,))
        row = cur.fetchone()
        if not row:
            raise ValueError("That reset link is invalid or has expired.")
        cur.execute(
            "UPDATE app_users SET password_hash=%s,session_version=session_version+1,"
            "updated_at=now() WHERE id=%s RETURNING *",
            (generate_password_hash(new_password), row["user_id"]))
        user = public_user(cur.fetchone())
        # Redeeming one reset credential invalidates every outstanding credential for the account.
        cur.execute("UPDATE app_password_reset_tokens SET used_at=now() "
                    "WHERE user_id=%s AND used_at IS NULL", (row["user_id"],))
        return user


def mail_stats():
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT status,count(*)::int AS n FROM app_mail_outbox GROUP BY status")
        return {r["status"]: r["n"] for r in cur.fetchall()}


def reset_schema_cache_for_tests():
    global _SCHEMA_READY
    _SCHEMA_READY = False
