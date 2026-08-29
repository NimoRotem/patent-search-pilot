"""Sign-in shared with the app that serves the ROOT of this domain.

A visitor of this domain has one account, in the root app's ``app_users`` table. This app has no
password of its own and never will. A second credential for the same person on the same domain is
a thing to lose, not a security measure.

* The root app signs a Flask session cookie with the key in ``$AUTH_ROOT/.secret_key`` at path
  ``/``, so the browser sends it here too, and the same key verifies it. A forged cookie cannot
  get in and no password is stored on this side.
* THE KEY AND THE ACCOUNT DATABASE ARE ONE SETTING ON PURPOSE. The key that verifies a cookie and
  the database asked about the user id inside it must belong to the same app. On 2026-08-27 the
  root of nimo.iptorch.com moved from patent-search-pilot to patent-fulltext, and the two number
  their users independently: id 1 is the owner at the root and a retired QA account in the old
  database. Verifying with the new key while looking the id up in the old one denied the owner and
  bounced him to the login page he had just used, which is indistinguishable from a wrong password.
  Two separate settings are what allowed that pair to drift, so there is now one, ``AUTH_ROOT``.
* A valid signature is necessary but not sufficient. The cookie lasts thirty days and carries
  ``session_version``, which the root app bumps on a password change, so deactivating a user or
  resetting their password closes this app to them as well.
* If Postgres cannot be reached we fail CLOSED. An outage in the account system must not turn a
  public endpoint into an open one.
"""
from __future__ import annotations

import os
import sys
import time
from functools import wraps
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from flask import Flask, jsonify, redirect, request
from flask.sessions import SecureCookieSessionInterface

#  PILOT_ROOT is the RETRIEVAL side: the search app's tree, its figures and its corpus. AUTH_ROOT
#  is the IDENTITY side, and defaults to it because on a host where one app does both they are the
#  same tree. Point AUTH_ROOT at whichever app owns the front door and the signing key and the
#  account database move together, which is the only combination that can be correct.
PILOT_ROOT = Path(os.environ.get("PILOT_ROOT", os.path.expanduser("~/patent-search-pilot")))
AUTH_ROOT = Path(os.environ.get("AUTH_ROOT", PILOT_ROOT))
SECRET_FILE = AUTH_ROOT / ".secret_key"
ENV_FILE = AUTH_ROOT / ".env"
LOGIN_URL = os.environ.get("PATENTS_LOGIN_URL", "https://nimo.iptorch.com/login")
COOKIE_NAME = os.environ.get("PILOT_SESSION_COOKIE", "session")
CACHE_SECONDS = float(os.environ.get("AUTH_USER_CACHE_SECONDS", "60"))
COOKIE_MAX_AGE = 30 * 24 * 3600

OPEN_PATHS = {"/healthz"}

_cache: dict[int, tuple[float, Optional[dict]]] = {}


class AuthUnavailable(RuntimeError):
    """The shared secret or the account database could not be reached."""


def _secret_key() -> str:
    value = (os.environ.get("SECRET_KEY") or "").strip()
    if value:
        return value
    try:
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AuthUnavailable(f"cannot read {SECRET_FILE}: {exc}") from exc


def _pg_settings() -> dict[str, str]:
    #  NO DEFAULTS. An unreadable env file used to fall back to the search app's database, so a
    #  misconfigured AUTH_ROOT would quietly ask the wrong store about a user id and answer with
    #  somebody else's account, or with a denial nobody could explain. Not being able to read it
    #  is an outage, and an outage here fails closed.
    values: dict[str, str] = {}
    wanted = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", 1)
            key = key.strip()
            if key in wanted:
                values[key] = raw.strip().strip('"').strip("'")
    except OSError as exc:
        raise AuthUnavailable(f"cannot read {ENV_FILE}: {exc}") from exc
    missing = [key for key in wanted if not values.get(key)]
    if missing:
        raise AuthUnavailable(f"{ENV_FILE} is missing {', '.join(missing)}")
    return values


_signer = Flask("fm-authgate-signer")
_signer.secret_key = _secret_key()
_signer.config.update(SESSION_COOKIE_NAME=COOKIE_NAME)
_interface = SecureCookieSessionInterface()


def read_session(raw_cookie: str) -> Optional[dict]:
    if not raw_cookie:
        return None
    serializer = _interface.get_signing_serializer(_signer)
    if serializer is None:
        return None
    try:
        return serializer.loads(raw_cookie, max_age=COOKIE_MAX_AGE)
    except Exception:
        return None


def _lookup(user_id: int) -> Optional[dict]:
    now = time.monotonic()
    hit = _cache.get(user_id)
    if hit and hit[0] > now:
        return hit[1]
    try:
        settings = _pg_settings()
        import psycopg

        with psycopg.connect(
                host=settings["PGHOST"], port=int(settings["PGPORT"]),
                dbname=settings["PGDATABASE"], user=settings["PGUSER"],
                password=settings["PGPASSWORD"], connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, email, is_active, session_version FROM app_users WHERE id=%s",
                    (int(user_id),))
                row = cursor.fetchone()
    except Exception as exc:
        # Could not ask. Do not cache a failure as an answer and do not let it authorise anyone.
        # SAY SO IN THE LOG: to the person in front of it a silent denial looks exactly like a
        # wrong password, and that is why the cutover above went a day without being noticed.
        print(f"authgate: cannot read account {user_id} from {ENV_FILE}: {exc!r}",
              file=sys.stderr, flush=True)
        return None
    user = None
    if row:
        user = {"id": int(row[0]), "email": str(row[1]), "is_active": bool(row[2]),
                "session_version": int(row[3])}
    _cache[user_id] = (now + CACHE_SECONDS, user)
    return user


def current_user() -> Optional[dict]:
    data = read_session(request.cookies.get(COOKIE_NAME, ""))
    if not data:
        return None
    try:
        user_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None
    user = _lookup(user_id)
    if not user or not user["is_active"]:
        return None
    stored = data.get("session_version")
    if stored is None or int(stored) != user["session_version"]:
        return None
    return user


# Paths whose callers are programs, not browsers. They get a 401 with a login URL rather than a
# redirect to a login PAGE: a fetch() follows the redirect, receives HTML and fails with
# "Unexpected token '<'", which tells the user nothing about having been signed out.
_API_PREFIXES = ("/api/",)


def _wants_json() -> bool:
    if request.path.startswith(_API_PREFIXES):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def login_redirect():
    prefix = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
    back = prefix + request.path
    if request.query_string:
        back += "?" + request.query_string.decode("utf-8", "ignore")
    return redirect(LOGIN_URL + "?next=" + quote(back, safe=""), code=302)


def install(app: Flask) -> None:
    """Gate every route except the liveness probe."""

    @app.before_request
    def _gate():
        if request.path in OPEN_PATHS:
            return None
        user = current_user()
        if user is None:
            if _wants_json():
                return jsonify({"error": "authentication required", "login": LOGIN_URL}), 401
            return login_redirect()
        request.environ["fm.user"] = user
        return None


def require_user(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if request.environ.get("fm.user") is None:
            return jsonify({"error": "authentication required"}), 401
        return view(*args, **kwargs)

    return wrapper


def user() -> dict:
    return request.environ.get("fm.user") or {}
