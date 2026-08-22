#!/bin/bash
# shardctl: the whole lifecycle of a cold domain shard, from one script.
#
# WHY SHELL AND gcloud AND NOT TERRAFORM. Terraform is not installed anywhere on this fleet and
# would need a state backend, a bucket and a lock table before it created its first VM, and the
# thing it would be describing is not a static topology: a shard is created once and then STARTED
# and STOPPED many times a day from the search path, which is imperative and belongs to
# src/retrieval/shard_backend.py, not to a plan/apply cycle. Idempotence here comes from asking
# GCE what exists before acting, which is re-runnable without a state file to lose or diverge.
#
#   ./shardctl.sh list                     the shard table
#   ./shardctl.sh status [shard...]        instance state + the shard's own answer
#   ./shardctl.sh create <shard>           create the VM and its disk, private IP only
#   ./shardctl.sh create-all               all eight, the command in the cost decision
#   ./shardctl.sh bootstrap <shard>        pg17 + pgvector + agent + tantivy + prewarm
#   ./shardctl.sh schema <shard>           apply the repo's migrations to the shard database
#   ./shardctl.sh start|stop <shard>
#   ./shardctl.sh wake <shard> [timeout]   start and wait for hot, printing the measured time
#   ./shardctl.sh health <shard>           the agent's JSON
#   ./shardctl.sh ready <shard> [gen]      flip shard_status to ready  (workstream F's seam)
#   ./shardctl.sh building <shard> [note]  flip it back
#   ./shardctl.sh egress on|off <shard>    temporary external address, for apt only
#   ./shardctl.sh reap [--dry-run]         the lease driven idle shutdown
#   ./shardctl.sh cost [n]                 what n shards cost, standing and running
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TABLE="${SHARD_TABLE:-$HERE/shards.tsv}"

MACHINE="${SHARD_MACHINE_TYPE:-c4-highmem-16}"
DISK_GB="${SHARD_DISK_GB:-250}"
DISK_TYPE="${SHARD_DISK_TYPE:-hyperdisk-balanced}"
DISK_IOPS="${SHARD_DISK_IOPS:-4500}"
DISK_MBPS="${SHARD_DISK_MBPS:-515}"
IMAGE_FAMILY="${SHARD_IMAGE_FAMILY:-debian-12}"
IMAGE_PROJECT="${SHARD_IMAGE_PROJECT:-debian-cloud}"
AGENT_PORT="${SHARD_AGENT_PORT:-8639}"
TANTIVY_PORT="${SHARD_TANTIVY_PORT:-8635}"
PGPORT_SHARD="${SHARD_PGPORT:-5432}"
PGDB_SHARD="${SHARD_PGDATABASE:-patents}"
PGUSER_SHARD="${SHARD_PGUSER:-patents}"
PY="${SHARD_PYTHON:-/home/nimrod_rotem/patent-search-pilot/.venv/bin/python}"

die() { echo "shardctl: $*" >&2; exit 2; }

# ---------------------------------------------------------------------------- the shard table
row() {                                   # row <shard> -> "shard vm zone prefixes"
  awk -F'\t' -v s="$1" '$1==s {print $1, $2, $3, $4}' "$TABLE"
}
all_shards() { awk -F'\t' '!/^#/ && NF>=3 {print $1}' "$TABLE"; }
vm_of()   { row "$1" | awk '{print $2}'; }
zone_of() { row "$1" | awk '{print $3}'; }

need() {
  [ -n "$(row "$1")" ] || die "no such shard '$1'. Known: $(all_shards | tr '\n' ' ')"
}

inst_status() {                            # -> RUNNING | TERMINATED | ABSENT | ...
  gcloud compute instances describe "$(vm_of "$1")" --zone "$(zone_of "$1")" \
      --format='value(status)' 2>/dev/null || echo ABSENT
}
inst_ip() {
  gcloud compute instances describe "$(vm_of "$1")" --zone "$(zone_of "$1")" \
      --format='value(networkInterfaces[0].networkIP)' 2>/dev/null
}

