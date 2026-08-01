"""Auth gate + rate limiting + spend caps for the pilot webapp.

Threat model: before this, POST /run triggered ~40 Vertex LLM calls and ~3 minutes of CPU for any
anonymous caller, /api/ref made live PAID SerpApi calls per card, and POST /api/flags/<slug> wrote
arbitrary JSON to disk under an attacker-chosen slug. The app is reachable at
https://rotem.ai/patents-data/ so all of that was a public cost-amplifier.

Three layers, no new infrastructure:
  1. A shared-secret session login over the whole app (single intended user).
  2. Per-IP + global token buckets on the expensive routes only.
  3. Hard caps: concurrent report generations, and LLM-spending runs per day.

Everything is configured from .env. Nothing here writes a secret to a tracked file.
"""
from __future__ import annotations
import os, hmac, json, time, threading, secrets
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from flask import (Blueprint, request, session, redirect, url_for, render_template_string,
                   render_template, jsonify, current_app, g, abort)

# Every secret below is read from the environment AT IMPORT TIME, so `load_dotenv()` must already
# have run. That is `config`'s job. Until now this module never imported it and worked only by
# accident: webapp.py imports `db` (which pulls in `config`) a couple of lines before it imports
# `auth`. Reordering those two imports would have silently emptied APP_PASSWORD and disabled the
# whole gate. Import it explicitly so the dependency is real rather than incidental.
import config  # noqa: F401  (imported for its load_dotenv side effect)
import accounts
import notifications

bp = Blueprint("auth", __name__)


def _env(name, default=""):
    return (os.environ.get(name, default) or "").strip()


def _flag(name, default="1"):
    return _env(name, default).lower() not in ("0", "false", "no", "")


def _num(name, default):
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return float(default)


APP_PASSWORD = _env("APP_PASSWORD")
API_TOKEN = _env("APP_API_TOKEN")
ACCOUNTS_ENABLED = _flag("USER_ACCOUNTS_ENABLED", "1")
SESSION_HOURS = _num("SESSION_HOURS", 720)          # 30 days; single-user tool, don't nag
# Requests arriving on the loopback interface are on-box (port 8631 is VPC-only behind the GCP
# firewall, and nginx proxies from another VM so its traffic is NOT loopback). Exempting loopback
# keeps regression.sh / warm_reports / cron able to hit the app without embedding the password.
TRUST_LOOPBACK = _flag("AUTH_TRUST_LOOPBACK", "1")
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
# How many reverse proxies sit in front of us, each appending one hop to X-Forwarded-For.
# Exactly one today (nginx). See client_ip() for why this is the security-critical number.
TRUSTED_PROXY_HOPS = max(1, int(_num("TRUSTED_PROXY_HOPS", 1)))

# Endpoints that must stay reachable without a session.
#
# `shared_report` and `shared_report_logo` are open because that is the whole point of a share
# link: a client with no account opens one report. The capability is the token itself, which is
# stored hashed, resolves to exactly ONE slug, is revocable, and grants nothing else — every
# route that writes, exports, re-runs or reaches another report stays gated below.
_OPEN_ENDPOINTS = {"healthz", "auth.login", "auth.logout", "auth.register",
                   "auth.forgot_password", "auth.reset_password", "static",
                   "shared_report", "shared_report_logo",
                   # An invitee has no account yet; a verification link may be opened from a mail
                   # client with no session. Both are single-use, expiring, hashed tokens.
                   "auth.accept_invitation", "auth.verify_email"}


# ---------------------------------------------------------------------------------------------
# client identity
# ---------------------------------------------------------------------------------------------
def client_ip():
    """The real caller, as far as we can actually prove it.

    REMOTE_ADDR is the TCP peer and cannot be forged; X-Forwarded-For can be, entirely. We only
    consult XFF when the peer is our own reverse proxy.

    Which ELEMENT of XFF to believe is the whole game. nginx fronts this app with

        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

    and `$proxy_add_x_forwarded_for` APPENDS the connecting peer to whatever the client already
    sent. A request that arrives carrying a forged header therefore reaches us as

        X-Forwarded-For: <whatever the attacker wrote>, <real client IP>

    so element [0] is 100% attacker-controlled and element [-1] is the one our own proxy wrote.
    Reading [0] (as this did) keyed every per-IP bucket on a value the caller picked: rotating one
    header handed out an unlimited supply of fresh login-attempt buckets.

    Generally, with N trusted proxies each appending one hop, the last N entries are ours and the
    first trustworthy one is at index -N. TRUSTED_PROXY_HOPS makes that explicit and configurable
    rather than hardcoding "the last one"; if a CDN is ever put in front of nginx, set it to 2.
    """
    peer = request.environ.get("REMOTE_ADDR", "") or "-"
    if not _peer_is_trusted_proxy(peer):
        return peer
    parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    if not parts:
        return peer
    # Clamp: a short chain means the client sent fewer hops than we expected (or none at all), so
    # fall back to the earliest entry we have rather than indexing off the front of the list.
    idx = max(0, len(parts) - TRUSTED_PROXY_HOPS)
    return parts[idx]


