"""
Embeddings (Annex B-6). EMBEDDING_MODEL / EMBEDDING_DIM come from the environment, are written to
corpus_metadata on start-up, and the worker refuses to run when the recorded identity differs
from the configured one. Login-session rows are embedded externally only when the partner
decision EMBED_LOGIN_SESSION_ROWS_EXTERNALLY is true; otherwise they are skipped and reported.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
from celery import shared_task
from sqlalchemy import select

from scraper.config import settings
from scraper.database import SessionLocal, embedding_identity_matches, run_async
from scraper.models import EmbeddingQueue, Judgment, StatuteSection, StatuteSectionVersion
from scraper.security import is_login_session

logger = logging.getLogger(__name__)


class EmbeddingIdentityMismatch(RuntimeError):
    pass


async def embed_texts(texts: List[str], *, client: Optional[httpx.AsyncClient] = None) -> List[List[float]]:
    key = settings.OPENAI_API_KEY.get_secret_value() if settings.OPENAI_API_KEY else ""
    if not key:
        raise RuntimeError("OPENAI_API_KEY is NOT CONFIGURED")
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.OPENAI_TIMEOUT_SECONDS)
    try:
        r = await client.post("https://api.openai.com/v1/embeddings", headers={"Authorization": f"Bearer {key}"}, json={"model": settings.EMBEDDING_MODEL, "input": texts, "dimensions": settings.EMBEDDING_DIM})
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        vectors = [d["embedding"] for d in data]
        for v in vectors:
            if len(v) != settings.EMBEDDING_DIM:
                raise EmbeddingIdentityMismatch(f"model returned {len(v)} dimensions, expected {settings.EMBEDDING_DIM}")
        return vectors
    finally:
        if own:
            await client.aclose()


async def process_embedding_queue(limit: Optional[int] = None, *, client: Optional[httpx.AsyncClient] = None) -> Dict[str, int]:
    counts = {"embedded": 0, "skipped_login_session": 0, "failed": 0, "refused": 0}
    if not await embedding_identity_matches():
        logger.error("embedding identity mismatch: corpus_metadata differs from EMBEDDING_MODEL/EMBEDDING_DIM; refusing to run")
        counts["refused"] = 1
        return counts
    if not settings.OPENAI_API_KEY:
        logger.info("OPENAI_API_KEY NOT CONFIGURED; embeddings idle")
        return counts
    limit = limit or settings.EMBEDDING_BATCH_SIZE
    async with SessionLocal() as db:
        items = (await db.execute(select(EmbeddingQueue).where(EmbeddingQueue.status == "pending").order_by(EmbeddingQueue.created_at).limit(limit))).scalars().all()
        batch: List[tuple] = []
        for item in items:
            if is_login_session(item.access_method) and not settings.EMBED_LOGIN_SESSION_ROWS_EXTERNALLY:
                item.status = "skipped"
                item.error_message = "login_session text is not sent to an external embedding model"
                counts["skipped_login_session"] += 1
                continue
            text = None
            if item.table_name == "judgment":
                rec = (await db.execute(select(Judgment).where(Judgment.id == item.record_id))).scalars().first()
                if rec:
                    text = f"{rec.canonical_citation} {rec.case_title or ''} {(rec.full_text or '')[:6000]}"
            elif item.table_name == "statute_section":
                rec = (await db.execute(select(StatuteSection).where(StatuteSection.id == item.record_id))).scalars().first()
                if rec:
                    ver = (await db.execute(select(StatuteSectionVersion).where(StatuteSectionVersion.id == rec.current_version_id))).scalars().first()
                    text = f"{rec.section_number} {rec.section_title or ''} {(ver.section_text if ver else '')[:6000]}"
            if not text:
                item.status = "failed"
                item.error_message = "record not found"
                counts["failed"] += 1
                continue
            batch.append((item, rec, text))
        await db.commit()
        for i in range(0, len(batch), 16):
            chunk = batch[i : i + 16]
            try:
                vectors = await embed_texts([t for _, _, t in chunk], client=client)
            except EmbeddingIdentityMismatch:
                raise
            except Exception as exc:
                for item, _, _ in chunk:
                    item.attempts += 1
                    item.last_attempt_at = datetime.now(timezone.utc)
                    item.error_message = str(exc)[:500]
                    item.status = "failed" if item.attempts >= item.max_attempts else "pending"
                    counts["failed"] += 1
                await db.commit()
                continue
            for (item, rec, _), vec in zip(chunk, vectors):
                rec.embedding = vec
                item.status = "done"
                item.last_attempt_at = datetime.now(timezone.utc)
                counts["embedded"] += 1
            await db.commit()
            await asyncio.sleep(max(0.0, 60.0 / max(1, settings.EMBEDDING_RPM_LIMIT)))
    return counts


@shared_task(name="scraper.tasks.embeddings.process_embedding_queue")
def process_embedding_queue_task():
    return run_async(process_embedding_queue())
