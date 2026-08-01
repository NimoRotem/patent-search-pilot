"""Gunicorn config for the prior-art pilot. Replaces the Werkzeug dev server.

    gunicorn -c gunicorn_conf.py webapp:app        (run from src/)

WORKER MODEL — why 1 process x 16 threads
-----------------------------------------
Measured on instance-3 (16 GB, ~4 GB free, 5 GB swap already in use, 4 vCPU):

    bge-reranker-v2-m3   ~670 MB RSS resident, ~1.86 GB peak while scoring

A pre-fork pool would pay that per worker, so even 2 workers risk 3.7 GB of peak reranker RAM on a
box already swapping. So the reranker does NOT live in the web workers at all — it runs in ONE
dedicated child process (src/rerank_pool.py). That is what allows the old global generation lock to
be deleted: the tokenizer that raised "Already borrowed" is now only ever touched by a single
interpreter, and there is exactly one copy of the weights regardless of web concurrency.

With the reranker gone from the request path, the web tier is almost purely I/O bound — Vertex LLM
calls, Vertex embeddings, Postgres, SerpApi. Every one of those releases the GIL, so THREADS give
real concurrency here and processes would only cost memory.

Threads also buy correctness. The app keeps genuinely shared in-process state:
  * `_JOBS`        — generation progress, read by /status and /events
  * `_SUBS`        — SSE subscriber queues
  * the `Retriever` singleton (family map) and `_QCACHE`
  * the rate-limit token buckets and the concurrency gate in auth.py
With >1 worker, a POST /run handled by worker A would be invisible to a /events request landing on
worker B, progress would vanish, per-IP rate limits would be divided by the worker count, and the
concurrency cap would be multiplied by it. Keeping one worker keeps all of that exact and needs no
Redis. Actual search concurrency is bounded deliberately by MAX_CONCURRENT_RUNS (default 2), which
is the right knob — a resource budget rather than a mutex.

TIMEOUTS
--------
A real search legitimately runs ~3 minutes, and an SSE client holds its connection open for the
whole run, so `timeout` must exceed that by a wide margin; 1800 s matches nginx's
proxy_read_timeout. Note gunicorn's `timeout` is a worker-liveness watchdog, not a request
deadline. graceful_timeout gives in-flight searches 2 minutes to land on a restart.
"""
import os

# ---- socket ----
bind = f"{os.environ.get('WEBAPP_HOST', '0.0.0.0')}:{os.environ.get('WEBAPP_PORT', '8631')}"
backlog = 128

# ---- worker model (see rationale above) ----
workers = int(os.environ.get("WEB_WORKERS", "1"))
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", "16"))
worker_connections = 200

# ---- timeouts ----
timeout = int(os.environ.get("WEB_TIMEOUT", "1800"))          # matches nginx proxy_read_timeout
graceful_timeout = int(os.environ.get("WEB_GRACEFUL", "120"))  # let a running search finish
keepalive = 5

# Never recycle on a request count: a restart mid-search would drop an in-flight ~3 min generation
# and its in-memory job state.
max_requests = 0

# Load the app in each worker, not the master. With a single worker there is no copy-on-write
# saving to be had, and a lean master makes `kill -HUP` reloads clean. It also guarantees no torch
# state can be inherited across a fork into the rerank child.
preload_app = False

# ---- logging ----
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
# Default access log plus response time in seconds, so slow endpoints are visible in the log.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(L)ss "%(a)s"'
proc_name = "patent-results"


def on_starting(server):
    server.log.info("patent-results: %s worker(s) x %s threads, timeout=%ss",
                    workers, threads, timeout)


def worker_exit(server, worker):
    """Tear background resources down with the worker.

    Besides the rerank child, named-account mode owns a mail thread and report archives own a
    bounded executor. Explicit shutdown keeps graceful deploys from leaving old workers or tasks
    behind after gunicorn has stopped accepting requests.
    """
    try:
        import rerank_pool
        rerank_pool.shutdown()
    except Exception:
        pass
    try:
        import notifications
        notifications.stop_worker()
    except Exception:
        pass
    try:
        import draft_worker
        draft_worker.stop_worker()
    except Exception:
        pass
    try:
        import report_archive
        report_archive.shutdown(wait=False)
    except Exception:
        pass
