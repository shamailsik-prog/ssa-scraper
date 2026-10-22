"""ensure_roles must rotate passwords on every call (R2 audit)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from scraper.config import settings
from scraper.database import engine, ensure_roles


async def _role_password_hash(role_name: str) -> str | None:
    async with engine.begin() as conn:
        row = await conn.execute(
            text("SELECT rolpassword FROM pg_authid WHERE rolname = :r"),
            {"r": role_name},
        )
        return row.scalar()


@pytest.mark.asyncio
async def test_ensure_roles_rotates_writer_password(monkeypatch):
    monkeypatch.setattr(settings, "CORPUS_WRITER_PASSWORD", SecretStr("writer-rotate-a"))
    await ensure_roles()
    first = await _role_password_hash(settings.CORPUS_WRITER_ROLE)
    assert first

    monkeypatch.setattr(settings, "CORPUS_WRITER_PASSWORD", SecretStr("writer-rotate-b"))
    await ensure_roles()
    second = await _role_password_hash(settings.CORPUS_WRITER_ROLE)
    assert second
    assert first != second
