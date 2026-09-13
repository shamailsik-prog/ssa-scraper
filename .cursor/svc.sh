#!/usr/bin/env bash
# Load the virtualenv and .env, then exec a long-running service command (used by terminals).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate
set -a
# shellcheck disable=SC1091
source .env
set +a
exec "$@"
