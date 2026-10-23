#!/usr/bin/env bash
# Shared host-side checks for PakistanLawSite harvest / login-session locks.
# Sourced by auto_deploy.sh and pls_keepalive_cron.sh (do not execute directly).
pls_host_redis_cli() {
  docker compose exec -T redis redis-cli "$@"
}

pls_host_redis_lock_busy() {
  local key exists
  if ! docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx redis; then
    return 1
  fi
  for key in \
    "corpus:login_session_lock:PakistanLawSite" \
    "corpus:login_session_lock:PakistanLawSite:holders" \
    "corpus:login_session_lock:PakistanLawSite:slot1" \
    "corpus:login_session_lock:PakistanLawSite:slot2"; do
    exists="$(pls_host_redis_cli EXISTS "$key" 2>/dev/null | tr -d '\r' || echo 0)"
    if [ "$exists" = "1" ]; then
      return 0
    fi
  done
  return 1
}

pls_host_running_jobs() {
  docker compose exec -T postgres psql -U "${POSTGRES_USER:-legal}" -d "${POSTGRES_DB:-legal_scraper}" -tAc \
    "SELECT COUNT(*) FROM scraper_jobs WHERE source_name = 'PakistanLawSite' AND status = 'running';" 2>/dev/null \
    | tr -d ' \r\n' || echo "0"
}

# True when a harvest or login-session worker holds the lock or a PLS job is running.
pls_host_harvest_busy() {
  if pls_host_redis_lock_busy; then
    return 0
  fi
  local n
  n="$(pls_host_running_jobs)"
  if [ -n "$n" ] && [ "${n:-0}" -gt 0 ] 2>/dev/null; then
    return 0
  fi
  return 1
}

# True when the celery-beat service container is running (not merely defined).
pls_host_beat_running() {
  docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx celery-beat
}

# A deploy waiting for the PakistanLawSite job to finish sets this Redis key instead of stopping celery-beat:
# the scraper then starts no new PakistanLawSite work (scraper/tasks/deploy_hold.py) while every other
# source, promotion and the Drive mirror keep running. The key expires on its own if a deploy dies.
PLS_HOST_LOGIN_HOLD_KEY="deploy_hold_login"

# True when the running worker image honours the hold in every PakistanLawSite entry point: version 1 (the
# first hold) still let slot recovery, keepalive, the watchdog and the spot check run, so it does not count.
PLS_HOST_LOGIN_HOLD_MIN_VERSION=2
pls_host_workers_know_login_hold() {
  docker compose exec -T worker-scraper python -c "import sys, scraper.tasks.deploy_hold as h; sys.exit(0 if getattr(h, 'HOLD_GUARDS_VERSION', 1) >= $PLS_HOST_LOGIN_HOLD_MIN_VERSION else 1)" >/dev/null 2>&1
}

pls_host_set_login_hold() {
  pls_host_redis_cli SET "$PLS_HOST_LOGIN_HOLD_KEY" "$(date +%s)" EX "${1:-3300}" >/dev/null 2>&1
}

pls_host_clear_login_hold() {
  pls_host_redis_cli DEL "$PLS_HOST_LOGIN_HOLD_KEY" >/dev/null 2>&1 || true
}

pls_host_stop_beat() {
  if pls_host_beat_running; then
    docker compose stop celery-beat
  fi
}

pls_host_start_beat() {
  docker compose up -d --no-build celery-beat
}

# Start the existing celery-beat container as it was (no recreate onto a newer local image).
pls_host_restore_beat() {
  docker compose start celery-beat
}

# True when /status.json reports a promotion stall (zero judgments while harvest is active).
pls_host_promotion_stalled() {
  local api="${PLS_HOST_STATUS_URL:-http://127.0.0.1:8000/status.json}"
  curl -fsS "$api" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not d.get("stalled"):
    sys.exit(1)
if d.get("stalled_reason") != "no_output_while_harvesting":
    sys.exit(1)
sys.exit(0)
' 2>/dev/null
}

# Stop a zero-output PLS job and drop login locks so auto_deploy can roll out fixes during a stall.
pls_host_env_flag_enabled() {
  local key="$1"
  local val
  val="$(grep -E "^${key}=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true)"
  case "${val,,}" in 1|true|yes|on) return 0 ;; esac
  return 1
}

