#!/usr/bin/env bash
# Idempotent droplet auto-deploy: when origin/main advances, pull and roll out only the
# docker-compose services that need it — after PakistanLawSite harvest/login work is idle.
set -euo pipefail

DIR="${SSA_SCRAPER_DIR:-/opt/ssa-scraper}"
BRANCH="${SSA_SCRAPER_BRANCH:-main}"
REMOTE="origin"
LOG_FILE="state/auto_deploy.log"
TIP_FILE="state/tip_sha.txt"
RESET_MARKER="state/reset_retired_frontier_sha.txt"
CRON_MARK="# ssa-scraper auto-deploy from origin/main (every 15 minutes)"

# PLS stack (shared corpus-service image).
DEFAULT_APP_SERVICES=(api worker-scraper worker-public celery-beat)
ALL_APP_SERVICES=(api worker-scraper worker-public worker-embed celery-beat celery-flower)

WAIT_MAX_SECONDS="${AUTO_DEPLOY_WAIT_MAX_SECONDS:-3300}"  # just under 55m (15m cron cadence)
WAIT_POLL_SECONDS="${AUTO_DEPLOY_WAIT_POLL_SECONDS:-30}"

usage() {
  sed -n '2,8p' "$0"
  exit "${1:-0}"
}

log() {
  local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
  printf '%s\n' "$msg"
  mkdir -p "$DIR/state"
  printf '%s\n' "$msg" >>"$DIR/$LOG_FILE"
}

die() {
  log "ERROR: $*"
  exit 1
}

install_cron() {
  local cron_line="*/15 * * * * cd $DIR && /bin/bash scripts/auto_deploy.sh >> state/auto_deploy.log 2>&1"
  local cron_now
  cron_now="$(crontab -l 2>/dev/null || true)"
  if printf '%s\n' "$cron_now" | grep -Fq "$CRON_MARK"; then
    return 0
  fi
  log "Installing host auto-deploy cron (every 15 minutes)"
  { printf '%s\n' "$cron_now"; printf '%s\n' "$CRON_MARK"; printf '%s\n' "$cron_line"; } | crontab -
}

redis_cli() {
  docker compose exec -T redis redis-cli "$@"
}

pls_redis_lock_busy() {
  local key exists
  if ! docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx redis; then
    return 1
  fi
  for key in \
    "corpus:login_session_lock:PakistanLawSite" \
    "corpus:login_session_lock:PakistanLawSite:holders" \
    "corpus:login_session_lock:PakistanLawSite:slot1" \
    "corpus:login_session_lock:PakistanLawSite:slot2"; do
    exists="$(redis_cli EXISTS "$key" 2>/dev/null | tr -d '\r' || echo 0)"
    if [ "$exists" = "1" ]; then
      return 0
    fi
  done
  return 1
}

pls_running_jobs() {
  # Running harvest/login rows for PakistanLawSite (login_session queue).
  docker compose exec -T postgres psql -U "${POSTGRES_USER:-corpus}" -d "${POSTGRES_DB:-corpus}" -tAc \
    "SELECT COUNT(*) FROM scraper_jobs WHERE source_name = 'PakistanLawSite' AND status = 'running';" 2>/dev/null \
    | tr -d ' \r\n' || echo "0"
}

pls_login_busy() {
  if pls_redis_lock_busy; then
    return 0
  fi
  local n
  n="$(pls_running_jobs)"
  if [ -n "$n" ] && [ "${n:-0}" -gt 0 ] 2>/dev/null; then
    return 0
  fi
  return 1
}

wait_for_pls_idle() {
  local waited=0
  while pls_login_busy; do
    if [ "$waited" -ge "$WAIT_MAX_SECONDS" ]; then
      log "PLS still busy after ${waited}s (lock or running job); deferring deploy to next cron tick"
      exit 0
    fi
    log "waiting for PLS harvest/login to finish (${waited}s / ${WAIT_MAX_SECONDS}s max)…"
    sleep "$WAIT_POLL_SECONDS"
    waited=$((waited + WAIT_POLL_SECONDS))
  done
}

