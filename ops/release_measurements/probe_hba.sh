#!/bin/bash
echo "=== pg_hba (relbuild) ==="
sudo grep -vE '^\s*#|^\s*$' /etc/postgresql/17/relbuild/pg_hba.conf 2>&1
echo "=== try relbuild role, no password ==="
PGPASSWORD= psql "host=127.0.0.1 port=5544 dbname=relbuild user=relbuild" -tAc "select current_user, current_database(), version()" 2>&1 | head -3
echo "=== env / files mentioning 5544 or CORPUS_RELEASE_DSN ==="
grep -rl "5544\|CORPUS_RELEASE_DSN" /home/nimrod_rotem/.bash_history /home/nimrod_rotem/patent-search-pilot/.env /etc/supervisor/conf.d/ 2>/dev/null
grep -h "CORPUS_RELEASE_DSN" /home/nimrod_rotem/.bash_history 2>/dev/null | tail -5
echo "=== v3-releases on disk ==="
ls -la /home/nimrod_rotem/v3-releases/ 2>&1
ls -la /home/nimrod_rotem/v3-releases/hot_v1/ 2>&1
