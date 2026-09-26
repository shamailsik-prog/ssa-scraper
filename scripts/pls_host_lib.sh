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
  docker compose exec -T postgres psql -U "${POSTGRES_USER:-corpus}" -d "${POSTGRES_DB:-corpus}" -tAc \
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
