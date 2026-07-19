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
from pathlib import Path
from urllib.parse import urlparse
from flask import (Blueprint, request, session, redirect, url_for, render_template_string,
                   jsonify, current_app)

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
SESSION_HOURS = _num("SESSION_HOURS", 720)          # 30 days; single-user tool, don't nag
# Requests arriving on the loopback interface are on-box (port 8631 is VPC-only behind the GCP
# firewall, and nginx proxies from another VM so its traffic is NOT loopback). Exempting loopback
# keeps regression.sh / warm_reports / cron able to hit the app without embedding the password.
TRUST_LOOPBACK = _flag("AUTH_TRUST_LOOPBACK", "1")
_LOOPBACK = ("127.0.0.1", "::1", "localhost")

# Endpoints that must stay reachable without a session.
_OPEN_ENDPOINTS = {"healthz", "auth.login", "auth.logout", "static"}


# ---------------------------------------------------------------------------------------------
# client identity
# ---------------------------------------------------------------------------------------------
def client_ip():
    """The real caller. REMOTE_ADDR is the TCP peer (unspoofable); only when that peer is our
    trusted reverse proxy do we believe X-Forwarded-For's first hop."""
    peer = request.environ.get("REMOTE_ADDR", "") or "-"
    if peer in _LOOPBACK or peer.startswith("10.") or peer.startswith("172.") or peer.startswith("192.168."):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return peer


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

    def __init__(self, name, rate, burst, global_rate, global_burst, max_ips=4096):
        self.name = name
        self.rate, self.burst = rate, burst
        self.per_ip = {}
        self.global_bucket = TokenBucket(global_rate, global_burst)
        self.lock = threading.Lock()
        self.max_ips = max_ips

    def _bucket(self, ip):
        with self.lock:
            b = self.per_ip.get(ip)
            if b is None:
                if len(self.per_ip) >= self.max_ips:     # crude bound; stops unbounded growth
                    self.per_ip.clear()
                b = self.per_ip[ip] = TokenBucket(self.rate, self.burst)
            return b

    def check(self, ip):
        ok, retry = self._bucket(ip).take()
        if not ok:
            return False, retry, f"per-IP limit for {self.name}"
        ok, retry = self.global_bucket.take()
        if not ok:
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
    # PDF/DOCX rendering
    "export":   Limiter("exports", _num("RL_EXPORT_RATE", 0.5), _num("RL_EXPORT_BURST", 10),
                        _num("RL_EXPORT_GRATE", 1.0), _num("RL_EXPORT_GBURST", 20)),
}


def limiter_for(endpoint):
    return _LIMITERS.get(endpoint)


def reset_limits():
    """Test helper: refill every bucket."""
    for lim in _LIMITERS.values():
        lim.per_ip.clear()
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
    return bool(app.config.get("APP_PASSWORD") or APP_PASSWORD)


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


def _authenticated():
    if session.get("auth") is True:
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
<title>Sign in — prior-art search</title>
<style>
 body{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e6e8ee;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 form{background:#171a21;border:1px solid #262b36;border-radius:12px;padding:28px;width:min(360px,90vw)}
 h1{font-size:17px;margin:0 0 4px} p{color:#8b93a7;font-size:13px;margin:0 0 18px}
 input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid #2d3341;
       background:#0f1115;color:#e6e8ee;font-size:15px}
 button{width:100%;margin-top:12px;padding:10px;border:0;border-radius:8px;background:#3b82f6;
        color:#fff;font-size:15px;font-weight:600;cursor:pointer}
 .err{color:#f87171;font-size:13px;margin-top:10px}
</style>
<form method=post>
  <h1>Prior-art search</h1>
  <p>This instance is private. Enter the access password.</p>
  <input type=password name=password autofocus autocomplete=current-password placeholder="Password">
  <button type=submit>Sign in</button>
  {% if error %}<div class=err>{{ error }}</div>{% endif %}
</form>"""


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        supplied = request.form.get("password", "")
        expected = _password()
        # constant-time: don't leak the password length/prefix via timing
        if expected and hmac.compare_digest(supplied, expected):
            session.clear()
            session["auth"] = True
            session.permanent = True
            nxt = _safe_next(request.form.get("next") or request.args.get("next"))
            if nxt:
                # `next` is app-relative; re-attach the proxy prefix (/patents-data) so the
                # redirect stays valid behind nginx.
                return redirect((request.script_root or "") + nxt)
            return redirect(url_for("index"))
        error = "Incorrect password."
        time.sleep(0.5)                      # blunt the brute-force rate
    return render_template_string(_LOGIN_HTML, error=error), (401 if error else 200)


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
        if lim is not None and not (TRUST_LOOPBACK and is_loopback()):
            ok, retry, why = lim.check(client_ip())
            if not ok:
                resp = jsonify({"error": "rate limited", "detail": why,
                                "retry_after": int(retry) + 1})
                resp.status_code = 429
                resp.headers["Retry-After"] = str(int(retry) + 1)
                return resp
        return None

    return app