def _peer_is_trusted_proxy(peer):
    return (peer in _LOOPBACK or peer.startswith("10.")
            or peer.startswith("172.") or peer.startswith("192.168."))


def is_loopback():
    return (request.environ.get("REMOTE_ADDR", "") or "") in _LOOPBACK


# ---------------------------------------------------------------------------------------------
# token bucket rate limiting
# ---------------------------------------------------------------------------------------------
class TokenBucket:
    """Classic token bucket: `rate` tokens/sec accruing up to `burst`."""

    def __init__(self, rate, burst):
        self.rate, self.burst = float(rate), float(burst)
        self.tokens = float(burst)
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def take(self, n=1.0):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            if self.tokens >= n:
                self.tokens -= n
                return True, 0.0
            deficit = n - self.tokens
            return False, deficit / self.rate if self.rate > 0 else 3600.0


class Limiter:
    """Per-IP buckets plus one global bucket, for a single named class of expensive work."""

    def __init__(self, name, rate, burst, global_rate, global_burst, max_ips=4096,
                 global_exempts_known_good=False):
        self.name = name
        self.rate, self.burst = rate, burst
        self.per_ip = {}
        self.global_bucket = TokenBucket(global_rate, global_burst)
        self.lock = threading.Lock()
        self.max_ips = max_ips
        # When set, IPs that have recently completed this action successfully skip the global
        # backstop. See the login limiter for the reasoning.
        self.global_exempts_known_good = global_exempts_known_good
        self.known_good = {}          # ip -> expiry monotonic

    def _bucket(self, ip):
        with self.lock:
            b = self.per_ip.get(ip)
            if b is None:
                if len(self.per_ip) >= self.max_ips:     # crude bound; stops unbounded growth
                    self.per_ip.clear()
                b = self.per_ip[ip] = TokenBucket(self.rate, self.burst)
            return b

    def mark_known_good(self, ip, ttl=86400.0):
        """Record that `ip` completed this action legitimately (e.g. logged in)."""
        if not self.global_exempts_known_good:
            return
        with self.lock:
            if len(self.known_good) > self.max_ips:
                self.known_good.clear()
            self.known_good[ip] = time.monotonic() + ttl

    def _is_known_good(self, ip):
        with self.lock:
            exp = self.known_good.get(ip)
            if exp is None:
                return False
            if exp < time.monotonic():
                self.known_good.pop(ip, None)
                return False
            return True

    def check(self, ip):
        ok, retry = self._bucket(ip).take()
        if not ok:
            return False, retry, f"per-IP limit for {self.name}"
        ok, retry = self.global_bucket.take()
        if not ok:
            # A uniform global reject means one abusive source denies service to everybody. For
            # login that is a trivial DoS: 30 requests locked out ALL logins for ~15 minutes,
            # including the legitimate user, which is a worse outcome than the brute-force the
            # bucket exists to stop. An IP that has authenticated successfully before is not the
            # flood, so let it past the backstop; its own per-IP bucket (checked above, and now
            # unspoofable) still bounds it.
            if self.global_exempts_known_good and self._is_known_good(ip):
                return True, 0.0, ""
            return False, retry, f"global limit for {self.name}"
        return True, 0.0, ""


