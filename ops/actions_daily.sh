#!/usr/bin/env bash
# The actions docket's daily check (src/actions_daily.py): every followed company's cases re-read
# from the registers, new applications in their names added, iptorch.com packages swept.
# crontab on pbox: 10 5 * * *  (05:10 UTC). Log: data/observations/daily/cron.log.
# One at a time (flock), never longer than two hours, and low priority beside the search app.
set -u
ROOT=/home/nimrod_rotem/patent-search-pilot
mkdir -p "$ROOT/data/observations/daily"
cd "$ROOT/src" || exit 1
{
  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) daily check"
  /usr/bin/flock -n /tmp/actions-daily.lock \
    nice -n 10 timeout 2h "$ROOT/.venv/bin/python" actions_daily.py
  echo "== exit $?"
} >> "$ROOT/data/observations/daily/cron.log" 2>&1
