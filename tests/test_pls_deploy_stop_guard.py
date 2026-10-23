"""Regression checks: a failed/unverified worker stop must never release live PLS locks."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(script: str, **settings):
    env = os.environ.copy()
    env.update({key: str(value) for key, value in settings.items()})
    return subprocess.run(["bash", "-c", script], cwd=ROOT, env=env,
                          capture_output=True, text=True, check=False)


MOCK_DOCKER = r"""
docker() {
  if [ "$1" = compose ] && [ "$2" = stop ]; then return "${STOP_RC:-0}"; fi
  if [ "$1" = compose ] && [ "$2" = ps ]; then
    printf '%s\n' "${PS_OUTPUT:-}"
    return "${PS_RC:-0}"
  fi
  if [ "$1" = compose ] && [ "$2" = exec ]; then
    if [ "$3" = -T ] && [ "$4" = worker-scraper ] && [ "$5" = hostname ]; then echo local-host; return 0; fi
    echo PSQL_CALLED; return 0
  fi
  echo UNEXPECTED_DOCKER >&2
  return 3
}
"""


@pytest.mark.parametrize("settings,allowed", [
    ({"STOP_RC": 1}, False),
    ({"PS_RC": 1}, False),
    ({"PS_OUTPUT": "redis\nworker-scraper"}, False),
    ({"PS_OUTPUT": "redis\napi"}, True),
])
def test_host_stop_guard_requires_confirmed_shutdown(settings, allowed):
    script = "source scripts/pls_host_lib.sh\n" + MOCK_DOCKER + """
if pls_host_stop_scraper_safely; then echo SAFE; else echo REFUSED; fi
"""
    result = _run(script, **settings)
    assert result.returncode == 0
    assert ("SAFE" in result.stdout) is allowed


@pytest.mark.parametrize("settings,allowed", [
    ({"STOP_RC": 1}, False),
    ({"PS_RC": 1}, False),
    ({"PS_OUTPUT": "worker-scraper"}, False),
    ({"PS_OUTPUT": "redis"}, True),
])
def test_force_release_never_proceeds_after_unverified_stop(settings, allowed):
    script = "source scripts/pls_host_lib.sh\n" + MOCK_DOCKER + """
pls_host_release_search_harvest_locks() { echo RELEASE_CALLED; }
pls_host_redis_cli() { :; }
if pls_host_force_release_stalled_harvest; then echo DONE; else echo REFUSED; fi
"""
    result = _run(script, **settings)
    assert result.returncode == 0
    assert ("RELEASE_CALLED" in result.stdout) is allowed
    assert ("PSQL_CALLED" in result.stdout) is allowed


@pytest.mark.parametrize("stop_rc,allowed", [(1, False), (0, True)])
def test_deploy_only_guard_blocks_release_on_stop_failure(stop_rc, allowed):
    installer = (ROOT / "cloud/install.sh").read_text()
    marker = '        log "deploy-only: stopping worker-scraper before clearing search-harvest Redis locks"'
    start = installer.index(marker)
    end = installer.index("\n      fi", start)
    fragment = installer[start:end]
    script = "source scripts/pls_host_lib.sh\n" + MOCK_DOCKER + """
log() { :; }
die() { echo REFUSED >&2; exit 17; }
pls_host_release_search_harvest_locks() { echo RELEASE_CALLED; }
""" + fragment
    result = _run(script, STOP_RC=stop_rc, PS_OUTPUT="redis")
    assert ("RELEASE_CALLED" in result.stdout) is allowed
    assert (result.returncode == 0) is allowed
