"""Exclude corpus_quarantined rows from agent search, admin read APIs, exports, and embeddings."""

from __future__ import annotations

from sqlalchemy import ColumnElement

from scraper.models import Judgment, Statute


def judgment_servable() -> ColumnElement[bool]:
    return Judgment.corpus_quarantined.is_(False)


def statute_servable() -> ColumnElement[bool]:
    return Statute.corpus_quarantined.is_(False)
