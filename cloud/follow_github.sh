#!/usr/bin/env bash
# =============================================================================
# Point a service checkout's `origin` at GitHub when it was installed from a local copy.
#
# The auto-deploy cron (scripts/auto_deploy.sh) fetches `origin` every 15 minutes. deploy-cloud uploads a git
# bundle and runs install.sh --repo /root/ssa-src, and install.sh sets `origin` to the repo it installed from,
# so the cron fetched that frozen upload for ever: the free-sites server stayed on the 13 October commit and
# every run died on the fetch or the fast-forward (16 October 2026). Only a checkout installed from a local copy
# (a file:// URL or a directory on this machine) is repointed; any remote form (https, ssh, git://, user@host:repo)
# keeps its URL.
#
# Usage: follow_github.sh DIR REPO      (REPO is what install.sh installed from)
# Environment: SSA_ORIGIN_URL (default https://github.com/shamailsik-prog/ssa-scraper.git)
# =============================================================================
set -euo pipefail

DIR="${1:?usage: follow_github.sh DIR REPO}"
REPO="${2:?usage: follow_github.sh DIR REPO}"
ORIGIN_URL="${SSA_ORIGIN_URL:-https://github.com/shamailsik-prog/ssa-scraper.git}"

case "$REPO" in
  file://*) ;;
  *) [ -d "$REPO" ] || exit 0 ;;
esac
git -C "$DIR" remote set-url origin "$ORIGIN_URL"
echo "origin of $DIR now follows $ORIGIN_URL (installed from the local copy $REPO), so the auto-deploy sees new commits"
