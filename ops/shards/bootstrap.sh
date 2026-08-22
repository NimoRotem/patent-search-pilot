#!/bin/bash
# Runs ON a shard VM, as root, from shardctl.sh bootstrap. Idempotent: run it again after a change
# and it converges, it does not stack.
#
# What it puts on the box:
#   PostgreSQL 17 + pgvector + pg_prewarm, tuned for c4-highmem-16 and a read-mostly vector shard
#   the shard agent   (:8639)  the honest cold / waking / hot answer
#   Tantivy           (:8635)  the real extension module, unpacked from the wheel in the payload;
#                              workstream C owns the schema and what is indexed, this owns that
#                              Tantivy is installed, listening and telling the truth about itself
#   the prewarm unit             so the first query after a start does not pay for the whole index
#
# Deliberately NOT here: the schema and the data. `shardctl.sh schema` applies the repo's own
# migrations from the controller, which is the only migration runner there is, and workstream F
# loads the corpus. This script's whole job is that an EMPTY shard comes up correctly.
set -euo pipefail

PAYLOAD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF=/opt/patents-shard/shard.env
[ -r "$CONF" ] || { echo "missing $CONF (shardctl.sh writes it)" >&2; exit 2; }
# shellcheck disable=SC1090
. "$CONF"

SHARD_ID="${SHARD_ID:?}"
SHARD_PGPORT="${SHARD_PGPORT:-5432}"
SHARD_PGDATABASE="${SHARD_PGDATABASE:-patents}"
SHARD_PGUSER="${SHARD_PGUSER:-patents}"
SHARD_PGPASSWORD="${SHARD_PGPASSWORD:?}"
SHARD_AGENT_PORT="${SHARD_AGENT_PORT:-8639}"
SHARD_TANTIVY_PORT="${SHARD_TANTIVY_PORT:-8635}"
PGMAJOR="${SHARD_PGMAJOR:-17}"

log() { echo "[bootstrap $(date -u +%H:%M:%S)] $*"; }

# ---------------------------------------------------------------------------- 1. PostgreSQL 17
if ! [ -x "/usr/lib/postgresql/$PGMAJOR/bin/postgres" ]; then
  log "installing PostgreSQL $PGMAJOR from PGDG"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates gnupg python3 >/dev/null
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc]" \
       "https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  apt-get install -y -qq "postgresql-$PGMAJOR" "postgresql-$PGMAJOR-pgvector" \
                          "postgresql-client-$PGMAJOR" >/dev/null
else
  log "PostgreSQL $PGMAJOR already installed"
fi

PGDATA="/var/lib/postgresql/$PGMAJOR/main"
PGCONF="/etc/postgresql/$PGMAJOR/main"

# ------------------------------------------------------------------ 1b. the cluster itself
#  postgresql-common creates a `main` cluster on install ONLY when the box has none. A shard
#  image that already carries a cluster from an earlier experiment therefore gets the server
#  binaries and no cluster, and the first symptom is `Invalid data directory`. Create it
#  explicitly instead of relying on the package's side effect, and match the live corpus exactly:
#  MEASURED on 10.128.0.53:5433, PostgreSQL 17.10, UTF8, en_US.utf8. A shard with a different
#  collation sorts text differently from the corpus it is a piece of.
LOCALE="${SHARD_LOCALE:-en_US.UTF-8}"
if ! [ -f "$PGCONF/postgresql.conf" ]; then
  #  A conf.d this script wrote on a failed earlier pass makes pg_lsclusters report a cluster
  #  that does not exist. Clear it before creating the real one.
  rm -rf "$PGCONF"
  grep -q "^${LOCALE}" /etc/locale.gen 2>/dev/null || echo "$LOCALE UTF-8" >> /etc/locale.gen
  locale-gen "$LOCALE" >/dev/null 2>&1 || true
  #  Refuse only on a port that is actually in use. A cluster left over from an experiment and
  #  shut down still declares its port in its config, and refusing on that would make bootstrap
  #  unrunnable on a box that is otherwise fine; it is a warning, not a wall.
  if ss -lnt "sport = :$SHARD_PGPORT" 2>/dev/null | grep -q LISTEN; then
    echo "port $SHARD_PGPORT is already listening; free it first" >&2; exit 3
  fi
  taken="$(pg_lsclusters -h 2>/dev/null | awk -v p="$SHARD_PGPORT" '$3==p {print $1"/"$2}')"
  [ -z "$taken" ] || echo "WARNING: stopped cluster $taken also declares port $SHARD_PGPORT" >&2
  log "creating cluster $PGMAJOR/main on port $SHARD_PGPORT"
  pg_createcluster "$PGMAJOR" main --port "$SHARD_PGPORT" --locale "$LOCALE" \
      --encoding UTF8 -- --data-checksums >/dev/null
