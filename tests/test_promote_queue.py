"""Dedicated promote queue, classify_treatment soft limit, promotion stale alert."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.celery_queues import PROMOTE_QUEUE, promote_task_queue
from scraper.config import settings
from scraper.models import Judgment, Notification, ScraperStaging
from scraper.tasks.celery_app import app
from scraper.tasks.promotion_health import NOTIFICATION_CODE, evaluate_promotion_stale_alert
from scraper.tasks.treatment import classify_treatment_task


def test_promote_staging_records_routes_to_promote_queue():
    assert promote_task_queue() == PROMOTE_QUEUE
    assert app.conf.task_routes["scraper.tasks.promotion.promote_staging_records"]["queue"] == PROMOTE_QUEUE


def test_promote_routes_to_maintenance_when_dedicated_queue_disabled(monkeypatch):
    monkeypatch.setattr(settings, "SSA_PROMOTE_DEDICATED_QUEUE_ENABLED", False)
    from scraper.celery_queues import promote_task_queue as pq

    assert pq() == "maintenance"


@pytest.mark.asyncio
async def test_promotion_stale_alert_fires_when_extracted_waiting(db, login_source, monkeypatch):
    from scraper.fetchers import record_provenance

    monkeypatch.setattr(settings, "PROMOTION_STALE_ALERT_ENABLED", True)
    monkeypatch.setattr(settings, "PROMOTION_STALE_PROMOTION_MINUTES", 15.0)
    monkeypatch.setattr(settings, "PROMOTION_STALE_EXTRACTED_MIN_AGE_MINUTES", 10.0)
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=30)
    prov = await record_provenance(
        db,
        source=login_source,
        url="https://www.pakistanlawsite.com/test/stale-promote",
        content=b"body",
        content_kind="html",
    )
    db.add(
        ScraperStaging(
            id=uuid4(),
            source_name="PakistanLawSite",
            access_method="rendered_copy",
            provenance_id=prov.id,
            content_hash="abc" + str(uuid4())[:8],
            status="extracted",
            confidence_score=0.9,
            created_at=old,
            updated_at=old,
        )
    )
    j = Judgment(
        id=uuid4(),
        source_name="PakistanLawSite",
        court_name="Test",
        canonical_citation="PLD 2020 SC 1",
        case_title="Stale promotion test",
        full_text="x" * 100,
        full_text_hash="h",
        judge_names=["J"],
        access_method="rendered_copy",
        promoted_at=now - timedelta(minutes=20),
    )
    db.add(j)
    await db.flush()
    verdict = await evaluate_promotion_stale_alert(db, now=now)
    assert verdict["alert"] is True
    assert verdict["pending_extracted_rows"] >= 1
    note = (
        await db.execute(select(Notification).where(Notification.code == NOTIFICATION_CODE))
    ).scalars().first()
    assert note is not None


def test_classify_treatment_task_catches_soft_time_limit(monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    def boom(coro):
        if hasattr(coro, "close"):
            coro.close()
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr("scraper.tasks.treatment.run_async", boom)
    out = classify_treatment_task(limit=5)
    assert out.get("soft_time_limit") is True
    assert out.get("limit") == 5


@pytest.mark.timeout(90)
def test_blocked_maintenance_worker_does_not_delay_promote_task():
    """Two workers (maintenance + promote): a slow classify must not block promote_staging_records."""
    if not os.environ.get("REDIS_URL"):
        pytest.skip("REDIS_URL not set")
    maint_env = {**os.environ, "CLASSIFY_TREATMENT_TEST_SLEEP_SECONDS": "25"}
    base_cmd = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "scraper.tasks.celery_app",
        "worker",
        "--loglevel=WARNING",
        "--pool=solo",
    ]
    maint = subprocess.Popen(
        [*base_cmd, "-Q", "maintenance", "--concurrency=1", "-n", "testmaint@%h"],
        env=maint_env,
    )
    promote = subprocess.Popen(
        [*base_cmd, "-Q", "promote", "--concurrency=1", "-n", "testpromote@%h"],
        env=os.environ.copy(),
    )
    try:
        time.sleep(8)
        from scraper.tasks.promotion import promote_staging_records_task
        from scraper.tasks.treatment import classify_treatment_task

        classify_treatment_task.apply_async(kwargs={"limit": 1}, queue="maintenance")
        promote_async = promote_staging_records_task.apply_async(kwargs={"limit": 0}, queue="promote")
        promote_async.get(timeout=20)
    finally:
        for proc in (promote, maint):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
