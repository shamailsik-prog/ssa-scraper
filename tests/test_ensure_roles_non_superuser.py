"""B6: ensure_roles idempotent for non-superuser CREATEROLE callers (no NOSUPERUSER on ALTER)."""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import uuid
from urllib.parse import quote, urlparse

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests import xdist_db

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _probe_url(source_url: str, role: str, password: str, database: str) -> str:
    parsed = urlparse(source_url)
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    return f"postgresql+asyncpg://{quote(role, safe='')}:{quote(password, safe='')}@{host}{port}/{quote(database, safe='')}"


@pytest.mark.asyncio
async def test_ensure_roles_twice_as_non_superuser_createrole():
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or xdist_db.database_name_from_url(os.environ["DATABASE_URL"])
    probe_role = f"ensure_roles_probe_{uuid.uuid4().hex[:10]}"
    probe_pw = "ensure-roles-probe-pw"
    probe_db = f"{base}_rolesprobe_{uuid.uuid4().hex[:10]}"
    source_url = os.environ["DATABASE_URL"]
    admin_url = xdist_db.admin_database_url(source_url)
    url = _probe_url(source_url, probe_role, probe_pw, probe_db)

    from tests.test_init_db_advisory_lock import _admin_on_database_url

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            safe_pw = probe_pw.replace("'", "''")
            await conn.execute(
                text(f'CREATE ROLE "{probe_role}" LOGIN PASSWORD \'{safe_pw}\' NOSUPERUSER CREATEROLE')
            )
            safe_db = probe_db.replace('"', "")
            await conn.execute(text(f'CREATE DATABASE "{safe_db}" OWNER "{probe_role}"'))
    finally:
        await admin_engine.dispose()

    super_on_probe = create_async_engine(_admin_on_database_url(admin_url, probe_db), isolation_level="AUTOCOMMIT")
    try:
        async with super_on_probe.connect() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    finally:
        await super_on_probe.dispose()

    suffix = uuid.uuid4().hex[:10]
    script = """
import asyncio
import os
os.environ["DATABASE_URL"] = os.environ["PROBE_DATABASE_URL"]
os.environ.pop("DB_LOCK_ON_CATALOG", None)
os.environ["DATABASE_NULL_POOL"] = "1"
os.environ["SIKANDER_READER_PASSWORD"] = "readerpw"
os.environ["CORPUS_WRITER_PASSWORD"] = "writerpw"
os.environ["CORPUS_WRITER_ROLE"] = os.environ["PROBE_WRITER_ROLE"]
os.environ["SIKANDER_READER_ROLE"] = os.environ["PROBE_READER_ROLE"]
from scraper.database import ensure_roles, init_db, engine
async def main():
    await init_db()
    await ensure_roles()
    await engine.dispose()
asyncio.run(main())
"""
    env = os.environ.copy()
    env["PROBE_DATABASE_URL"] = url
    env["PROBE_WRITER_ROLE"] = f"probe_writer_{suffix}"
    env["PROBE_READER_ROLE"] = f"probe_reader_{suffix}"
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
            await conn.execute(text(f'DROP ROLE IF EXISTS "probe_writer_{suffix}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "probe_reader_{suffix}"'))
    finally:
        await admin_engine.dispose()

    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
