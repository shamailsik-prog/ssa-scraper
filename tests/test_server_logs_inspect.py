"""The read-only inspect report prints a named allow-list of on/off settings from the server's .env (11 Oct 2026:
the case-ID walk had stopped because Beat no longer scheduled it, and nothing showed whether .env had turned it off)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _inspect_script() -> str:
    text = Path("cloud/do_server_logs.sh").read_text(encoding="utf-8")
    start = text.index("REMOTE='", text.index('if [ "$SERVICES" = "inspect" ]')) + len("REMOTE='")
    return text[start : text.index("'\n  ssh ", start)]


def test_inspect_prints_the_walk_and_harvest_switches_and_nothing_secret():
    script = _inspect_script()
    line = next(ln for ln in script.splitlines() if "on/off and mode settings" in ln)
    names = re.search(r"for k in ([A-Z0-9_ ]+); do", line).group(1).split()
    assert {"PLS_CASEID_WALK_ENABLED", "PLS_SEARCH_HARVEST_ENABLED", "DEPLOY_ROLE", "HARVEST_MODE"} <= set(names)
    assert not [n for n in names if re.search(r"KEY|SECRET|TOKEN|PASS|CREDENTIAL|URL|DSN", n)]
    subprocess.run(["bash", "-n", "-c", script], check=True)


def test_inspect_shows_the_auto_deploy_state_and_only_its_own_log_lines(tmp_path):
    """The auto-deploy failed on every cron run from 13 to 16 October and nothing showed it (#223). Inspect now prints
    its own timestamped log lines (never raw command output: the workflow log is public), the recorded tip and rollout
    commits, and the origin the cron fetches."""
    line = next(ln for ln in _inspect_script().splitlines() if "== auto-deploy" in ln)
    srv = tmp_path / "opt-ssa-scraper"
    (srv / "state").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(srv)], check=True)
    subprocess.run(["git", "-C", str(srv), "remote", "add", "origin", "https://github.com/shamailsik-prog/ssa-scraper.git"], check=True)
    own = [f"[2026-10-16T04:{m:02d}:00Z] auto_deploy: fetch origin main" for m in range(45)]
    raw = ["From https://github.com/shamailsik-prog/ssa-scraper", " Admin key:  not-a-hex-secret-from-old-installer-output"]
    (srv / "state" / "auto_deploy.log").write_text("\n".join(own[:20] + raw + own[20:]) + "\n")
    (srv / "state" / "tip_sha.txt").write_text("057294c\n")
    (srv / "state" / "rollout_sha.txt").write_text("057294c\n")

    out = subprocess.run(["bash", "-c", line.replace("/opt/ssa-scraper", str(srv))], capture_output=True, text=True, check=True).stdout

    shown = [ln for ln in out.splitlines() if ln.startswith("[2026-")]
    assert shown == own[-40:]
    assert "Admin key" not in out and "From https" not in out
    assert "tip_sha: 057294c" in out and "rollout_sha: 057294c" in out
    assert "origin: https://github.com/shamailsik-prog/ssa-scraper.git" in out
