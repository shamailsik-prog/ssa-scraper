#!/usr/bin/env bash
# One-shot deployment on a Docker host. Generates ENCRYPTION_KEY / ADMIN_API_KEY on first run; never asks
# for site credentials — the operator types those into the streamed human-login browser on the dashboard.
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v docker >/dev/null 2>&1; then curl -fsSL https://get.docker.com | sh; fi
if [ ! -f .env ]; then
  cp .env.example .env
  python3 - <<'PY'
import base64, os, re, secrets
# A Fernet key is 32 random bytes, url-safe base64 encoded; generated with the standard library so the
# host needs nothing beyond python3 (the cryptography package lives inside the image, not on the host).
fernet_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
s = open(".env").read()
s = re.sub(r"^ENCRYPTION_KEY=.*$", "ENCRYPTION_KEY=" + fernet_key, s, flags=re.M)
s = re.sub(r"^ADMIN_API_KEY=.*$", "ADMIN_API_KEY=" + secrets.token_hex(32), s, flags=re.M)
s = re.sub(r"^SECRET_KEY=.*$", "SECRET_KEY=" + secrets.token_hex(32), s, flags=re.M)
s = re.sub(r"^SIKANDER_READER_PASSWORD=.*$", "SIKANDER_READER_PASSWORD=" + secrets.token_urlsafe(24), s, flags=re.M)
s = re.sub(r"^CORPUS_WRITER_PASSWORD=.*$", "CORPUS_WRITER_PASSWORD=" + secrets.token_urlsafe(24), s, flags=re.M)
open(".env", "w").write(s)
PY
  echo "Generated .env with fresh ENCRYPTION_KEY, ADMIN_API_KEY, SECRET_KEY and database role passwords."
  echo "Set ENVIRONMENT=chambers and ALLOW_LOGIN_SCRAPING=true ONLY on the chambers machine."
fi
mkdir -p live raw state
docker compose up -d --build
docker compose ps
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8000/dashboard  (X-API-Key = ADMIN_API_KEY from .env)"
