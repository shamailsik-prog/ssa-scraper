#!/usr/bin/env bash
# Host half of the PakistanLawSite self-healing watchdog (cron, every 10 minutes; installed by scripts/auto_deploy.sh).
#
# The in-app watchdog (scraper.tasks.pls_throughput_watchdog, Celery Beat) diagnoses idle harvest and fixes
# what it can from inside the stack. What needs Docker is done here:
#   * recreate worker-scraper when the in-app watchdog asks (state/pls_watchdog_request.json),
#   * recreate worker-scraper when its container is missing or unhealthy,
#   * restart celery-beat when it is down while PakistanLawSite is ACTIVE and no deploy is running
#     (a deploy that died after stopping beat would otherwise stall everything; touch state/beat_hold to keep it off),
#   * cap worker-public memory (docker update, no restart),
#   * keep state/Caddyfile rendered from the repository (scripts/render_caddyfile.sh).
# Every run writes state/pls_watchdog_host.json, which the live dashboard shows.
set -uo pipefail

DIR="${SSA_SCRAPER_DIR:-/opt/ssa-scraper}"
cd "$DIR" || exit 1
mkdir -p state
REQ="state/pls_watchdog_request.json"
OUT="state/pls_watchdog_host.json"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env >/dev/null 2>&1 || true
  set +a
fi
# shellcheck source=scripts/pls_host_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/pls_host_lib.sh"

json_get() {  # json_get FILE KEY
  python3 - "$1" "$2" <<'PY' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")
except Exception:
    print("")
PY
}

ACTIONS=()
NOTES=()
HANDLED="$(json_get "$OUT" handled_request_at)"
LAST_ACTION="$(json_get "$OUT" last_action)"
LAST_AT="$(json_get "$OUT" at)"

deploy_running() { pgrep -f "scripts/auto_deploy.sh" >/dev/null 2>&1 || pgrep -f "cloud/install.sh" >/dev/null 2>&1; }

recreate_worker() {
  if deploy_running; then
    NOTES+=("deploy running; worker recreate deferred")
    return 1
  fi
  if docker compose up -d --no-deps --no-build --force-recreate worker-scraper >/dev/null 2>&1; then
    ACTIONS+=("recreated worker-scraper: $1")
    return 0
  fi
  NOTES+=("worker-scraper recreate failed: $1")
  return 1
}

# 1. In-app watchdog request
if [ -f "$REQ" ]; then
  REQ_AT="$(json_get "$REQ" requested_at)"
  if [ -n "$REQ_AT" ] && [ "$REQ_AT" != "$HANDLED" ]; then
    if recreate_worker "$(json_get "$REQ" reason | cut -c1-200)"; then
      HANDLED="$REQ_AT"
    fi
  fi
fi

# 2. worker-scraper missing or unhealthy
CID="$(docker compose ps -q worker-scraper 2>/dev/null | head -1)"
HEALTH=""
if [ -n "$CID" ]; then
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CID" 2>/dev/null || true)"
fi
if [ -z "$CID" ] || [ "$HEALTH" = "unhealthy" ] || [ "$HEALTH" = "exited" ] || [ "$HEALTH" = "dead" ]; then
  recreate_worker "container ${HEALTH:-missing}" || true
fi

# 3. celery-beat down while PakistanLawSite is ACTIVE
if ! pls_host_beat_running && [ ! -f state/beat_hold ] && ! deploy_running; then
  PLS_STATE="$(docker compose exec -T postgres psql -U "${POSTGRES_USER:-legal}" -d "${POSTGRES_DB:-legal_scraper}" -tAc \
    "SELECT state FROM scraper_sources WHERE source_name = 'PakistanLawSite';" 2>/dev/null | tr -d ' \r\n')"
  if [ "$PLS_STATE" = "ACTIVE" ]; then
    if pls_host_start_beat >/dev/null 2>&1; then
      ACTIONS+=("restarted celery-beat (was down while PakistanLawSite ACTIVE)")
    else
      NOTES+=("celery-beat down and restart failed")
    fi
  fi
fi

# 4. memory guard: cap worker-public so a runaway public scrape cannot starve the Chromium of the PLS slots.
#    Applied live with docker update (no restart); re-applied after every recreate. SSA_WORKER_PUBLIC_MEM=0 disables.
PUB_MEM="${SSA_WORKER_PUBLIC_MEM:-2560m}"
PUB_SWAP="${SSA_WORKER_PUBLIC_MEMSWAP:-3584m}"
if [ "$PUB_MEM" != "0" ]; then
  PUB_CID="$(docker compose ps -q worker-public 2>/dev/null | head -n1)"
  if [ -n "$PUB_CID" ] && [ "$(docker inspect -f '{{.HostConfig.Memory}}' "$PUB_CID" 2>/dev/null)" = "0" ]; then
    if docker update --memory "$PUB_MEM" --memory-swap "$PUB_SWAP" "$PUB_CID" >/dev/null 2>&1; then
      ACTIONS+=("capped worker-public memory at $PUB_MEM")
    else
      NOTES+=("could not cap worker-public memory")
    fi
  fi
fi

# 5. Caddy config from the repository
if ! CADDY_OUT="$(bash scripts/render_caddyfile.sh 2>&1)"; then
  NOTES+=("caddy: ${CADDY_OUT:0:200}")
elif printf '%s' "$CADDY_OUT" | grep -q "reloaded"; then
  ACTIONS+=("caddy config re-rendered and reloaded")
fi

if [ "${#ACTIONS[@]}" -gt 0 ]; then
  LAST_ACTION="$(IFS='; '; echo "${ACTIONS[*]}")"
  LAST_AT="$NOW"
fi
NOTES_JOINED="$(IFS='; '; echo "${NOTES[*]:-}")"
python3 - "$OUT" "$NOW" "$HANDLED" "$LAST_ACTION" "$LAST_AT" "$NOTES_JOINED" "${HEALTH:-missing}" <<'PY'
import json, sys
out, now, handled, last_action, last_at, notes, health = sys.argv[1:8]
json.dump({"checked_at": now, "handled_request_at": handled, "last_action": last_action, "at": last_at,
           "notes": notes, "worker_scraper": health}, open(out, "w"))
PY
if [ "${#ACTIONS[@]}" -gt 0 ] || [ -n "$NOTES_JOINED" ]; then
  echo "[$NOW] actions: ${LAST_ACTION:-none} notes: ${NOTES_JOINED:-none}"
fi
