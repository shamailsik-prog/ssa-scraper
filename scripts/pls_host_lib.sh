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

pls_host_stop_beat() {
  if pls_host_beat_running; then
    docker compose stop celery-beat
  fi
}

pls_host_start_beat() {
  docker compose up -d --no-build celery-beat
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
pls_host_force_release_stalled_harvest() {
  docker compose stop worker-scraper 2>/dev/null || true
  docker compose exec -T postgres psql -U "${POSTGRES_USER:-legal}" -d "${POSTGRES_DB:-legal_scraper}" -v ON_ERROR_STOP=1 -c \
    "UPDATE scraper_jobs SET status = 'failed', finished_at = COALESCE(finished_at, NOW()), error_message = LEFT(COALESCE(error_message, '') || ' auto_deploy: released stalled zero-output job for deploy', 4000) WHERE source_name = 'PakistanLawSite' AND status = 'running';" \
    2>/dev/null || true
  if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx redis; then
    for key in \
      "corpus:login_session_lock:PakistanLawSite" \
      "corpus:login_session_lock:PakistanLawSite:holders" \
      "corpus:login_session_lock:PakistanLawSite:slot1" \
      "corpus:login_session_lock:PakistanLawSite:slot2"; do
      pls_host_redis_cli DEL "$key" >/dev/null 2>&1 || true
    done
  fi
}