# Expensive routes -> limiter. Rates are per second; burst is what a human can do in a flurry.
# Cheap routes (static, report reads, /status, /events, flags GET) are deliberately unlimited.
_LIMITERS = {
    # a full agent run: ~40 Vertex calls + ~3 min CPU
    "run":      Limiter("search runs",  _num("RL_RUN_RATE", 1 / 60.0),   _num("RL_RUN_BURST", 5),
                        _num("RL_RUN_GRATE", 1 / 30.0),  _num("RL_RUN_GBURST", 10)),
    # live PAID SerpApi call per uncached card
    "api_ref":  Limiter("reference enrichment", _num("RL_REF_RATE", 2.0), _num("RL_REF_BURST", 60),
                        _num("RL_REF_GRATE", 4.0), _num("RL_REF_GBURST", 120)),
    "api_graph": Limiter("citation graph", _num("RL_GRAPH_RATE", 2.0), _num("RL_GRAPH_BURST", 40),
                         _num("RL_GRAPH_GRATE", 4.0), _num("RL_GRAPH_GBURST", 80)),
    # index-backed ANN over 1.82M vectors
    "api_morelike": Limiter("more-like-this", _num("RL_MORE_RATE", 1.0), _num("RL_MORE_BURST", 20),
                            _num("RL_MORE_GRATE", 2.0), _num("RL_MORE_GBURST", 40)),
    # per-reference claim chart: one Vertex generate call per request
    "api_chart": Limiter("claim charts", _num("RL_CHART_RATE", 1.0), _num("RL_CHART_BURST", 20),
                         _num("RL_CHART_GRATE", 2.0), _num("RL_CHART_GBURST", 40)),
    # document/patent-link ingestion: pdftotext/pdftoppm + a Gemini condense + a Gemini vision
    # call (and, for a link, a paid SerpApi detail fetch). Bounded like the other Vertex routes.
    "extract":  Limiter("document extraction", _num("RL_EXTRACT_RATE", 1 / 20.0), _num("RL_EXTRACT_BURST", 6),
                        _num("RL_EXTRACT_GRATE", 1 / 10.0), _num("RL_EXTRACT_GBURST", 20)),
    # One Vertex call each: rewriting a query, drafting a narrative section of a client report,
    # and generating a patent figure. All are cheap individually and all are a button somebody can
    # hold down, so they get their own buckets rather than riding on a neighbour's.
    "api_improve_query": Limiter("query improvement", _num("RL_IMPROVE_RATE", 0.5),
                                 _num("RL_IMPROVE_BURST", 10), _num("RL_IMPROVE_GRATE", 1.0),
                                 _num("RL_IMPROVE_GBURST", 30)),
    "api_report_suggest": Limiter("report narrative drafting", _num("RL_SUGGEST_RATE", 0.5),
                                  _num("RL_SUGGEST_BURST", 8), _num("RL_SUGGEST_GRATE", 1.0),
                                  _num("RL_SUGGEST_GBURST", 20)),
    "draft_drawing": Limiter("patent figure generation", _num("RL_DRAWING_RATE", 1 / 15.0),
                             _num("RL_DRAWING_BURST", 6), _num("RL_DRAWING_GRATE", 1 / 6.0),
                             _num("RL_DRAWING_GBURST", 20)),
    # chunked Vertex translation of a full reference
    "api_translate": Limiter("translation", _num("RL_TRANS_RATE", 1.0), _num("RL_TRANS_BURST", 20),
                             _num("RL_TRANS_GRATE", 2.0), _num("RL_TRANS_GBURST", 40)),
    # PDF/DOCX rendering
    "export":   Limiter("exports", _num("RL_EXPORT_RATE", 0.5), _num("RL_EXPORT_BURST", 10),
                        _num("RL_EXPORT_GRATE", 1.0), _num("RL_EXPORT_GBURST", 20)),
    # One long-form model call (plus bounded retries) queued into the durable drafting worker.
    "draft_generate": Limiter(
        "US draft generation", _num("RL_DRAFT_RATE", 1 / 120.0), _num("RL_DRAFT_BURST", 3),
        _num("RL_DRAFT_GRATE", 1 / 30.0), _num("RL_DRAFT_GBURST", 12)),
    # Password guessing. The handler's time.sleep(0.5) only serialises a SINGLE connection, so N
    # parallel requests still get N guesses per 0.5 s — measured: 10 concurrent wrong passwords all
    # answered in 2.0 s, unthrottled. This bucket is the actual defence: ~10 attempts per 15 min per
    # IP (burst 10, refill 1/90 s), with a global ceiling so a botnet spread across many source IPs
    # can't sidestep the per-IP bucket either. Mirrors the login bucket App A already had.
    # The per-IP bucket is the real defence and, now that client_ip() can no longer be steered by
    # a forged X-Forwarded-For, it is actually enforceable -- so the global backstop no longer has
    # to be the hair trigger it was. It was 30 burst / 1 per 30 s, which any single source could
    # drain in seconds to lock every other user out for ~15 minutes. Widened to a genuine
    # botnet-scale ceiling (120 burst, 12/min sustained), and IPs that have logged in before skip
    # it entirely so an attack cannot lock out the intended user.
    "auth.login": Limiter("login attempts", _num("RL_LOGIN_RATE", 1 / 90.0), _num("RL_LOGIN_BURST", 10),
                          _num("RL_LOGIN_GRATE", 1 / 5.0), _num("RL_LOGIN_GBURST", 120),
                          global_exempts_known_good=True),
    "auth.forgot_password": Limiter(
        "password reset requests", _num("RL_RESET_RATE", 1 / 120.0), _num("RL_RESET_BURST", 5),
        _num("RL_RESET_GRATE", 1 / 15.0), _num("RL_RESET_GBURST", 30)),
    # Public signup is intentionally available, but a bot must not be able to fill the users table
    # or spend unbounded password-hashing CPU. A human can still retry a form several times.
    "auth.register": Limiter(
        "account registrations", _num("RL_REGISTER_RATE", 1 / 300.0),
        _num("RL_REGISTER_BURST", 4), _num("RL_REGISTER_GRATE", 1 / 30.0),
        _num("RL_REGISTER_GBURST", 20)),
}


