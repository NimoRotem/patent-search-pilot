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
#   ./shardctl.sh verify-cold              exit 1 unless every shard is TERMINATED
#   ./shardctl.sh bootstrap <shard>        pg17 + pgvector + agent + tantivy + prewarm
#   ./shardctl.sh schema <shard>           apply the repo's migrations to the shard database
#   ./shardctl.sh start|stop <shard>
#   ./shardctl.sh wake <shard> [timeout]   start and wait for hot, printing the measured time
#   ./shardctl.sh health <shard>           the agent's JSON
#   ./shardctl.sh verify-ids <shard>       shard ids ARE the hot corpus ids, or exit 1
#   ./shardctl.sh ready <shard> [gen]      flip shard_status to ready  (workstream F's seam)
#   ./shardctl.sh building <shard> [note]  flip it back
#   ./shardctl.sh egress on|off <shard>    temporary external address, for apt only
#   ./shardctl.sh hold on|off <shard>      stop the reaper touching it, for a corpus load
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
#  A c4 create that hits a zone stockout clears on retry; see cmd_create.
CREATE_RETRIES="${SHARD_CREATE_RETRIES:-6}"
CREATE_RETRY_SECONDS="${SHARD_CREATE_RETRY_SECONDS:-20}"
#  Starting a TERMINATED c4 hits the same stockout; a stopped VM holds no capacity reservation.
START_RETRIES="${SHARD_START_RETRIES:-20}"
START_RETRY_SECONDS="${SHARD_START_RETRY_SECONDS:-30}"
#  The fleet's own service account, the same one every other patents VM runs as. A shard needs an
#  identity to pull its corpus release out of GCS when workstream F loads it; with none it can only
#  ever be filled by pushing over ssh.
SERVICE_ACCOUNT="${SHARD_SERVICE_ACCOUNT:-nimo-843@nimo-gpt.iam.gserviceaccount.com}"
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
#  `|| true` is not decoration. `describe` on an instance that does not exist exits non zero, and
#  under `set -e` a bare `ip="$(inst_ip ...)"` KILLS the script: `status` printed its header and
#  then nothing at all for the whole fleet, which is exactly the read only command a session is
#  told to run first. Absent is an answer, not an error.
inst_ip() {
  gcloud compute instances describe "$(vm_of "$1")" --zone "$(zone_of "$1")" \
      --format='value(networkInterfaces[0].networkIP)' 2>/dev/null || true
}
inst_external_ip() {
  gcloud compute instances describe "$(vm_of "$1")" --zone "$(zone_of "$1")" \
      --format='value(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || true
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
  printf '%-12s %-20s %-16s %7s  %s\n' SHARD VM ZONE DOMAINS 'FIRST FEW'
  awk -F'\t' '!/^#/ && NF>=3 {n=split($4,a,","); printf "%-12s %-20s %-16s %7d  %s...\n",
              $1, $2, $3, n, substr($4,1,44)}' "$TABLE"
}