#  The shard database password is the corpus password: the same role, the same secret, so a shard
#  needs no new credential and nothing new to rotate. It is read from the repo's .env at call
#  time and never written into the repo.
pgpassword() {
  if [ -n "${SHARD_PGPASSWORD:-}" ]; then printf '%s' "$SHARD_PGPASSWORD"; return; fi
  [ -r "$REPO/.env" ] || die "no \$SHARD_PGPASSWORD and no $REPO/.env to read PGPASSWORD from"
  grep -E '^PGPASSWORD=' "$REPO/.env" | tail -1 | sed -e 's/^PGPASSWORD=//' -e 's/^["'\'']//' -e 's/["'\'']$//'
}

ssh_shard() {                              # ssh_shard <shard> <command...>
  local s="$1"; shift
  gcloud compute ssh "$(vm_of "$s")" --zone "$(zone_of "$s")" --internal-ip \
      --strict-host-key-checking=no --command "$*"
}

# ---------------------------------------------------------------------------- commands
cmd_list() {
  printf '%-16s %-30s %-16s %s\n' SHARD VM ZONE PREFIXES
  awk -F'\t' '!/^#/ && NF>=3 {printf "%-16s %-30s %-16s %s\n", $1, $2, $3, $4}' "$TABLE"
}

cmd_status() {
  local shards=("$@"); [ ${#shards[@]} -eq 0 ] && mapfile -t shards < <(all_shards)
  printf '%-16s %-12s %-15s %-9s %-9s %s\n' SHARD INSTANCE IP STATE INDEX REASON
  for s in "${shards[@]}"; do
    local st ip health state index reason
    st="$(inst_status "$s")"; ip="$(inst_ip "$s")"; state="-"; index="-"; reason=""
    if [ "$st" = "RUNNING" ] && [ -n "$ip" ]; then
      health="$(curl -fsS --max-time 3 "http://$ip:$AGENT_PORT/health" 2>/dev/null || echo '')"
      if [ -n "$health" ]; then
        state="$(echo "$health" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["state"])' 2>/dev/null || echo '?')"
        index="$(echo "$health" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["index"]["state"])' 2>/dev/null || echo '?')"
        reason="$(echo "$health" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["reason"])' 2>/dev/null || echo '')"
      else
        state="waking"; reason="agent not answering"
      fi
    elif [ "$st" = "TERMINATED" ] || [ "$st" = "SUSPENDED" ]; then
      state="cold"
    elif [ "$st" = "ABSENT" ]; then
      state="absent"; reason="not created"
    fi
    printf '%-16s %-12s %-15s %-9s %-9s %s\n' "$s" "$st" "${ip:--}" "$state" "$index" "$reason"
  done
}

cmd_create() {
  local s="$1"; need "$s"
  local vm zone; vm="$(vm_of "$s")"; zone="$(zone_of "$s")"
  if gcloud compute instances describe "$vm" --zone "$zone" >/dev/null 2>&1; then
    echo "$vm already exists, nothing to create"; return 0
  fi
  echo "creating $vm ($MACHINE, ${DISK_GB}GB $DISK_TYPE, no external address) in $zone"
  gcloud compute instances create "$vm" \
    --zone "$zone" \
    --machine-type "$MACHINE" \
    --image-family "$IMAGE_FAMILY" --image-project "$IMAGE_PROJECT" \
    --boot-disk-size "${DISK_GB}GB" --boot-disk-type "$DISK_TYPE" \
    --boot-disk-device-name "$vm" \
    --boot-disk-provisioned-iops "$DISK_IOPS" \
    --boot-disk-provisioned-throughput "$DISK_MBPS" \
    --network-interface=subnet=default,no-address \
    --metadata enable-oslogin=TRUE \
    --labels "purpose=patents-shard,shard=$s,owner=workstream-e" \
    --scopes cloud-platform
  #  Created RUNNING. It has nothing on it yet, so bring it straight back down: a shard's resting
  #  state is TERMINATED and an unbootstrapped one that is left up is pure burn.
  gcloud compute instances stop "$vm" --zone "$zone" --quiet
}

cmd_create_all() {
  #  THE COST DECISION. Eight of these are ~$340/month in standing disk with every VM stopped and
  #  ~$761/month EACH the moment one is left running. `cost` prints the arithmetic.
  for s in $(all_shards); do cmd_create "$s"; done
}

cmd_egress() {
  local mode="$1" s="$2"; need "$s"
  local vm zone; vm="$(vm_of "$s")"; zone="$(zone_of "$s")"
  case "$mode" in
    on)  gcloud compute instances add-access-config "$vm" --zone "$zone" \
           --access-config-name external-nat 2>/dev/null \
         && echo "external address attached (apt only, take it off again)" \
         || echo "already has an external address" ;;
    off) gcloud compute instances delete-access-config "$vm" --zone "$zone" \
           --access-config-name external-nat 2>/dev/null \
         && echo "external address removed; private IP only" \
         || echo "no external address to remove" ;;
    *) die "egress on|off <shard>" ;;
  esac
}

