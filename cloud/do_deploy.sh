#!/usr/bin/env bash
# =============================================================================
# Create (or reuse) a DigitalOcean droplet and install the corpus service on it.
# Run by .github/workflows/deploy-cloud.yml; can also be run from any machine with curl, jq, ssh,
# git and python3 + cryptography, with DO_TOKEN in the environment.
#
# Environment (all optional except DO_TOKEN):
#   DO_TOKEN         DigitalOcean API token (read/write)
#   DO_REGION        e.g. fra1 (Frankfurt), lon1, ams3, sgp1, blr1 (Bangalore)   default fra1
#   DO_SIZE          e.g. s-2vcpu-4gb, s-4vcpu-8gb                                default s-2vcpu-4gb
#   DROPLET_NAME     default ssa-corpus
#   DOMAIN           DNS name pointing at the server (optional)
#   REPORTERS, EARLIEST_YEAR, DEPLOY_REGION_LABEL   firm values passed to the installer
#   SGAI_API_KEY     optional; written to .env on the server over stdin, never printed
#   SRC_DIR          the checked-out repository to upload                          default: this repo
#   SUMMARY_FILE     if set, a markdown summary is appended there (GitHub job summary)
# =============================================================================
set -euo pipefail

: "${DO_TOKEN:?DO_TOKEN is required}"
DO_REGION="${DO_REGION:-fra1}"
DO_SIZE="${DO_SIZE:-s-2vcpu-4gb}"
DROPLET_NAME="${DROPLET_NAME:-ssa-corpus}"
DOMAIN="${DOMAIN:-}"
REPORTERS="${REPORTERS:-}"
EARLIEST_YEAR="${EARLIEST_YEAR:-}"
DEPLOY_REGION_LABEL="${DEPLOY_REGION_LABEL:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="${SRC_DIR:-$(cd "$HERE/.." && pwd)}"
API="https://api.digitalocean.com/v2"
KEY_NAME="ssa-corpus-deploy"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
api() { # api METHOD PATH [JSON]
  local m="$1" p="$2" d="${3:-}"
  if [ -n "$d" ]; then
    curl -fsS -X "$m" -H "Authorization: Bearer $DO_TOKEN" -H "Content-Type: application/json" -d "$d" "$API$p"
  else
    curl -fsS -X "$m" -H "Authorization: Bearer $DO_TOKEN" "$API$p"
  fi
}
for t in curl jq ssh scp git python3; do command -v "$t" >/dev/null || die "$t is required"; done

# ---------------------------------------------------------------- deploy key (derived from DO_TOKEN, never stored)
KEYDIR="$(mktemp -d)"
ARCHIVE="$(mktemp)"
trap 'rm -rf "$KEYDIR" "$ARCHIVE"' EXIT
KEYINFO="$(python3 "$HERE/derive_ssh_key.py" "$KEYDIR")"
PUBKEY="$(echo "$KEYINFO" | sed -n 1p)"
FINGERPRINT="$(echo "$KEYINFO" | sed -n 2p)"
log "Registering the deploy key with DigitalOcean (fingerprint $FINGERPRINT)"
KEY_ID="$(api GET "/account/keys?per_page=200" | jq -r --arg fp "$FINGERPRINT" '.ssh_keys[] | select(.fingerprint==$fp) | .id' | head -1)"
if [ -z "$KEY_ID" ]; then
  KEY_ID="$(api POST "/account/keys" "$(jq -nc --arg n "$KEY_NAME" --arg k "$PUBKEY" '{name:$n, public_key:$k}')" | jq -r '.ssh_key.id')"
fi
[ -n "$KEY_ID" ] && [ "$KEY_ID" != "null" ] || die "could not register the deploy key"

# ---------------------------------------------------------------- droplet (create or reuse)
log "Looking for an existing droplet named $DROPLET_NAME"
DROPLET_ID="$(api GET "/droplets?tag_name=ssa-corpus&per_page=200" | jq -r --arg n "$DROPLET_NAME" '.droplets[] | select(.name==$n) | .id' | head -1)"
if [ -z "$DROPLET_ID" ]; then
  log "Creating droplet $DROPLET_NAME ($DO_SIZE in $DO_REGION, Ubuntu 24.04)"
  BODY="$(jq -nc --arg n "$DROPLET_NAME" --arg r "$DO_REGION" --arg s "$DO_SIZE" --argjson k "$KEY_ID" \
    '{name:$n, region:$r, size:$s, image:"ubuntu-24-04-x64", ssh_keys:[$k], monitoring:true, tags:["ssa-corpus"]}')"
  DROPLET_ID="$(api POST "/droplets" "$BODY" | jq -r '.droplet.id')"
  [ -n "$DROPLET_ID" ] && [ "$DROPLET_ID" != "null" ] || die "droplet creation failed"
  NEW_DROPLET=1
else
  log "Reusing droplet $DROPLET_ID"
  NEW_DROPLET=0
fi

log "Waiting for the droplet to be active"
STATUS=""; IP=""
for i in $(seq 1 60); do
  D="$(api GET "/droplets/$DROPLET_ID")"
  STATUS="$(echo "$D" | jq -r '.droplet.status')"
  IP="$(echo "$D" | jq -r '.droplet.networks.v4[] | select(.type=="public") | .ip_address' | head -1)"
  if [ "$STATUS" = "active" ] && [ -n "$IP" ] && [ "$IP" != "null" ]; then break; fi
  sleep 5
