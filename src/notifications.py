"""Durable transactional email for account recovery and completed searches.

Messages are inserted into Postgres before delivery.  A small daemon worker claims rows with
``FOR UPDATE SKIP LOCKED`` so a future multi-worker deployment cannot double-send.  SMTP is used
when configured; otherwise a local sendmail-compatible MTA is used.  Tests can select the
``capture`` transport, which records delivery without contacting anyone.
"""
from __future__ import annotations

import atexit
import os
import shutil
import smtplib
import subprocess
import sys
import threading
from email.message import EmailMessage
from pathlib import Path
import hashlib

import accounts
import db


PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "https://rotem.ai/patents").rstrip("/")
MAIL_FROM = (os.environ.get("MAIL_FROM") or "Rotem Patents <patents@rotem.ai>").strip()
MAIL_TRANSPORT = (os.environ.get("MAIL_TRANSPORT") or "auto").strip().lower()
POLL_SECONDS = max(5.0, float(os.environ.get("MAIL_POLL_SECONDS", "20")))

_START_LOCK = threading.Lock()
_THREAD = None
_STOP = threading.Event()
_WAKE = threading.Event()
_CAPTURED = []


class TransportUnavailable(RuntimeError):
    pass


def _sendmail_path():
    configured = (os.environ.get("SENDMAIL_PATH") or "").strip()
    candidates = [configured, shutil.which("sendmail"), "/usr/sbin/sendmail", "/usr/lib/sendmail"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def transport_status():
    """Non-secret status for the admin page and health checks."""
    if MAIL_TRANSPORT == "capture":
        return {"configured": True, "transport": "capture", "detail": "test capture"}
    smtp_host = (os.environ.get("SMTP_HOST") or "").strip()
    if MAIL_TRANSPORT == "smtp" or (MAIL_TRANSPORT == "auto" and smtp_host):
        return {"configured": bool(smtp_host), "transport": "smtp",
                "detail": smtp_host or "SMTP_HOST is missing"}
    path = _sendmail_path()
    return {"configured": bool(path), "transport": "sendmail",
            "detail": path or "no local sendmail-compatible MTA"}


def _message(row):
    msg = EmailMessage()
    msg["From"] = MAIL_FROM.replace("\r", " ").replace("\n", " ")
    msg["To"] = str(row["to_email"]).replace("\r", " ").replace("\n", " ")
    msg["Subject"] = str(row["subject"]).replace("\r", " ").replace("\n", " ")[:240]
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(row["body_text"])
    return msg


def _deliver(row):
    msg = _message(row)
    if MAIL_TRANSPORT == "capture":
        _CAPTURED.append({"to": row["to_email"], "subject": row["subject"],
                          "body": row["body_text"]})
        return

    smtp_host = (os.environ.get("SMTP_HOST") or "").strip()
    if MAIL_TRANSPORT == "smtp" or (MAIL_TRANSPORT == "auto" and smtp_host):
        if not smtp_host:
            raise TransportUnavailable("SMTP_HOST is not configured")
        port = int(os.environ.get("SMTP_PORT", "587"))
        timeout = float(os.environ.get("SMTP_TIMEOUT", "20"))
        use_ssl = (os.environ.get("SMTP_SSL", "0").lower() in ("1", "true", "yes"))
        client_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with client_cls(smtp_host, port, timeout=timeout) as client:
            if not use_ssl and os.environ.get("SMTP_STARTTLS", "1").lower() not in ("0", "false", "no"):
                client.starttls()
            username = os.environ.get("SMTP_USERNAME") or ""
            password = os.environ.get("SMTP_PASSWORD") or ""
            if username:
                client.login(username, password)
            client.send_message(msg)
        return

    path = _sendmail_path()
    if not path:
        raise TransportUnavailable("no SMTP host or sendmail-compatible MTA is configured")
    proc = subprocess.run([path, "-i", "-t"], input=msg.as_bytes(), capture_output=True,
                          timeout=30, check=False)
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"sendmail exited {proc.returncode}: {detail}")


