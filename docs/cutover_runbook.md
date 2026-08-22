# The durable cutover: what is deployed, and how to put it back

Written BEFORE the deploy, deliberately. The rollback is the part that has to exist first.

## What the cutover changes

`patent-results` stops executing searches. It writes a row into `search_runs` and reads progress
back out of it; `patent-search-worker` claims the row and does the work in its own process. So
`sudo supervisorctl restart patent-results` no longer kills a search in flight, which is the
defect this exists to fix: the app was restarted seven times on 2026-08-22, the worst measured
deep search takes 5h01m, and a typical one exceeds an hour, so a long search could not survive to
completion on this box.

Two flags, opposite failure directions on purpose:

| Flag | On | Unparseable |
|---|---|---|
| `DURABLE_SEARCH_RUNS` on `patent-results` | route and SSE read `search_runs` | OFF, loudly, legacy path. Refusing to serve the site over a typo is the worse failure |
| `DURABLE_WORKER_ENABLED` on `patent-search-worker` | the worker may claim and spend | REFUSES TO START. Guessing "on" spends money |

## Rollback, in order of how much you want to undo

Every command names one program. **Never `supervisorctl update`, `restart all` or `reload`**:
they bounce unrelated programs, and `patent-results` has already been bounced that way.

### 1. Route only, code unchanged. About 20 seconds.

The web app goes back to executing searches in process. Anything already running in the worker
keeps running and still settles; the app just stops reading `search_runs` for status.

```
sudo sed -i 's/,DURABLE_SEARCH_RUNS="1"//' /etc/supervisor/conf.d/patent-results.conf
sudo supervisorctl reread
sudo supervisorctl restart patent-results
```

### 2. Stop the worker as well.

```
sudo supervisorctl stop patent-search-worker
```

Runs it was holding stay `running` with an expired lease and no worker. They are not lost: they
are checkpointed, and starting a worker again resumes them. To settle them by hand instead:

```
cd /home/nimrod_rotem/patent-search-pilot && .venv/bin/python -c \
  "import sys;sys.path.insert(0,'src');import runstore;print(runstore.reap())"
```

### 3. Previous code, exactly.

The snapshot of what was replaced is in `data/cutover_snapshot/` in this worktree: the deployed
SHA, both supervisor units as they were, and `supervisorctl status`.

```
cd /home/nimrod_rotem/patent-search-pilot
git checkout $(cat /home/nimrod_rotem/v3/L-cutover/data/cutover_snapshot/deployed_sha)
sudo cp /home/nimrod_rotem/v3/L-cutover/data/cutover_snapshot/patent-results.conf \
        /etc/supervisor/conf.d/patent-results.conf
sudo rm -f /etc/supervisor/conf.d/patent-search-worker.conf
sudo supervisorctl stop patent-search-worker
sudo supervisorctl reread
sudo supervisorctl restart patent-results
```

The site is back on the previous commit and the previous route inside a minute.

### What rollback does NOT undo

Migrations 012 and 013. Both are additive: 012 adds three columns and three partial indexes to
`search_runs`, 013 creates `run_side_effects`. Nothing in the legacy path reads either, so leaving
them applied is correct and dropping them is not part of any rollback.

## Before every restart of patent-results

```
curl -s localhost:8631/healthz | python3 -c 'import sys,json;print(json.load(sys.stdin)["runs"])'
```

If `active` is non-zero and the run is not yours, wait. Real users run searches on this box. After
the cutover this check protects legacy in-process runs only, which is the whole point: once the
route is over, a restart cannot hurt a durable run.

## Programs that must keep running

`patents-desc-backfill`, `patents-fulltext-acquire`, `patents-fulltext-acquire-1`. None of them is
touched by any command above, because every command above names one program.
