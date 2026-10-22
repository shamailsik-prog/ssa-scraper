"""B6/R1: ensure_roles reader attribute correction and non-superuser demotion guard."""

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
from tests.postgres_catalog_probe import drop_owned_role, postgres_catalog_probe_cluster_lock, subprocess_timeout_sec
from tests.test_init_db_advisory_lock import _admin_on_database_url

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _probe_url(source_url: str, role: str, password: str, database: str) -> str:
    parsed = urlparse(source_url)
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    return f"postgresql+asyncpg://{quote(role, safe='')}:{quote(password, safe='')}@{host}{port}/{quote(database, safe='')}"


@pytest.mark.asyncio
async def test_ensure_roles_demotes_superuser_reader():
    """Superuser caller must demote a SUPERUSER reader back to NOSUPERUSER."""
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or xdist_db.database_name_from_url(os.environ["DATABASE_URL"])
    probe_role = f"ensure_roles_probe_{uuid.uuid4().hex[:10]}"
    probe_pw = "ensure-roles-probe-pw"
    probe_db = f"{base}_rolesprobe_{uuid.uuid4().hex[:10]}"
    source_url = os.environ["DATABASE_URL"]
    admin_url = xdist_db.admin_database_url(source_url)
    url = _probe_url(source_url, probe_role, probe_pw, probe_db)
    suffix = uuid.uuid4().hex[:10]
    reader_role = f"probe_reader_{suffix}"

    async with postgres_catalog_probe_cluster_lock(admin_url):
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin_engine.connect() as conn:
                safe_pw = probe_pw.replace("'", "''")
                await conn.execute(
                    text(f'CREATE ROLE "{probe_role}" LOGIN PASSWORD \'{safe_pw}\' SUPERUSER')
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

        script = """
import asyncio
import os
os.environ["DATABASE_URL"] = os.environ["PROBE_DATABASE_URL"]
os.environ.pop("DB_LOCK_ON_CATALOG", None)
os.environ["DATABASE_NULL_POOL"] = "1"
os.environ["SIKANDER_READER_PASSWORD"] = "reader-demote-a"
os.environ["CORPUS_WRITER_PASSWORD"] = ""
os.environ["SIKANDER_READER_ROLE"] = os.environ["PROBE_READER_ROLE"]
from sqlalchemy import text
from scraper.database import ensure_roles, init_db, engine
async def main():
    await init_db()
    async with engine.begin() as conn:
        await conn.execute(text(f'ALTER ROLE "{os.environ["PROBE_READER_ROLE"]}" SUPERUSER'))
    os.environ["SIKANDER_READER_PASSWORD"] = "reader-demote-b"
    await ensure_roles()
    async with engine.connect() as conn:
        sup = (await conn.execute(
            text("SELECT rolsuper FROM pg_roles WHERE rolname = :r"),
            {"r": os.environ["PROBE_READER_ROLE"]},
        )).scalar()
    if sup:
        raise SystemExit("reader still superuser")
    await engine.dispose()
asyncio.run(main())
"""
        env = os.environ.copy()
        env["PROBE_DATABASE_URL"] = url
        env["PROBE_READER_ROLE"] = reader_role
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", script],
                cwd=str(_REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=subprocess_timeout_sec(),
            )
            assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
        finally:
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
                    await drop_owned_role(conn, reader_role)
                    await drop_owned_role(conn, probe_role)
            finally:
                await admin_engine.dispose()


@pytest.mark.asyncio
async def test_ensure_roles_twice_as_non_superuser_createrole():
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or xdist_db.database_name_from_url(os.environ["DATABASE_URL"])
    probe_role = f"ensure_roles_probe_{uuid.uuid4().hex[:10]}"
    probe_pw = "ensure-roles-probe-pw"
    probe_db = f"{base}_rolesprobe_{uuid.uuid4().hex[:10]}"
    source_url = os.environ["DATABASE_URL"]
    admin_url = xdist_db.admin_database_url(source_url)
    url = _probe_url(source_url, probe_role, probe_pw, probe_db)

    suffix = uuid.uuid4().hex[:10]
    writer_role = f"probe_writer_{suffix}"
    reader_role = f"probe_reader_{suffix}"

    async with postgres_catalog_probe_cluster_lock(admin_url):
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
        env["PROBE_WRITER_ROLE"] = writer_role
        env["PROBE_READER_ROLE"] = reader_role
        env.pop("DB_LOCK_ON_CATALOG", None)
        env["DATABASE_NULL_POOL"] = "1"
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", script],
                cwd=str(_REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=subprocess_timeout_sec(),
            )
            assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
        finally:
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
                    await drop_owned_role(conn, writer_role)
                    await drop_owned_role(conn, reader_role)
                    await drop_owned_role(conn, probe_role)
            finally:
                await admin_engine.dispose()


async def _run_non_superuser_probe_expect_demotion_error(reader_create_sql: str) -> None:
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or xdist_db.database_name_from_url(os.environ["DATABASE_URL"])
    probe_role = f"ensure_roles_probe_{uuid.uuid4().hex[:10]}"
    probe_pw = "ensure-roles-probe-pw"
    probe_db = f"{base}_rolesprobe_{uuid.uuid4().hex[:10]}"
    source_url = os.environ["DATABASE_URL"]
    admin_url = xdist_db.admin_database_url(source_url)
    url = _probe_url(source_url, probe_role, probe_pw, probe_db)

    suffix = uuid.uuid4().hex[:10]
    writer_role = f"probe_writer_{suffix}"
    reader_role = f"probe_reader_{suffix}"

    async with postgres_catalog_probe_cluster_lock(admin_url):
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin_engine.connect() as conn:
                safe_pw = probe_pw.replace("'", "''")
                await conn.execute(
                    text(f'CREATE ROLE "{probe_role}" LOGIN PASSWORD \'{safe_pw}\' NOSUPERUSER CREATEROLE')
                )
                safe_db = probe_db.replace('"', "")
                await conn.execute(text(f'CREATE DATABASE "{safe_db}" OWNER "{probe_role}"'))
                await conn.execute(text(reader_create_sql.format(reader=reader_role)))
                await conn.execute(text(f'GRANT CONNECT ON DATABASE "{safe_db}" TO "{reader_role}"'))
        finally:
            await admin_engine.dispose()

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
from scraper.database import ensure_roles, engine
async def main():
    await ensure_roles()
    await engine.dispose()
asyncio.run(main())
"""
        env = os.environ.copy()
        env["PROBE_DATABASE_URL"] = url
        env["PROBE_WRITER_ROLE"] = writer_role
        env["PROBE_READER_ROLE"] = reader_role
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", script],
                cwd=str(_REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=subprocess_timeout_sec(),
            )
            assert proc.returncode != 0
            combined = (proc.stdout or "") + (proc.stderr or "")
            assert "RoleAttributeChangeRequiresSuperuserError" in combined or "not a superuser" in combined
        finally:
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
                    await drop_owned_role(conn, writer_role)
                    await drop_owned_role(conn, reader_role)
                    await drop_owned_role(conn, probe_role)
            finally:
                await admin_engine.dispose()


@pytest.mark.asyncio
async def test_ensure_roles_superuser_reader_raises_for_non_superuser_caller():
    await _run_non_superuser_probe_expect_demotion_error(
        'CREATE ROLE "{reader}" LOGIN PASSWORD \'x\' SUPERUSER'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reader_create_sql",
    [
        'CREATE ROLE "{reader}" LOGIN PASSWORD \'x\' NOSUPERUSER CREATEROLE',
        'CREATE ROLE "{reader}" LOGIN PASSWORD \'x\' NOSUPERUSER REPLICATION',
        'CREATE ROLE "{reader}" LOGIN PASSWORD \'x\' NOSUPERUSER BYPASSRLS',
    ],
    ids=["createrole", "replication", "bypassrls"],
)
async def test_ensure_roles_privileged_reader_raises_for_non_superuser_caller(reader_create_sql: str):
    await _run_non_superuser_probe_expect_demotion_error(reader_create_sql)


async def _run_superuser_probe_reader_demotion(
    *,
    elevate_sql: str,
    flag_column: str,
    fail_message: str,
) -> None:
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or xdist_db.database_name_from_url(os.environ["DATABASE_URL"])
    probe_role = f"ensure_roles_probe_{uuid.uuid4().hex[:10]}"
    probe_pw = "ensure-roles-probe-pw"
    probe_db = f"{base}_rolesprobe_{uuid.uuid4().hex[:10]}"
    source_url = os.environ["DATABASE_URL"]
    admin_url = xdist_db.admin_database_url(source_url)
    url = _probe_url(source_url, probe_role, probe_pw, probe_db)
    suffix = uuid.uuid4().hex[:10]
    reader_role = f"probe_reader_{suffix}"

    async with postgres_catalog_probe_cluster_lock(admin_url):
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin_engine.connect() as conn:
                safe_pw = probe_pw.replace("'", "''")
                await conn.execute(
                    text(f'CREATE ROLE "{probe_role}" LOGIN PASSWORD \'{safe_pw}\' SUPERUSER')
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

        script = f'''
import asyncio
import os
os.environ["DATABASE_URL"] = os.environ["PROBE_DATABASE_URL"]
os.environ.pop("DB_LOCK_ON_CATALOG", None)
os.environ["DATABASE_NULL_POOL"] = "1"
os.environ["SIKANDER_READER_PASSWORD"] = "reader-demote-a"
os.environ["CORPUS_WRITER_PASSWORD"] = ""
os.environ["SIKANDER_READER_ROLE"] = os.environ["PROBE_READER_ROLE"]
from sqlalchemy import text
from scraper.database import ensure_roles, init_db, engine
async def main():
    await init_db()
    async with engine.begin() as conn:
        await conn.execute(text({elevate_sql!r}.format(role=os.environ["PROBE_READER_ROLE"])))
    os.environ["SIKANDER_READER_PASSWORD"] = "reader-demote-b"
    await ensure_roles()
    async with engine.connect() as conn:
        bad = (await conn.execute(
            text("SELECT {flag_column} FROM pg_roles WHERE rolname = :r"),
            {{"r": os.environ["PROBE_READER_ROLE"]}},
        )).scalar()
    if bad:
        raise SystemExit({fail_message!r})
    await engine.dispose()
asyncio.run(main())
'''
        env = os.environ.copy()
        env["PROBE_DATABASE_URL"] = url
        env["PROBE_READER_ROLE"] = reader_role
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", script],
                cwd=str(_REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=subprocess_timeout_sec(),
            )
            assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
        finally:
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
                    await drop_owned_role(conn, reader_role)
                    await drop_owned_role(conn, probe_role)
            finally:
                await admin_engine.dispose()


@pytest.mark.asyncio
async def test_ensure_roles_demotes_createrole_reader():
    await _run_superuser_probe_reader_demotion(
        elevate_sql='ALTER ROLE "{role}" CREATEROLE',
        flag_column="rolcreaterole",
        fail_message="reader still createrole",
    )


@pytest.mark.asyncio
async def test_ensure_roles_demotes_bypassrls_reader():
    await _run_superuser_probe_reader_demotion(
        elevate_sql='ALTER ROLE "{role}" BYPASSRLS',
        flag_column="rolbypassrls",
        fail_message="reader still bypassrls",
    )


@pytest.mark.asyncio
async def test_ensure_roles_demotes_replication_reader():
    await _run_superuser_probe_reader_demotion(
        elevate_sql='ALTER ROLE "{role}" REPLICATION',
        flag_column="rolreplication",
        fail_message="reader still replication",
    )