cmd_bootstrap() {
  local s="$1"; need "$s"
  [ "$(inst_status "$s")" = "RUNNING" ] || cmd_start "$s"
  #  There is no Cloud NAT in this project, so a private IP only shard has NO route to
  #  apt.postgresql.org. Attach an address for the install and take it off at the end: the shard's
  #  steady state is private IP only, exactly as specified.
  local needs_apt=1
  ssh_shard "$s" "test -x /usr/lib/postgresql/${SHARD_PGMAJOR:-17}/bin/postgres" && needs_apt=0 || true
  if [ "$needs_apt" = "1" ] && ! gcloud compute instances describe "$(vm_of "$s")" \
       --zone "$(zone_of "$s")" \
       --format='value(networkInterfaces[0].accessConfigs[0].name)' 2>/dev/null | grep -q .; then
    cmd_egress on "$s"; sleep 5
  fi

  local tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  cp -r "$HERE"/{bootstrap.sh,shard_agent.py,prewarm.py,tantivy_serve.sh,tantivy_stub.py,systemd,sql} "$tmp/"
  tar czf "$tmp/payload.tgz" -C "$tmp" bootstrap.sh shard_agent.py prewarm.py \
      tantivy_serve.sh tantivy_stub.py systemd sql

  echo "shipping the payload to $(vm_of "$s")"
  gcloud compute scp --internal-ip --zone "$(zone_of "$s")" --strict-host-key-checking=no \
      "$tmp/payload.tgz" "$(vm_of "$s")":/tmp/patents-shard-payload.tgz

  #  shard.env carries the database password. It goes over the ssh channel into a 0600 root owned
  #  file, so it is never a process argument on the shard and never lands in a shell history.
  echo "writing /opt/patents-shard/shard.env"
  {
    printf 'SHARD_ID=%s\n' "$s"
    printf 'SHARD_PGPORT=%s\n' "$PGPORT_SHARD"
    printf 'SHARD_PGDATABASE=%s\n' "$PGDB_SHARD"
    printf 'SHARD_PGUSER=%s\n' "$PGUSER_SHARD"
    printf 'SHARD_AGENT_PORT=%s\n' "$AGENT_PORT"
    printf 'SHARD_TANTIVY_PORT=%s\n' "$TANTIVY_PORT"
    printf 'SHARD_PGPASSWORD=%s\n' "$(pgpassword)"
  } | ssh_shard "$s" "sudo install -d -m 0755 /opt/patents-shard && \
      sudo install -m 0600 /dev/stdin /opt/patents-shard/shard.env"

  echo "running bootstrap.sh"
  ssh_shard "$s" "sudo rm -rf /opt/patents-shard/payload && sudo install -d /opt/patents-shard/payload && \
      sudo tar xzf /tmp/patents-shard-payload.tgz -C /opt/patents-shard/payload && \
      sudo bash /opt/patents-shard/payload/bootstrap.sh"

  #  Always ends private IP only. A shard's steady state has no external address; the one it
  #  borrowed for apt goes back. There is no Cloud NAT in this project, so this is the whole of a
  #  shard's internet access and it exists only for the length of an install.
  cmd_egress off "$s" || true
  echo "bootstrapped. Next: ./shardctl.sh schema $s"
}

