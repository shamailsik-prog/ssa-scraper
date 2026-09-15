#!/usr/bin/env bash
# =============================================================================
# Print recent service logs from the corpus server, for diagnosing what the operator saw.
#
# Finds the droplet created by do_deploy.sh (name ssa-corpus, tag ssa-corpus), connects with the
# deploy key derived from DO_TOKEN and prints `docker compose ps` plus the last LINES lines of the
# chosen services. The admin key and any 64-hex token are scrubbed from the output.
#
# Environment: DO_TOKEN (required), SERVICES (default "api worker-scraper"), LINES (default 300)
# =============================================================================
set -euo pipefail

: "${DO_TOKEN:?DO_TOKEN is required}"
DROPLET_NAME="${DROPLET_NAME:-ssa-corpus}"
SERVICES="${SERVICES:-api worker-scraper}"
LINES="${LINES:-300}"
HERE="$(cd "$(dirname "$0")" && pwd)"
API="https://api.digitalocean.com/v2"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
api() { curl -fsS -H "Authorization: Bearer $DO_TOKEN" "$API$1"; }
for t in curl jq ssh python3; do command -v "$t" >/dev/null || die "$t is required"; done
case "$SERVICES$LINES" in *[!A-Za-z0-9_\ -]*) die "SERVICES and LINES may contain only letters, digits, spaces, - and _";; esac

KEYDIR="$(mktemp -d)"
trap 'rm -rf "$KEYDIR"' EXIT
python3 "$HERE/derive_ssh_key.py" "$KEYDIR" >/dev/null

DROPLET="$(api "/droplets?tag_name=ssa-corpus&per_page=200" | jq -c --arg n "$DROPLET_NAME" '[.droplets[] | select(.name==$n)][0]')"
[ -n "$DROPLET" ] && [ "$DROPLET" != "null" ] || die "no droplet named $DROPLET_NAME; run the deploy-cloud workflow first"
IP="$(echo "$DROPLET" | jq -r '.networks.v4[] | select(.type=="public") | .ip_address' | head -1)"
[ -n "$IP" ] && [ "$IP" != "null" ] || die "droplet has no public IPv4 address"

SSH_OPTS=(-i "$KEYDIR/id_ed25519" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR)
ssh "${SSH_OPTS[@]}" "root@$IP" "cd /opt/ssa-scraper && echo '== docker compose ps' && docker compose ps --format 'table {{.Service}}\t{{.Status}}' && echo && echo '== logs (last $LINES lines of: $SERVICES)' && docker compose logs --no-color --tail=$LINES $SERVICES" 2>&1 \
  | sed -E 's/[0-9a-f]{64}/<redacted-64-hex>/g'
