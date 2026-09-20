from __future__ import annotations

from sqlalchemy import select

from scraper.database import PAKISTANCODE_CRAWL_MAX_PAGES, seed_data
from scraper.models import ScraperSource
from scraper.tasks.pakistancode import (
    PC_CRAWL_MAX_PAGES,
    PC_DOCUMENT_PRIORITY,
    PC_LISTING_PRIORITY,
)


def test_pakistancode_priority_constants_ordered():
    assert PC_DOCUMENT_PRIORITY < PC_LISTING_PRIORITY
    assert PC_CRAWL_MAX_PAGES == 200
    assert PAKISTANCODE_CRAWL_MAX_PAGES == PC_CRAWL_MAX_PAGES


async def test_pakistancode_seed_sets_crawl_max_pages_to_200(db):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))
    ).scalars().first()
    assert source is not None
    assert source.crawl_max_pages == PC_CRAWL_MAX_PAGES


async def test_pakistancode_seed_bumps_existing_lower_crawl_max_pages(db):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))
    ).scalars().first()
    assert source is not None
    source.crawl_max_pages = 80
    await db.commit()

    await seed_data()

    refreshed = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))
    ).scalars().first()
    assert refreshed is not None
    assert refreshed.crawl_max_pages == PC_CRAWL_MAX_PAGES
