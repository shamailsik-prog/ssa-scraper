"""Guards against regressions that removed the PLS grid harvest path or Beat self-healing (#127)."""

from __future__ import annotations

from pathlib import Path


def test_citation_grid_harvester_still_present():
    text = Path("scraper/tasks/pakistanlawsite.py").read_text(encoding="utf-8")
    for token in (
        "run_citation_grid_surface",
        "run_citation_grid_window",
        "citation_grid_cursor",
        "archivedpatientGrid",
    ):
        assert token in text, f"missing {token} in pakistanlawsite.py"


def test_login_session_dispatch_still_wired():
    dispatcher = Path("scraper/tasks/dispatcher.py").read_text(encoding="utf-8")
    assert "run_login_session_job" in dispatcher
    assert "PakistanLawSite" in dispatcher


def test_celery_beat_pls_keepalive_and_watchdog_scheduled():
    celery = Path("scraper/tasks/celery_app.py").read_text(encoding="utf-8")
    assert '"pls-keepalive"' in celery or "'pls-keepalive'" in celery
    assert "pls_keepalive_hourly" in celery
    assert '"pls-stall-watchdog"' in celery or "'pls-stall-watchdog'" in celery
    assert "pls_stall_watchdog" in celery
    assert "dispatch-due-sources" in celery


def test_host_auto_deploy_and_keepalive_scripts_exist():
    assert Path("scripts/auto_deploy.sh").is_file()
    assert Path("scripts/pls_keepalive_cron.sh").is_file()
    assert Path("scripts/pls_host_lib.sh").is_file()