cmd_schema() {
  #  The repo's own migration runner, pointed at the shard. There is exactly one migration runner
  #  and this is it; a shard that got its schema from a hand written DDL file would drift from the
  #  corpus the first time anybody added a column.
  #
  #  002 IS EXCLUDED, and not as a convenience. MEASURED 2026-08-22 against a fresh PostgreSQL
  #  17.10 + pgvector 0.8.6 shard: `CREATE INDEX ix_bench3072_hnsw` fails with
  #  `column cannot have more than 2000 dimensions for hnsw index`. pgvector's hnsw limit is 2000
  #  and bench_emb_3072 is 3072, so that statement cannot succeed on any database, and `apply`
  #  puts one file in one transaction, so 002 as written can never be applied anywhere.
  #  docs/migrations.md says 002 is pending until the backfill stops competing for resources;
  #  it is pending because it is broken. Splitting the two benchmark indexes out is workstream H's
  #  call, not a shard's.
  #
  #  It would be the wrong step for a shard anyway: building an empty HNSW index and then loading
  #  millions of rows through it is the slow path. The index belongs at the END of workstream F's
  #  load, on the data.
  local s="$1"; need "$s"
  local ip; ip="$(inst_ip "$s")"; [ -n "$ip" ] || die "$s has no address (is it running?)"
  local extra=("${@:2}")
  if [ ${#extra[@]} -eq 0 ]; then extra=(--exclude 002); fi
  echo "applying migrations to $s at $ip:$PGPORT_SHARD (${extra[*]})"
  PGHOST="$ip" PGPORT="$PGPORT_SHARD" PGDATABASE="$PGDB_SHARD" PGUSER="$PGUSER_SHARD" \
  PGPASSWORD="$(pgpassword)" \
    "$PY" "$REPO/src/migrate.py" apply --sql-dir "$REPO/sql" "${extra[@]}"
}

cmd_start() {
  local s="$1"; need "$s"
  gcloud compute instances start "$(vm_of "$s")" --zone "$(zone_of "$s")" --quiet >/dev/null
  echo "$s starting"
}

cmd_stop() {
  local s="$1"; need "$s"
  gcloud compute instances stop "$(vm_of "$s")" --zone "$(zone_of "$s")" --quiet >/dev/null
  echo "$s stopped"
}

cmd_wake() {
  #  The measurement the 20 s SHARD_WAKE_TIMEOUT has to live with. Delegates to the same backend
  #  the search path uses, so what is timed here is what a query would experience.
  local s="$1"; local timeout="${2:-300}"
  PYTHONPATH="$REPO/src" "$PY" -c "
import sys, time
from retrieval import shard_backend
b = shard_backend.ShardBackend()
t0 = time.time()
state = b.ensure(['$s'], timeout=float('$timeout')).get('$s')
print('%s -> %s in %.1fs' % ('$s', state, time.time() - t0))
sys.exit(0 if state == 'hot' else 1)"
}

cmd_health() {
  local s="$1"; need "$s"
  local ip; ip="$(inst_ip "$s")"; [ -n "$ip" ] || die "$s has no address"
  curl -fsS --max-time 5 "http://$ip:$AGENT_PORT/health" | "$PY" -m json.tool
}

_shard_psql() {
  local s="$1"; shift
  local ip; ip="$(inst_ip "$s")"; [ -n "$ip" ] || die "$s has no address"
  PGPASSWORD="$(pgpassword)" psql -h "$ip" -p "$PGPORT_SHARD" -U "$PGUSER_SHARD" \
      -d "$PGDB_SHARD" -v ON_ERROR_STOP=1 -tAX "$@"
}

cmd_ready() {
  local s="$1"; need "$s"; local gen="${2:-manual}"
  _shard_psql "$s" -c "INSERT INTO shard_status (shard, state, generation, n_chunks, note)
       VALUES ('$s','ready','$gen',(SELECT count(*) FROM chunks),'marked ready by shardctl')
       ON CONFLICT (shard) DO UPDATE SET state='ready', generation=EXCLUDED.generation,
            n_chunks=EXCLUDED.n_chunks, note=EXCLUDED.note, updated_at=now()
       RETURNING shard || ' ' || state || ' ' || n_chunks"
}

cmd_building() {
  local s="$1"; need "$s"; local note="${2:-marked building by shardctl}"
  _shard_psql "$s" -c "UPDATE shard_status SET state='building', note='$note', updated_at=now()
       WHERE shard='$s' RETURNING shard || ' ' || state"
}

cmd_reap() {
  PYTHONPATH="$REPO/src" "$PY" "$HERE/idle_reaper.py" "$@"
}

cmd_cost() {
  local n="${1:-8}"
  "$PY" - "$n" "$DISK_GB" "$DISK_IOPS" "$DISK_MBPS" <<'PY'
import sys
n, gb, iops, mbps = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
#  GCP list price, us-central1 (Iowa), read from the Cloud Billing catalog on 2026-08-22.
CAP, PER_IOPS, PER_TP = 0.080, 0.005, 0.040  # per GiB-month, per IOPS-month, per (MiB/s)-month
CORE, RAM = 0.034650, 0.003938               # C4 per vCPU-hour, per GiB-hour
BASE_IOPS, BASE_TP = 3000.0, 140.0           # included in the capacity price
disk = gb * CAP + max(0.0, iops - BASE_IOPS) * PER_IOPS + max(0.0, mbps - BASE_TP) * PER_TP
vm_hr = 16 * CORE + 124 * RAM
print(f"per shard, disk standing (VM TERMINATED): ${disk:8.2f}/month")
print(f"per shard, VM while RUNNING:              ${vm_hr:8.4f}/hour = ${vm_hr*730:.2f}/month 24x7")
print(f"{n} shards, standing disk only:            ${disk*n:8.2f}/month")
print(f"{n} shards, all running 24x7:              ${disk*n + vm_hr*730*n:8.2f}/month")
print(f"one shard awake 1 hour a day, {n} shards:  ${disk*n + vm_hr*30.4*n:8.2f}/month")
print()
print(f"at the baseline {BASE_IOPS:.0f} IOPS / {BASE_TP:.0f} MiB/s the disk is "
      f"${gb*CAP:.2f}/month, so {n} shards stand at ${gb*CAP*n:.2f}/month.")
PY
}

case "${1:-}" in
  list)       shift; cmd_list "$@" ;;
  status)     shift; cmd_status "$@" ;;
  create)     shift; cmd_create "$@" ;;
  create-all) shift; cmd_create_all "$@" ;;
  bootstrap)  shift; cmd_bootstrap "$@" ;;
  schema)     shift; cmd_schema "$@" ;;
  start)      shift; cmd_start "$@" ;;
  stop)       shift; cmd_stop "$@" ;;
  wake)       shift; cmd_wake "$@" ;;
  health)     shift; cmd_health "$@" ;;
  ready)      shift; cmd_ready "$@" ;;
  building)   shift; cmd_building "$@" ;;
  egress)     shift; cmd_egress "$@" ;;
  reap)       shift; cmd_reap "$@" ;;
  cost)       shift; cmd_cost "$@" ;;
  *) sed -n '2,32p' "$0"; exit 1 ;;
esac
