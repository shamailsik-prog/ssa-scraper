#!/usr/bin/env bash
# =============================================================================
# Register (or update) a Google Drive archive target on the corpus server and prove it works.
#
# Finds the droplet created by do_deploy.sh, connects with the deploy key derived from DO_TOKEN and
# posts the target to the server's own API on 127.0.0.1:8000 with the ADMIN_API_KEY read from
# /opt/ssa-scraper/.env. The Google credentials travel over SSH on standard input only: they never
# appear on a command line, in the job log or in the response (the API stores them Fernet-encrypted
# and never echoes them). It then builds the adapter inside the api container, creates the archive
# root folder and lists it, so a wrong folder id or a refused grant fails this run instead of the
# hourly mirror.
#
# Environment:
#   DO_TOKEN                      required
#   GDRIVE_FOLDER_ID              required: the id at the end of the Drive folder's address
#   GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET / GDRIVE_REFRESH_TOKEN
#                                 an OAuth grant from the Google account that owns the folder
#                                 (personal Drive), or instead
#   GDRIVE_SERVICE_ACCOUNT_JSON   a service account added to a Workspace Shared Drive
#   TARGET_NAME                   default google_drive
#   ROOT_PATH                     folder inside the Drive folder, default SIKANDER_Corpus
#   MIRROR_LOGIN_SESSION_ROWS     true|false: also mirror PakistanLawSite judgments (default true)
# =============================================================================
set -euo pipefail

: "${DO_TOKEN:?DO_TOKEN is required}"
: "${GDRIVE_FOLDER_ID:?GDRIVE_FOLDER_ID is required}"
DROPLET_NAME="${DROPLET_NAME:-ssa-corpus}"
TARGET_NAME="${TARGET_NAME:-google_drive}"
ROOT_PATH="${ROOT_PATH:-SIKANDER_Corpus}"
MIRROR_LOGIN_SESSION_ROWS="${MIRROR_LOGIN_SESSION_ROWS:-true}"
HERE="$(cd "$(dirname "$0")" && pwd)"
API="https://api.digitalocean.com/v2"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
api() { curl -fsS -H "Authorization: Bearer $DO_TOKEN" "$API$1"; }
for t in curl jq ssh python3; do command -v "$t" >/dev/null || die "$t is required"; done
case "$TARGET_NAME" in *[!A-Za-z0-9_-]*|"") die "TARGET_NAME may contain only letters, digits, - and _";; esac
case "$MIRROR_LOGIN_SESSION_ROWS" in true|false) ;; *) die "MIRROR_LOGIN_SESSION_ROWS must be true or false";; esac
if [ -z "${GDRIVE_REFRESH_TOKEN:-}" ] && [ -z "${GDRIVE_SERVICE_ACCOUNT_JSON:-}" ]; then
  die "set GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET and GDRIVE_REFRESH_TOKEN (personal Drive) or GDRIVE_SERVICE_ACCOUNT_JSON (Shared Drive)"
fi
if [ -n "${GDRIVE_REFRESH_TOKEN:-}" ] && { [ -z "${GDRIVE_CLIENT_ID:-}" ] || [ -z "${GDRIVE_CLIENT_SECRET:-}" ]; }; then
  die "GDRIVE_REFRESH_TOKEN needs GDRIVE_CLIENT_ID and GDRIVE_CLIENT_SECRET"
fi

KEYDIR="$(mktemp -d)"
trap 'rm -rf "$KEYDIR"' EXIT
python3 "$HERE/derive_ssh_key.py" "$KEYDIR" >/dev/null

DROPLET="$(api "/droplets?tag_name=ssa-corpus&per_page=200" | jq -c --arg n "$DROPLET_NAME" '[.droplets[] | select(.name==$n)][0]')"
[ -n "$DROPLET" ] && [ "$DROPLET" != "null" ] || die "no droplet named $DROPLET_NAME; run the deploy-cloud workflow first"
IP="$(echo "$DROPLET" | jq -r '.networks.v4[] | select(.type=="public") | .ip_address' | head -1)"
[ -n "$IP" ] && [ "$IP" != "null" ] || die "droplet has no public IPv4 address"
SSH_OPTS=(-i "$KEYDIR/id_ed25519" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR)

# The request body is built from the environment and written to the SSH session's stdin.
BODY="$(TARGET_NAME="$TARGET_NAME" ROOT_PATH="$ROOT_PATH" MIRROR="$MIRROR_LOGIN_SESSION_ROWS" python3 - <<'PY'
import json, os
cfg = {"folder_id": os.environ["GDRIVE_FOLDER_ID"].strip()}
if os.environ.get("GDRIVE_REFRESH_TOKEN"):
    cfg.update(client_id=os.environ["GDRIVE_CLIENT_ID"].strip(), client_secret=os.environ["GDRIVE_CLIENT_SECRET"].strip(),
               refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"].strip())
else:
    cfg["service_account_json"] = os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"].strip()
print(json.dumps({"name": os.environ["TARGET_NAME"], "target_type": "google_drive", "root_path": os.environ["ROOT_PATH"],
                  "config": cfg, "enabled": True, "mirror_login_session_rows": os.environ["MIRROR"] == "true"}))
PY
)"

echo "== registering archive target $TARGET_NAME (google_drive, root $ROOT_PATH, PakistanLawSite rows: $MIRROR_LOGIN_SESSION_ROWS)"
REMOTE='set -e
K="$(sed -n "s/^ADMIN_API_KEY=//p" /opt/ssa-scraper/.env)"
[ -n "$K" ] || { echo "ADMIN_API_KEY is not set on the server" >&2; exit 1; }
curl -fsS -X POST -H "X-API-Key: $K" -H "Content-Type: application/json" --data-binary @- http://127.0.0.1:8000/admin/archive/targets'
printf '%s' "$BODY" | ssh "${SSH_OPTS[@]}" "root@$IP" "$REMOTE" || die "the server refused the target (see the message above)"
echo

echo "== checking the grant and the folder from the server"
CHECK="cd /opt/ssa-scraper && docker compose exec -T api python -c '
import asyncio, sys
from sqlalchemy import select
from scraper.database import SessionLocal
from scraper.models import ArchiveTarget
from scraper.storage.adapters import build_adapter
from scraper.storage.archive import target_config
async def main():
    async with SessionLocal() as db:
        t = (await db.execute(select(ArchiveTarget).where(ArchiveTarget.name == sys.argv[1]))).scalars().first()
        adapter = build_adapter(t.target_type, target_config(t))
        adapter.ensure_tree(\"_index/.keep\")
        print(\"Drive folder reachable; archive root holds:\", adapter.list(\"\")[:10] or \"(empty so far)\")
asyncio.run(main())
' $TARGET_NAME"
ssh "${SSH_OPTS[@]}" "root@$IP" "$CHECK" 2>&1 | sed -E 's/[0-9a-f]{64}/<redacted-64-hex>/g' | tail -20
echo "The hourly archive-mirror task now copies every promoted judgment, statute and instrument to this folder."
