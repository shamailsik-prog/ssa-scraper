#!/bin/bash
# SessionStart hook for Claude Code on the web: installs everything the test suite needs
# (mirrors .github/workflows/ci.yml) so a fresh cloud container is ready without manual setup.
# Idempotent: every step skips work that is already done.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# System packages: Tesseract (OCR tests), fonts (reportlab), pgvector, PostgreSQL 16, Redis.
need_apt=()
command -v tesseract >/dev/null 2>&1 || need_apt+=(tesseract-ocr tesseract-ocr-eng)
dpkg -s fonts-dejavu-core >/dev/null 2>&1 || need_apt+=(fonts-dejavu-core)
command -v pg_ctlcluster >/dev/null 2>&1 || need_apt+=(postgresql-16)
dpkg -s postgresql-16-pgvector >/dev/null 2>&1 || need_apt+=(postgresql-16-pgvector)
command -v redis-server >/dev/null 2>&1 || need_apt+=(redis-server)
if [ "${#need_apt[@]}" -gt 0 ]; then
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq --no-install-recommends "${need_apt[@]}" >/dev/null
fi

# Python dependencies (pinned set used by CI); skipped when requirements.txt is unchanged since the last install.
stamp="${HOME}/.cache/ssa-scraper-requirements.sha256"
if ! sha256sum -c --status "$stamp" 2>/dev/null; then
  python3 -m pip install --quiet --disable-pip-version-check --root-user-action=ignore --ignore-installed -r requirements.txt
  mkdir -p "$(dirname "$stamp")" && sha256sum "$PWD/requirements.txt" > "$stamp"
fi

# PostgreSQL: start the cluster and create the role/database the tests expect.
if ! pg_lsclusters -h 2>/dev/null | grep -q '^16 \+main'; then
  $SUDO pg_createcluster 16 main >/dev/null
fi
pg_isready -q -h localhost -p 5432 || $SUDO pg_ctlcluster 16 main start 2>/dev/null || true
for _ in $(seq 1 30); do pg_isready -q -h localhost -p 5432 && break; sleep 1; done
psql_su() { $SUDO -u postgres psql -v ON_ERROR_STOP=1 -qtA "$@" 2>/dev/null || su postgres -c "psql -v ON_ERROR_STOP=1 -qtA $(printf '%q ' "$@")"; }
if [ "$(psql_su -c "SELECT 1 FROM pg_roles WHERE rolname='legal'")" != "1" ]; then
  psql_su -c "CREATE ROLE legal LOGIN SUPERUSER PASSWORD 'legal'"
fi
if [ "$(psql_su -c "SELECT 1 FROM pg_database WHERE datname='legal_scraper'")" != "1" ]; then
  psql_su -c "CREATE DATABASE legal_scraper OWNER legal"
fi
psql_su -d legal_scraper -c "SET client_min_messages = warning; CREATE EXTENSION IF NOT EXISTS vector"

# Redis.
if ! redis-cli ping >/dev/null 2>&1; then
  redis-server --daemonize yes --save "" --appendonly no >/dev/null
fi

# Session environment (same values as CI).
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo 'export DATABASE_URL="postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper"'
    echo 'export REDIS_URL="redis://localhost:6379/0"'
    echo 'export SIKANDER_READER_PASSWORD="readerpw"'
    echo 'export CORPUS_WRITER_PASSWORD="writerpw"'
  } >> "$CLAUDE_ENV_FILE"
fi

echo "session-start: dependencies, PostgreSQL and Redis ready"