done
[ "$STATUS" = "active" ] || die "droplet did not become active"
log "Droplet $DROPLET_ID is active at $IP"

# ---------------------------------------------------------------- ssh
SSH_OPTS=(-i "$KEYDIR/id_ed25519" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 -o ConnectTimeout=15 -o LogLevel=ERROR)
log "Waiting for SSH"
for i in $(seq 1 40); do
  if ssh "${SSH_OPTS[@]}" "root@$IP" true 2>/dev/null; then break; fi
  sleep 10
done
ssh "${SSH_OPTS[@]}" "root@$IP" true || die "SSH to root@$IP failed. If this droplet predates the current DO_TOKEN, the derived deploy key differs: add the key '$KEY_NAME' to the server or recreate the droplet."

# A new droplet runs cloud-init and unattended-upgrades for a few minutes after boot; they hold the apt
# lock. Wait for them here so the git install below and the installer do not fail on the lock.
log "Waiting for the server's first-boot setup to finish"
ssh "${SSH_OPTS[@]}" "root@$IP" 'command -v cloud-init >/dev/null 2>&1 && cloud-init status --wait >/dev/null 2>&1; for i in $(seq 1 120); do if ! fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1 && ! pgrep -x apt-get >/dev/null 2>&1 && ! pgrep -x dpkg >/dev/null 2>&1 && ! pgrep -f '"'"'[u]nattended-upgrade([[:space:]]|$)'"'"' >/dev/null 2>&1; then exit 0; fi; sleep 5; done; exit 0'

# ---------------------------------------------------------------- upload the code and install
log "Uploading the repository"
BRANCH="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
[ "$BRANCH" = "HEAD" ] && BRANCH="deploy"
git -C "$SRC_DIR" bundle create "$ARCHIVE" HEAD >/dev/null 2>&1 || die "git bundle failed (is $SRC_DIR a git checkout?)"
scp "${SSH_OPTS[@]}" -q "$ARCHIVE" "root@$IP:/root/ssa-src.bundle"
ssh "${SSH_OPTS[@]}" "root@$IP" "set -e; command -v git >/dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq git >/dev/null; }; rm -rf /root/ssa-src; git -c advice.detachedHead=false clone -q /root/ssa-src.bundle /root/ssa-src; git -C /root/ssa-src checkout -q -B $(printf '%q' "$BRANCH")"

log "Running the installer on the server (first run builds the image and downloads Chromium: 5–10 minutes)"
ARGS=(--repo /root/ssa-src --branch "$BRANCH")
[ -n "$DOMAIN" ] && ARGS+=(--domain "$DOMAIN")
[ -n "$REPORTERS" ] && ARGS+=(--reporters "$REPORTERS")
[ -n "$EARLIEST_YEAR" ] && ARGS+=(--earliest-year "$EARLIEST_YEAR")
[ -n "$DEPLOY_REGION_LABEL" ] && ARGS+=(--region "$DEPLOY_REGION_LABEL")
ssh "${SSH_OPTS[@]}" "root@$IP" "bash /root/ssa-src/cloud/install.sh $(printf '%q ' "${ARGS[@]}") < /dev/null" 2>&1 | sed -E 's/Admin key:  [0-9a-f]{64}/Admin key:  (see the job summary)/'

# ---------------------------------------------------------------- optional secrets, sent over stdin, never echoed
if [ -n "${SGAI_API_KEY:-}" ]; then
  log "Storing the ScrapeGraph key in the server's .env"
  printf '%s' "$SGAI_API_KEY" | ssh "${SSH_OPTS[@]}" "root@$IP" 'python3 /root/ssa-src/cloud/set_env.py SGAI_API_KEY >/dev/null && cd /opt/ssa-scraper && docker compose up -d >/dev/null 2>&1'
fi

# ---------------------------------------------------------------- result
ADMIN_KEY="$(ssh "${SSH_OPTS[@]}" "root@$IP" "grep '^ADMIN_API_KEY=' /opt/ssa-scraper/.env | cut -d= -f2-")"
if [ -n "$DOMAIN" ]; then URL="https://$DOMAIN/dashboard"; NOTE=""; else URL="https://$IP/dashboard"; NOTE=" (self-signed certificate: accept the browser warning once)"; fi
HEALTH="$(ssh "${SSH_OPTS[@]}" "root@$IP" "curl -sk https://127.0.0.1/health" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status"), "· db", d.get("db_connected"), "· redis", d.get("redis_connected"), "· login scraping", d.get("login_scraping_permitted"))' 2>/dev/null || echo unknown)"

log "Done. Dashboard: $URL$NOTE"
if [ -n "${SUMMARY_FILE:-}" ]; then
  {
    echo "## SIKANDER AI corpus service is running"
    echo
    echo "| | |"
    echo "|---|---|"
    echo "| Dashboard | $URL$NOTE |"
    echo "| Admin key | \`$ADMIN_KEY\` |"
    echo "| Server | droplet $DROPLET_ID, $IP, $DO_SIZE in $DO_REGION$( [ "$NEW_DROPLET" = 1 ] && echo ", created now" || echo ", reused") |"
    echo "| Health | $HEALTH |"
    echo
    echo "Next: open the dashboard, paste the admin key, press Connect. On **Human login**, start a login on slot 1,"
    echo "type your PakistanLawSite username and password into the streamed browser, then press Complete."
    echo "Anything under *Not configured* on the Overview tab is a value in /opt/ssa-scraper/.env on the server."
  } >> "$SUMMARY_FILE"
fi
