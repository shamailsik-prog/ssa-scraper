"""
Event-driven corpus chain: fetch → promote → archive mirror, without waiting for Beat.

A fetched record is only staged (status "extracted"); it reaches the corpus when
promote_staging_records runs and Google Drive when mirror_pending runs. Both used to run only
from Beat, so with celery-beat down fetched records piled up unsaved (151 on 8 October 2026).
Now a committed fetch batch asks for a promotion run, and a promotion run that saved something
asks for a mirror run. The Beat entries stay as the safety net.

Requests are debounced with Redis SET NX EX: at most one task per key is enqueued per window,
however many rows a job stages. The countdown equals the window, so a request made after the
key expired (the queued run is due or already running) enqueues the next run and no committed
row waits for Beat. A request never raises into the caller: a scraper must not fail because
Redis or the broker is unreachable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from scraper.config import settings

logger = logging.getLogger(__name__)

PROMOTE_KEY_PREFIX = "corpus:chain:promote:"
MIRROR_KEY = "corpus:chain:mirror"
PROMOTE_WINDOW_SECONDS = 30
MIRROR_WINDOW_SECONDS = 60
PROMOTE_TASK = "scraper.tasks.promotion.promote_staging_records"
MIRROR_TASK = "scraper.tasks.archive_mirror.mirror_pending"
QUEUE = "maintenance"  # the queue Beat's promote-staging and archive-mirror entries route to
BATCH_LIMIT = 200  # same batch size as the Beat entries

_PENDING_INFO_KEY = "corpus_chain_promote_sources"
_LISTENER_INFO_KEY = "corpus_chain_listener"
_client = None


def _redis():
    """Sync client (Celery's send_task is sync too); short timeouts so a dead Redis costs seconds, not a job."""
    global _client
    if _client is None:
        import redis

        _client = redis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
    return _client


def _request(key: str, window: int, task_name: str, kwargs: Dict[str, Any]) -> bool:
    """Enqueue `task_name` unless a request for `key` is already pending. True when a task was sent."""
    if not settings.CORPUS_CHAIN_ENABLED:
        return False
    client = None
    try:
        client = _redis()
        if not client.set(key, datetime.now(timezone.utc).isoformat(), nx=True, ex=window):
            return False
        from scraper.tasks.celery_app import app

        app.send_task(task_name, kwargs=kwargs, queue=QUEUE, countdown=window)
        return True
    except Exception as exc:
        logger.warning("chain: %s not enqueued (Beat will pick the work up): %s", task_name, exc)
        if client is not None:
            # The key was set but nothing was queued: free it so the next batch can try again.
            try:
                client.delete(key)
            except Exception:
                pass
        return False


def request_promotion(source_name: Optional[str] = None) -> bool:
    """Ask for a promote_staging_records run soon (pinned to `source_name` when given)."""
    kwargs: Dict[str, Any] = {"limit": BATCH_LIMIT}
    if source_name:
        kwargs["source_name"] = source_name
    return _request(PROMOTE_KEY_PREFIX + (source_name or "all"), PROMOTE_WINDOW_SECONDS, PROMOTE_TASK, kwargs)


def request_mirror() -> bool:
    """Ask for a mirror_pending run soon (Google Drive copy of newly promoted records)."""
    return _request(MIRROR_KEY, MIRROR_WINDOW_SECONDS, MIRROR_TASK, {"limit": BATCH_LIMIT})


def _on_commit(session) -> None:
    for source_name in sorted(session.info.pop(_PENDING_INFO_KEY, None) or ()):
        request_promotion(source_name)


def request_promotion_after_commit(db, source_name: str) -> None:
    """Request promotion once `db` commits the staging row just written (a promotion run started
    before the commit could not see it). Repeated calls before one commit collapse into one request."""
    try:
        sync = db.sync_session
        sync.info.setdefault(_PENDING_INFO_KEY, set()).add(source_name)
        if not sync.info.get(_LISTENER_INFO_KEY):
            from sqlalchemy import event

            event.listen(sync, "after_commit", _on_commit)
            sync.info[_LISTENER_INFO_KEY] = True
    except Exception as exc:
        logger.warning("chain: commit hook not installed for %s, requesting now: %s", source_name, exc)
        request_promotion(source_name)