def limiter_for(endpoint):
    return _LIMITERS.get(endpoint)


def reset_limits():
    """Test helper: refill every bucket."""
    for lim in _LIMITERS.values():
        lim.per_ip.clear()
        lim.known_good.clear()
        lim.global_bucket.tokens = lim.global_bucket.burst


# ---------------------------------------------------------------------------------------------
# hard caps: concurrent generations + daily LLM-spending runs
# ---------------------------------------------------------------------------------------------
class RunGate:
    """Bounds how much money/CPU the app can burn.

    - `max_concurrent`: how many report generations may be in flight at once. This REPLACES the old
      global `_GEN_LOCK`: concurrency is now bounded by a resource budget we chose, not by a
      thread-safety accident.
    - `daily_cap`: how many LLM-spending runs may start per UTC day. Persisted so a restart (or a
      crash loop) can't be used to reset the meter.
    """

    def __init__(self, max_concurrent, daily_cap, state_path=None):
        self.max_concurrent = int(max_concurrent)
        self.daily_cap = int(daily_cap)
        self.state_path = Path(state_path) if state_path else None
        self.active = 0
        self.lock = threading.Lock()
        self.day, self.count = self._today(), 0
        self._load()

    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _load(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text())
            if d.get("day") == self._today():
                self.day, self.count = d["day"], int(d.get("count", 0))
        except Exception:
            pass

    def _save(self):
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"day": self.day, "count": self.count}))
        except Exception:
            pass

    def try_begin(self):
        """Reserve a slot. Returns (ok, reason). Caller MUST call end() if ok."""
        with self.lock:
            today = self._today()
            if today != self.day:                        # UTC rollover
                self.day, self.count = today, 0
            if self.active >= self.max_concurrent:
                return False, (f"Server is already running {self.active} searches "
                               f"(limit {self.max_concurrent}). Try again in a minute.")
            if self.count >= self.daily_cap:
                return False, (f"Daily search budget reached ({self.daily_cap} runs). "
                               "Resets at 00:00 UTC.")
            self.active += 1
            self.count += 1
            self._save()
            return True, ""

    def end(self):
        with self.lock:
            self.active = max(0, self.active - 1)

    def stats(self):
        with self.lock:
            return {"active": self.active, "max_concurrent": self.max_concurrent,
                    "today": self.count, "daily_cap": self.daily_cap, "day": self.day}


run_gate = None


def init_run_gate(state_path):
    global run_gate
    run_gate = RunGate(_num("MAX_CONCURRENT_RUNS", 2), _num("DAILY_RUN_CAP", 50), state_path)
    return run_gate


# ---------------------------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------------------------
def auth_enabled(app=None):
    """Auth is on when a password is configured.

    Off under app.config['TESTING'] so the existing hermetic suite (which drives the Flask test
    client directly) keeps exercising the real handlers rather than the login page. Tests that DO
    want the gate set config['FORCE_AUTH'] = True. TESTING is never set in production."""
    app = app or current_app
    if app.config.get("AUTH_DISABLED"):
        return False
    if app.config.get("TESTING") and not app.config.get("FORCE_AUTH"):
        return False
    return accounts_enabled(app) or bool(app.config.get("APP_PASSWORD") or APP_PASSWORD)


def accounts_enabled(app=None):
    """Named-account mode. Tests keep the historical password gate unless they opt in."""
    app = app or current_app
    if app.config.get("ACCOUNTS_DISABLED"):
        return False
    if app.config.get("TESTING") and not app.config.get("FORCE_ACCOUNTS"):
        return False
    return bool(app.config.get("ACCOUNTS_ENABLED", ACCOUNTS_ENABLED))


def _password(app=None):
    app = app or current_app
    return app.config.get("APP_PASSWORD") or APP_PASSWORD


