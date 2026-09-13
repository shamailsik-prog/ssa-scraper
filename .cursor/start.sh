#!/usr/bin/env bash
# Per-boot reconciliation: bring up Postgres and Redis, then ensure the application
# role, database and extensions exist. Idempotent and safe to re-run. Long-running
# application processes (API, workers, beat, flower) are launched by terminals.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "start: starting PostgreSQL"
sudo pg_ctlcluster 16 main start 2>/dev/null || sudo service postgresql start || true

echo "start: starting Redis"
sudo service redis-server start 2>/dev/null || true

echo "start: waiting for PostgreSQL to accept connections"
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done

echo "start: ensuring application role, database and extensions"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='legal') THEN
    CREATE ROLE legal LOGIN PASSWORD 'legal' SUPERUSER CREATEDB CREATEROLE;
  END IF;
END $$;
SQL
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='legal_scraper'" | grep -q 1; then
  sudo -u postgres createdb -O legal legal_scraper
fi
sudo -u postgres psql -d legal_scraper -q \
  -c 'CREATE EXTENSION IF NOT EXISTS vector;' \
  -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm;' \
  -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'

echo "start: waiting for Redis"
for _ in $(seq 1 30); do
  if redis-cli ping 2>/dev/null | grep -q PONG; then break; fi
  sleep 1
done

echo "start: infrastructure ready"
