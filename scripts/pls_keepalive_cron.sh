#!/usr/bin/env bash
# Host cron backup for PLS slot keepalive — same rules as Celery Beat pls_keepalive_hourly:
# never touch slots while harvest holds the lock; only probe/re-login unhealthy slots.
set -euo pipefail

DIR="${SSA_SCRAPER_DIR:-/opt/ssa-scraper}"
cd "$DIR"
mkdir -p state

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# shellcheck source=scripts/pls_host_lib.sh
source "$DIR/scripts/pls_host_lib.sh"

if pls_host_harvest_busy; then
  printf '[%s] pls_keepalive_cron: skipped — PLS harvest lock or running job\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit 0
fi

exec docker compose exec -T api python scripts/pls_server_login.py
