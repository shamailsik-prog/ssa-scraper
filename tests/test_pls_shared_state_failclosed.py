"""Stalled-deploy recovery must not rewrite shared PLS job state or delete login locks.

These tests source the real host library with deterministic shell mocks.
No production database, Redis, Docker, or licensed source is contacted.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

MOCK = r"""
docker() {
  if [ "$1" = compose ] && [ "$2" = exec ] && [ "$4" = worker-scraper ] && [ "$5" = hostname ]; then
    [ "${HOST_RC:-0}" = 0 ] || return 1
    echo local-host
    return 0
  fi
  if [ "$1" = compose ] && [ "$2" = stop ]; then return "${STOP_RC:-0}"; fi
  if [ "$1" = compose ] && [ "$2" = ps ]; then
    [ "${PS_RC:-0}" = 0 ] || return 1
    echo redis
    return 0
  fi
  if [ "$1" = compose ] && [ "$2" = exec ] && [ "$4" = postgres ]; then
    printf 'SQL %s\n' "$*" >> "$MOCK_LOG"
    [ "${JOBS_RC:-0}" = 0 ] || return 1
    echo "${JOBS_OUTPUT:-0}"
    return 0
  fi
  return 1
}
pls_host_env_flag_enabled() { return 1; }
pls_host_redis_cli() {
  case "$1" in
    EXISTS)
      [ "${EXISTS_RC:-0}" = 0 ] || return 1
      echo "${EXISTS_OUTPUT:-0}"
      ;;
    --scan)
      [ "${SCAN_RC:-0}" = 0 ] || return 1
      printf '%s' "${SCAN_OUTPUT:-}"
      ;;
    DEL|SET|EVAL)
      echo "UNSAFE_REDIS_WRITE $*" >> "$MOCK_LOG"
      return 1
      ;;
    *) return 1 ;;
  esac
}
"""

def _run(tmp_path: Path, **overrides: object) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "operations.log"
    env = os.environ.copy()
    env.update({k: str(v) for k, v in overrides.items()})
    env["MOCK_LOG"] = str(log)
    script = "source scripts/pls_host_lib.sh\n" + MOCK + "\npls_host_force_release_stalled_harvest\n"
    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )
    return completed, (log.read_text() if log.exists() else "")

def test_verified_idle_does_not_mutate_shared_jobs_or_login_locks(tmp_path: Path) -> None:
    result, operations = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SELECT COUNT(*) FROM scraper_jobs" in operations
    assert "UPDATE scraper_jobs" not in operations
    assert "UNSAFE_REDIS_WRITE" not in operations

@pytest.mark.parametrize("condition", [
    {"EXISTS_OUTPUT": "1"},
    {"SCAN_OUTPUT": "corpus:login_session_lock:PakistanLawSite:search_harvest:remote"},
    {"JOBS_OUTPUT": "1"},
    {"JOBS_OUTPUT": "unknown"},
    {"JOBS_RC": "1"},
    {"EXISTS_RC": "1"},
    {"SCAN_RC": "1"},
    {"STOP_RC": "1"},
    {"HOST_RC": "1"},
    {"PS_RC": "1"},
])
def test_unknown_or_busy_shared_state_aborts_without_mutation(tmp_path: Path, condition: dict[str, str]) -> None:
    result, operations = _run(tmp_path, **condition)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "UPDATE scraper_jobs" not in operations
    assert "UNSAFE_REDIS_WRITE" not in operations
