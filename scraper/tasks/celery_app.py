"""
Celery application. Queues:
  * scraper        — public sources (worker-public)
  * login_session  — PakistanLawSite, one worker, concurrency 1 (worker-scraper)
  * embeddings     — worker-embed
  * maintenance    — promotion, treatment, archive mirror, reconcile, dispatch (worker-public)
Beat owns scheduling; ScrapeGraph 'monitor' jobs are supplemental only and never replace it.
"""

from __future__ import annotations

import logging

from celery import Celery
from celery.signals import task_postrun, task_prerun, worker_process_init, worker_process_shutdown, worker_ready

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
        "scraper.tasks.unreported_reader",
        "scraper.tasks.embeddings",
        "scraper.tasks.archive_mirror",
        "scraper.tasks.login_recovery",
        "scraper.tasks.spot_check",
        "scraper.tasks.pls_self_healing",
        "scraper.tasks.pls_search_harvest",
        "scraper.tasks.pls_caseid_walk",
        "scraper.tasks.pls_backfill",
        "scraper.tasks.judgment_identity_reconcile",
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
        "scraper.tasks.pls_backfill.requalify_quarantined": {"queue": "maintenance"},
        "scraper.tasks.promotion.reconcile_instrument_relations": {"queue": "maintenance"},
        "scraper.tasks.treatment.classify_treatment": {"queue": "maintenance"},
        "scraper.tasks.unreported_reader.read_unreported_batch": {"queue": "maintenance"},
        "scraper.tasks.treatment.reconcile_treatment_citation_links": {"queue": "maintenance"},
        "scraper.tasks.treatment.reconcile_judgment_citation_relations": {"queue": "maintenance"},
        "scraper.tasks.archive_mirror.mirror_pending": {"queue": "maintenance"},
        "scraper.tasks.archive_mirror.reconcile_storage": {"queue": "maintenance"},
        "scraper.tasks.dispatcher.dispatch_due_sources": {"queue": "maintenance"},
        # its own queue and worker: behind two back-to-back harvests every check expired unread (13 October 2026)
        "scraper.tasks.login_recovery.recover_login_slots": {"queue": "login_recovery"},
        "scraper.tasks.pls_self_healing.pls_keepalive_hourly": {"queue": "login_session"},
        "scraper.tasks.pls_self_healing.pls_stall_watchdog": {"queue": "login_session"},
        "scraper.tasks.pls_search_harvest.pls_search_harvest_tick": {"queue": "login_session"},
        "scraper.tasks.pls_caseid_walk.pls_caseid_walk_tick": {"queue": "login_session"},
        "scraper.tasks.spot_check.spot_check_judgments": {"queue": "login_session"},
        "scraper.tasks.spot_check.spot_check_statutes": {"queue": "maintenance"},
    },
    beat_schedule={
        "dispatch-due-sources": {"task": "scraper.tasks.dispatcher.dispatch_due_sources", "schedule": settings.DISPATCH_LOOP_SECONDS},
        "promote-staging": {"task": "scraper.tasks.promotion.promote_staging_records", "schedule": 900, "kwargs": {"limit": 200}},
        # PakistanLawSite judgments held for a reason a later extractor fix removes ("court unknown") are re-checked
        # from their stored text; no request goes to the site, and merits holds are left to a person.
        "pls-requalify-quarantined": {
            "task": "scraper.tasks.pls_backfill.requalify_quarantined",
            "schedule": 3600,
            "kwargs": {"limit": None, "skip_final": True},
        },
        # Operator decision of 24 September 2026: a lost slot is re-verified and, if dead, signed in
        # again with the saved credentials. Runs on worker-login-recovery; it waits only for its own login's
        # lock, so it never opens a second browser on a login that a job is using.
        # Every minute (owner's 3-minute re-login rule, 13 October 2026); a check still queued behind a running
        # harvest when the next one is due is dropped, so a busy worker never meets a pile of stale ones.
        "recover-login-slots": {
            "task": "scraper.tasks.login_recovery.recover_login_slots",
            "schedule": settings.LOGIN_RECOVERY_SCHEDULE_SECONDS,
            "options": {"expires": settings.LOGIN_RECOVERY_SCHEDULE_SECONDS},
        },
        "pls-keepalive": {
            "task": "scraper.tasks.pls_self_healing.pls_keepalive_hourly",
            "schedule": settings.PLS_KEEPALIVE_SCHEDULE_SECONDS,
        },
        "pls-stall-watchdog": {
            "task": "scraper.tasks.pls_self_healing.pls_stall_watchdog",
            "schedule": settings.PLS_STALL_WATCHDOG_SECONDS,
        },
        # Operator request of 24 September 2026: re-fetch a few promoted records and compare.
        "spot-check-judgments": {"task": "scraper.tasks.spot_check.spot_check_judgments", "schedule": settings.SPOT_CHECK_SCHEDULE_SECONDS},
        "spot-check-statutes": {"task": "scraper.tasks.spot_check.spot_check_statutes", "schedule": settings.SPOT_CHECK_SCHEDULE_SECONDS},
        "reconcile-instrument-relations": {
            "task": "scraper.tasks.promotion.reconcile_instrument_relations",
            "schedule": settings.INSTRUMENT_RELATION_RECONCILE_SCHEDULE_SECONDS,
        },
        "classify-treatment-nightly": {"task": "scraper.tasks.treatment.classify_treatment", "schedule": 86400},
        "read-unreported": {"task": "scraper.tasks.unreported_reader.read_unreported_batch", "schedule": settings.READER_SCHEDULE_SECONDS},
        "process-embeddings": {"task": "scraper.tasks.embeddings.process_embedding_queue", "schedule": 300},
        "archive-mirror": {"task": "scraper.tasks.archive_mirror.mirror_pending", "schedule": 1800},
        # hourly bounded slices (RECONCILE_BATCH_SIZE objects per target, least recently verified first)
        "reconcile-storage": {"task": "scraper.tasks.archive_mirror.reconcile_storage", "schedule": 3600},
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


def search_harvest_beat_entries() -> dict:
    """One search-harvest tick per login slot, each on its own reporter share (handbook R3.1; owner's decision of
    13 October 2026). No expiry (R3.7): a tick whose slot's harvest is already running exits at once, and an
    expiring tick queued behind a long job was revoked before it ran. A tick that worked queues its slot's next
    one itself (PLS_SEARCH_HARVEST_CONTINUE); the beat is the fallback that restarts a slot whose tick was skipped
    (its login briefly busy with another job), so it fires at least once per tick budget."""
    budget = int(settings.PLS_SEARCH_HARVEST_TICK_SECONDS or 0)
    schedule = min(settings.PLS_SEARCH_HARVEST_SCHEDULE_SECONDS, budget) if budget > 0 else settings.PLS_SEARCH_HARVEST_SCHEDULE_SECONDS
    return {
        name: {
            "task": "scraper.tasks.pls_search_harvest.pls_search_harvest_tick",
            "schedule": schedule,
            "kwargs": {"slot": slot},
        }
        for name, slot in (("pls-search-harvest", 1), ("pls-search-harvest-s2", 2))
    }


if settings.PLS_SEARCH_HARVEST_ENABLED:
    app.conf.beat_schedule.update(search_harvest_beat_entries())
if settings.PLS_CASEID_WALK_ENABLED:
    # two walks, one per login, each over its own half of the (year, court) groups (see pls_caseid_walk)
    for _name, _partition in (("pls-caseid-walk", 0), ("pls-caseid-walk-p1", 1)):
        app.conf.beat_schedule[_name] = {
            "task": "scraper.tasks.pls_caseid_walk.pls_caseid_walk_tick",
            "schedule": settings.PLS_CASEID_WALK_SCHEDULE_SECONDS,
            "kwargs": {"partition": _partition},
            # a tick that waited behind a long job is dropped, not stacked: the next one carries on
            "options": {"expires": settings.PLS_CASEID_WALK_SCHEDULE_SECONDS},
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
from scraper.deploy_role import beat_schedule_for_role, deploy_role  # noqa: E402

app.conf.beat_schedule = beat_schedule_for_role(app.conf.beat_schedule)
logger.info("Celery configured for deploy role %s: queues scraper/login_session/embeddings/maintenance; beat %s", deploy_role(), list(app.conf.beat_schedule))


@worker_process_init.connect
def _pls_worker_child_init(**_kwargs) -> None:
    """Prefork pool child: heartbeat daemon (lock release is worker_process_shutdown / task finally)."""
    from scraper.pls_worker_heartbeat import start_worker_heartbeat_daemon

    start_worker_heartbeat_daemon()


@task_prerun.connect
def _pls_lock_task_prerun(sender=None, task=None, **kwargs) -> None:
    """Any task that may hold a PLS Redis lock must refresh ``pls:worker_alive`` for its worker host."""
    from scraper.config import settings
    from scraper.pls_lock_celery import PLS_HEARTBEAT_TASKS
    from scraper.pls_lock_holder import configure_lock_host_from_task_hostname
    from scraper.pls_worker_heartbeat import start_worker_heartbeat_daemon

    if not settings.PLS_LOCK_HOLDER_IDENTITY:
        return
    name = getattr(sender, "name", None) if sender is not None else None
    if name not in PLS_HEARTBEAT_TASKS:
        return
    req = getattr(task, "request", None) if task is not None else None
    configure_lock_host_from_task_hostname(str(getattr(req, "hostname", "") or ""))
    start_worker_heartbeat_daemon()


@worker_process_shutdown.connect
def _release_pls_locks_on_worker_process_shutdown(**_kwargs) -> None:
    import signal

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    from scraper.pls_lock_shutdown import release_on_worker_process_shutdown

    release_on_worker_process_shutdown()


@task_postrun.connect
def _release_pls_harvest_locks_after_task(sender=None, **kwargs) -> None:
    name = getattr(sender, "name", None) if sender is not None else None
    if name != "scraper.tasks.pls_search_harvest.pls_search_harvest_tick":
        return
    from scraper.pls_lock_shutdown import release_on_worker_process_shutdown

    release_on_worker_process_shutdown()


@worker_ready.connect
def _retire_orphaned_login_jobs_at_start(sender=None, **_kwargs) -> None:
    """The login-session worker is the only process that runs login-session jobs: any job still
    recorded as running when it boots died with the previous container (deploy, crash). Retire
    those rows at once so the dispatcher can start the next job instead of waiting for the
    heartbeat cut-off."""
    hostname = str(getattr(sender, "hostname", "") or "")
    if not hostname.startswith("scraper-login@"):
        return
    from scraper.database import run_async
    from scraper.tasks.dispatcher import retire_orphaned_login_jobs

    try:
        retired = run_async(retire_orphaned_login_jobs())
        logger.info("login-session worker start: retired %s orphaned running job(s)", retired)
    except Exception as exc:  # never keep the worker from starting
        logger.warning("could not retire orphaned login-session jobs at start: %s", exc)
