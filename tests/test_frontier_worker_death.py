"""A frontier item that kills the worker is not retried forever (16 October 2026).

A Senate PDF drove the public worker out of memory; the kernel killed it mid-item. Everything the
run had not committed rolled back, including the attempt _drain_one had counted, so the item came
back as 'pending' with attempts=0 and killed the worker again, every six hours, and the Senate
source never moved past it. The claim is now committed before the item is processed, a row still
'in_progress' when claimed is a run that died on it, and after WORKER_DEATH_RETIRE_AFTER such deaths
the row is retired.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scraper.database import SessionLocal
from scraper.models import CrawlFrontier, ScraperSource
from scraper.tasks import public_pipeline
from scraper.tasks.public_pipeline import WORKER_DEATH_RETIRE_AFTER, PublicPipeline

POISON_URL = "https://senate.gov.pk/uploads/documents/poison.pdf"


class _WorkerKilled(BaseException):
    """Stands in for SIGKILL: no `except Exception` or cleanup in the pipeline sees it."""


class _KillingFetcher:
    async def get(self, url):
        raise _WorkerKilled(url)


class _NoFetchExpected:
    async def get(self, url):
        raise AssertionError(f"a retired item must not be fetched: {url}")


async def _poison_row(db, source) -> object:
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"statute:{POISON_URL}",
        query_json={"kind": "statute", "url": POISON_URL},
        cursor_json={},
        priority=50,
    )
    db.add(row)
    await db.commit()
    return row.id


async def _run(source_name: str, fetcher) -> PublicPipeline:
    """One worker run in its own session, as a restarted worker would have."""
    async with SessionLocal() as session:
        src = (await session.execute(select(ScraperSource).where(ScraperSource.source_name == source_name))).scalars().one()
        pipeline = PublicPipeline(session, src, fetcher=fetcher)
        try:
            await pipeline.drain(limit=5)
        except _WorkerKilled:
            await session.rollback()  # what the killed worker's open transaction amounts to
            raise
        return pipeline


async def _die_on_the_item(source_name: str) -> None:
    with pytest.raises(_WorkerKilled):
        await _run(source_name, _KillingFetcher())


async def _stored(row_id) -> CrawlFrontier:
    async with SessionLocal() as other:
        return (await other.execute(select(CrawlFrontier).where(CrawlFrontier.id == row_id))).scalars().one()


async def test_claim_is_committed_before_the_item_is_processed(db, source):
    row_id = await _poison_row(db, source)
    await _die_on_the_item(source.source_name)
    assert (await _stored(row_id)).status == "in_progress"


async def test_item_that_kills_the_worker_is_retired_after_the_limit(db, source):
    row_id = await _poison_row(db, source)
    for _ in range(WORKER_DEATH_RETIRE_AFTER):
        await _die_on_the_item(source.source_name)
        assert (await _stored(row_id)).status == "in_progress"

    pipeline = await _run(source.source_name, _NoFetchExpected())

    row = await _stored(row_id)
    assert row.status == "retired"
    assert row.attempts == WORKER_DEATH_RETIRE_AFTER
    assert "worker died" in (row.last_error or "")
    assert pipeline.stats["retired_after_worker_death"] == 1


def _in_container(monkeypatch, name: str) -> None:
    monkeypatch.setattr(public_pipeline, "_container_id", lambda: name)


async def test_a_deploy_that_replaces_the_container_is_not_a_worker_death(db, source, monkeypatch):
    """A deploy recreates the worker container and kills whatever item it was on: that says nothing about the item.
    Counting it retired long OCR items after a few deploys; the free-sites worker was recreated twice in three
    minutes on 16 October 2026. A death in the same container (out of memory, SIGKILL, time limit) still counts:
    Docker restarts a crashed container in place, under the same id."""
    row_id = await _poison_row(db, source)
    for n in range(WORKER_DEATH_RETIRE_AFTER + 1):
        _in_container(monkeypatch, f"container-{n}")  # each run in a container a deploy has just replaced
        await _die_on_the_item(source.source_name)

    row = await _stored(row_id)
    assert row.status == "in_progress"
    assert row.attempts == 0


async def test_deaths_in_one_container_still_retire_the_item(db, source, monkeypatch):
    row_id = await _poison_row(db, source)
    _in_container(monkeypatch, "container-a")
    for _ in range(WORKER_DEATH_RETIRE_AFTER):
        await _die_on_the_item(source.source_name)

    await _run(source.source_name, _NoFetchExpected())

    assert (await _stored(row_id)).status == "retired"


async def test_a_row_claimed_before_claims_named_their_container_is_not_counted(db, source, monkeypatch):
    """Rows left 'in_progress' by the release before this one carry no container; the deploy that brings this
    release replaces the container anyway, so their death is not counted."""
    row_id = await _poison_row(db, source)
    async with SessionLocal() as other:
        row = (await other.execute(select(CrawlFrontier).where(CrawlFrontier.id == row_id))).scalars().one()
        row.status = "in_progress"
        await other.commit()
    _in_container(monkeypatch, "container-new")

    await _die_on_the_item(source.source_name)

    row = await _stored(row_id)
    assert row.attempts == 0
    assert row.cursor_json.get("claimed_by") == "container-new"
