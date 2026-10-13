"""Owner's rule of 13 October 2026: after a logout, wait 3 minutes, then log back in."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scraper.config import Settings, settings
from scraper.pls_navigation import pls_check_url
from scraper.tasks.login_recovery import BACKOFF_MINUTES, recover_slot, recovery_key
from tests.fixtures import BrowserScript
from tests.test_login_recovery import GRID_HTML, MAINPAGE_HTML, MAINPAGE_URL, _bounce_slot, _slot


def test_a_recovery_check_queued_behind_a_long_job_expires():
    from scraper.tasks.celery_app import app

    entry = app.conf.beat_schedule["recover-login-slots"]
    assert entry["options"]["expires"] == entry["schedule"]


def test_defaults_wait_three_minutes_and_check_every_minute():
    fields = Settings.model_fields
    assert fields["LOGIN_RECOVERY_COOLDOWN_MINUTES"].default == 3
    # Checked every minute, so a 3-minute wait signs in at 3-4 minutes, not up to 8.
    assert fields["LOGIN_RECOVERY_SCHEDULE_SECONDS"].default == 60
    assert BACKOFF_MINUTES[0] == 3


async def test_logged_out_slot_signs_in_again_three_minutes_later(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", Settings.model_fields["LOGIN_RECOVERY_COOLDOWN_MINUTES"].default)
    mgr = await _bounce_slot(db, login_source)
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", pls_check_url()), GRID_HTML)

    first = await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory())
    assert datetime.fromisoformat(first["scheduled"]) == t0 + timedelta(minutes=3)

    early = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(minutes=2, seconds=59), browser_factory=sc.factory())
    assert "waiting_until" in early and sc.log == []  # nothing opened inside the 3 minutes

    done = await recover_slot(db, mgr, await _slot(db, 1), now=t0 + timedelta(minutes=3), browser_factory=sc.factory())
    await db.commit()
    assert done.get("recovered") == "verified"
    assert (await _slot(db, 1)).state == "ACTIVE"


async def test_a_failed_sign_in_is_tried_again_three_minutes_later(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_RECOVERY_COOLDOWN_MINUTES", 0)
    mgr = await _bounce_slot(db, login_source)
    t0 = datetime.now(timezone.utc)
    sc = BrowserScript()
    sc.page(("goto", pls_check_url()), MAINPAGE_HTML, url=MAINPAGE_URL)

    await recover_slot(db, mgr, await _slot(db, 1), now=t0, browser_factory=sc.factory())
    t1 = t0 + timedelta(seconds=1)
    result = await recover_slot(db, mgr, await _slot(db, 1), now=t1, browser_factory=sc.factory())
    await db.commit()
    assert "failed" in result
    record = (login_source.config_json or {})[recovery_key(1)]
    assert datetime.fromisoformat(record["next_attempt_at"]) == t1 + timedelta(minutes=3)


def test_auto_deploy_pins_the_three_minute_wait_in_env(tmp_path):
    """install.sh copied .env.example (15 minutes, a 5-minute check) into the server's .env, which outranks the
    code default; the deploy rewrites both keys so the rule holds on the live server."""
    env = tmp_path / ".env"
    env.write_text("FOO=1\nLOGIN_RECOVERY_COOLDOWN_MINUTES=15\nLOGIN_RECOVERY_SCHEDULE_SECONDS=300", encoding="utf-8")
    script = Path("scripts/auto_deploy.sh").read_text(encoding="utf-8")
    funcs = ""
    for name in ("pin_env() {", "ensure_relogin_wait() {"):
        start = script.index(name)
        funcs += script[start : script.index("\n}\n", start) + 3]
    prog = 'log() { echo "$*"; }\n' + funcs + 'cd "$1" && ensure_relogin_wait && ensure_relogin_wait\n'
    subprocess.run(["bash", "-c", prog, "deploy-test", str(tmp_path)], check=True, capture_output=True, text=True)
    assert env.read_text(encoding="utf-8").splitlines() == [
        "FOO=1",
        "LOGIN_RECOVERY_COOLDOWN_MINUTES=3",
        "LOGIN_RECOVERY_SCHEDULE_SECONDS=60",
    ]
