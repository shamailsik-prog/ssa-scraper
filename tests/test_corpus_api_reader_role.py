"""corpus_api_reader: dedicated /corpus/v1 DB role, distinct from corpus_writer."""

from __future__ import annotations

import asyncpg
import pytest

from scraper.config import settings
from scraper.database import ensure_roles


def _dsn(user: str, pw: str) -> str:
    url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "")
    hostpart = url.split("@", 1)[1]
    return f"postgresql://{user}:{pw}@{hostpart}"


def test_corpus_api_reader_role_name_differs_from_writer():
    assert settings.CORPUS_API_READER_ROLE != settings.CORPUS_WRITER_ROLE
    assert settings.CORPUS_API_READER_ROLE != settings.SIKANDER_READER_ROLE


async def test_corpus_api_reader_can_read_judgment_full_text(db):
    await ensure_roles()
    conn = await asyncpg.connect(
        _dsn(settings.CORPUS_API_READER_ROLE, settings.CORPUS_API_READER_PASSWORD.get_secret_value())
    )
    try:
        await conn.fetch("SELECT full_text FROM judgment LIMIT 1")
        await conn.fetch('SELECT * FROM "treatment" LIMIT 1')
    finally:
        await conn.close()


async def test_corpus_api_reader_cannot_read_internal_tables(db):
    await ensure_roles()
    conn = await asyncpg.connect(
        _dsn(settings.CORPUS_API_READER_ROLE, settings.CORPUS_API_READER_PASSWORD.get_secret_value())
    )
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT 1 FROM scraper_staging LIMIT 1")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("INSERT INTO corpus_metadata(key, value) VALUES ('x', 'y')")
    finally:
        await conn.close()
