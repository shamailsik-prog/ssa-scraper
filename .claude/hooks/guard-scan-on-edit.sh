#!/bin/bash
# PostToolUse hook (Edit|Write|MultiEdit): when Claude edits a file that scripts/guard_scan.sh covers,
# run the scan (the same one CI runs) and hand any failure straight back to Claude (exit 2, stderr),
# instead of it surfacing twenty minutes later in CI. The scan takes well under a second.
set -uo pipefail
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0

path=$(python3 -c 'import json, sys; print((json.load(sys.stdin).get("tool_input") or {}).get("file_path", ""))' 2>/dev/null) || exit 0

case "$path" in
  scraper/* | */scraper/* | migrations/* | */migrations/* | docker-compose.yml | */docker-compose.yml | Dockerfile | */Dockerfile) ;;
  *) exit 0 ;;
esac

if ! out=$(bash scripts/guard_scan.sh 2>&1); then
  printf 'scripts/guard_scan.sh failed after the edit to %s. CI runs the same scan and will fail; fix this before moving on:\n%s\n' "$path" "$out" >&2
  exit 2
fi
exit 0