def _wants_json():
    """API-ish callers get 401 JSON; browsers get a 302 to the login form. EventSource cannot
    follow a login redirect meaningfully, so /events is treated as an API caller."""
    if request.path.startswith(("/api/", "/status/", "/events/")):
        return True
    accept = request.headers.get("Accept", "")
    if "text/event-stream" in accept:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    return "application/json" in accept and "text/html" not in accept


def current_user():
    """Active named user for this request, cached on Flask ``g``."""
    if hasattr(g, "patent_user"):
        return g.patent_user
    user = None
    uid = session.get("user_id")
    if uid:
        try:
            candidate = accounts.get_user(uid)
            current_version = int((candidate or {}).get("session_version") or 1)
            stored_version = session.get("session_version")
            # Password changes increment the database value. A cookie without a version predates
            # this revocation mechanism and cannot safely be distinguished from a stolen session,
            # so it must sign in again once after the migration.
            if candidate and candidate.get("is_active") and stored_version is not None and (
                    int(stored_version) == current_version):
                user = candidate
            elif candidate:
                session.clear()
        except Exception:
            user = None
    g.patent_user = user
    return user


def is_legacy_admin():
    return session.get("auth") is True and not session.get("user_id")


def is_admin():
    user = current_user()
    return is_legacy_admin() or bool(user and user.get("is_admin"))


def _authenticated():
    if current_user() is not None or session.get("auth") is True:
        return True
    # Bearer / X-API-Key for scripts and cron, if configured.
    tok = API_TOKEN or current_app.config.get("APP_API_TOKEN")
    if tok:
        hdr = request.headers.get("Authorization", "")
        supplied = hdr[7:].strip() if hdr.lower().startswith("bearer ") else \
            request.headers.get("X-API-Key", "")
        if supplied and hmac.compare_digest(supplied, tok):
            return True
    return False


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = session["csrf_token"] = secrets.token_urlsafe(32)
    return token


def require_csrf():
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("csrf_token") or ""
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        abort(400, "The form expired. Reload the page and try again.")


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _authenticated():
            return redirect(url_for("auth.login", next=request.path))
        if not is_admin():
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def _safe_next(raw):
    """Only ever redirect to a same-site path — never an absolute URL (open-redirect guard)."""
    if not raw:
        return None
    p = urlparse(raw)
    if p.scheme or p.netloc or not raw.startswith("/"):
        return None
    return raw


