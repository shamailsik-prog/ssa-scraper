"""R6: production init_db must not require CONNECT on the postgres catalog database."""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import uuid
from urllib.parse import quote, urlparse, urlunparse

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests import xdist_db

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _probe_database_url(source_url: str, role: str, password: str, database: str) -> str:
    parsed = urlparse(source_url)
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    user = quote(role, safe="")
    pw = quote(password, safe="")
    db = quote(database, safe="")
    return f"postgresql+asyncpg://{user}:{pw}@{host}{port}/{db}"


@pytest.mark.asyncio
async def test_init_db_without_connect_on_postgres_catalog():
    """Dedicated superuser probe without postgres CONNECT runs init_db() on a disposable DB only."""
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or xdist_db.database_name_from_url(os.environ["DATABASE_URL"])
    probe_role = f"init_db_lock_probe_{uuid.uuid4().hex[:10]}"
    probe_pw = "init-db-lock-probe-pw"
    probe_db = f"{base}_lockprobe_{uuid.uuid4().hex[:10]}"
    source_url = os.environ["DATABASE_URL"]
    admin_url = xdist_db.admin_database_url(source_url)
    probe_url = _probe_database_url(source_url, probe_role, probe_pw, probe_db)

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            safe_pw = probe_pw.replace("'", "''")
            await conn.execute(text(f"CREATE ROLE \"{probe_role}\" LOGIN PASSWORD '{safe_pw}' SUPERUSER"))
            safe_db = probe_db.replace('"', "")
            await conn.execute(text(f'CREATE DATABASE "{safe_db}" OWNER "{probe_role}"'))
            await conn.execute(text(f'REVOKE CONNECT ON DATABASE postgres FROM "{probe_role}"'))
    finally:
        await admin_engine.dispose()

    script = """
import asyncio
import os
os.environ["DATABASE_URL"] = os.environ["PROBE_DATABASE_URL"]
os.environ.pop("DB_LOCK_ON_CATALOG", None)
os.environ["DATABASE_NULL_POOL"] = "1"
os.environ.setdefault("SIKANDER_READER_PASSWORD", "readerpw")
os.environ.setdefault("CORPUS_WRITER_PASSWORD", "writerpw")
from scraper.database import init_db, engine
async def main():
    await init_db()
    await engine.dispose()
asyncio.run(main())
"""
    env = os.environ.copy()
    env["PROBE_DATABASE_URL"] = probe_url
    env.pop("DB_LOCK_ON_CATALOG", None)
    env["DATABASE_NULL_POOL"] = "1"
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            safe_db = probe_db.replace('"', "")
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": probe_db},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{safe_db}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{probe_role}"'))
    finally:
        await admin_engine.dispose()

    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
