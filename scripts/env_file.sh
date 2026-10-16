#!/usr/bin/env bash
# Read docker compose's .env as data. Sourced by scripts/auto_deploy.sh and cloud/install.sh (do not execute directly).
#
# The scripts used `set -a; source .env` under `set -euo pipefail`. Compose's .env is literal KEY=VALUE text: a value
# with a space, a quote, a `$` or a bracket is fine for compose but makes bash run a command or expand a variable,
# and the script exits. Every auto-deploy run on the free-sites server stopped there, before it fetched, from at
# least 12 to 16 October 2026.

# load_env_file FILE: export each KEY=VALUE line of FILE verbatim, as compose reads it. Comments, blank lines and
# lines that are not KEY=VALUE are skipped; one pair of matching surrounding quotes and a trailing CR are removed.
load_env_file() {
  local file="$1" line key value decl
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in '' | '#'*) continue ;; esac
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    if [[ "$value" =~ ^\"(.*)\"$ ]] || [[ "$value" =~ ^\'(.*)\'$ ]]; then
      value="${BASH_REMATCH[1]}"
    fi
    # a readonly shell variable (UID, EUID, PPID): exporting it aborts a non-interactive bash, `|| true` or not
    decl="$(declare -p "$key" 2>/dev/null || true)"
    if [[ "$decl" =~ ^declare\ -[A-Za-z]*r ]]; then
      continue
    fi
    export "$key=$value"
  done <"$file"
}
