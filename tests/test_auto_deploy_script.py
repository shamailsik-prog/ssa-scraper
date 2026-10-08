from __future__ import annotations

import subprocess
from pathlib import Path


def test_auto_deploy_script_is_valid_bash():
    script = Path("scripts/auto_deploy.sh")
    assert script.is_file()
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_auto_deploy_default_services_exclude_celery_beat():
    text = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    assert "DEFAULT_APP_SERVICES=(api worker-scraper worker-public worker-maintenance)" in text
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


def test_auto_deploy_merges_before_pls_idle_wait():
    deploy = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    merge_marker = "before waiting for PLS idle"
    assert merge_marker in deploy
    main_body = deploy.split("main() {", 1)[1]
    assert main_body.index(merge_marker) < main_body.index("wait_for_pls_idle")


def test_pls_host_running_jobs_uses_compose_postgres_defaults():
    text = Path("scripts/pls_host_lib.sh").read_text(encoding="utf-8")
    assert "POSTGRES_USER:-legal" in text
    assert "POSTGRES_DB:-legal_scraper" in text
    assert "POSTGRES_USER:-corpus" not in text


def test_auto_deploy_tracks_rollout_sha_separately_from_tip_sha():
    deploy = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    assert "state/rollout_sha.txt" in deploy
    assert "rollout_sha" in deploy
    assert "rollout_sha match" in deploy or "rollout_sha matches" in deploy
    assert deploy.index("rollout_sha") < deploy.index(">\"$TIP_FILE\"")
