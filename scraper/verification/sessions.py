"""Database sessions for corpus verification (reader SELECT + verifier schema writes)."""

from __future__ import annotations

from typing import AsyncIterator, Optional

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from scraper.config import settings

_reader_engine: Optional[AsyncEngine] = None
_writer_engine: Optional[AsyncEngine] = None


def reader_engine() -> AsyncEngine:
    global _reader_engine
    if _reader_engine is None:
        password = settings.SIKANDER_READER_PASSWORD.get_secret_value()
        if not password:
            raise RuntimeError("SIKANDER_READER_PASSWORD is not configured")
        url = make_url(settings.DATABASE_URL).set(username=settings.SIKANDER_READER_ROLE, password=password)
        _reader_engine = create_async_engine(
            url,
            poolclass=NullPool,
            connect_args={
                "server_settings": {
                    "default_transaction_read_only": "on",
                    "statement_timeout": str(settings.CORPUS_VERIFIER_STATEMENT_TIMEOUT_MS),
                }
            },
        )
    return _reader_engine


def verifier_writer_engine() -> AsyncEngine:
    global _writer_engine
    if _writer_engine is None:
        password = settings.CORPUS_VERIFIER_PASSWORD.get_secret_value()
        role = settings.CORPUS_VERIFIER_ROLE
        if not password:
            password = settings.CORPUS_WRITER_PASSWORD.get_secret_value()
            role = settings.CORPUS_WRITER_ROLE
        url = make_url(settings.DATABASE_URL).set(username=role, password=password)
        _writer_engine = create_async_engine(url, poolclass=NullPool)
    return _writer_engine


async def open_reader_session() -> AsyncSession:
    factory = async_sessionmaker(reader_engine(), expire_on_commit=False, class_=AsyncSession)
    return factory()


async def open_verifier_writer_session() -> AsyncSession:
    factory = async_sessionmaker(verifier_writer_engine(), expire_on_commit=False, class_=AsyncSession)
    return factory()
