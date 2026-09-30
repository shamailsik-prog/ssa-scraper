"""The /status dashboard's health, speed and progress sections, and the heartbeats behind them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scraper.dashboard_metrics import health_section


def test_status_json_has_health_speed_progress(client):
    body = client.get("/status.json").json()
    assert body["health"]["overall"] in ("ok", "warn", "down")
    names = {c["name"] for c in body["health"]["checks"]}
    assert {"Database", "Redis (task queue)", "Scheduler (Celery Beat) and dispatch", "Housekeeping worker"} <= names
    assert set(body["speed"]) >= {"15m", "1h", "24h", "by_source_last_hour"}
    assert "pages_per_hour" in body["speed"]["1h"]
    assert set(body["progress"]) == {"pakistanlawsite_journals", "public_sources", "archive"}
    assert body["sources_summary"]["total"] >= 1


def test_status_page_is_served(client):
    html = client.get("/status").text
    assert "Everything connected?" in html and "Speed right now" in html and "/status.json" in html


async def test_heartbeat_roundtrip_and_stale_light():
    from scraper.heartbeat import _PREFIX, beat, read_all

    import redis.asyncio as aioredis
    from scraper.config import settings

    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete(_PREFIX + "dispatch")
    await beat("dispatch")
    beats = await read_all()
    assert "dispatch" in beats
    # a heartbeat an hour old turns the scheduler light red (limit five minutes)
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await r.set(_PREFIX + "dispatch", old)
    await r.aclose()
    out = await health_section(sources=[{"source": "PakistanLawSite", "state": "ACTIVE", "slots": []}], targets=[], now=datetime.now(timezone.utc))
    sched = next(c for c in out["checks"] if c["name"].startswith("Scheduler"))
    assert sched["state"] == "down"
    assert out["overall"] == "down"
