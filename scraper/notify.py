"""Operator notifications (Amendment §9: 'notify'). Rows appear on the dashboard until acknowledged."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import Notification
from scraper.security import scrub_secrets

logger = logging.getLogger("scraper.notify")


async def notify(db: AsyncSession, *, level: str, code: str, message: str, source_name: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> Notification:
    msg = scrub_secrets(message)[:2000]
    row = Notification(level=level, code=code, message=msg, source_name=source_name, details=details or {})
    db.add(row)
    await db.flush()
    log = {"critical": logger.critical, "error": logger.error, "warning": logger.warning}.get(level, logger.info)
    log("NOTIFY [%s] %s: %s", code, source_name or "-", msg)
    return row
