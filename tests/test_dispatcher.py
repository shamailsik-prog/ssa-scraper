from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from scraper.models import ScraperJob, ScraperSource
from scraper.tasks.dispatcher import run_source


async def test_run_source_skips_when_existing_job_is_running(db):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "PakistanCode",
            )
        )
    ).scalars().first()
    assert source is not None

    running = ScraperJob(
        source_id=source.id,
        source_name=source.source_name,
        job_type="scrape",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(running)
    await db.commit()

    result = await run_source(source.source_name)
    assert result == {"skipped": "already_running", "job_id": str(running.id)}

    running_jobs = (
        await db.execute(
            select(func.count())
            .select_from(ScraperJob)
            .where(
                ScraperJob.source_name == source.source_name,
                ScraperJob.status == "running",
            )
        )
    ).scalar()
    assert running_jobs == 1
