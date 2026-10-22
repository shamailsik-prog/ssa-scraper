"""Exclude document_quarantine rows from agent search, admin read APIs, exports, and embeddings."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import DocumentQuarantine, Judgment, Statute

DOC_TYPE_JUDGMENT = "judgment"
DOC_TYPE_STATUTE = "statute"


def judgment_servable() -> ColumnElement[bool]:
    return ~exists(
        select(1).where(
            DocumentQuarantine.doc_type == DOC_TYPE_JUDGMENT,
            DocumentQuarantine.doc_id == Judgment.id,
        )
    )


def statute_servable() -> ColumnElement[bool]:
    return ~exists(
        select(1).where(
            DocumentQuarantine.doc_type == DOC_TYPE_STATUTE,
            DocumentQuarantine.doc_id == Statute.id,
        )
    )


async def is_document_quarantined(db: AsyncSession, doc_type: str, doc_id: UUID) -> bool:
    row = (
        await db.execute(
            select(DocumentQuarantine.doc_id).where(
                DocumentQuarantine.doc_type == doc_type,
                DocumentQuarantine.doc_id == doc_id,
            ).limit(1)
        )
    ).first()
    return row is not None
