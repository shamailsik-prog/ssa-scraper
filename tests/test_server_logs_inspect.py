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
