#!/usr/bin/env bash
# =============================================================================
# SIKANDER AI corpus service — one-command cloud installer
#
# Fresh Ubuntu 22.04/24.04 or Debian 12 server, run as root:
#
#   curl -fsSL https://raw.githubusercontent.com/shamailsik-prog/ssa-scraper/main/cloud/install.sh \
#     | sudo bash -s -- --domain corpus.example.com --reporters PLD,SCMR,CLC --earliest-year 1990 --region Frankfurt
#
# While the repository is PRIVATE, GitHub needs a token (fine-grained, this repository, Contents: read):
#
#   read -rsp "GitHub token: " GH_TOKEN; echo; export GH_TOKEN
#   curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
#     https://raw.githubusercontent.com/shamailsik-prog/ssa-scraper/main/cloud/install.sh \
#     | sudo -E bash -s -- --domain corpus.example.com --reporters PLD,SCMR,CLC --earliest-year 1990 --region Frankfurt
#
# The token is used only for the clone/update over HTTPS and is never written to disk.
# Every flag is optional. Without --domain the dashboard is served on https://<server-ip>/ with a
# self-signed certificate. The script never asks for site credentials: PakistanLawSite login is done
# by a human inside the streamed browser on the dashboard. The ScrapeGraph key is read from a hidden
# prompt (never a flag, so it stays out of shell history) or added to .env later.
#
# Re-running is safe: it updates the code, keeps the existing .env, and restarts the stack.
# =============================================================================
set -euo pipefail

REPO="https://github.com/shamailsik-prog/ssa-scraper.git"
BRANCH="main"
DIR="/opt/ssa-scraper"
DOMAIN=""
REPORTERS=""
EARLIEST=""
REGION=""
SKIP_DOCKER=0
SKIP_FIREWALL=0
PREPARE_ONLY=0

usage() { sed -n '2,24p' "$0"; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2;;
    --reporters) REPORTERS="$2"; shift 2;;
    --earliest-year) EARLIEST="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    --dir) DIR="$2"; shift 2;;
    --branch) BRANCH="$2"; shift 2;;
    --repo) REPO="$2"; shift 2;;
    --skip-docker-install) SKIP_DOCKER=1; shift;;
    --skip-firewall) SKIP_FIREWALL=1; shift;;
    --prepare-only) PREPARE_ONLY=1; shift;;
    -h|--help) usage 0;;
    *) echo "unknown option: $1"; usage 1;;
  esac