fi

# ---------------------------------------------------------------------------- 2. tuning
#  A shard is 16 vCPU / 124 GB serving one read-mostly database whose working set is an HNSW
#  index. shared_buffers at a quarter of RAM leaves the page cache room to hold the rest of the
#  index, which is what pg_prewarm's `read` mode fills.
#  autoprewarm is the reason a stop/start cycle is cheap: the buffer list is dumped at shutdown
#  and restored by a background worker at startup, so a shard that was hot an hour ago comes back
#  hot without anybody asking it to.
mkdir -p "$PGCONF/conf.d"
install -o postgres -g postgres -m 0644 /dev/stdin "$PGCONF/conf.d/10-shard.conf" <<CONF
# Managed by ops/shards/bootstrap.sh. Edit there, not here.
listen_addresses = '*'
port = $SHARD_PGPORT
max_connections = 100
shared_buffers = 32GB
effective_cache_size = 88GB
work_mem = 256MB
maintenance_work_mem = 8GB
random_page_cost = 1.1
effective_io_concurrency = 256
max_worker_processes = 24
max_parallel_workers = 16
max_parallel_workers_per_gather = 4
max_parallel_maintenance_workers = 8
wal_compression = on
checkpoint_timeout = 30min
max_wal_size = 8GB
shared_preload_libraries = 'pg_prewarm'
pg_prewarm.autoprewarm = on
pg_prewarm.autoprewarm_interval = 300s
log_min_duration_statement = 5000
CONF
grep -q "conf.d" "$PGCONF/postgresql.conf" || \
  echo "include_dir = 'conf.d'" >> "$PGCONF/postgresql.conf"

#  Only the VPC, only this database, only this role, and a password. There is no external address
#  on a shard, but `default-allow-internal` opens every port to 10.128.0.0/9, so host based
#  restriction is the actual control and not decoration.
HBA="$PGCONF/pg_hba.conf"
if ! grep -q "patents-shard" "$HBA"; then
  cat >> "$HBA" <<HBA_EOF

# patents-shard: the VPC only, scram, this database only.
host    $SHARD_PGDATABASE    $SHARD_PGUSER    10.128.0.0/9    scram-sha-256
HBA_EOF
fi

systemctl enable postgresql >/dev/null 2>&1 || true
systemctl restart "postgresql@$PGMAJOR-main"

