#!/bin/bash
# PostToolUse hook (Edit|Write|MultiEdit|Bash): when Claude changes a file that scripts/guard_scan.sh
# covers, run the scan (the same one CI runs) and hand any failure straight back to Claude (exit 2,
# stderr), instead of it surfacing twenty minutes later in CI. The scan takes well under a second.
#
# It scans the checkout that was changed: the edited file's repository, or for Bash the hook input's
# cwd. CLAUDE_PROJECT_DIR stays at the session's original checkout, so it would miss a worktree.
# Bash can rewrite files without any Edit/Write call, so after a Bash command the scan runs whenever a
# covered path has uncommitted or untracked changes.
set -uo pipefail

input=$(python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("tool_name", ""))
print(d.get("cwd", ""))
print((d.get("tool_input") or {}).get("file_path", ""))' 2>/dev/null) || exit 0
tool=$(sed -n 1p <<<"$input")
cwd=$(sed -n 2p <<<"$input")
path=$(sed -n 3p <<<"$input")
cwd=${cwd:-$PWD}

covered_paths=(scraper migrations docker-compose.yml Dockerfile)

if [ "$tool" = "Bash" ]; then
  root=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null) || exit 0
  [ -n "$(git -C "$root" status --porcelain -- "${covered_paths[@]}" 2>/dev/null)" ] || exit 0
  what="a Bash command (uncommitted changes under ${covered_paths[*]})"
else
  [ -n "$path" ] || exit 0
  case "$path" in /*) ;; *) path="$cwd/$path" ;; esac
  root=$(git -C "$(dirname "$path")" rev-parse --show-toplevel 2>/dev/null) || exit 0
  rel=${path#"$root"/}
  case "$rel" in
    scraper/* | migrations/* | docker-compose.yml | Dockerfile) ;;
    *) exit 0 ;;
  esac
  what="the edit to $rel"
fi

[ -f "$root/scripts/guard_scan.sh" ] || exit 0
if ! out=$(bash "$root/scripts/guard_scan.sh" 2>&1); then
  printf 'scripts/guard_scan.sh failed in %s after %s. CI runs the same scan and will fail; fix this before moving on:\n%s\n' "$root" "$what" "$out" >&2
  exit 2
fi
exit 0