_LOGIN_HTML = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Sign in — rotemAI patent search</title>
<style>
 body{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e6e8ee;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 form{background:#171a21;border:1px solid #262b36;border-radius:12px;padding:28px;width:min(360px,90vw)}
 h1{font-size:17px;margin:0 0 4px} p{color:#8b93a7;font-size:13px;margin:0 0 18px}
 label{display:block;font-size:12px;color:#8b93a7;margin:0 0 6px;font-weight:600}
 input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid #2d3341;
       background:#0f1115;color:#e6e8ee;font-size:15px}
 button{width:100%;margin-top:12px;padding:10px;border:0;border-radius:8px;background:#3b82f6;
        color:#fff;font-size:15px;font-weight:600;cursor:pointer}
 .err{color:#f87171;font-size:13px;margin-top:10px}
</style>
<form method=post>
  <h1>rotemAI patent search</h1>
  <p id=hint>This instance is private. Enter the access password.</p>
  <label for=password>Access password</label>
  <input id=password type=password name=password autofocus autocomplete=current-password
         placeholder="Password" aria-describedby="hint"
         {% if error %}aria-invalid=true aria-errormessage=loginerr{% endif %}>
  <button type=submit>Sign in</button>
  {% if error %}<div class=err id=loginerr role=alert>{{ error }}</div>{% endif %}
</form>"""


# Shown instead of raw JSON when a BROWSER (not a fetch/XHR caller) trips a rate limit.
_TOOMANY_HTML = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Slow down — rotemAI patent search</title>
<style>
 body{font:15px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e6e8ee;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 main{background:#171a21;border:1px solid #262b36;border-radius:12px;padding:28px;width:min(430px,90vw)}
 h1{font-size:18px;margin:0 0 8px} p{color:#8b93a7;font-size:14px;margin:0 0 12px}
 b{color:#e6e8ee} a{color:#60a5fa}
</style>
<main role=alert>
  <h1>Too many requests</h1>
  <p>You hit the {{ why }}. Nothing was lost — this is just a speed bump so one browser tab
     can&rsquo;t exhaust the server&rsquo;s budget.</p>
  <p>Try again in about <b>{{ retry_after }} second{{ '' if retry_after == 1 else 's' }}</b>.</p>
  <p><a href="javascript:history.back()">&larr; Go back</a></p>
</main>"""


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    inline = (request.headers.get("X-Reauth") == "1" or
              "application/json" in request.headers.get("Accept", ""))
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        supplied = request.form.get("password", "")
        if accounts_enabled() and email:
            try:
                user = accounts.authenticate(email, supplied)
            except Exception:
                user = None
                error = "Accounts are temporarily unavailable. The administrator login still works."
            if user:
                session.clear()
                session["user_id"] = user["id"]
                session["session_version"] = int(user.get("session_version") or 1)
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                _LIMITERS["auth.login"].mark_known_good(client_ip())
                if inline:
                    return jsonify({"ok": True, "csrf_token": session["csrf_token"],
                                    "email": user["email"]})
                nxt = _safe_next(request.form.get("next") or request.args.get("next"))
                if nxt:
                    return redirect((request.script_root or "") + nxt)
                return redirect(url_for("index"))
            if not error:
                error = "Email or password is incorrect."
            time.sleep(0.5)
            if inline:
                return jsonify({"ok": False, "error": error}), 401
            return render_template("login.html", error=error,
                                   next_path=_safe_next(request.form.get("next") or request.args.get("next"))), 401
        expected = _password()
        # constant-time: don't leak the password length/prefix via timing
        if expected and hmac.compare_digest(supplied, expected):
            session.clear()
            session["auth"] = True
            session["legacy_admin"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            # This IP demonstrably holds the password, so it is not the brute-force. Remember it
            # so a flood from somewhere else can't lock the real user out via the global bucket.
            _LIMITERS["auth.login"].mark_known_good(client_ip())
            if inline:
                return jsonify({"ok": True, "csrf_token": session["csrf_token"]})
            nxt = _safe_next(request.form.get("next") or request.args.get("next"))
            if nxt:
                # `next` is app-relative; re-attach the proxy prefix (/patents-data) so the
                # redirect stays valid behind nginx.
                return redirect((request.script_root or "") + nxt)
            return redirect(url_for("index"))
        error = "Incorrect password."
        time.sleep(0.5)                      # blunt the brute-force rate
        if inline:
            return jsonify({"ok": False, "error": error}), 401
    if accounts_enabled():
        return render_template("login.html", error=error,
                               next_path=_safe_next(request.args.get("next"))), (401 if error else 200)
    return render_template_string(_LOGIN_HTML, error=error), (401 if error else 200)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if not accounts_enabled():
        return redirect(url_for("auth.login"))
    error = ""
    values = {"full_name": request.form.get("full_name", ""),
              "email": request.form.get("email", "")}
    if request.method == "POST":
        require_csrf()
        password = request.form.get("password", "")
        if password != request.form.get("password_confirm", ""):
            error = "Passwords do not match."
        else:
            try:
                user = accounts.create_user(values["email"], values["full_name"], password)
                #  Verification confirms we can REACH the address; it does not gate the account.
                #  Locking somebody out of a tool they just signed up for because a mail relay is
                #  slow is worse than the risk it removes, and the completion email — the only
                #  thing that needs a working address — is what verification actually protects.
                try:
                    token = accounts.create_email_verification(user["id"])
                    notifications.queue_email_verification(
                        user, f"{notifications.PUBLIC_BASE_URL}/verify-email/{token}")
                except Exception:
                    pass
                session.clear()
                session["user_id"] = user["id"]
                session["session_version"] = int(user.get("session_version") or 1)
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                return redirect(url_for("index"))
            except ValueError as exc:
                error = str(exc)
            except Exception:
                error = "Account creation is temporarily unavailable. Please try again shortly."
    return render_template("register.html", error=error, values=values), (400 if error else 200)


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not accounts_enabled():
        return redirect(url_for("auth.login"))
    submitted = False
    if request.method == "POST":
        require_csrf()
        email = request.form.get("email", "")
        try:
            def reset_url(token):
                return f"{notifications.PUBLIC_BASE_URL}/reset-password/{token}"
            notifications.queue_password_reset(email, reset_url)
        except Exception:
            # Deliberately generic: this endpoint must not enumerate registered addresses, and a
            # temporary mail problem should not change the response for a known account.
            pass
        submitted = True
    return render_template("forgot_password.html", submitted=submitted)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not accounts_enabled():
        return redirect(url_for("auth.login"))
    error = ""
    if request.method == "POST":
        require_csrf()
        password = request.form.get("password", "")
        if password != request.form.get("password_confirm", ""):
            error = "Passwords do not match."
        else:
            try:
                user = accounts.reset_password(token, password)
                session.clear()
                session["user_id"] = user["id"]
                session["session_version"] = int(user.get("session_version") or 1)
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                return redirect(url_for("index"))
            except ValueError as exc:
                error = str(exc)
            except Exception:
                error = "Password reset is temporarily unavailable."
    return render_template("reset_password.html", error=error, token=token), (400 if error else 200)


@bp.route("/account", methods=["GET", "POST"])
def account():
    user = current_user()
    if not user:
        if is_legacy_admin():
            return redirect(url_for("auth.admin_users"))
        return redirect(url_for("auth.login", next="/account"))
    message = request.args.get("message", "")
    error = request.args.get("error", "")
    if request.method == "POST":
        require_csrf()
        action = request.form.get("action")
        try:
            if action == "profile":
                user = accounts.update_profile(
                    user["id"], full_name=request.form.get("full_name", ""),
                    email_on_completion=request.form.get("email_on_completion") == "1",
                    organization=request.form.get("organization", ""),
                    default_applicant=request.form.get("default_applicant", ""),
                    default_inventors=request.form.get("default_inventors", ""),
                    preferred_jurisdiction=request.form.get("preferred_jurisdiction", "US"))
                g.patent_user = user
                message = "Account preferences saved."
            elif action == "password":
                new = request.form.get("new_password", "")
                if new != request.form.get("new_password_confirm", ""):
                    raise ValueError("New passwords do not match.")
                user = accounts.change_password(
                    user["id"], request.form.get("current_password", ""), new)
                session["session_version"] = int(user.get("session_version") or 1)
                g.patent_user = user
                message = "Password changed."
        except ValueError as exc:
            error = str(exc)
        except Exception:
            error = "The account could not be updated right now."
    try:
        activity = accounts.account_activity(user["id"])
    except Exception:
        activity = {"searches": 0, "saved": 0, "completed": 0, "emailed": 0}
    return render_template("account.html", user=user, activity=activity,
                           message=message, error=error)


@bp.route("/admin/users")
@admin_required
def admin_users():
    try:
        users = accounts.list_users()
        stats = accounts.mail_stats()
        store_error = None
    except Exception as exc:
        users, stats = [], {}
        store_error = str(exc)[:200]
    try:
        invitations = accounts.list_invitations()
    except Exception:
        invitations = []
    return render_template("admin_users.html", users=users, mail_stats=stats,
                           mail_transport=notifications.transport_status(),
                           store_error=store_error, invitations=invitations,
                           message=request.args.get("message", ""),
                           error=request.args.get("error", ""))


@bp.route("/admin/users/<int:user_id>", methods=["POST"])
@admin_required
def admin_update_user(user_id):
    require_csrf()
    action = request.form.get("action")
    kwargs = {}
    if action == "promote":
        kwargs["is_admin"] = True
    elif action == "demote":
        kwargs["is_admin"] = False
    elif action == "activate":
        kwargs["is_active"] = True
    elif action == "deactivate":
        kwargs["is_active"] = False
    else:
        abort(400)
    try:
        accounts.update_user_role(user_id, **kwargs)
        return redirect(url_for("auth.admin_users"))
    except ValueError as exc:
        return render_template("notfound.html", slug=str(exc)), 400


@bp.route("/verify-email/<token>")
def verify_email(token):
    user = accounts.verify_email(token)
    if not user:
        return render_template("notfound.html",
                               slug="that confirmation link has expired or was already used"), 404
    return render_template("verified.html", user=user)


@bp.route("/account/resend-verification", methods=["POST"])
def resend_verification():
    user = current_user()
    if not user:
        return redirect(url_for("auth.login"))
    require_csrf()
    try:
        token = accounts.create_email_verification(user["id"])
        notifications.queue_email_verification(
            user, f"{notifications.PUBLIC_BASE_URL}/verify-email/{token}")
        notifications.kick()
    except Exception:
        return redirect(url_for("auth.account", error="Could not send that just now."))
    return redirect(url_for("auth.account", message="Confirmation email sent."))


@bp.route("/invite/<token>", methods=["GET", "POST"])
def accept_invitation(token):
    """Open an invitation: the invitee chooses their own password, then is signed in.

    Deliberately outside the auth gate — the whole point is that the person has no account yet.
    The token is the capability, is single-use, expires, and creates exactly the account the
    administrator named.
    """
    invite = accounts.get_invitation(token)
    if not invite:
        return render_template("notfound.html",
                               slug="that invitation has expired, been used, or was withdrawn"), 404
    error = ""
    if request.method == "POST":
        password = request.form.get("password", "")
        if password != request.form.get("password_confirm", ""):
            error = "Passwords do not match."
        else:
            try:
                user = accounts.accept_invitation(token, request.form.get("full_name", ""), password)
                session.clear()
                session["user_id"] = user["id"]
                session["session_version"] = int(user.get("session_version") or 1)
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                return redirect(url_for("index"))
            except ValueError as exc:
                error = str(exc)
            except Exception:
                error = "Could not open that account. Please try again."
    return render_template("invite.html", invite=invite, error=error, token=token)


@bp.route("/admin/invitations", methods=["POST"])
@admin_required
def admin_invite():
    require_csrf()
    me = current_user()
    email = request.form.get("email", "")
    try:
        token, invite = accounts.create_invitation(
            email, request.form.get("full_name", ""),
            is_admin=request.form.get("is_admin") == "1",
            invited_by=(me or {}).get("id"))
        notifications.queue_invitation(
            invite["email"], invite.get("full_name") or "",
            f"{notifications.PUBLIC_BASE_URL}/invite/{token}",
            inviter_name=(me or {}).get("full_name") or "")
        notifications.kick()
        return redirect(url_for("auth.admin_users",
                                message=f"Invitation sent to {invite['email']}."))
    except ValueError as exc:
        return redirect(url_for("auth.admin_users", error=str(exc)))
    except Exception:
        return redirect(url_for("auth.admin_users", error="Could not send that invitation."))


@bp.route("/admin/invitations/<int:invitation_id>/revoke", methods=["POST"])
@admin_required
def admin_revoke_invite(invitation_id):
    require_csrf()
    accounts.revoke_invitation(invitation_id)
    return redirect(url_for("auth.admin_users", message="Invitation withdrawn."))


@bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    require_csrf()
    me = current_user()
    try:
        accounts.delete_user(user_id, acting_admin_id=(me or {}).get("id"))
        return redirect(url_for("auth.admin_users", message="Account deleted."))
    except ValueError as exc:
        return redirect(url_for("auth.admin_users", error=str(exc)))


@bp.route("/admin/searches/<slug>")
@admin_required
def admin_search_detail(slug):
    """Who ran one search, when, and what happened to it."""
    try:
        events = accounts.search_events(slug)
    except Exception:
        events = []
    return render_template("admin_search_detail.html", slug=slug, events=events)


@bp.route("/admin/searches")
@admin_required
def admin_searches():
    try:
        searches = accounts.list_searches(all_users=True, limit=1000)
    except Exception:
        searches = []
    return render_template("admin_searches.html", searches=searches)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def init_app(app, state_path=None):
    """Install the gate. Call AFTER all routes are registered."""
    import datetime
    app.register_blueprint(bp)
    app.permanent_session_lifetime = datetime.timedelta(seconds=int(SESSION_HOURS * 3600))
    init_run_gate(state_path)

    @app.context_processor
    def _auth_context():
        try:
            user = current_user()
        except Exception:
            user = None
        return {"current_user": user, "account_mode": accounts_enabled(app),
                "current_is_admin": is_admin(), "csrf_token": csrf_token}

    @app.before_request
    def _gate():                                              # noqa: unused
        ep = request.endpoint or ""
        # ---- 1. auth ----
        if auth_enabled(app) and ep not in _OPEN_ENDPOINTS:
            if not (_authenticated() or (TRUST_LOOPBACK and is_loopback())):
                if _wants_json():
                    return jsonify({"error": "authentication required"}), 401
                nxt = request.full_path.rstrip("?") or "/"
                # strip SCRIPT_NAME so `next` is app-relative; the prefix is re-added on redirect
                root = request.script_root or ""
                if root and nxt.startswith(root):
                    nxt = nxt[len(root):] or "/"
                return redirect(url_for("auth.login", next=nxt))
        # ---- 2. rate limits on expensive routes only ----
        lim = limiter_for(ep)
        # Only POST /login spends a login token; GETs just render the form, and charging them
        # would lock a legitimate user out of the page by reloading it.
        if ep in ("auth.login", "auth.forgot_password", "auth.register") and request.method != "POST":
            lim = None
        if lim is not None and not (TRUST_LOOPBACK and is_loopback()):
            ok, retry, why = lim.check(client_ip())
            if not ok:
                retry_after = int(retry) + 1
                # POST /run and POST /login are full form navigations, not fetch() calls, so a
                # browser lands ON this response. Returning bare JSON put raw text on screen.
                if not _wants_json():
                    body = render_template_string(_TOOMANY_HTML, retry_after=retry_after, why=why)
                    resp = current_app.make_response(body)
                    resp.status_code = 429
                else:
                    resp = jsonify({"error": "rate limited", "detail": why,
                                    "retry_after": retry_after})
                    resp.status_code = 429
                resp.headers["Retry-After"] = str(retry_after)
                return resp
        return None

    return app