# ---------------------------------------------------------------------------- 3. role, db, exts
if [ "$(su - postgres -c "psql -p $SHARD_PGPORT -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$SHARD_PGUSER'\"")" != "1" ]; then
  log "creating role $SHARD_PGUSER"
  su - postgres -c "psql -p $SHARD_PGPORT -v ON_ERROR_STOP=1 -c \"CREATE ROLE $SHARD_PGUSER LOGIN PASSWORD '$SHARD_PGPASSWORD'\"" >/dev/null
else
  su - postgres -c "psql -p $SHARD_PGPORT -v ON_ERROR_STOP=1 -c \"ALTER ROLE $SHARD_PGUSER LOGIN PASSWORD '$SHARD_PGPASSWORD'\"" >/dev/null
fi
if [ "$(su - postgres -c "psql -p $SHARD_PGPORT -tAc \"SELECT 1 FROM pg_database WHERE datname='$SHARD_PGDATABASE'\"")" != "1" ]; then
  log "creating database $SHARD_PGDATABASE"
  su - postgres -c "createdb -p $SHARD_PGPORT -O $SHARD_PGUSER $SHARD_PGDATABASE"
fi
su - postgres -c "psql -p $SHARD_PGPORT -d $SHARD_PGDATABASE -v ON_ERROR_STOP=1 \
  -c 'CREATE EXTENSION IF NOT EXISTS vector' \
  -c 'CREATE EXTENSION IF NOT EXISTS pg_prewarm'" >/dev/null

#  shard_status is the honest answer to "is this shard serving?", and it is a TABLE and not a file
#  because the process that fills the shard commits the rows and the readiness flip in the same
#  transaction. A shard that has been bootstrapped but not loaded is `building`, which makes
#  available() False, which is the whole point: an empty result set and a genuine miss are
#  indistinguishable downstream and a miss scores as a recall failure.
su - postgres -c "psql -p $SHARD_PGPORT -d $SHARD_PGDATABASE -v ON_ERROR_STOP=1 -f -" \
  < "$PAYLOAD/sql/shard_status.sql" >/dev/null
#  The loader and the operator both connect as $SHARD_PGUSER, not as the superuser, so the table
#  that says whether this shard is serving has to be theirs to write. Created by postgres and
#  never handed over, the readiness flip fails with `permission denied` at the end of a load,
#  which is the worst possible moment to find out.
su - postgres -c "psql -p $SHARD_PGPORT -d $SHARD_PGDATABASE -v ON_ERROR_STOP=1 \
  -c 'ALTER TABLE shard_status OWNER TO $SHARD_PGUSER'" >/dev/null
su - postgres -c "psql -p $SHARD_PGPORT -d $SHARD_PGDATABASE -tAc \
  \"INSERT INTO shard_status (shard, state, note) VALUES ('$SHARD_ID','building','bootstrapped, no corpus loaded')
    ON CONFLICT (shard) DO NOTHING\"" >/dev/null

# ---------------------------------------------------------------------------- 4. the processes
#  The units read this, not shard.env: it carries no password, so it can be world readable and the
#  agent and prewarm can run as `postgres` with peer auth on the socket and no secret in either
#  process. The password exists only in shard.env (0600, root) and in the controller's .env.
install -m 0644 /dev/stdin /etc/default/patents-shard <<ENV
SHARD_ID=$SHARD_ID
SHARD_PGPORT=$SHARD_PGPORT
SHARD_PGDATABASE=$SHARD_PGDATABASE
SHARD_AGENT_PORT=$SHARD_AGENT_PORT
SHARD_TANTIVY_PORT=$SHARD_TANTIVY_PORT
SHARD_PREWARM_STATE=/run/patents-shard/prewarm.json
SHARD_PREWARM_BLOCKING_MB=${SHARD_PREWARM_BLOCKING_MB:-6144}
SHARD_TANTIVY_INDEX=/opt/patents-shard/tantivy/index
SHARD_TANTIVY_LIB=/opt/patents-shard/lib
ENV

install -d -m 0755 /opt/patents-shard/bin /opt/patents-shard/tantivy /opt/patents-shard/lib
install -m 0755 "$PAYLOAD/shard_agent.py"     /opt/patents-shard/bin/shard_agent.py
install -m 0755 "$PAYLOAD/prewarm.py"         /opt/patents-shard/bin/prewarm.py
install -m 0755 "$PAYLOAD/tantivy_serve.sh"   /opt/patents-shard/bin/tantivy_serve.sh
install -m 0755 "$PAYLOAD/tantivy_server.py"  /opt/patents-shard/bin/tantivy_server.py

# ------------------------------------------------------------------ 4b. the Tantivy extension
#  Unpacked from the wheel with python3's own zipfile rather than installed with pip, on purpose.
#  A shard has NO route to PyPI: it is private IP only and the external address it borrows for apt
#  is taken off again at the end of bootstrap. A wheel is a zip, python3 is on the image, and
#  unpacking it into a directory on PYTHONPATH needs neither pip nor apt nor a network, so this
#  step is re-runnable on a shard with no egress at all. The wheel is shipped in the payload by
#  shardctl.sh, which fetches it once on the controller and caches it.
WHEEL="$(ls "$PAYLOAD"/tantivy-*.whl 2>/dev/null | head -1 || true)"
if [ -n "$WHEEL" ]; then
  log "unpacking $(basename "$WHEEL") into /opt/patents-shard/lib"
  rm -rf /opt/patents-shard/lib/tantivy /opt/patents-shard/lib/tantivy-*.dist-info
  python3 -m zipfile -e "$WHEEL" /opt/patents-shard/lib
  chmod -R a+rX /opt/patents-shard/lib
  if PYTHONPATH=/opt/patents-shard/lib python3 -c "import tantivy" 2>/dev/null; then
    log "tantivy imports on this shard"
  else
    #  A wheel that will not import is NOT a bootstrap failure. tantivy_server.py reports
    #  `state: missing, available: false`, the lexical channel falls back to Postgres, and that
    #  is the existing fail soft contract. A shard that refused to finish bootstrapping over it
    #  would take the dense channel down with it, which is the far bigger loss.
    log "WARNING: the tantivy wheel will not import here; the server will report it as missing"
  fi
else
  log "no tantivy wheel in the payload; the lexical server will report state=missing"
fi
install -m 0644 "$PAYLOAD/systemd/patents-shard-agent.service"   /etc/systemd/system/
install -m 0644 "$PAYLOAD/systemd/patents-shard-prewarm.service" /etc/systemd/system/
install -m 0644 "$PAYLOAD/systemd/patents-shard-tantivy.service" /etc/systemd/system/
install -d -m 0755 -o postgres -g postgres /run/patents-shard

systemctl daemon-reload
systemctl enable --now patents-shard-agent.service
systemctl enable --now patents-shard-tantivy.service
systemctl enable patents-shard-prewarm.service
systemctl restart patents-shard-agent.service patents-shard-tantivy.service
systemctl start patents-shard-prewarm.service || true

log "shard $SHARD_ID bootstrapped: pg$PGMAJOR:$SHARD_PGPORT agent:$SHARD_AGENT_PORT tantivy:$SHARD_TANTIVY_PORT"
