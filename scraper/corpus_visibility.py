"""Exclude document_quarantine rows from agent search, admin read APIs, exports, and embeddings."""

from __future__ import annotations

from sqlalchemy import ColumnElement, exists, select

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
