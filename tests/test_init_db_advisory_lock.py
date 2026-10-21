"""R6: production init_db must not require CONNECT on the postgres catalog database."""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests import xdist_db

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_init_db_without_connect_on_postgres_catalog():
    """App-role init_db uses advisory locks on the app DB only (default, no DB_LOCK_ON_CATALOG)."""
    from urllib.parse import urlparse

    app_url = os.environ["DATABASE_URL"]
    admin_url = xdist_db.admin_database_url(app_url)
    parsed = urlparse(app_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    app_role = parsed.username or "legal"
    if not app_role:
        pytest.skip("DATABASE_URL has no username")

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'REVOKE CONNECT ON DATABASE postgres FROM "{app_role}"'))
    finally:
        await admin_engine.dispose()

    script = """
import asyncio
import os
os.environ.pop("DB_LOCK_ON_CATALOG", None)
os.environ.setdefault("DATABASE_NULL_POOL", "1")
os.environ.setdefault("SIKANDER_READER_PASSWORD", "readerpw")
os.environ.setdefault("CORPUS_WRITER_PASSWORD", "writerpw")
from scraper.database import init_db, engine
async def main():
    await init_db()
    await engine.dispose()
asyncio.run(main())
"""
    env = os.environ.copy()
    env.pop("DB_LOCK_ON_CATALOG", None)
    env.setdefault("DATABASE_NULL_POOL", "1")
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    try:
        assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    finally:
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin_engine.connect() as conn:
                await conn.execute(text(f'GRANT CONNECT ON DATABASE postgres TO "{app_role}"'))
        finally:
            await admin_engine.dispose()
