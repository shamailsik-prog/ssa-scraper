from __future__ import annotations

import subprocess
from pathlib import Path


def test_auto_deploy_script_is_valid_bash():
    script = Path("scripts/auto_deploy.sh")
    assert script.is_file()
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_auto_deploy_default_services_exclude_celery_beat():
    text = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    assert "DEFAULT_APP_SERVICES=(api worker-scraper worker-public)" in text
    assert "celery-beat" not in text.split("DEFAULT_APP_SERVICES=")[1].split(")")[0]


def test_auto_deploy_supports_dry_run():
    text = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    assert "--dry-run" in text
    assert "DRY_RUN" in text


def test_pls_host_lib_exposes_beat_helpers():
    text = Path("scripts/pls_host_lib.sh").read_text(encoding="utf-8")
    for fn in ("pls_host_beat_running", "pls_host_stop_beat", "pls_host_start_beat"):
        assert fn in text


def test_auto_deploy_unblocks_stalled_zero_output_harvest():
    deploy = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    host = Path("scripts/pls_host_lib.sh").read_text(encoding="utf-8")
    assert "pls_host_promotion_stalled" in host
    assert "pls_host_force_release_stalled_harvest" in host
    assert "no_output_while_harvesting" in deploy
    assert "pls_host_force_release_stalled_harvest" in deploy