def _claim_one():
    accounts.ensure_schema()
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_mail_outbox WHERE status IN ('pending','retry','sending') "
                "AND next_attempt_at<=now() ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1")
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
            # Lease the row. If this process dies after commit and before recording the result,
            # another worker can reclaim it after ten minutes instead of leaving it stuck forever.
            # The lease is comfortably longer than either transport's 30-second timeout.
            cur.execute("UPDATE app_mail_outbox SET status='sending',attempts=attempts+1,"
                        "next_attempt_at=now()+interval '10 minutes' "
                        "WHERE id=%s RETURNING *", (row["id"],))
            claimed = dict(cur.fetchone())
        conn.commit()
        return claimed
    finally:
        conn.close()


def _sent(row):
    with db.cursor() as cur:
        cur.execute("UPDATE app_mail_outbox SET status='sent',sent_at=now(),last_error=NULL "
                    "WHERE id=%s", (row["id"],))
        if row.get("kind") == "search_complete" and row.get("user_id") and row.get("search_slug"):
            cur.execute("UPDATE app_saved_searches SET notification_status='sent' "
                        "WHERE user_id=%s AND slug=%s",
                        (row["user_id"], row["search_slug"]))


def _failed(row, exc):
    unavailable = isinstance(exc, TransportUnavailable)
    attempts = int(row.get("attempts") or 1)
    terminal = attempts >= 5 and not unavailable
    delay_minutes = 5 if unavailable else min(60, 2 ** max(0, attempts - 1))
    with db.cursor() as cur:
        cur.execute(
            "UPDATE app_mail_outbox SET status=%s,last_error=%s,"
            "next_attempt_at=now()+(%s * interval '1 minute') WHERE id=%s",
            ("failed" if terminal else "retry", str(exc)[:500], delay_minutes, row["id"]))
        if terminal and row.get("kind") == "search_complete" and row.get("user_id"):
            cur.execute("UPDATE app_saved_searches SET notification_status='failed' "
                        "WHERE user_id=%s AND slug=%s",
                        (row["user_id"], row.get("search_slug")))


_NO_TRANSPORT_LOGGED = False


def deliver_pending(limit=20):
    """Deliver up to ``limit`` messages; returns counts and never raises on a bad message.

    AN INSTANCE THAT CANNOT SEND MUST NOT CLAIM. The outbox is one table in a Postgres that more
    than one instance of this app can share, and _claim_one leases the oldest due row to whichever
    worker asks first. A deployment configured deliberately WITHOUT a transport (the fable fix
    bench carries no MAIL_* on purpose) still runs this worker, wins that race for every row, and
    fails it as TransportUnavailable, which is non-terminal by design and so retries for ever.
    Observed 2026-08-18: outbox row 16 reached 53 attempts and no search notification had been
    delivered since the second instance came up, while the instance that COULD send sat idle.

    So a working transport is a precondition of CLAIMING, not a step inside delivery. It is
    re-checked each pass rather than cached at import, because a local MTA can appear underneath
    a running process.
    """
    global _NO_TRANSPORT_LOGGED
    status = transport_status()
    if not status.get("configured"):
        if not _NO_TRANSPORT_LOGGED:
            print("[mail] no transport on this instance (%s); leaving the outbox to an instance "
                  "that can send" % status.get("detail"), flush=True)
            _NO_TRANSPORT_LOGGED = True
        return {"sent": 0, "failed": 0, "skipped": "no transport"}
    _NO_TRANSPORT_LOGGED = False
    result = {"sent": 0, "failed": 0}
    for _ in range(max(0, min(int(limit), 100))):
        row = _claim_one()
        if not row:
            break
        try:
            _deliver(row)
            _sent(row)
            result["sent"] += 1
        except Exception as exc:
            _failed(row, exc)
            result["failed"] += 1
    return result


