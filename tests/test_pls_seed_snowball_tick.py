"""Scheduled capped snowball seeding for PLS search harvest (DB-only, maintenance queue)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from scraper.config import settings
from scraper.deploy_role import LOGIN_ONLY_BEAT_ENTRIES
from scraper.tasks.celery_app import pls_seed_snowball_beat_entry
from scraper.tasks.pls_search_harvest import SEED_SNOWBALL_TICK_HARD_CAP, pls_seed_snowball_tick, seed_snowball


def test_pls_seed_snowball_tick_clamps_limit_to_hard_cap():
    seen = {}

    async def _fake(db, *, dry_run=False, max_queries=None, **kwargs):
        seen["max_queries"] = max_queries
        return {"queries_added": 0}

    with patch("scraper.tasks.pls_search_harvest.seed_snowball", new=_fake):
        pls_seed_snowball_tick(limit=999)
    assert seen["max_queries"] == SEED_SNOWBALL_TICK_HARD_CAP


async def test_seed_snowball_stops_after_max_queries(db, monkeypatch):
    from scraper.models import Judgment, PlsSearchHarvestQuery

    for n in range(5):
        db.add(
            Judgment(
                canonical_citation=f"2000 PLD {100 + n}",
                source_name="PakistanLawSite",
                source_url=f"https://example.test/j/{n}",
                full_text=f"Cited as 2000 PLD {100 + n}",
            )
        )
    await db.flush()
    fresh = [f"2000 PLD {100 + n}" for n in range(5, 10)]

    async def _known(_db, keys):
        return set()

    monkeypatch.setattr(
        "scraper.tasks.pls_search_harvest.load_known_citation_keys",
        _known,
    )
    monkeypatch.setattr(
        "scraper.tasks.pls_search_harvest.cited_citations_in_text",
        lambda text: set(fresh) if "Cited as" in text else set(),
    )

    out = await seed_snowball(db, dry_run=False, limit=5, max_queries=2)
    assert out["queries_added"] == 2
    assert out.get("truncated_at_max_queries") == 2
    count = (
        await db.execute(
            select(func.count())
            .select_from(PlsSearchHarvestQuery)
            .where(PlsSearchHarvestQuery.source_name == "PakistanLawSite")
        )
    ).scalar()
    assert count == 2


def test_pls_seed_snowball_beat_entry_when_harvest_enabled():
    entry = pls_seed_snowball_beat_entry()["pls-seed-snowball"]
    assert entry["task"] == "scraper.tasks.pls_search_harvest.pls_seed_snowball_tick"
    assert entry["schedule"] == 86400
    assert entry["kwargs"] == {"limit": 200}


def test_pls_seed_snowball_beat_key_registered_with_search_harvest(monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_ENABLED", True)
    import importlib

    from scraper.tasks import celery_app

    importlib.reload(celery_app)
    assert "pls-seed-snowball" in celery_app.app.conf.beat_schedule
    assert celery_app.app.conf.beat_schedule["pls-seed-snowball"]["kwargs"]["limit"] == 200


def test_pls_seed_snowball_is_login_only_beat_entry():
    assert "pls-seed-snowball" in LOGIN_ONLY_BEAT_ENTRIES


def test_pls_seed_snowball_routes_to_maintenance_queue():
    from scraper.tasks.celery_app import app

    assert app.conf.task_routes["scraper.tasks.pls_search_harvest.pls_seed_snowball_tick"] == {"queue": "maintenance"}
