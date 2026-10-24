"""CLI persistence for pls_admin search-harvest-plan."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from scraper.database import SessionLocal, run_async
from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import make_query_key, normalize_query_json
from scraper.tasks import pls_search_harvest as harvest


@pytest.fixture
def _plan_seed_env(monkeypatch):
    year = datetime.now(timezone.utc).year
    reporter = "PLAN_TEST_REP"
    monkeypatch.setattr(harvest.settings, "PLS_EARLIEST_YEAR", year)
    monkeypatch.setattr(harvest.settings, "PLS_SUBSCRIBED_REPORTERS", reporter)

    async def _no_map(_db, _source):
        return None

    monkeypatch.setattr(harvest, "active_map", _no_map)
    query_json = normalize_query_json({"reporter": reporter, "year": year})
    return make_query_key(query_json)


async def _delete_plan_key(key: str) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.query_key == key))
        await db.commit()


async def _fetch_plan_key(key: str):
    async with SessionLocal() as db:
        return (await db.execute(select(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.query_key == key))).scalar_one_or_none()


def test_search_harvest_plan_dry_run_does_not_persist(_plan_seed_env):
    key = _plan_seed_env
    run_async(_delete_plan_key(key))

    assert harvest.cli_plan([]) == 0
    assert run_async(_fetch_plan_key(key)) is None


def test_search_harvest_plan_apply_persists_in_fresh_session(_plan_seed_env):
    key = _plan_seed_env
    run_async(_delete_plan_key(key))

    assert harvest.cli_plan(["--apply"]) == 0

    row = run_async(_fetch_plan_key(key))
    assert row is not None
    assert row.status == "pending"

    run_async(_delete_plan_key(key))
