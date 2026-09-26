from __future__ import annotations

import subprocess
from pathlib import Path


def test_auto_deploy_script_is_valid_bash():
    script = Path("scripts/auto_deploy.sh")
    assert script.is_file()
    subprocess.run(["bash", "-n", str(script)], check=True)
