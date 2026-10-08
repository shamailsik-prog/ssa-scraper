#!/usr/bin/env bash
# Idempotent droplet auto-deploy: when origin/main advances, pull and roll out only the
# docker-compose services that need it — after PakistanLawSite harvest/login work is idle.
set -euo pipefail

DIR="${SSA_SCRAPER_DIR:-/opt/ssa-scraper}"
BRANCH="${SSA_SCRAPER_BRANCH:-main}"
REMOTE="origin"
LOG_FILE="state/auto_deploy.log"
TIP_FILE="state/tip_sha.txt"
ROLLOUT_FILE="state/rollout_sha.txt"
RESET_MARKER="state/reset_retired_frontier_sha.txt"
CRON_MARK="# ssa-scraper auto-deploy from origin/main (every 15 minutes)"

# PLS stack (shared corpus-service image). celery-beat is the scheduler for every scraping, promotion and
# mirror task: a deploy holds new PakistanLawSite work (Redis key deploy_hold_login) while the PLS job
# drains, keeps beat running, and always clears the hold and makes sure beat runs afterwards, unless the
# operator has switched beat off with state/beat_disabled.
DEFAULT_APP_SERVICES=(api worker-scraper worker-public worker-maintenance)
ALL_APP_SERVICES=(api worker-scraper worker-public worker-maintenance worker-embed celery-beat celery-flower)

WAIT_MAX_SECONDS="${AUTO_DEPLOY_WAIT_MAX_SECONDS:-600}"  # bounded wait before deploy when beat was running
WAIT_POLL_SECONDS="${AUTO_DEPLOY_WAIT_POLL_SECONDS:-30}"
# A deferred deploy leaves beat paused (so no new PLS job starts) and records when in this file; once the
# pause is older than MAX_BEAT_PAUSE_SECONDS the deploy goes ahead and releases the running PLS job.
BEAT_PAUSE_MARKER="state/beat_paused_by_deploy"
BEAT_DISABLED_FILE="state/beat_disabled"
MAX_BEAT_PAUSE_SECONDS="${AUTO_DEPLOY_MAX_BEAT_PAUSE_SECONDS:-2700}"
DRY_RUN=0
PLS_FORCE_RELEASED=0

usage() {
  sed -n '2,8p' "$0"
  printf '\n  --dry-run   log actions without git merge, compose, or beat changes\n'
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

# shellcheck source=scripts/pls_host_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/pls_host_lib.sh"

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

beat_wanted() {
  [ ! -f "$DIR/$BEAT_DISABLED_FILE" ]
}

beat_pause_seconds() {
  local since
  since="$(cat "$DIR/$BEAT_PAUSE_MARKER" 2>/dev/null || true)"
  case "$since" in
    ''|*[!0-9]*) echo 0 ;;
    *) echo $(( $(date +%s) - since )) ;;
  esac
}

# Hold new PakistanLawSite work for this deploy and remember since when, across cron ticks. celery-beat keeps
# running, so public sources, promotion and the mirror do not stop while the PLS job drains (8 October 2026:
# stopping beat for each deferred deploy left nothing scheduled for up to 45 minutes per push). Only when
# the hold cannot be set is beat stopped, as before.
pause_beat() {
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: would hold new PakistanLawSite work and record $BEAT_PAUSE_MARKER"
    return 0
  fi
  if pls_host_set_login_hold $((MAX_BEAT_PAUSE_SECONDS + 600)); then
    log "new PakistanLawSite work held for the deploy; celery-beat keeps running"
  else
    log "could not set the login hold in Redis; stopping celery-beat instead"
    pls_host_stop_beat
  fi
  if [ ! -f "$DIR/$BEAT_PAUSE_MARKER" ]; then
    date +%s >"$DIR/$BEAT_PAUSE_MARKER"
  fi
}

