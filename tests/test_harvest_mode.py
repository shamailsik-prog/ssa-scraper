from __future__ import annotations

from sqlalchemy import select

from scraper.fetchers import HttpFetcher
from scraper.models import ScraperSource
from scraper.tasks.public_pipeline import run_public_source


def test_harvest_mode_toggle_and_scheduler_controls(client, admin_headers):
    baseline = client.get("/admin/sources/harvest-mode", headers=admin_headers).json()
    assert baseline["mode"] in ("backfill", "updates")
    changed = client.post(
        "/admin/sources/harvest-mode",
        headers=admin_headers,
        json={"mode": "backfill", "changed_by": "qa", "reason": "initial harvest"},
    ).json()
    assert changed["mode"] == "backfill"
    assert changed["last_change"]["changed_by"] == "qa"

    updated = client.post(
        "/admin/sources/NasirLawSite/config",
        headers=admin_headers,
        json={
            "backfill_frequency_minutes": 5,
            "update_frequency_hours": 6,
            "backfill_priority": 7,
            "backfill_enabled": True,
            "update_enabled": True,
            "auto_retry_on_block": True,
            "block_retry_cooldown_minutes": 45,
            "block_retry_max_attempts": 4,
        },
    ).json()
    assert updated["scheduler"]["backfill_frequency_minutes"] == 5
    assert updated["scheduler"]["update_frequency_hours"] == 6
    assert updated["scheduler"]["backfill_priority"] == 7
    assert updated["scheduler"]["auto_retry_on_block"] is True
    assert updated["scheduler"]["block_retry_cooldown_minutes"] == 45
    assert updated["scheduler"]["block_retry_max_attempts"] == 4


def test_retry_now_requires_review_for_halted_source(client, admin_headers):
    from scraper.database import SessionLocal, run_async

    async def mark_halted():
        async with SessionLocal() as db:
            src = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "NasirLawSite"))).scalars().first()
            src.state = "HALTED"
            src.requires_admin_review = True
            await db.commit()

    run_async(mark_halted())
    assert (
        client.post(
            "/admin/sources/NasirLawSite/state",
            headers=admin_headers,
            json={"action": "retry_now", "reason": "quick retry"},
        ).status_code
        == 422
    )
    ok = client.post(
        "/admin/sources/NasirLawSite/state",
        headers=admin_headers,
        json={"action": "retry_now", "reviewed_by": "QA", "reason": "cooldown elapsed"},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["state"] == "ACTIVE"


async def test_public_pipeline_block_retry_cooldown(db, source, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/blocked", "<html>blocked</html>", status=403)
    source.config_json = {
        "auto_retry_on_block": True,
        "block_retry_cooldown_minutes": 30,
        "block_retry_max_attempts": 3,
    }
    await db.commit()

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await run_public_source(
            db,
            source,
            seed_listings=[{"url": fixture_server.url("/blocked"), "target_kind": "judgment"}],
            fetcher=fetcher,
            limit=1,
        )
    await db.commit()
    assert stats["blocked_cooldown"] is True
    assert source.state == "ACTIVE"
    assert source.next_scrape_at is not None
    assert (source.config_json or {}).get("block_retry", {}).get("count") == 1


async def test_backfill_progress_never_completes_without_configured_targets(db, monkeypatch):
    """At first boot the frontier is empty before any source has run, and the PakistanLawSite
    citation grid never uses the frontier: an empty frontier alone must not flip the mode."""
    from scraper.config import settings
    from scraper.harvest_mode import backfill_progress

    monkeypatch.setattr(settings, "BACKFILL_TARGET_JUDGMENTS", 0)
    monkeypatch.setattr(settings, "BACKFILL_TARGET_STATUTES", 0)
    progress = await backfill_progress(db, source_names=["PakistanLawSite", "PakistanCode"])
    assert progress["frontier_remaining"] == 0
    assert progress["complete"] is False
    assert progress["targets_configured"] is False
    assert "BACKFILL_TARGET" in (progress["auto_switch_blocked_reason"] or "")

    monkeypatch.setattr(settings, "BACKFILL_TARGET_JUDGMENTS", 1)
    progress = await backfill_progress(db, source_names=["PakistanLawSite"])
    assert progress["complete"] is False
    assert progress["targets_met"]["judgments"] is False


async def test_dispatch_does_not_auto_switch_to_updates_on_empty_frontier(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from scraper.config import settings
    from scraper.harvest_mode import get_harvest_mode, set_harvest_mode
    from scraper.tasks.celery_app import app
    from scraper.tasks.dispatcher import dispatch_due_sources

    monkeypatch.setattr(settings, "HARVEST_AUTO_SWITCH", True)
    monkeypatch.setattr(settings, "BACKFILL_TARGET_JUDGMENTS", 0)
    monkeypatch.setattr(settings, "BACKFILL_TARGET_STATUTES", 0)
    await set_harvest_mode(db, "backfill", changed_by="qa", reason="fresh install")
    now = datetime.now(timezone.utc)
    for source in (await db.execute(select(ScraperSource))).scalars().all():
        source.next_scrape_at = now + timedelta(hours=1)
    await db.commit()
    monkeypatch.setattr(app, "send_task", lambda *a, **k: None)
    result = await dispatch_due_sources()
    assert result["auto_switched"] is False
    assert result["mode"] == "backfill"
    assert await get_harvest_mode(db) == "backfill"
