"""
Celery application. Queues:
  * scraper        — public sources (worker-scraper)
  * login_session  — PakistanLawSite, concurrency 1, one worker (worker-scraper runs it with -c 1)
  * embeddings     — worker-embed
  * maintenance    — promotion, treatment, archive mirror, reconcile, dispatch
Beat owns scheduling; ScrapeGraph 'monitor' jobs are supplemental only and never replace it.
"""

from __future__ import annotations

import logging

from celery import Celery

from scraper.config import settings

logger = logging.getLogger(__name__)

app = Celery(
    "sikander_corpus",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "scraper.tasks.dispatcher",
        "scraper.tasks.promotion",
        "scraper.tasks.treatment",
        "scraper.tasks.embeddings",
        "scraper.tasks.archive_mirror",
    ],
)
app.conf.update(
    timezone=settings.TIMEZONE,
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=3 * 3600,
    task_soft_time_limit=3 * 3600 - 300,
    task_default_queue="scraper",
    task_routes={
        "scraper.tasks.dispatcher.run_login_session_job": {"queue": "login_session"},
        "scraper.tasks.dispatcher.run_source_job": {"queue": "scraper"},
        "scraper.tasks.embeddings.process_embedding_queue": {"queue": "embeddings"},
        "scraper.tasks.promotion.promote_staging_records": {"queue": "maintenance"},
        "scraper.tasks.treatment.classify_treatment": {"queue": "maintenance"},
        "scraper.tasks.archive_mirror.mirror_pending": {"queue": "maintenance"},
        "scraper.tasks.archive_mirror.reconcile_storage": {"queue": "maintenance"},
        "scraper.tasks.dispatcher.dispatch_due_sources": {"queue": "maintenance"},
    },
    beat_schedule={
        "dispatch-due-sources": {"task": "scraper.tasks.dispatcher.dispatch_due_sources", "schedule": 1800},
        "promote-staging": {"task": "scraper.tasks.promotion.promote_staging_records", "schedule": 900},
        "classify-treatment-nightly": {"task": "scraper.tasks.treatment.classify_treatment", "schedule": 86400},
        "process-embeddings": {"task": "scraper.tasks.embeddings.process_embedding_queue", "schedule": 300},
        "archive-mirror": {"task": "scraper.tasks.archive_mirror.mirror_pending", "schedule": 1800},
        "reconcile-storage": {"task": "scraper.tasks.archive_mirror.reconcile_storage", "schedule": 86400},
    },
)
logger.info("Celery configured: queues scraper/login_session/embeddings/maintenance; beat %s", list(app.conf.beat_schedule))