done

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
case "$BRANCH$DIR$DOMAIN$REPORTERS$EARLIEST$REGION" in *[\'\"\;\`]*) die "quotes, semicolons and backticks are not allowed in options";; esac

# ---------------------------------------------------------------- packages
# A freshly created server runs cloud-init and unattended-upgrades during its first minutes; both hold
# the apt lock. Wait for them rather than fail with "Could not get lock".
wait_for_apt() {
  if command -v cloud-init >/dev/null 2>&1; then cloud-init status --wait >/dev/null 2>&1 || true; fi
  local i
  for i in $(seq 1 120); do
    if ! fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1 \
       && ! pgrep -x apt-get >/dev/null 2>&1 && ! pgrep -x dpkg >/dev/null 2>&1 \
       && ! pgrep -f '[u]nattended-upgrade([[:space:]]|$)' >/dev/null 2>&1; then
      return 0
    fi
    [ "$i" = 1 ] && printf 'waiting for the system package manager to finish its first-boot work...\n'
    sleep 5
  done
  return 0
}
log "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  wait_for_apt
  apt-get update -qq
  apt-get install -y -qq curl git ca-certificates python3 ufw >/dev/null
else
  die "this installer supports Debian/Ubuntu (apt-get). For other systems follow docs/CLOUD_DEPLOYMENT.md manually."
fi

# ---------------------------------------------------------------- docker
if [ "$SKIP_DOCKER" = 0 ] && ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker Engine"
  curl -fsSL https://get.docker.com | sh
fi
command -v docker >/dev/null 2>&1 || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing (install docker-compose-plugin)"
if command -v systemctl >/dev/null 2>&1 && [ "$SKIP_DOCKER" = 0 ]; then systemctl enable --now docker >/dev/null 2>&1 || true; fi

# ---------------------------------------------------------------- code
# GH_TOKEN (optional) authenticates the clone/update of a private repository. It is passed as a
# per-command header, so it is never stored in .git/config or anywhere on disk.
GIT=(git)
if [ -n "${GH_TOKEN:-}" ]; then
  GIT=(git -c "http.https://github.com/.extraheader=AUTHORIZATION: bearer $GH_TOKEN")
fi
if [ -d "$DIR/.git" ]; then
  log "Updating code in $DIR ($BRANCH)"
  # The service checkout is not a working copy anyone edits: make it exactly the fetched commit,
  # even when the branch history was rewritten (squash merges, rebased deploy branches).
  # .env, state/, raw/ and live/ are untracked and untouched.
  git -C "$DIR" remote set-url origin "$REPO"
  "${GIT[@]}" -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q -B "$BRANCH" FETCH_HEAD
else
  log "Cloning $REPO ($BRANCH) into $DIR"
  "${GIT[@]}" clone -q -b "$BRANCH" "$REPO" "$DIR" || die "clone failed. If the repository is private, export GH_TOKEN (see the header of this script) and re-run."
fi
unset GH_TOKEN
cd "$DIR"
mkdir -p live raw state

# ---------------------------------------------------------------- .env
if [ ! -f .env ]; then
  log "Writing .env with fresh keys and passwords"
  cp .env.example .env
  DOMAIN="$DOMAIN" REPORTERS="$REPORTERS" EARLIEST="$EARLIEST" REGION="$REGION" python3 - <<'PY'
import base64, os, re, secrets
s = open(".env").read()
def put(key, value):
    global s
    line = f"{key}={value}"
    s, n = re.subn(rf"^{re.escape(key)}=.*$", line.replace("\\", "\\\\"), s, flags=re.M)
    if n == 0:
        s += ("" if s.endswith("\n") else "\n") + line + "\n"
pg = secrets.token_urlsafe(24)
put("ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())   # a valid Fernet key
put("ADMIN_API_KEY", secrets.token_hex(32))
put("SECRET_KEY", secrets.token_hex(32))
put("SIKANDER_READER_PASSWORD", secrets.token_urlsafe(24))
put("CORPUS_WRITER_PASSWORD", secrets.token_urlsafe(24))
put("POSTGRES_PASSWORD", pg)
put("DATABASE_URL", f"postgresql+asyncpg://legal:{pg}@postgres:5432/legal_scraper")
# The firm's own trusted host. 'chambers' is the label the service requires before it will run
# login-session scraping; a cloud server the firm controls is that host.
put("ENVIRONMENT", "chambers")
put("ALLOW_LOGIN_SCRAPING", "true")
put("DATA_RESIDENCY_NOTE", "firm-controlled cloud host; login-session content never leaves it")
put("API_BIND", "127.0.0.1")
put("COMPOSE_FILE", "docker-compose.yml:docker-compose.cloud.yml")
put("DASHBOARD_DOMAIN", os.environ.get("DOMAIN", ""))
if os.environ.get("REPORTERS"): put("PLS_SUBSCRIBED_REPORTERS", os.environ["REPORTERS"])
if os.environ.get("EARLIEST"): put("PLS_EARLIEST_YEAR", os.environ["EARLIEST"])
if os.environ.get("REGION"): put("DEPLOY_REGION", os.environ["REGION"])
open(".env", "w").write(s)
PY
  chmod 600 .env
  if [ -t 0 ]; then
    printf 'ScrapeGraph API key (typed hidden; press Enter to add it to .env later): '
    read -rs SGAI_KEY || SGAI_KEY=""
    echo
    if [ -n "${SGAI_KEY:-}" ]; then
      SGAI_KEY="$SGAI_KEY" python3 - <<'PY'
import os, re
s = open(".env").read()
s = re.sub(r"^SGAI_API_KEY=.*$", "SGAI_API_KEY=" + os.environ["SGAI_KEY"], s, flags=re.M)
open(".env", "w").write(s)
PY
      echo "ScrapeGraph key stored in .env (permissions 600)."
    fi
    unset SGAI_KEY
  fi
else
  log "Keeping existing .env (adding keys new in .env.example; retiring known stale values)"
  python3 - <<'RECONCILE'
import re
example = open(".env.example").read()
s = open(".env").read()
present = {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", s, flags=re.M)}
added = []
for line in example.splitlines():
    m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
    if not m or m.group(1) in present:
        continue
    s += ("" if s.endswith("\n") else "\n") + line + "\n"
    added.append(m.group(1))
# Values an earlier release wrote that are known to throttle the harvest or to cost the human login
# (0.4-1.0s backfill pacing made PakistanLawSite end the session after ~500 pages). Only the exact
# old defaults are replaced; a value the operator changed on purpose is left alone.
stale = {
    "PLS_CITATION_GRID_MAX_DETAIL": ("40", "120"),
    "PLS_ARCHIVED_GRID_MAX_ROWS": ("200", "400"),
    "BACKFILL_LOGIN_DELAY_MIN": ("0.4", "6"),
    "BACKFILL_LOGIN_DELAY_MAX": ("1.0", "9"),
    "BACKFILL_PAGES_PER_HOUR": ("10000", "450"),
    "LOGIN_SESSION_CONCURRENCY": ("1", "2"),
}
migrated = []
for key, (old_value, new_value) in stale.items():
    s, n = re.subn(rf"^{key}={old_value}\s*$", f"{key}={new_value}", s, flags=re.M)
    if n:
        migrated.append(f"{key} {old_value}->{new_value}")
open(".env", "w").write(s)
if added:
    print("  added to .env:", ", ".join(added))
if migrated:
    print("  migrated in .env:", ", ".join(migrated))
RECONCILE
fi

# Reuse a previously configured domain on updates unless --domain overrides it.
if [ -z "$DOMAIN" ] && [ -f .env ]; then
  DOMAIN="$(python3 - <<'PY'
import re
s = open(".env").read()
m = re.search(r"^DASHBOARD_DOMAIN=(.*)$", s, flags=re.M)
print(m.group(1) if m else "")
PY
)"
fi
if [ -n "$DOMAIN" ] && [ -f .env ]; then
  DOMAIN="$DOMAIN" python3 - <<'PY'
import os, re
s = open(".env").read()
line = "DASHBOARD_DOMAIN=" + os.environ["DOMAIN"]
s, n = re.subn(r"^DASHBOARD_DOMAIN=.*$", line, s, flags=re.M)
if n == 0:
    s += ("" if s.endswith("\n") else "\n") + line + "\n"
open(".env", "w").write(s)
PY
fi

# ---------------------------------------------------------------- reverse proxy
log "Writing state/Caddyfile"
if [ -n "$DOMAIN" ]; then
  cat > state/Caddyfile <<EOF
$DOMAIN {
    encode gzip
    reverse_proxy api:8000
}
EOF
  ACCESS_URL="https://$DOMAIN/dashboard"
else
  PUBLIC_IP="$(curl -fsS -4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
  case "$PUBLIC_IP" in *[!0-9.]*|"") die "could not determine the server's public IPv4 address; pass --domain instead";; esac
  cat > state/Caddyfile <<EOF
{
    default_sni $PUBLIC_IP
}
https://$PUBLIC_IP, https://localhost {
    tls internal
    encode gzip
    reverse_proxy api:8000
}
http:// {
    redir https://{host}{uri} permanent
}
EOF
  ACCESS_URL="https://${PUBLIC_IP}/dashboard   (self-signed certificate: accept the browser warning once)"