pls_host_release_search_harvest_locks() {
  if ! pls_host_env_flag_enabled PLS_DEPLOY_RELEASE_SEARCH_HARVEST_LOCKS; then
    return 0
  fi
  # Only a positively identified, stopped local worker may release its own locks.
  # Never DEL by pattern: Redis may be shared with a live worker on another host.
  local expected_host="${PLS_HOST_STOPPED_WORKER_HOSTNAME:-}"
  if [ -z "$expected_host" ]; then
    printf 'pls_host: refusing search-harvest lock release without verified stopped worker identity\n' >&2
    return 1
  fi
  if ! docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx redis; then
    return 0
  fi
  local n=0 key token rest pid nonce result
  while IFS= read -r key; do
    [ -z "$key" ] && continue
    token="$(pls_host_redis_cli --raw GET "$key" 2>/dev/null)" || continue
    case "$token" in "$expected_host":*:*) ;; *) continue ;; esac
    rest="${token#*:}"
    pid="${rest%%:*}"
    nonce="${rest#*:}"
    [[ "$pid" =~ ^[0-9]+$ ]] && [ -n "$nonce" ] || continue
    # Compare-and-delete protects against an owner change between GET and EVAL.
    result="$(pls_host_redis_cli EVAL "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end" 1 "$key" "$token" 2>/dev/null)" || continue
    [ "$result" = 1 ] && n=$((n + 1))
  done < <(pls_host_redis_cli --scan --pattern 'corpus:login_session_lock:PakistanLawSite:search_harvest:*' 2>/dev/null || true)
  if [ "$n" -gt 0 ]; then
    printf '[%s] pls_host: released %s verified local search-harvest Redis lock(s)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$n"
  fi
}

# Fail closed: Docker stop success alone does not prove the scraper is gone.
pls_host_stop_scraper_safely() {
  local running stopped_host
  PLS_HOST_STOPPED_WORKER_HOSTNAME=""
  # Capture the actual container hostname before stopping; the lock token
  # uses socket.gethostname() inside this same worker container.
  if ! stopped_host="$(docker compose exec -T worker-scraper hostname 2>/dev/null)"; then
    printf 'pls_host: cannot identify local worker; refusing lock release\n' >&2
    return 1
  fi
  if ! [[ "$stopped_host" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    printf 'pls_host: invalid local worker identity; refusing lock release\n' >&2
    return 1
  fi
  if ! docker compose stop worker-scraper; then
    printf 'pls_host: worker-scraper stop failed; refusing lock release\n' >&2
    return 1
  fi
  if ! running="$(docker compose ps --status running --format '{{.Service}}')"; then
    printf 'pls_host: cannot verify worker-scraper state; refusing lock release\n' >&2
    return 1
  fi
  if printf '%s\n' "$running" | grep -qx 'worker-scraper'; then
    printf 'pls_host: worker-scraper is still running; refusing lock release\n' >&2
    return 1
  fi
  PLS_HOST_STOPPED_WORKER_HOSTNAME="$stopped_host"
  return 0
}

# Force-recovery is allowed to stop the local worker and release only locks proven
# to belong to that stopped worker. It must NEVER fail another host's jobs or
# delete shared login locks merely because the local worker stopped.
pls_host_confirm_pls_idle_after_local_stop() {
  local key exists remaining jobs
  if ! docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx redis; then
    return 1
  fi
  for key in \
    "corpus:login_session_lock:PakistanLawSite" \
    "corpus:login_session_lock:PakistanLawSite:holders" \
    "corpus:login_session_lock:PakistanLawSite:slot1" \
    "corpus:login_session_lock:PakistanLawSite:slot2"; do
    if ! exists="$(pls_host_redis_cli EXISTS "$key" 2>/dev/null)"; then
      return 1
    fi
    exists="$(printf '%s' "$exists" | tr -d ' \\r\\n')"
    [ "$exists" = 0 ] || return 1
  done
  if ! remaining="$(pls_host_redis_cli --scan --pattern 'corpus:login_session_lock:PakistanLawSite:search_harvest:*' 2>/dev/null)"; then
    return 1
  fi
  [ -z "$remaining" ] || return 1
  if ! jobs="$(docker compose exec -T postgres psql -U "${POSTGRES_USER:-legal}" -d "${POSTGRES_DB:-legal_scraper}" -tAc \
    "SELECT COUNT(*) FROM scraper_jobs WHERE source_name = 'PakistanLawSite' AND status = 'running';" 2>/dev/null)"; then
    return 1
  fi
  jobs="$(printf '%s' "$jobs" | tr -d ' \\r\\n')"
  [[ "$jobs" =~ ^[0-9]+$ ]] && [ "$jobs" -eq 0 ]
}

pls_host_force_release_stalled_harvest() {
  if ! pls_host_stop_scraper_safely; then
    return 1
  fi
  if ! pls_host_release_search_harvest_locks; then
    return 1
  fi
  if ! pls_host_confirm_pls_idle_after_local_stop; then
    printf 'pls_host: PLS lock/job ownership or idle state not verified; refusing global lock deletion and job failure marking\\n' >&2
    return 1
  fi
  return 0
}