def _worker():
    while not _STOP.is_set():
        try:
            deliver_pending()
        except Exception as exc:
            # Database restarts must not kill delivery forever.  Do not print message bodies or
            # transport credentials; this one-line operational error is enough for the journal.
            print(f"[mail] worker retry: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
        _WAKE.wait(POLL_SECONDS)
        _WAKE.clear()


def start_worker():
    global _THREAD
    with _START_LOCK:
        if _THREAD and _THREAD.is_alive():
            return _THREAD
        _STOP.clear()
        _THREAD = threading.Thread(target=_worker, name="patent-mail-outbox", daemon=True)
        _THREAD.start()
        return _THREAD


def stop_worker():
    _STOP.set()
    _WAKE.set()


def kick():
    _WAKE.set()


def queue_search_completion(slug):
    report_url = f"{PUBLIC_BASE_URL}/report/{slug}"
    rows = accounts.queue_completion_notifications(slug, report_url)
    if rows:
        kick()
    return rows


def queue_draft_completion(user, project, version):
    """Queue one durable message for a newly published immutable draft version."""
    if not user or not user.get("is_active") or not user.get("email_on_completion", True):
        return None
    project_id = int(project["id"])
    version_no = int(version["version_no"])
    draft_url = f"{PUBLIC_BASE_URL}/drafts/{project_id}?version={version_no}"
    mail = accounts.enqueue_mail(
        to_email=user["email"], user_id=user["id"], kind="draft_complete",
        dedupe_key=f"draft-complete:{user['id']}:{project_id}:{version_no}",
        subject="Your US patent application working draft is ready",
        body_text=(
            f"Hello {user['full_name']},\n\n"
            f"Draft version {version_no} for “{str(project.get('title') or '')[:180]}” is ready.\n\n"
            f"Open, review and edit the draft:\n{draft_url}\n\n"
            "The draft is AI-assisted working material. Confirm every technical statement, "
            "claim limitation, inventor detail and filing requirement with qualified US patent "
            "counsel before filing.\n"
        ),
    )
    kick()
    return mail


def queue_invitation(email, full_name, invite_url, inviter_name=""):
    """The email that carries an invitation link. Sent once, at creation."""
    who = f" by {inviter_name}" if inviter_name else ""
    name = (full_name or "").strip() or "there"
    return accounts.enqueue_mail(
        to_email=email, user_id=None, search_slug=None, kind="invitation",
        dedupe_key=f"invite:{email.lower()}:{hashlib.sha256(invite_url.encode()).hexdigest()[:16]}",
        subject="You have been invited to Rotem Patents",
        body_text=(f"Hello {name},\n\nYou have been invited{who} to Rotem Patents, a "
                   "prior-art search and drafting tool.\n\nChoose a password and open your "
                   f"account here:\n{invite_url}\n\nThe link works once and expires in two "
                   "weeks. If you were not expecting this, ignore it — no account exists until "
                   "the link is used.\n"))


def queue_email_verification(user, verify_url):
    return accounts.enqueue_mail(
        to_email=user["email"], user_id=user["id"], search_slug=None, kind="verify_email",
        dedupe_key=f"verify:{user['id']}:{hashlib.sha256(verify_url.encode()).hexdigest()[:16]}",
        subject="Confirm your email address",
        body_text=(f"Hello {user.get('full_name') or ''},\n\nConfirm this address so we can "
                   "email you when a search finishes:\n"
                   f"{verify_url}\n\nThe link expires in seven days. Your account already works "
                   "— confirming only enables the completion emails.\n"))


def queue_password_reset(email, reset_url):
    token, user = accounts.create_password_reset(email)
    if not token or not user:
        return False
    url = reset_url(token)
    accounts.enqueue_mail(
        to_email=user["email"], user_id=user["id"], kind="password_reset",
        # Python's hash() is process-randomized, so it is unsuitable for a durable outbox key.
        # Hash the opaque token deterministically without storing the reset credential itself.
        dedupe_key=(f"password-reset:{user['id']}:"
                    f"{hashlib.sha256(token.encode()).hexdigest()}"),
        subject="Reset your Rotem Patents password",
        body_text=(f"Hello {user['full_name']},\n\nUse this link within one hour to reset your password:\n"
                   f"{url}\n\nIf you did not request this, you can ignore this message.\n"))
    kick()
    return True


def captured_messages():
    return list(_CAPTURED)


def init_app(app):
    """Start delivery in production. Test suites call deliver_pending explicitly."""
    if ("pytest" not in sys.modules and not app.config.get("TESTING")
            and os.environ.get("MAIL_WORKER", "1").lower() not in (
            "0", "false", "no")):
        start_worker()
    return app


atexit.register(stop_worker)