cmd_status() {
  local shards=("$@"); [ ${#shards[@]} -eq 0 ] && mapfile -t shards < <(all_shards)
  printf '%-12s %-12s %-15s %-9s %-9s %s\n' SHARD INSTANCE IP STATE INDEX REASON
  for s in "${shards[@]}"; do
    local st ip health state index reason
    st="$(inst_status "$s")" || st=ABSENT
    ip="$(inst_ip "$s")" || ip=""
    state="-"; index="-"; reason=""
    if [ "$st" = "RUNNING" ] && [ -n "$ip" ]; then
      health="$(curl -fsS --max-time 3 "http://$ip:$AGENT_PORT/health" 2>/dev/null || echo '')"
      if [ -n "$health" ]; then
        state="$(echo "$health" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["state"])' 2>/dev/null || echo '?')"
        index="$(echo "$health" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["index"]["state"])' 2>/dev/null || echo '?')"
        reason="$(echo "$health" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["reason"])' 2>/dev/null || echo '')"
      else
        state="waking"; reason="agent not answering"
      fi
    elif [ "$st" = "TERMINATED" ] || [ "$st" = "SUSPENDED" ] || [ "$st" = "STOPPING" ] \
         || [ "$st" = "SUSPENDING" ]; then
      state="cold"
    elif [ "$st" = "PROVISIONING" ] || [ "$st" = "STAGING" ]; then
      #  Named, because this is the state an operator most wants to see during a wake and the
      #  previous version printed a bare dash for it, which reads like "no answer".
      state="waking"; reason="instance is $st"
    elif [ "$st" = "ABSENT" ]; then
      state="absent"; reason="not created"
    fi
    printf '%-12s %-12s %-15s %-9s %-9s %s\n' "$s" "$st" "${ip:--}" "$state" "$index" "$reason"
  done
}

cmd_create() {
  local s="$1"; need "$s"
  local vm zone; vm="$(vm_of "$s")"; zone="$(zone_of "$s")"
  if gcloud compute instances describe "$vm" --zone "$zone" >/dev/null 2>&1; then
    echo "$vm already exists, nothing to create"
    #  Idempotent means convergent, not "skip". A shard created by an earlier pass and then left
    #  RUNNING is the expensive mistake this whole fleet is exposed to, so bring it back to its
    #  resting state rather than walking past it. Anything actually serving holds a lease and is
    #  the reaper's business, not create's; create only ever sees a shard nobody asked for.
    if [ "$(inst_status "$s")" = "RUNNING" ] && [ "${SHARD_CREATE_LEAVE_RUNNING:-0}" != "1" ]; then
      echo "  ...and it is RUNNING with nothing asking for it; stopping it"
      gcloud compute instances stop "$vm" --zone "$zone" --quiet >/dev/null
    fi
    return 0
  fi
  echo "creating $vm ($MACHINE, ${DISK_GB}GB $DISK_TYPE, no external address) in $zone"
  #  STOCKOUT RETRY, and it is not defensive programming. MEASURED 2026-08-22: creating a
  #  c4-highmem-16 in us-central1-b returned ZONE_RESOURCE_POOL_EXHAUSTED and the SAME request
  #  succeeded on a later attempt with nothing else changed. c4 is a newer family with thinner
  #  per-zone capacity than the e2/n2 machines the rest of this project runs on, so a first-try
  #  failure says nothing about whether the shard can exist.
  #
  #  Retrying in the SAME zone on purpose. The zone is a column in shards.tsv and the shard's
  #  disk lives in it; a create that quietly landed a shard in another zone would leave the table
  #  wrong, and a wrong table routes a query to a VM that is not the one holding that domain.
  local attempt=1 err rc
  while :; do
    err="$(mktemp)"
    rc=0
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
      --labels "purpose=patents-shard,shard=$s,tier=cold" \
      --service-account "$SERVICE_ACCOUNT" --scopes cloud-platform 2>"$err" || rc=$?
    cat "$err" >&2
    [ "$rc" = "0" ] && { rm -f "$err"; break; }
    #  Only a capacity error is retried. A quota error, a bad flag or a permission error will
    #  never clear by waiting, and retrying it eight times just buries the real message.
    if grep -qE 'ZONE_RESOURCE_POOL_EXHAUSTED|does not have enough resources available|resource pool exhausted|RESOURCE_POOL_EXHAUSTED_WITH_DETAILS|Internal error' "$err" \
       && [ "$attempt" -lt "$CREATE_RETRIES" ]; then
      local wait=$(( CREATE_RETRY_SECONDS * attempt ))
      echo "  $vm: capacity error in $zone on attempt $attempt/$CREATE_RETRIES; retrying in ${wait}s" >&2
      rm -f "$err"
      sleep "$wait"
      attempt=$(( attempt + 1 ))
      continue
    fi
    rm -f "$err"
    #  Never leave a half created shard behind: `create` is re-runnable and a caller that sees a
    #  non zero exit must be able to run it again from a clean state.
    gcloud compute instances describe "$vm" --zone "$zone" >/dev/null 2>&1 \
      && gcloud compute instances delete "$vm" --zone "$zone" --quiet >/dev/null 2>&1 || true
    die "could not create $vm in $zone after $attempt attempt(s)"
  done
  #  Created RUNNING. It has nothing on it yet, so bring it straight back down: a shard's resting
  #  state is TERMINATED and an unbootstrapped one that is left up is pure burn.
  gcloud compute instances stop "$vm" --zone "$zone" --quiet
}

cmd_create_all() {
  #  THE COST DECISION, taken 2026-08-22. `cost` prints the arithmetic from the machine and disk
  #  types actually used. Serial and not parallel: eight c4-highmem-16 created at once is 128 vCPU
  #  live at the same moment, and each one is stopped before the next is created.
  for s in $(all_shards); do cmd_create "$s"; done
  echo
  echo "created. Verifying every shard is TERMINATED, which is the whole cost argument:"
  cmd_verify_cold
}

cmd_verify_cold() {
  #  The check the cost decision rests on, as a command rather than a promise. Exit 1 if anything
  #  is up, so it can be the last line of a script or a cron and actually mean something.
  local bad=0 st
  for s in $(all_shards); do
    st="$(inst_status "$s")"
    printf '  %-12s %s\n' "$s" "$st"
    [ "$st" = "TERMINATED" ] || [ "$st" = "ABSENT" ] || bad=1
  done
  if [ "$bad" = "1" ]; then echo "NOT ALL COLD: a running shard is \$761/month" >&2; return 1; fi
  echo "all cold."
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

#  Where the fetched wheel is kept between bootstraps. Gitignored: a 4.6 MB binary does not belong
#  in the repo, and a second copy of one that already exists in two other worktrees belongs there
#  even less.
WHEEL_CACHE="${SHARD_WHEEL_CACHE:-$HERE/.cache}"

fetch_tantivy_wheel() {                    # fetch_tantivy_wheel <dest-dir>
  local dest="$1" wheel=""
  if [ -n "${SHARD_TANTIVY_WHEEL:-}" ] && [ -r "${SHARD_TANTIVY_WHEEL}" ]; then
    cp "$SHARD_TANTIVY_WHEEL" "$dest/" && return 0
  fi
  wheel="$(ls "$WHEEL_CACHE"/tantivy-*.whl 2>/dev/null | head -1 || true)"
  if [ -z "$wheel" ]; then
    mkdir -p "$WHEEL_CACHE"
    echo "fetching the tantivy wheel once, to $WHEEL_CACHE"
    "$(dirname "$PY")/pip" download tantivy --no-deps -q -d "$WHEEL_CACHE" >/dev/null 2>&1 || true
    wheel="$(ls "$WHEEL_CACHE"/tantivy-*.whl 2>/dev/null | head -1 || true)"
  fi
  #  cp311 and manylinux, because the shard runs Debian 12's python3.11. A wheel for another
  #  interpreter would unpack and then fail to import, and tantivy_server.py would report
  #  `state: missing`, which is honest but is not what anybody wanted.
  case "$(basename "${wheel:-none}")" in
    *cp311*manylinux*) ;;
    *) [ -n "$wheel" ] && echo "  the cached wheel $(basename "$wheel") is not cp311 manylinux" >&2 ;;
  esac
  [ -n "$wheel" ] || return 1
  cp "$wheel" "$dest/"
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
  cp -r "$HERE"/{bootstrap.sh,shard_agent.py,prewarm.py,tantivy_serve.sh,tantivy_server.py,systemd,sql} "$tmp/"
  #  The Tantivy extension travels WITH the payload. A shard is private IP only and the address it
  #  borrows for apt is taken off at the end of bootstrap, so it has no route to PyPI at any point
  #  a re-run might happen. The wheel is fetched once here, on the controller, cached, and
  #  unpacked on the shard with python3's own zipfile: no pip and no network on the far side.
  fetch_tantivy_wheel "$tmp" || echo "  ...continuing without Tantivy; the server will say so" >&2
  tar czf "$tmp/payload.tgz" -C "$tmp" $(cd "$tmp" && ls | grep -v '^payload.tgz$')

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
  #  RETRIED ON A CAPACITY ERROR, for the same reason `create` is. MEASURED 2026-08-22, four times
  #  in us-central1-b: `instances start` on a TERMINATED c4-highmem-16 fails with
  #  ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS. A stopped VM holds no capacity reservation, so a
  #  shard is not guaranteed to come back just because it exists, and this is the operator path
  #  where waiting is the right answer. The SEARCH path does not wait: shard_backend._start
  #  reissues on a background thread and `ensure` reports `cold` and moves on.
  local s="$1"; need "$s"
  local vm zone attempt=1 err rc
  vm="$(vm_of "$s")"; zone="$(zone_of "$s")"
  while :; do
    err="$(mktemp)"; rc=0
    gcloud compute instances start "$vm" --zone "$zone" --quiet >/dev/null 2>"$err" || rc=$?
    [ "$rc" = "0" ] && { rm -f "$err"; echo "$s starting"; return 0; }
    if grep -qE 'ZONE_RESOURCE_POOL_EXHAUSTED|does not have enough resources available|STOCKOUT' "$err" \
       && [ "$attempt" -lt "$START_RETRIES" ]; then
      #  Backoff, capped: a stockout clears when the zone frees a machine, not sooner for asking
      #  politely, and an uncapped ramp would leave the last attempts hours apart.
      local wait=$(( START_RETRY_SECONDS * attempt )); [ "$wait" -gt 120 ] && wait=120
      echo "  $vm: $zone has no c4 capacity on attempt $attempt/$START_RETRIES; retrying in ${wait}s" >&2
      rm -f "$err"; sleep "$wait"; attempt=$(( attempt + 1 )); continue
    fi
    cat "$err" >&2; rm -f "$err"
    die "could not start $vm in $zone after $attempt attempt(s)"
  done
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

cmd_verify_ids() {
  #  A shard's publication ids MUST be the hot corpus's ids. See ops/shards/verify_ids.py for why
  #  a mismatch is a wrong answer that looks like a right one rather than a failure.
  local s="$1"; need "$s"; shift
  PYTHONPATH="$REPO/src" "$PY" "$HERE/verify_ids.py" "$s" "$@"
}

cmd_ready() {
  local s="$1"; need "$s"; local gen="${2:-manual}"
  #  THE ID GATE. `ready` is the only thing that can make a shard `hot`, so it is the only place
  #  that can stop a renumbered shard being queried. A mismatch here is not a slow shard or an
  #  empty one: it is a hot family silently attributed to a cold document, and nothing downstream
  #  can see it. SHARD_SKIP_ID_CHECK=1 exists for a shard whose hot corpus is unreachable, and
  #  using it is a decision somebody has to type.
  if [ "${SHARD_SKIP_ID_CHECK:-0}" != "1" ]; then
    cmd_verify_ids "$s" || die "$s failed the publication id check; refusing to mark it ready"
  fi
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

cmd_hold() {
  #  Stop the idle reaper touching a shard. A corpus load runs for hours, holds no SEARCH lease
  #  because it is not a search, and is idle between batches, so the reaper's in-flight-query rule
  #  is not enough to protect it on its own.
  local mode="$1" s="$2"; need "$s"
  local vm zone; vm="$(vm_of "$s")"; zone="$(zone_of "$s")"
  case "$mode" in
    on)  gcloud compute instances add-labels "$vm" --zone "$zone" --labels reap-hold=on --quiet \
           && echo "$s is held: the reaper will not stop it. TAKE IT OFF WHEN THE LOAD IS DONE." ;;
    off) gcloud compute instances remove-labels "$vm" --zone "$zone" --labels reap-hold --quiet \
           && echo "$s is no longer held" ;;
    *)   die "hold on|off <shard>" ;;
  esac
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
  verify-cold) shift; cmd_verify_cold "$@" ;;
  bootstrap)  shift; cmd_bootstrap "$@" ;;
  schema)     shift; cmd_schema "$@" ;;
  start)      shift; cmd_start "$@" ;;
  stop)       shift; cmd_stop "$@" ;;
  wake)       shift; cmd_wake "$@" ;;
  health)     shift; cmd_health "$@" ;;
  ready)      shift; cmd_ready "$@" ;;
  verify-ids) shift; cmd_verify_ids "$@" ;;
  building)   shift; cmd_building "$@" ;;
  egress)     shift; cmd_egress "$@" ;;
  hold)       shift; cmd_hold "$@" ;;
  reap)       shift; cmd_reap "$@" ;;
  cost)       shift; cmd_cost "$@" ;;
  *) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 1 ;;
esac
