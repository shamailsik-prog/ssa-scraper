#!/usr/bin/env bash
# =============================================================================
# Point a service checkout's `origin` at GitHub when it was installed from a local copy.
#
# The auto-deploy cron (scripts/auto_deploy.sh) fetches `origin` every 15 minutes. deploy-cloud uploads a git
# bundle and runs install.sh --repo /root/ssa-src, and install.sh sets `origin` to the repo it installed from,
# so the cron fetched that frozen upload for ever: the free-sites server stayed on the 13 October commit and
# every run died on the fetch or the fast-forward (16 October 2026). A checkout installed from a remote URL
# keeps it; one installed from a path or file:// URL follows GitHub instead.
#
# Usage: follow_github.sh DIR REPO      (REPO is what install.sh installed from)
# Environment: SSA_ORIGIN_URL (default https://github.com/shamailsik-prog/ssa-scraper.git)
# =============================================================================
set -euo pipefail

DIR="${1:?usage: follow_github.sh DIR REPO}"
REPO="${2:?usage: follow_github.sh DIR REPO}"
ORIGIN_URL="${SSA_ORIGIN_URL:-https://github.com/shamailsik-prog/ssa-scraper.git}"

case "$REPO" in
  http://*|https://*|ssh://*|git@*) exit 0 ;;
esac
git -C "$DIR" remote set-url origin "$ORIGIN_URL"
echo "origin of $DIR now follows $ORIGIN_URL (installed from the local copy $REPO), so the auto-deploy sees new commits"
