"""
Content-hash cache for AI extraction results (Amendment §8, §21).

Key: (content_hash, schema_version, extraction_type, engine_mode). A page whose hash and schema
version are unchanged never spends a second managed call.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.models import ScrapegraphCache

logger = logging.getLogger(__name__)


async def cache_get(db: AsyncSession, *, content_hash: str, schema_version: int, extraction_type: str, engine_mode: str) -> Optional[Dict[str, Any]]:
    if not settings.SGAI_CACHE_ENABLED:
        return None
    row = (
        await db.execute(
            select(ScrapegraphCache).where(
                ScrapegraphCache.content_hash == content_hash,
                ScrapegraphCache.schema_version == schema_version,
                ScrapegraphCache.extraction_type == extraction_type,
                ScrapegraphCache.engine_mode == engine_mode,
            )
        )
    ).scalars().first()
    return dict(row.result_json) if row is not None else None


async def cache_put(db: AsyncSession, *, content_hash: str, schema_version: int, extraction_type: str, engine_mode: str, result: Dict[str, Any]) -> None:
    if not settings.SGAI_CACHE_ENABLED:
        return
    existing = await cache_get(db, content_hash=content_hash, schema_version=schema_version, extraction_type=extraction_type, engine_mode=engine_mode)
    if existing is not None:
        return
    db.add(ScrapegraphCache(content_hash=content_hash, schema_version=schema_version, extraction_type=extraction_type, engine_mode=engine_mode, result_json=result))
    await db.flush()


async def cache_invalidate_schema(db: AsyncSession, schema_version: int) -> int:
    res = await db.execute(delete(ScrapegraphCache).where(ScrapegraphCache.schema_version < schema_version))
    return res.rowcount or 0
