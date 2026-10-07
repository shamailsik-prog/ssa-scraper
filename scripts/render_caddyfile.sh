#!/usr/bin/env bash
# Render state/Caddyfile from this repository (the only source of truth) and reload Caddy when it changed.
#
#   * A stable hostname with a real Let's Encrypt certificate and no purchase: <ip-with-dashes>.sslip.io
#     (sslip.io resolves it to the IP), or DASHBOARD_DOMAIN from .env when set.
#   * The bare-IP route keeps working with Caddy's internal certificate.
#   * "/" on the hostname opens the live dashboard (/live).
# Idempotent; safe from cron. SSA_DASHBOARD_SSLIP=0 turns the sslip.io hostname off.
set -euo pipefail

DIR="${SSA_SCRAPER_DIR:-/opt/ssa-scraper}"
cd "$DIR"
mkdir -p state

env_value() {
  [ -f .env ] || return 0
  python3 - "$1" <<'PY'
import re, sys
s = open(".env").read()
m = re.search(r"^%s=(.*)$" % re.escape(sys.argv[1]), s, flags=re.M)
print((m.group(1).strip().strip('"').strip("'")) if m else "")
PY
}

PUBLIC_IP="${PUBLIC_IP:-}"
if [ -z "$PUBLIC_IP" ] && [ -s state/public_ip ]; then
  PUBLIC_IP="$(tr -d ' \r\n' < state/public_ip)"
fi
if [ -z "$PUBLIC_IP" ]; then
  PUBLIC_IP="$(curl -fsS -4 --max-time 10 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
fi
case "$PUBLIC_IP" in *[!0-9.]*|"") echo "render_caddyfile: could not determine the public IPv4 address" >&2; exit 1;; esac
printf '%s\n' "$PUBLIC_IP" > state/public_ip

DOMAIN="${DASHBOARD_DOMAIN:-$(env_value DASHBOARD_DOMAIN)}"
SSLIP="${SSA_DASHBOARD_SSLIP:-$(env_value SSA_DASHBOARD_SSLIP)}"
if [ -z "$DOMAIN" ] && [ "${SSLIP:-1}" != "0" ]; then
  DOMAIN="${PUBLIC_IP//./-}.sslip.io"
fi

render() {
  printf '{\n    # Browsers connecting by IP send no server name; serve the IP certificate by default.\n    default_sni %s\n}\n' "$PUBLIC_IP"
  if [ -n "$DOMAIN" ]; then
    cat <<EOF2
# Stable dashboard hostname: Caddy obtains and renews a Let's Encrypt certificate automatically.
$DOMAIN {
    encode gzip
    @root path /
    redir @root /live 302
    reverse_proxy api:8000
}
EOF2
  fi
  cat <<EOF2
https://$PUBLIC_IP, https://localhost {
    tls internal
    encode gzip
    reverse_proxy api:8000
}
http://$PUBLIC_IP {
    redir https://{host}{uri} permanent
}
EOF2
}

NEW="$(render)"
OLD="$(cat state/Caddyfile 2>/dev/null || true)"
if [ "$NEW" = "$OLD" ]; then
  echo "render_caddyfile: unchanged (${DOMAIN:-no hostname})"
  exit 0
fi
# Write in place: the file is bind-mounted into the caddy container, so its inode must not change.
printf '%s\n' "$OLD" > state/Caddyfile.prev
printf '%s\n' "$NEW" > state/Caddyfile
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx caddy; then
  if ! docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
    echo "render_caddyfile: reload failed; restoring the previous Caddyfile" >&2
    cat state/Caddyfile.prev > state/Caddyfile
    docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile || true
    exit 1
  fi
fi
echo "render_caddyfile: written and reloaded (${DOMAIN:-no hostname}, IP $PUBLIC_IP)"
