"""Host-side Redis lock release must not steal other workers' locks.

These subprocess checks source the real host library. No production Redis or
licensed-source network access is used.
"""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

MOCK = r"""
docker() {
  if [ "$1" = compose ] && [ "$2" = exec ] && [ "$4" = worker-scraper ] && [ "$5" = hostname ]; then
    [ "${HOST_RC:-0}" = 0 ] || return 1
    echo "${MOCK_HOST:-local-host}"
    return 0
  fi
  if [ "$1" = compose ] && [ "$2" = stop ]; then return "${STOP_RC:-0}"; fi
  if [ "$1" = compose ] && [ "$2" = ps ]; then echo redis; return 0; fi
  return 1
}
pls_host_env_flag_enabled() { return 0; }
pls_host_redis_cli() {
  case "$1" in
    --scan)
      printf '%s\n' \
        'corpus:login_session_lock:PakistanLawSite:search_harvest:local' \
        'corpus:login_session_lock:PakistanLawSite:search_harvest:remote' \
        'corpus:login_session_lock:PakistanLawSite:search_harvest:legacy'
      ;;
    --raw)
      case "$3" in
        *:local) echo 'local-host:123:nonce';;
        *:remote) echo 'remote-host:987:nonce';;
        *:legacy) echo 'old-opaque-token';;
      esac
      ;;
    EVAL)
      printf 'CAS %s %s\n' "$4" "$5" >> "$MOCK_LOG"
      echo "${CAS_RESULT:-1}"
      ;;
    DEL)
      printf 'UNSAFE_DEL %s\n' "$2" >> "$MOCK_LOG"
      ;;
    *) return 1 ;;
  esac
}
"""


def _run(tmp_path, *, host="local-host", stop_rc=0, host_rc=0, cas_result=1, do_stop=True):
    log = tmp_path / "redis-ops.txt"
    script = "source scripts/pls_host_lib.sh\n" + MOCK
    if do_stop:
        script += "pls_host_stop_scraper_safely || exit 19\n"
    script += "pls_host_release_search_harvest_locks\n"
    env = os.environ.copy()
    env.update(MOCK_LOG=str(log), MOCK_HOST=host, STOP_RC=str(stop_rc),
               HOST_RC=str(host_rc), CAS_RESULT=str(cas_result))
    result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env,
                            capture_output=True, text=True, check=False)
    return result, (log.read_text() if log.exists() else "")


def test_only_stopped_local_worker_lock_released(tmp_path):
    result, operations = _run(tmp_path)
    assert result.returncode == 0
    assert operations.count("CAS ") == 1
    assert "local-host:123:nonce" in operations
    assert "remote-host" not in operations
    assert "UNSAFE_DEL" not in operations
    assert "released 1 verified local" in result.stdout


@pytest.mark.parametrize("kwargs", [
    {"do_stop": False},
    {"host_rc": 1},
    {"stop_rc": 1},
    {"host": "invalid:host"},
])
def test_unverified_local_worker_cannot_release_any_lock(tmp_path, kwargs):
    result, operations = _run(tmp_path, **kwargs)
    assert result.returncode != 0
    assert operations == ""


def test_compare_and_delete_race_is_not_counted_as_success(tmp_path):
    result, operations = _run(tmp_path, cas_result=0)
    assert result.returncode == 0
    assert operations.count("CAS ") == 1
    assert "released" not in result.stdout


def test_legacy_and_remote_tokens_are_not_deleted(tmp_path):
    result, operations = _run(tmp_path)
    assert result.returncode == 0
    assert "legacy" not in operations
    assert "remote" not in operations
