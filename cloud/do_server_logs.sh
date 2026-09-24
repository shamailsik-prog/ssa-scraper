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
if [ "$SERVICES" = "status" ]; then
  # The corpus numbers as the key-free /status page shows them (the API listens on 127.0.0.1:8000
  # behind Caddy), plus the promotion and archive lines of the last hour.
  ssh "${SSH_OPTS[@]}" "root@$IP" 'cd /opt/ssa-scraper && echo "== /status.json" && curl -sS http://127.0.0.1:8000/status.json | python3 -m json.tool && echo && echo "== promotion and archive (last hour)" && docker compose logs --no-color --since 1h worker-public 2>/dev/null | grep -E "promote_staging_records|mirror_pending|reconcile_storage" | tail -20' 2>&1 \
    | sed -E 's/[0-9a-f]{64}/<redacted-64-hex>/g'
  exit 0
fi
if [ "$SERVICES" = "inspect" ]; then
  # Read-only inventory of what else runs on the server: anything outside the compose stack that
  # touches the database or the site (a cron job, a timer, a second checkout, a hand edit set aside
  # by a deploy) shows up here. No secrets are printed; 64-hex tokens are scrubbed below.
  REMOTE='set +e
echo "== uptime"; uptime
echo; echo "== containers (all, including those outside the compose project)"; docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Command}}"
echo; echo "== root crontab"; crontab -l 2>/dev/null || echo "(none)"
echo; echo "== /etc/cron.d and hourly"; ls -la /etc/cron.d /etc/cron.hourly 2>/dev/null
echo; echo "== systemd timers"; systemctl list-timers --all --no-pager 2>/dev/null | head -30
echo; echo "== running services other than docker/ssh/system"; systemctl list-units --type=service --state=running --no-pager --plain 2>/dev/null | grep -Ev "docker|ssh|systemd|dbus|cron|getty|rsyslog|snap|networkd|resolved|journald|udev|polkit|unattended|multipath|qemu|do-agent|chrony|apparmor|containerd|user@|ModemManager|thermald|irqbalance|accounts|udisks|packagekit" | head -20
echo; echo "== host processes that look like scrapers or browsers (outside containers)"; ps -eo pid,ppid,etimes,user,cmd --no-headers | grep -Ei "python|node|playwright|chrom|celery|uvicorn|scrap" | grep -Ev "grep|containerd-shim|dockerd" | cut -c1-160 | head -30
echo; echo "== /opt and /root"; ls -la /opt /root 2>/dev/null | head -40
echo; echo "== other checkouts of this project on disk"; find / -maxdepth 4 -type d -name ssa-scraper -not -path "/proc/*" 2>/dev/null; find / -maxdepth 5 -name "docker-compose.yml" -path "*ssa*" -not -path "/proc/*" 2>/dev/null
echo; echo "== git state of /opt/ssa-scraper"; git -C /opt/ssa-scraper log --oneline -1; git -C /opt/ssa-scraper status --porcelain --untracked-files=no; git -C /opt/ssa-scraper stash list; git -C /opt/ssa-scraper for-each-ref refs/server-edits
echo; echo "== hand edits set aside by deploys (diff, first 120 lines)"; git -C /opt/ssa-scraper stash show -p "stash@{0}" 2>/dev/null | head -120
echo; echo "== .env keys that differ from .env.example (names only)"; diff <(grep -oE "^[A-Z][A-Z0-9_]*=" /opt/ssa-scraper/.env | sort) <(grep -oE "^[A-Z][A-Z0-9_]*=" /opt/ssa-scraper/.env.example | sort) | head -20; echo "LOGIN_SESSION_CONCURRENCY=$(grep -E "^LOGIN_SESSION_CONCURRENCY=" /opt/ssa-scraper/.env | cut -d= -f2)"
echo; echo "== SSH logins in the last 48 hours"; journalctl -u ssh --since "48 hours ago" --no-pager 2>/dev/null | grep -E "Accepted|session opened" | tail -30; last -n 20 -F 2>/dev/null | head -25
echo; echo "== recent shell history of root (commands only, last 60)"; tail -60 /root/.bash_history 2>/dev/null
echo; echo "== docker exec / compose invocations seen by the docker daemon (last 200 journal lines)"; journalctl -u docker --since "48 hours ago" --no-pager 2>/dev/null | tail -20'
  ssh "${SSH_OPTS[@]}" "root@$IP" "$REMOTE" 2>&1 | sed -E 's/[0-9a-f]{64}/<redacted-64-hex>/g'
  exit 0
fi
ssh "${SSH_OPTS[@]}" "root@$IP" "cd /opt/ssa-scraper && echo '== docker compose ps' && docker compose ps --format 'table {{.Service}}\t{{.Status}}' && echo && echo '== logs (last $LINES lines of: $SERVICES)' && docker compose logs --no-color --tail=$LINES $SERVICES" 2>&1 \
  | sed -E 's/[0-9a-f]{64}/<redacted-64-hex>/g'
