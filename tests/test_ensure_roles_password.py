"""ensure_roles must rotate passwords on every call (R2 audit)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from scraper.config import settings
from scraper.database import engine, ensure_roles
from tests.postgres_catalog_probe import drop_owned_role


async def _role_password_hash(role_name: str) -> str | None:
    async with engine.begin() as conn:
        row = await conn.execute(
            text("SELECT rolpassword FROM pg_authid WHERE rolname = :r"),
            {"r": role_name},
        )
        return row.scalar()


@pytest.mark.asyncio
async def test_ensure_roles_rotates_writer_password(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    writer = f"probe_writer_{suffix}"
    monkeypatch.setattr(settings, "CORPUS_WRITER_ROLE", writer)
    monkeypatch.setattr(settings, "SIKANDER_READER_PASSWORD", SecretStr(""))
    try:
        monkeypatch.setattr(settings, "CORPUS_WRITER_PASSWORD", SecretStr("writer-rotate-a"))
        await ensure_roles()
        first = await _role_password_hash(writer)
        assert first

        monkeypatch.setattr(settings, "CORPUS_WRITER_PASSWORD", SecretStr("writer-rotate-b"))
        await ensure_roles()
        second = await _role_password_hash(writer)
        assert second
        assert first != second
    finally:
        async with engine.begin() as conn:
            await drop_owned_role(conn, writer)


@pytest.mark.asyncio
async def test_ensure_roles_rotates_reader_password(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    reader = f"probe_reader_{suffix}"
    monkeypatch.setattr(settings, "SIKANDER_READER_ROLE", reader)
    monkeypatch.setattr(settings, "CORPUS_WRITER_PASSWORD", SecretStr(""))
    try:
        monkeypatch.setattr(settings, "SIKANDER_READER_PASSWORD", SecretStr("reader-rotate-a"))
        await ensure_roles()
        first = await _role_password_hash(reader)
        assert first

        monkeypatch.setattr(settings, "SIKANDER_READER_PASSWORD", SecretStr("reader-rotate-b"))
        await ensure_roles()
        second = await _role_password_hash(reader)
        assert second
        assert first != second
    finally:
        async with engine.begin() as conn:
            await drop_owned_role(conn, reader)