fi

# ---------------------------------------------------------------- firewall
if [ "$SKIP_FIREWALL" = 0 ] && command -v ufw >/dev/null 2>&1; then
  log "Firewall: allow SSH, 80, 443 only"
  ufw allow OpenSSH >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443 >/dev/null
  ufw --force enable >/dev/null
fi

if [ "$PREPARE_ONLY" = 1 ]; then
  log "Prepared only (--prepare-only). Start with: cd $DIR && docker compose up -d --build"
  exit 0
fi

# ---------------------------------------------------------------- start
log "Building the image and starting the stack (first build downloads Chromium; allow 5–10 minutes)"
docker compose up -d --build
if docker compose config --services | grep -qx 'caddy'; then
  log "Recreating caddy so state/Caddyfile changes are applied"
  docker compose up -d --no-deps --force-recreate caddy
fi

log "Waiting for the API to report healthy"
for i in $(seq 1 60); do
  if docker compose ps --format '{{.Service}} {{.Health}}' | grep -q '^api healthy'; then break; fi
  sleep 5
done
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
docker compose ps --format '{{.Service}} {{.Health}}' | grep -q '^api healthy' || die "API did not become healthy; run: docker compose logs api"

ADMIN_KEY="$(grep '^ADMIN_API_KEY=' .env | cut -d= -f2-)"
cat <<EOF

============================================================================
 SIKANDER AI corpus service is running.

 Dashboard:  $ACCESS_URL
 Admin key:  $ADMIN_KEY
             (also in $DIR/.env — keep it private; it is the only login to the console)

 Next, in the dashboard:
   1. Overview  — anything listed under "Not configured" is set in $DIR/.env,
                  then: cd $DIR && docker compose up -d
   2. Human login — Start login, type your PakistanLawSite username and password
                  into the streamed browser, then press Complete.
   3. Archive storage — add where copies are kept.
   4. Sources — public courts start on their own schedule; PakistanLawSite starts
                  once a slot is ACTIVE.

 Update later:   re-run the same install command (add the GH_TOKEN lines again while the repository is private)
 Logs:           cd $DIR && docker compose logs -f --tail=100
============================================================================
EOF
