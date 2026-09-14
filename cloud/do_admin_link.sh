#!/usr/bin/env bash
# =============================================================================
# Print a one-click dashboard login link for the corpus server.
#
# Finds the droplet created by do_deploy.sh (name ssa-corpus, tag ssa-corpus), reads ADMIN_API_KEY
# from /opt/ssa-scraper/.env over SSH (deploy key derived from DO_TOKEN, as in do_deploy.sh) and
# prints https://<host>/dashboard#key=<admin key>. The key travels in the URL fragment, which the
# browser never sends to the server; the dashboard stores it in that browser and clears the address
# bar. Run by .github/workflows/admin-link.yml: the link is written to the job log and summary, so
# treat that run as private and delete its logs once the link has been used.
# =============================================================================
set -euo pipefail

: "${DO_TOKEN:?DO_TOKEN is required}"
DROPLET_NAME="${DROPLET_NAME:-ssa-corpus}"
HERE="$(cd "$(dirname "$0")" && pwd)"
API="https://api.digitalocean.com/v2"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
api() { curl -fsS -H "Authorization: Bearer $DO_TOKEN" "$API$1"; }
for t in curl jq ssh python3; do command -v "$t" >/dev/null || die "$t is required"; done

KEYDIR="$(mktemp -d)"
trap 'rm -rf "$KEYDIR"' EXIT
python3 "$HERE/derive_ssh_key.py" "$KEYDIR" >/dev/null

DROPLET="$(api "/droplets?tag_name=ssa-corpus&per_page=200" | jq -c --arg n "$DROPLET_NAME" '[.droplets[] | select(.name==$n)][0]')"
[ -n "$DROPLET" ] && [ "$DROPLET" != "null" ] || die "no droplet named $DROPLET_NAME; run the deploy-cloud workflow first"
IP="$(echo "$DROPLET" | jq -r '.networks.v4[] | select(.type=="public") | .ip_address' | head -1)"
[ -n "$IP" ] && [ "$IP" != "null" ] || die "droplet has no public IPv4 address"

SSH_OPTS=(-i "$KEYDIR/id_ed25519" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR)
ENV_LINE="$(ssh "${SSH_OPTS[@]}" "root@$IP" "grep -E '^(ADMIN_API_KEY|DASHBOARD_DOMAIN)=' /opt/ssa-scraper/.env")" || die "could not read the server's .env over SSH"
ADMIN_KEY="$(echo "$ENV_LINE" | sed -n 's/^ADMIN_API_KEY=//p')"
DOMAIN="$(echo "$ENV_LINE" | sed -n 's/^DASHBOARD_DOMAIN=//p')"
[ -n "$ADMIN_KEY" ] || die "ADMIN_API_KEY is not set on the server"
HOST="${DOMAIN:-$IP}"
LINK="https://$HOST/dashboard#key=$ADMIN_KEY"

echo "LOGIN_LINK=$LINK"
if [ -n "${SUMMARY_FILE:-}" ]; then
  {
    echo "## One-click dashboard login"
    echo
    echo "$LINK"
    echo
    echo "Open it once in the browser you will use; the key is then stored in that browser and the plain address https://$HOST/dashboard works from then on."
    [ -z "$DOMAIN" ] && echo "The certificate is self-signed: press Advanced, then Proceed, the first time."
    echo
    echo "Delete this run's logs after use (run page → ⋯ → Delete all logs)."
  } >> "$SUMMARY_FILE"
fi
