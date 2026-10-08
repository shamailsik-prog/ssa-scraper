"""scripts/auto_deploy.sh always starts celery-beat again after it pauses it for a deploy.

On 7 October 2026 a deploy that had to wait for the PakistanLawSite job stopped beat, deferred to the
next cron tick and exited; the next tick saw beat "not running" and deployed without starting it, so
nothing was scheduled again (no source dispatch, promotion or mirror). These tests run the real script
against a fake host: docker, crontab and curl are stubs that keep their state in files."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "auto_deploy.sh"

FAKE_DOCKER = r"""#!/usr/bin/env bash
# fake `docker compose ...`: running services in $FAKE/running, PLS busy while $FAKE/busy exists
shift  # compose
case "$1" in
  ps) cat "$FAKE/running" 2>/dev/null ;;
  stop) grep -vx "$2" "$FAKE/running" > "$FAKE/running.new" || true; mv "$FAKE/running.new" "$FAKE/running"; echo "stop $2" >> "$FAKE/calls" ;;
  up) svc="${@: -1}"; grep -qx "$svc" "$FAKE/running" || echo "$svc" >> "$FAKE/running"; echo "up $svc" >> "$FAKE/calls" ;;
  exec)
    case "$*" in
      *redis-cli\ EXISTS*) [ -f "$FAKE/busy" ] && echo 1 || echo 0 ;;
      *redis-cli\ DEL*) rm -f "$FAKE/busy"; echo "release" >> "$FAKE/calls" ;;
      *psql*UPDATE*) echo "release-job" >> "$FAKE/calls" ;;
      *psql*) echo 0 ;;
      *) echo "exec $*" >> "$FAKE/calls" ;;
    esac ;;
  *) echo "docker $*" >> "$FAKE/calls" ;;
esac
exit 0
"""

FAKE_CRONTAB = "#!/usr/bin/env bash\n[ \"$1\" = -l ] && cat \"$FAKE/crontab\" 2>/dev/null; [ \"$1\" = - ] && cat > \"$FAKE/crontab\"; exit 0\n"
FAKE_CURL = "#!/usr/bin/env bash\nexit 1\n"  # /status.json unreachable: never reports a promotion stall


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def host(tmp_path: Path):
    fake = tmp_path / "fake"
    bin_dir = fake / "bin"
    bin_dir.mkdir(parents=True)
    for name, body in (("docker", FAKE_DOCKER), ("crontab", FAKE_CRONTAB), ("curl", FAKE_CURL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (fake / "running").write_text("api\nredis\npostgres\nworker-scraper\ncelery-beat\n")

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "cloud").mkdir()
    (seed / "cloud" / "install.sh").write_text('#!/usr/bin/env bash\necho "deploy $AUTO_DEPLOY_SERVICES" >> "$FAKE/calls"\n')
    (seed / "scraper").mkdir()
    (seed / "scraper" / "a.py").write_text("v = 1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "one")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    app = tmp_path / "app"
    _git(tmp_path, "clone", "-q", str(origin), str(app))
    (app / "state").mkdir()
    sha = _git(app, "rev-parse", "HEAD")
    (app / "state" / "tip_sha.txt").write_text(sha + "\n")
    (app / "state" / "rollout_sha.txt").write_text(sha + "\n")

    def push_change():
        (seed / "scraper" / "a.py").write_text(f"v = {time.time()}\n")
        _git(seed, "commit", "-qam", "change")
        _git(seed, "push", "-q", str(origin), "main")

    def run(**env_extra):
        env = dict(os.environ, FAKE=str(fake), PATH=f"{bin_dir}:{os.environ['PATH']}", SSA_SCRAPER_DIR=str(app),
                   AUTO_DEPLOY_WAIT_MAX_SECONDS="0", AUTO_DEPLOY_WAIT_POLL_SECONDS="0", **env_extra)
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    class Host:
        pass

    h = Host()
    h.fake, h.app, h.run, h.push_change = fake, app, run, push_change
    h.running = lambda: (fake / "running").read_text().split()
    h.calls = lambda: (fake / "calls").read_text().splitlines() if (fake / "calls").exists() else []
    h.marker = app / "state" / "beat_paused_by_deploy"
    return h


def test_deferred_deploy_keeps_beat_paused_then_starts_it_after_the_deploy(host):
    host.push_change()
    (host.fake / "busy").touch()
    r = host.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "deferring deploy" in r.stdout
    assert "celery-beat" not in host.running() and host.marker.exists()  # paused while the PLS job drains

    (host.fake / "busy").unlink()  # the PLS job finished
    r = host.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("deploy ") for c in host.calls())
    assert "celery-beat" in host.running() and not host.marker.exists()


def test_beat_left_stopped_by_an_old_deploy_is_started_again(host):
    """The state found on the server on 8 October 2026: beat stopped, no marker, nothing pending."""
    (host.fake / "running").write_text("api\nredis\npostgres\nworker-scraper\n")
    r = host.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing to do" in r.stdout and "celery-beat" in host.running()


def test_pause_longer_than_the_cap_releases_the_job_and_deploys(host):
    host.push_change()
    (host.fake / "busy").touch()
    host.marker.write_text(str(int(time.time()) - 3000) + "\n")  # paused 50 minutes ago
    r = host.run(AUTO_DEPLOY_MAX_BEAT_PAUSE_SECONDS="2700")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "releasing the PLS job" in r.stdout
    calls = host.calls()
    assert "release-job" in calls and any(c.startswith("deploy ") for c in calls)
    assert "celery-beat" in host.running() and not host.marker.exists()


def test_operator_switch_keeps_beat_off(host):
    (host.app / "state" / "beat_disabled").touch()
    (host.fake / "running").write_text("api\nredis\npostgres\nworker-scraper\n")
    host.push_change()
    r = host.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("deploy ") for c in host.calls())
    assert "celery-beat" not in host.running()
