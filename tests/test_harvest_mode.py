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
