#!/usr/bin/env bash
# Read one value from docker compose's .env as data. Sourced by scripts/auto_deploy.sh (do not execute directly).
#
# The deploy scripts used `set -a; source .env` under `set -euo pipefail`. Compose's .env has compose's own rules: a
# value with a space, a quote, a `$` or a bracket is fine for compose but makes bash run a command or expand a
# variable, and the script exits. Every auto-deploy run on the free-sites server stopped there, before it fetched,
# from at least 12 to 16 October 2026. Nothing is exported either: exported variables override .env for
# `docker compose`, which must keep reading its own file (an exported COMPOSE_FILE with an inline comment would
# break every deployment).

# env_file_value FILE KEY: print KEY's value in FILE (its last assignment), or nothing when FILE or KEY is absent.
# Read like a plain compose value: `export ` and spaces around `=` allowed; a quoted value is taken between its
# quotes; an unquoted one loses a trailing ` # comment` and surrounding blanks. No `${VAR}` interpolation.
env_file_value() {
  local file="$1" want="$2" line value="" found=""
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$ ]] || continue
    [ "${BASH_REMATCH[2]}" = "$want" ] || continue
    value="${BASH_REMATCH[3]}"
    found=1
  done <"$file"
  [ -n "$found" ] || return 0
  if [[ "$value" =~ ^\"([^\"]*)\" ]] || [[ "$value" =~ ^\'([^\']*)\' ]]; then
    value="${BASH_REMATCH[1]}"
  else
    [[ "$value" =~ ^(.*[^[:space:]])?[[:space:]]+#.*$ ]] && value="${BASH_REMATCH[1]}"
    [[ "$value" =~ ^(.*[^[:space:]])[[:space:]]*$ ]] && value="${BASH_REMATCH[1]}"
  fi
  printf '%s' "$value"
}
