"""
Celery application. Queues:
  * scraper        — public sources (worker-public)
  * login_session  — PakistanLawSite, concurrency from LOGIN_SESSION_CONCURRENCY (1–2; reporter shards)
  * embeddings     — worker-embed
  * maintenance    — promotion, treatment, archive mirror, reconcile, dispatch (worker-maintenance:
                     never share this queue with long scrape runs, or dispatch and promotion stall
                     behind them)
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
        "scraper.tasks.promotion.reconcile_instrument_relations": {"queue": "maintenance"},
        "scraper.tasks.treatment.classify_treatment": {"queue": "maintenance"},
        "scraper.tasks.treatment.reconcile_treatment_citation_links": {"queue": "maintenance"},
        "scraper.tasks.treatment.reconcile_judgment_citation_relations": {"queue": "maintenance"},
        "scraper.tasks.archive_mirror.mirror_pending": {"queue": "maintenance"},
        "scraper.tasks.archive_mirror.reconcile_storage": {"queue": "maintenance"},
        "scraper.tasks.dispatcher.dispatch_due_sources": {"queue": "maintenance"},
    },
    beat_schedule={
        "dispatch-due-sources": {"task": "scraper.tasks.dispatcher.dispatch_due_sources", "schedule": settings.DISPATCH_LOOP_SECONDS},
        # Promotion must keep up with a continuous login-session harvest (hundreds of staged rows per
        # hour): 500 rows every 5 minutes, on its own worker (see worker-maintenance in docker-compose.yml).
        "promote-staging": {"task": "scraper.tasks.promotion.promote_staging_records", "schedule": 300, "kwargs": {"limit": 500}},
        "reconcile-instrument-relations": {
            "task": "scraper.tasks.promotion.reconcile_instrument_relations",
            "schedule": settings.INSTRUMENT_RELATION_RECONCILE_SCHEDULE_SECONDS,
        },
        "classify-treatment-nightly": {"task": "scraper.tasks.treatment.classify_treatment", "schedule": 86400},
        "process-embeddings": {"task": "scraper.tasks.embeddings.process_embedding_queue", "schedule": 300},
        "archive-mirror": {"task": "scraper.tasks.archive_mirror.mirror_pending", "schedule": 1800},
        "reconcile-storage": {"task": "scraper.tasks.archive_mirror.reconcile_storage", "schedule": 86400},
    },
)
if settings.TREATMENT_RECONCILE_ENABLED:
    app.conf.beat_schedule["reconcile-treatment-citation-links"] = {
        "task": "scraper.tasks.treatment.reconcile_treatment_citation_links",
        "schedule": settings.TREATMENT_RECONCILE_INTERVAL_SECONDS,
        "kwargs": {
            "lookback_hours": settings.TREATMENT_RECONCILE_LOOKBACK_HOURS,
            "batch_size": settings.TREATMENT_RECONCILE_BATCH_SIZE,
        },
    }
if settings.JUDGMENT_CITATION_RECONCILE_ENABLED:
    app.conf.beat_schedule["reconcile-judgment-citation-relations"] = {
        "task": "scraper.tasks.treatment.reconcile_judgment_citation_relations",
        "schedule": settings.JUDGMENT_CITATION_RECONCILE_INTERVAL_SECONDS,
        "kwargs": {
            "lookback_hours": settings.JUDGMENT_CITATION_RECONCILE_LOOKBACK_HOURS,
            "batch_size": settings.JUDGMENT_CITATION_RECONCILE_BATCH_SIZE,
        },
    }
logger.info("Celery configured: queues scraper/login_session/embeddings/maintenance; beat %s", list(app.conf.beat_schedule))