# Start beat again (unless the operator disabled it) and clear the pause marker.
resume_beat() {
  if ! beat_wanted; then
    log "celery-beat left stopped: $BEAT_DISABLED_FILE is present"
    [ "$DRY_RUN" = 1 ] || pls_host_clear_login_hold
    rm -f "$DIR/$BEAT_PAUSE_MARKER"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: would start celery-beat and remove $BEAT_PAUSE_MARKER"
    return 0
  fi
  pls_host_clear_login_hold
  pls_host_start_beat || { log "ERROR: celery-beat did not start; next cron tick retries"; return 1; }
  rm -f "$DIR/$BEAT_PAUSE_MARKER"
}

wait_for_pls_idle() {
  local waited=0 paused
  while pls_host_harvest_busy; do
    if [ "$waited" -ge "$WAIT_MAX_SECONDS" ]; then
      paused="$(beat_pause_seconds)"
      if [ -f "$DIR/$BEAT_PAUSE_MARKER" ] && [ "$paused" -ge "$MAX_BEAT_PAUSE_SECONDS" ]; then
        log "celery-beat paused ${paused}s (max ${MAX_BEAT_PAUSE_SECONDS}s) and PLS still busy; releasing the PLS job and deploying"
        if [ "$DRY_RUN" = 1 ]; then
          log "dry-run: would release the PLS job and continue deploy"
        else
          pls_host_force_release_stalled_harvest
          PLS_FORCE_RELEASED=1
        fi
        return 0
      fi
      if pls_host_promotion_stalled; then
        log "PLS busy but /status reports no_output_while_harvesting; releasing lock for pending deploy"
        if [ "$DRY_RUN" = 1 ]; then
          log "dry-run: would force-release stalled PLS harvest and continue deploy"
        else
          pls_host_force_release_stalled_harvest
          PLS_FORCE_RELEASED=1
        fi
        return 0
      fi
      log "PLS still busy after ${waited}s (lock or running job); deferring deploy to next cron tick (celery-beat paused ${paused}s of ${MAX_BEAT_PAUSE_SECONDS}s)"
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
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: would run reset-retired-frontier for $sha"
    return 0
  fi
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
    --dry-run) DRY_RUN=1; shift ;;
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
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: would git fetch $REMOTE $BRANCH"
  else
    git fetch "$REMOTE" "$BRANCH"
  fi

  local new_sha old_sha deployed_sha rollout_sha
  new_sha="$(git rev-parse "$REMOTE/$BRANCH")"
  deployed_sha="$(cat "$TIP_FILE" 2>/dev/null || true)"
  rollout_sha="$(cat "$ROLLOUT_FILE" 2>/dev/null || true)"
  old_sha="$(git rev-parse HEAD)"

  if [ -n "$deployed_sha" ] && [ "$deployed_sha" = "$new_sha" ] && [ "$rollout_sha" = "$new_sha" ]; then
    if pls_host_promotion_stalled; then
      log "origin/$BRANCH at $new_sha (tip_sha and rollout_sha match) but /status reports no_output_while_harvesting; retrying compose rollout"
    else
      log "origin/$BRANCH still at $new_sha (tip_sha and rollout_sha match); nothing to do"
      # nothing is pending, so no PakistanLawSite hold may linger (a deploy that died leaves one behind)
      [ "$DRY_RUN" = 1 ] || pls_host_clear_login_hold
      if beat_wanted && ! pls_host_beat_running; then
        log "celery-beat is not running and no deploy is pending; starting it"
        resume_beat || exit 1
      fi
      exit 0
    fi
  elif [ -n "$deployed_sha" ] && [ "$deployed_sha" = "$new_sha" ] && [ "$rollout_sha" != "$new_sha" ]; then
    log "tip_sha=$new_sha but rollout_sha=${rollout_sha:-missing}; docker rollout still required"
  fi

  local pre_merge_sha="$old_sha"
  local merged=0
  if [ "$pre_merge_sha" != "$new_sha" ]; then
    log "fast-forward merge $pre_merge_sha -> $new_sha before waiting for PLS idle (same cron run picks up deploy script fixes)"
    if [ "$DRY_RUN" = 1 ]; then
      log "dry-run: would git merge --ff-only $REMOTE/$BRANCH"
    elif ! git merge --ff-only "$REMOTE/$BRANCH"; then
      die "fast-forward merge failed; manual intervention required"
    else
      merged=1
      # shellcheck source=scripts/pls_host_lib.sh
      source "$(dirname "${BASH_SOURCE[0]}")/pls_host_lib.sh"
    fi
  fi

  local beat_was_running=0
  if beat_wanted; then
    beat_was_running=1
    if pls_host_beat_running; then
      log "celery-beat is running; pausing it before deploy and waiting for PLS idle"
    else
      log "celery-beat is not running (paused $(beat_pause_seconds)s); keeping it paused until PLS is idle, then starting it"
    fi
    pause_beat
    wait_for_pls_idle
  else
    log "celery-beat is switched off ($BEAT_DISABLED_FILE); deploy will not start it"
    if pls_host_promotion_stalled && pls_host_harvest_busy; then
      wait_for_pls_idle
    fi
  fi

  log "deploying $pre_merge_sha -> $new_sha"
  if [ "$merged" = 0 ] && [ "$pre_merge_sha" != "$new_sha" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      log "dry-run: would git merge --ff-only $REMOTE/$BRANCH"
    elif ! git merge --ff-only "$REMOTE/$BRANCH"; then
      die "fast-forward merge failed; manual intervention required"
    fi
  fi

  local services_line services=()
  services_line="$(services_for_diff "$pre_merge_sha" "$new_sha" || true)"
  if [ -z "$services_line" ]; then
    log "no service-impacting files changed; recording tip_sha only"
    if [ "$DRY_RUN" = 1 ]; then
      log "dry-run: would record tip_sha=$new_sha"
    else
      printf '%s\n' "$new_sha" >"$TIP_FILE"
      run_reset_frontier_once "$new_sha"
    fi
    if [ "$beat_was_running" = 1 ]; then
      resume_beat || exit 1
    else
      rm -f "$DIR/$BEAT_PAUSE_MARKER"
    fi
    if [ "$PLS_FORCE_RELEASED" = 1 ]; then
      if [ "$DRY_RUN" = 1 ]; then
        log "dry-run: would start worker-scraper after force-releasing PLS harvest"
      else
        docker compose up -d --no-build worker-scraper || log "ERROR: worker-scraper did not start; next cron tick retries"
      fi
    fi
    exit 0
  fi
  read -r -a services <<<"$services_line"
  if [ "$beat_was_running" = 0 ]; then
    local filtered=()
    for svc in "${services[@]}"; do
      [ "$svc" = "celery-beat" ] && continue
      filtered+=("$svc")
    done
    services=("${filtered[@]}")
  fi

  export AUTO_DEPLOY_SERVICES="${services[*]}"
  log "rolling out via cloud/install.sh --deploy-only (fail-safe): ${services[*]}"
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: would run install.sh --deploy-only with AUTO_DEPLOY_SERVICES=${services[*]}"
  elif ! SSA_SCRAPER_DIR="$DIR" bash "$DIR/cloud/install.sh" --deploy-only --dir "$DIR" --skip-docker-install --skip-firewall; then
    log "install.sh --deploy-only FAILED — leaving existing containers running"
    git reset --hard "$old_sha" || true
    if [ "$beat_was_running" = 1 ]; then
      resume_beat || true
    fi
    exit 1
  fi

  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: would record tip_sha=$new_sha and run reset-retired-frontier"
  else
    run_reset_frontier_once "$new_sha"
    printf '%s\n' "$new_sha" >"$TIP_FILE"
    printf '%s\n' "$new_sha" >"$ROLLOUT_FILE"
  fi
  if [ "$beat_was_running" = 1 ]; then
    resume_beat || true
  else
    rm -f "$DIR/$BEAT_PAUSE_MARKER"
  fi
  log "deploy complete; tip_sha=$new_sha beat_restarted=$beat_was_running"
}

main "$@"
