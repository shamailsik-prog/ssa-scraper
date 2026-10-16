"""A full install followed by an auto-deploy tick changes nothing in .env (16 October 2026).

install.sh dropped BACKFILL_LOGIN_SESSION_CONCURRENCY from .env as "no longer read", although scraper/config.py reads
it and .env.example lists it; the next auto-deploy tick pinned it back (pin_env). Every app service takes .env as its
env_file, so that one line recreated all of them a second time, minutes after the deploy had: the deploy-cloud run of
06:42 and the cron tick of 06:45 each killed the free-sites worker mid-item.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _block(text: str, start: str, end: str) -> str:
    i = text.index(start) + len(start)
    return text[i : text.index(end, i)]


def _reconcile(cwd: Path) -> None:
    """install.sh's reconcile step for an existing .env (the deploy-cloud path for a server that is already set up)."""
    program = _block((ROOT / "cloud" / "install.sh").read_text(), "python3 - <<'RECONCILE'\n", "\nRECONCILE\n")
    subprocess.run(["python3", "-c", program], cwd=cwd, check=True, capture_output=True, text=True)


def _auto_deploy_pins(cwd: Path) -> str:
    """The .env pins every auto-deploy tick applies; returns what they logged."""
    script = (ROOT / "scripts" / "auto_deploy.sh").read_text()
    funcs = ""
    for name in ("pin_env() {", "ensure_two_pls_logins() {", "ensure_relogin_wait() {"):
        start = script.index(name)
        funcs += script[start : script.index("\n}\n", start) + 3]
    prog = 'log() { echo "$*"; }\n' + funcs + 'cd "$1" && ensure_two_pls_logins && ensure_relogin_wait\n'
    res = subprocess.run(["bash", "-c", prog, "pins", str(cwd)], check=True, capture_output=True, text=True)
    return res.stdout


def test_the_auto_deploy_tick_after_an_install_leaves_env_unchanged(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env")  # a server's .env: install.sh starts it from the example

    _reconcile(tmp_path)
    reconciled = (tmp_path / ".env").read_text()
    logged = _auto_deploy_pins(tmp_path)

    assert logged == ""
    assert (tmp_path / ".env").read_text() == reconciled


def test_install_keeps_the_backfill_login_concurrency_that_config_reads(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    (tmp_path / ".env").write_text("LOGIN_SESSION_CONCURRENCY=2\nBACKFILL_LOGIN_SESSION_CONCURRENCY=2\n")

    _reconcile(tmp_path)

    assert "BACKFILL_LOGIN_SESSION_CONCURRENCY=2" in (tmp_path / ".env").read_text().splitlines()
    assert "BACKFILL_LOGIN_SESSION_CONCURRENCY" in (ROOT / "scraper" / "config.py").read_text()
