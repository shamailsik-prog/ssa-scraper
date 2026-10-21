"""R3: init_db holds a session advisory lock for the whole routine (parallel safe)."""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from scraper.config import settings
from tests.xdist_db import admin_database_url

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


async def _create_empty_database(admin_url: str, name: str) -> str:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            safe = name.replace('"', "")
            await conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"),
                {"n": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{safe}"'))
            await conn.execute(text(f'CREATE DATABASE "{safe}"'))
    finally:
        await engine.dispose()
    return _replace_db(settings.DATABASE_URL, name)


async def _drop_database(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            safe = name.replace('"', "")
            await conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"),
                {"n": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{safe}"'))
    finally:
        await engine.dispose()


def _replace_db(url: str, database: str) -> str:
    from tests.xdist_db import _replace_url_database

    return _replace_url_database(url, database)


def _init_db_subprocess(database_url: str) -> int:
    script = f"""
import asyncio
import os
os.environ["DATABASE_URL"] = {database_url!r}
os.environ.setdefault("DATABASE_NULL_POOL", "1")
os.environ.setdefault("SIKANDER_READER_PASSWORD", "readerpw")
os.environ.setdefault("CORPUS_WRITER_PASSWORD", "writerpw")
os.environ["CORPUS_INIT_SKIP_ENSURE_ROLES"] = "1"
from scraper.database import init_db, engine
async def main():
    await init_db()
    await engine.dispose()
asyncio.run(main())
"""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.setdefault("DATABASE_NULL_POOL", "1")
    env["CORPUS_INIT_SKIP_ENSURE_ROLES"] = "1"
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
    ).returncode


@pytest.mark.asyncio
async def test_four_parallel_init_db_on_empty_database():
    admin_url = admin_database_url(settings.DATABASE_URL)
    db_name = f"legal_scraper_test_parallel_{uuid.uuid4().hex[:10]}"
    target_url = await _create_empty_database(admin_url, db_name)
    try:
        results = await asyncio.gather(
            *[asyncio.to_thread(_init_db_subprocess, target_url) for _ in range(4)]
        )
        assert results == [0, 0, 0, 0]
        check_engine = create_async_engine(target_url, poolclass=NullPool)
        try:
            async with check_engine.connect() as conn:
                courts = (await conn.execute(text("SELECT COUNT(*) FROM court"))).scalar()
                sources = (await conn.execute(text("SELECT COUNT(*) FROM scraper_sources"))).scalar()
        finally:
            await check_engine.dispose()
        assert courts and sources
    finally:
        await _drop_database(admin_url, db_name)