services_for_diff() {
  # stdout: space-separated service names for `docker compose build` / `up`.
  local old_sha="$1" new_sha="$2"
  local changed f need_image=0 need_workers=0
  if [ "$old_sha" = "$new_sha" ]; then
    printf '%s\n' "${DEFAULT_APP_SERVICES[*]}"
    return 0
  fi
  changed="$(git -C "$DIR" diff --name-only "$old_sha" "$new_sha" || true)"
  if [ -z "$changed" ]; then
    printf '%s\n' "${DEFAULT_APP_SERVICES[*]}"
    return 0
  fi

  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      Dockerfile|.dockerignore|docker-compose.yml|requirements*.txt|pyproject.toml|poetry.lock)
        need_image=1
        ;;
      scraper/*|migrations/*|scripts/pls_server_login.py|scripts/pls_*)
        need_image=1
        ;;
      docs/*|*.md|LICENSE|SIKANDER_*|tests/*|.github/*)
        ;;
      *)
        need_image=1
        ;;
    esac
    case "$f" in
      docker-compose.yml)
        need_workers=1
        ;;
    esac
  done <<<"$changed"

  if [ "$need_image" = 0 ] && [ "$need_workers" = 0 ]; then
    return 0
  fi
  if [ "$need_workers" = 1 ]; then
    printf '%s\n' "${ALL_APP_SERVICES[*]}"
    return 0
  fi
  printf '%s\n' "${DEFAULT_APP_SERVICES[*]}"
}

run_reset_frontier_once() {
  local sha="$1"
  local marker="$DIR/$RESET_MARKER"
  if [ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null || true)" = "$sha" ]; then
    log "reset-retired-frontier already applied for $sha"
    return 0
  fi
  log "Running reset-retired-frontier for $sha"
  if docker compose exec -T api python -m scraper.tasks.pls_admin reset-retired-frontier; then
    mkdir -p "$DIR/state"
    printf '%s\n' "$sha" >"$marker"
  else
    log "reset-retired-frontier failed (deployed code is live; will retry on next SHA or manual run)"
  fi
}

main() {
  case "${1:-}" in
    -h|--help) usage 0 ;;
    --install-cron)
      install_cron
      exit 0
      ;;
  esac

  [ -d "$DIR/.git" ] || die "not a git checkout: $DIR"
  cd "$DIR"
  mkdir -p state
  install_cron

  # Load compose env for postgres user/db names when present.
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
  fi

  log "auto_deploy: fetch $REMOTE $BRANCH"
  git fetch "$REMOTE" "$BRANCH"

  local new_sha old_sha deployed_sha
  new_sha="$(git rev-parse "$REMOTE/$BRANCH")"
  deployed_sha="$(cat "$TIP_FILE" 2>/dev/null || true)"
  old_sha="$(git rev-parse HEAD)"

  if [ -n "$deployed_sha" ] && [ "$deployed_sha" = "$new_sha" ]; then
    log "origin/$BRANCH still at $new_sha (matches $TIP_FILE); nothing to do"
    exit 0
  fi

  if [ "$old_sha" = "$new_sha" ] && [ -z "$deployed_sha" ]; then
    log "checkout already at $new_sha; recording tip_sha without rollout"
    printf '%s\n' "$new_sha" >"$TIP_FILE"
    exit 0
  fi

  wait_for_pls_idle

  log "deploying $old_sha -> $new_sha"
  if ! git merge --ff-only "$REMOTE/$BRANCH"; then
    die "fast-forward merge failed; manual intervention required"
  fi

  local services_line services=()
  services_line="$(services_for_diff "$old_sha" "$new_sha" || true)"
  if [ -z "$services_line" ]; then
    log "no service-impacting files changed; recording tip_sha only"
    printf '%s\n' "$new_sha" >"$TIP_FILE"
    run_reset_frontier_once "$new_sha"
    exit 0
  fi
  read -r -a services <<<"$services_line"

  log "building services (fail-safe): ${services[*]}"
  if ! docker compose build "${services[@]}"; then
    log "docker compose build FAILED — leaving existing containers running"
    git reset --hard "$old_sha" || true
    exit 1
  fi

  log "rolling out: ${services[*]}"
  if ! docker compose up -d --no-build "${services[@]}"; then
    log "docker compose up FAILED after successful build — containers may be partially updated; not updating tip_sha"
    exit 1
  fi

  # Wait for API health before maintenance exec.
  local i
  for i in $(seq 1 40); do
    if docker compose ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -q '^api healthy'; then
      break
    fi
    sleep 5
  done

  run_reset_frontier_once "$new_sha"
  printf '%s\n' "$new_sha" >"$TIP_FILE"
  log "deploy complete; tip_sha=$new_sha"
}

main "$@"
