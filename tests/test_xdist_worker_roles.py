"""R5/R6: full ensure_roles on init_db grants reader SELECT on contract tables (isolated DB)."""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import uuid

import pytest

from tests import xdist_db

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _reader_select_subprocess(database_url: str) -> tuple[int, str]:
    script = """
import asyncio
import os
os.environ["DATABASE_URL"] = os.environ["ROLECHECK_DATABASE_URL"]
os.environ.pop("PYTEST_XDIST_WORKER", None)
if os.environ.get("DB_LOCK_ON_CATALOG"):
    os.environ["DB_LOCK_ON_CATALOG"] = os.environ["DB_LOCK_ON_CATALOG"]
else:
    os.environ.pop("DB_LOCK_ON_CATALOG", None)
os.environ.setdefault("DATABASE_NULL_POOL", "1")
os.environ.setdefault("SIKANDER_READER_PASSWORD", "readerpw")
os.environ.setdefault("CORPUS_WRITER_PASSWORD", "writerpw")
from tests.xdist_db import refuse_unsafe_database_url, ensure_worker_database_exists
refuse_unsafe_database_url(os.environ["DATABASE_URL"])
from scraper.database import init_db, engine
from scraper.config import settings
from scraper.models import CONTRACT_TABLES
from scraper.reader_grants import READER_OPS_COLUMN_ALLOWLIST
import asyncpg

async def main():
    await ensure_worker_database_exists(os.environ["DATABASE_URL"])
    await init_db()
    url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "")
    hostpart = url.split("@", 1)[1]
    dsn = f"postgresql://{settings.SIKANDER_READER_ROLE}:{settings.SIKANDER_READER_PASSWORD.get_secret_value()}@{hostpart}"
    conn = await asyncpg.connect(dsn)
    try:
        for t in CONTRACT_TABLES:
            if t in READER_OPS_COLUMN_ALLOWLIST:
                cols = ", ".join(f'"{c}"' for c in READER_OPS_COLUMN_ALLOWLIST[t])
                await conn.fetch(f'SELECT {cols} FROM "{t}" LIMIT 1')
            else:
                await conn.fetch(f'SELECT * FROM "{t}" LIMIT 1')
    finally:
        await conn.close()
    await engine.dispose()

asyncio.run(main())
"""
    env = os.environ.copy()
    env["ROLECHECK_DATABASE_URL"] = database_url
    env["DATABASE_NULL_POOL"] = "1"
    env.pop("PYTEST_XDIST_WORKER", None)
    if os.environ.get("DB_LOCK_ON_CATALOG"):
        env["DB_LOCK_ON_CATALOG"] = os.environ["DB_LOCK_ON_CATALOG"]
    else:
        env.pop("DB_LOCK_ON_CATALOG", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


async def _drop_database_best_effort(admin_url: str, db_name: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            safe = db_name.replace('"', "")
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": db_name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{safe}"'))
    finally:
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_reader_grants_after_init_on_isolated_rolecheck_database():
    """init_db + reader contract SELECT on a disposable rolecheck DB (never a live gwN worker DB)."""
    source_url = os.environ["DATABASE_URL"]
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or xdist_db.database_name_from_url(source_url)
    rolecheck_db = f"{base}_rolecheck_{uuid.uuid4().hex[:12]}"
    rolecheck_url = xdist_db._replace_url_database(source_url, rolecheck_db)
    admin_url = xdist_db.admin_database_url(source_url)
    await xdist_db.ensure_worker_database_exists(rolecheck_url)
    try:
        code, output = await asyncio.to_thread(_reader_select_subprocess, rolecheck_url)
        assert code == 0, output
    finally:
        await _drop_database_best_effort(admin_url, rolecheck_db)
