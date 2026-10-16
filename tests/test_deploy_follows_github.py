"""A server installed from an uploaded copy keeps following GitHub, and no deploy publishes the admin key (16 October 2026).

deploy-cloud uploads a git bundle, clones it to /root/ssa-src and runs install.sh --repo /root/ssa-src. install.sh set
the service checkout's `origin` to that path, so the 15-minute auto-deploy (scripts/auto_deploy.sh) fetched the frozen
upload for ever: the free-sites server stayed on the 13 October commit through #211-#222, its upload held no `main`,
and every cron run died on the fetch or the fast-forward. The checkout now follows GitHub whenever it was installed
from a local copy.

The repository is public, and do_deploy.sh wrote the dashboard admin key into the job summary, which anyone can read.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "cloud" / "follow_github.sh"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _service_checkout_from_upload(tmp_path: Path) -> tuple[Path, Path]:
    """An upload clone (like /root/ssa-src) and a service checkout (like /opt/ssa-scraper) whose origin is the upload."""
    upload = tmp_path / "ssa-src"
    upload.mkdir()
    _git("init", "-q", "-b", "main", cwd=upload)
    (upload / "README").write_text("x\n")
    _git("add", "README", cwd=upload)
    _git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "c1", cwd=upload)
    service = tmp_path / "ssa-scraper"
    _git("clone", "-q", str(upload), str(service), cwd=tmp_path)
    return upload, service


def _run_helper(service: Path, repo: str, origin_url: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if origin_url is not None:
        env["SSA_ORIGIN_URL"] = origin_url
    return subprocess.run(["bash", str(HELPER), str(service), repo], capture_output=True, text=True, env=env)


def test_a_checkout_installed_from_a_local_upload_follows_github(tmp_path):
    upload, service = _service_checkout_from_upload(tmp_path)
    assert _git("remote", "get-url", "origin", cwd=service) == str(upload)

    res = _run_helper(service, str(upload))

    assert res.returncode == 0, res.stderr
    assert _git("remote", "get-url", "origin", cwd=service) == "https://github.com/shamailsik-prog/ssa-scraper.git"
    assert "follows" in res.stdout


def test_a_file_url_upload_is_local_too(tmp_path):
    upload, service = _service_checkout_from_upload(tmp_path)
    res = _run_helper(service, f"file://{upload}", origin_url="https://example.test/fork.git")
    assert res.returncode == 0, res.stderr
    assert _git("remote", "get-url", "origin", cwd=service) == "https://example.test/fork.git"


def test_a_checkout_installed_from_a_remote_url_is_left_alone(tmp_path):
    _upload, service = _service_checkout_from_upload(tmp_path)
    remote = "https://github.com/someone/ssa-scraper-fork.git"
    _git("remote", "set-url", "origin", remote, cwd=service)

    res = _run_helper(service, remote)

    assert res.returncode == 0, res.stderr
    assert _git("remote", "get-url", "origin", cwd=service) == remote


def test_install_points_origin_at_github_after_both_the_update_and_the_clone():
    text = (ROOT / "cloud" / "install.sh").read_text()
    update_end = text.index('git -C "$DIR" checkout -q -B "$BRANCH" FETCH_HEAD')
    clone = text.index('clone -q -b "$BRANCH" "$REPO" "$DIR"')
    call = text.index('cloud/follow_github.sh" "$DIR" "$REPO"')
    unset = text.index("unset GH_TOKEN")
    assert update_end < call and clone < call < unset  # one call after the if/else, covering both paths


def test_installer_shows_the_admin_key_only_on_a_terminal():
    """deploy-cloud runs install.sh over ssh with its output piped into a public workflow log; the do_deploy.sh sed
    mask only catches a 64-lowercase-hex key, so a hand-set key would pass. The installer itself must not print it."""
    text = (ROOT / "cloud" / "install.sh").read_text()
    banner = text[text.index('ADMIN_KEY="$(grep'):]
    assert "if [ -t 1 ]; then" in banner
    assert " Admin key:  $ADMIN_KEY_SHOWN" in banner
    assert " Admin key:  $ADMIN_KEY\n" not in banner


def test_deploy_never_writes_the_admin_key_to_the_job_summary():
    text = (ROOT / "cloud" / "do_deploy.sh").read_text()
    assert "$ADMIN_KEY" not in text
    assert "ADMIN_API_KEY=" not in text  # the key is not even read off the server
    # the installer prints the key; the deploy log masks it
    assert "sed -E 's/Admin key:  [0-9a-f]{64}/" in text
