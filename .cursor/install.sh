#!/usr/bin/env bash
# Idempotent repository bootstrap for the SIKANDER AI corpus service on a Cloud Agent VM.
# Prepares durable, source-derived state only: system packages (guarded), the Python
# virtualenv, the Playwright browser, and a .env with fresh local-dev secrets. Per-boot
# service startup lives in start.sh / terminals, never here.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

PW_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}"

# ---------------------------------------------------------------- system packages
# Snapshot-based environments already carry these; the guard keeps install fast on a
# warm VM and self-healing on a cold one. Native Postgres 16 + pgvector + Redis replace
# the docker-compose stack, which cannot run in the Cloud Agent VM.
if ! command -v pg_ctlcluster >/dev/null 2>&1 \
   || ! dpkg -s postgresql-16-pgvector >/dev/null 2>&1 \
   || ! command -v redis-server >/dev/null 2>&1 \
   || ! command -v tesseract >/dev/null 2>&1; then
  echo "install: ensuring system packages"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    postgresql-16 postgresql-16-pgvector postgresql-contrib \
    redis-server tesseract-ocr tesseract-ocr-eng \
    fonts-dejavu-core libffi-dev shared-mime-info curl ca-certificates \
    build-essential python3-venv python3-dev
fi

# ------------------------------------------------------------------- python venv
if [ ! -x .venv/bin/python ]; then
  echo "install: creating virtualenv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip -q
echo "install: installing Python requirements"
pip install -q -r requirements.txt

# ---------------------------------------------------------------- playwright browser
export PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS_PATH"
sudo mkdir -p "$PW_BROWSERS_PATH"
sudo chown "$(id -u):$(id -g)" "$PW_BROWSERS_PATH"
echo "install: installing Playwright Chromium"
playwright install --with-deps chromium

# --------------------------------------------------------------------------- .env
# Generated once with fresh local-dev secrets; never committed (see .gitignore).
# Docker service hostnames (postgres/redis) are rewritten to localhost for native run.
if [ ! -f .env ]; then
  echo "install: generating .env with fresh local-dev secrets"
  python - "$REPO_ROOT" "$PW_BROWSERS_PATH" <<'PY'
import base64, os, re, secrets, sys
repo, pw_path = sys.argv[1], sys.argv[2]
s = open(os.path.join(repo, ".env.example")).read()
s = re.sub(r"^DATABASE_URL=.*$", "DATABASE_URL=postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper", s, flags=re.M)
s = re.sub(r"^REDIS_URL=.*$", "REDIS_URL=redis://localhost:6379/0", s, flags=re.M)
s = re.sub(r"^PDF_STORAGE_PATH=.*$", f"PDF_STORAGE_PATH={repo}/live", s, flags=re.M)
s = re.sub(r"^RAW_STORAGE_PATH=.*$", f"RAW_STORAGE_PATH={repo}/raw", s, flags=re.M)
s = re.sub(r"^STATE_STORAGE_PATH=.*$", f"STATE_STORAGE_PATH={repo}/state", s, flags=re.M)
# This is not a chambers deployment: login-session scraping must stay disabled (config validator enforces it).
s = re.sub(r"^ENVIRONMENT=.*$", "ENVIRONMENT=cloud", s, flags=re.M)
s = re.sub(r"^ALLOW_LOGIN_SCRAPING=.*$", "ALLOW_LOGIN_SCRAPING=false", s, flags=re.M)
s = re.sub(r"^ENCRYPTION_KEY=.*$", "ENCRYPTION_KEY=" + base64.urlsafe_b64encode(os.urandom(32)).decode(), s, flags=re.M)
s = re.sub(r"^ADMIN_API_KEY=.*$", "ADMIN_API_KEY=" + secrets.token_hex(32), s, flags=re.M)
s = re.sub(r"^SECRET_KEY=.*$", "SECRET_KEY=" + secrets.token_hex(32), s, flags=re.M)
s = re.sub(r"^SIKANDER_READER_PASSWORD=.*$", "SIKANDER_READER_PASSWORD=" + secrets.token_urlsafe(24), s, flags=re.M)
s = re.sub(r"^CORPUS_WRITER_PASSWORD=.*$", "CORPUS_WRITER_PASSWORD=" + secrets.token_urlsafe(24), s, flags=re.M)
if "PLAYWRIGHT_BROWSERS_PATH" not in s:
    s += f"\nPLAYWRIGHT_BROWSERS_PATH={pw_path}\n"
open(os.path.join(repo, ".env"), "w").write(s)
print("wrote .env")
PY
fi

mkdir -p live raw state
echo "install: done"
