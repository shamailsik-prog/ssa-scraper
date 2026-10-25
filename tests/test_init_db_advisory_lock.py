"""R6/B6: production init_db must not require CONNECT on the postgres catalog database."""

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
from tests.postgres_catalog_probe import (
    drop_owned_role,
    grant_public_connect_on_postgres,
    postgres_catalog_probe_cluster_lock,
    subprocess_timeout_sec,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _probe_database_url(source_url: str, role: str, password: str, database: str) -> str:
    parsed = urlparse(source_url)
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    return f"postgresql+asyncpg://{quote(role, safe='')}:{quote(password, safe='')}@{host}{port}/{quote(database, safe='')}"


def _admin_on_database_url(admin_url: str, database: str) -> str:
    parsed = urlparse(admin_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    user = quote(parsed.username or "legal", safe="")
    pw = quote(parsed.password or "", safe="")
    return f"postgresql+asyncpg://{user}:{pw}@{host}{port}/{quote(database, safe='')}"


def _subprocess_env(probe_url: str, *, catalog_lock: bool, role_suffix: str) -> dict:
    env = os.environ.copy()
    env["PROBE_DATABASE_URL"] = probe_url
    env["DATABASE_NULL_POOL"] = "1"
    env["CORPUS_WRITER_ROLE"] = f"probe_writer_{role_suffix}"
    env["SIKANDER_READER_ROLE"] = f"probe_reader_{role_suffix}"
    env["CORPUS_API_READER_ROLE"] = f"probe_api_reader_{role_suffix}"
    env.setdefault("CORPUS_API_READER_PASSWORD", "corpusapipw")
    if catalog_lock:
        env["DB_LOCK_ON_CATALOG"] = "1"
    else:
        env.pop("DB_LOCK_ON_CATALOG", None)
    return env


_INIT_DB_SCRIPT = """
import asyncio
import os
os.environ["DATABASE_URL"] = os.environ["PROBE_DATABASE_URL"]
if os.environ.get("FORCE_CATALOG_LOCK"):
    os.environ["DB_LOCK_ON_CATALOG"] = "1"
else:
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


async def _provision_probe_db(
    admin_url: str,
    probe_role: str,
    probe_pw: str,
    probe_db: str,
) -> bool:
    """Create probe role/DB and block postgres CONNECT. Returns whether PUBLIC was revoked."""
    public_revoked = False
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        try:
            async with admin_engine.connect() as conn:
                safe_pw = probe_pw.replace("'", "''")
                await conn.execute(
                    text(f'CREATE ROLE "{probe_role}" LOGIN PASSWORD \'{safe_pw}\' NOSUPERUSER CREATEROLE')
                )
                safe_db = probe_db.replace('"', "")
                await conn.execute(text(f'CREATE DATABASE "{safe_db}" OWNER "{probe_role}"'))
                public_has_connect = (
                    await conn.execute(
                        text("SELECT has_database_privilege('public', 'postgres', 'CONNECT')")
                    )
                ).scalar()
                if public_has_connect:
                    await conn.execute(text("REVOKE CONNECT ON DATABASE postgres FROM PUBLIC"))
                    public_revoked = True
                await conn.execute(text(f'REVOKE CONNECT ON DATABASE postgres FROM "{probe_role}"'))
                await conn.execute(text(f'GRANT CONNECT ON DATABASE "{safe_db}" TO "{probe_role}"'))
        except Exception:
            if public_revoked:
                await grant_public_connect_on_postgres(admin_url)
            raise
        finally:
            await admin_engine.dispose()

        super_on_probe = create_async_engine(_admin_on_database_url(admin_url, probe_db), isolation_level="AUTOCOMMIT")
        try:
            async with super_on_probe.connect() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
        except Exception:
            if public_revoked:
                await grant_public_connect_on_postgres(admin_url)
            raise
        finally:
            await super_on_probe.dispose()
    except Exception:
        raise
    return public_revoked


async def _teardown_probe_db(
    admin_url: str,
    probe_role: str,
    probe_db: str,
    restore_public: bool,
    *,
    corpus_writer_role: str | None = None,
    corpus_reader_role: str | None = None,
    corpus_api_reader_role: str | None = None,
) -> None:
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            try:
                safe_db = probe_db.replace('"', "")
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :n AND pid <> pg_backend_pid()"
                    ),
                    {"n": probe_db},
                )
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{safe_db}"'))
                if corpus_writer_role:
                    await drop_owned_role(conn, corpus_writer_role)
                if corpus_reader_role:
                    await drop_owned_role(conn, corpus_reader_role)
                if corpus_api_reader_role:
                    await drop_owned_role(conn, corpus_api_reader_role)
                await drop_owned_role(conn, probe_role)
            finally:
                if restore_public:
                    await conn.execute(text("GRANT CONNECT ON DATABASE postgres TO PUBLIC"))
    finally:
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_init_db_without_connect_on_postgres_catalog():
    """NOSUPERUSER CREATEROLE probe without postgres CONNECT runs init_db() on a disposable DB."""
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or xdist_db.database_name_from_url(os.environ["DATABASE_URL"])
    probe_role = f"init_db_lock_probe_{uuid.uuid4().hex[:10]}"
    probe_pw = "init-db-lock-probe-pw"
    probe_db = f"{base}_lockprobe_{uuid.uuid4().hex[:10]}"
    source_url = os.environ["DATABASE_URL"]
    admin_url = xdist_db.admin_database_url(source_url)
    probe_url = _probe_database_url(source_url, probe_role, probe_pw, probe_db)
    suffix = uuid.uuid4().hex[:10]
    writer_role = f"probe_writer_{suffix}"
    reader_role = f"probe_reader_{suffix}"
    api_reader_role = f"probe_api_reader_{suffix}"

    public_revoked = False
    async with postgres_catalog_probe_cluster_lock(admin_url):
        try:
            public_revoked = await _provision_probe_db(admin_url, probe_role, probe_pw, probe_db)
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", _INIT_DB_SCRIPT],
                cwd=str(_REPO_ROOT),
                env=_subprocess_env(probe_url, catalog_lock=False, role_suffix=suffix),
                capture_output=True,
                text=True,
                timeout=subprocess_timeout_sec(),
            )
            assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
        except Exception:
            if public_revoked:
                await grant_public_connect_on_postgres(admin_url)
            raise
        finally:
            await _teardown_probe_db(
                admin_url,
                probe_role,
                probe_db,
                public_revoked,
                corpus_writer_role=writer_role,
                corpus_reader_role=reader_role,
                corpus_api_reader_role=api_reader_role,
            )


@pytest.mark.asyncio
async def test_init_db_with_catalog_lock_fails_without_postgres_connect():
    """DB_LOCK_ON_CATALOG=1 must fail when the role cannot CONNECT to postgres."""
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or xdist_db.database_name_from_url(os.environ["DATABASE_URL"])
    probe_role = f"init_db_lock_probe_{uuid.uuid4().hex[:10]}"
    probe_pw = "init-db-lock-probe-pw"
    probe_db = f"{base}_lockprobe_{uuid.uuid4().hex[:10]}"
    source_url = os.environ["DATABASE_URL"]
    admin_url = xdist_db.admin_database_url(source_url)
    probe_url = _probe_database_url(source_url, probe_role, probe_pw, probe_db)

    suffix = uuid.uuid4().hex[:10]
    writer_role = f"probe_writer_{suffix}"
    reader_role = f"probe_reader_{suffix}"
    api_reader_role = f"probe_api_reader_{suffix}"

    public_revoked = False
    async with postgres_catalog_probe_cluster_lock(admin_url):
        try:
            public_revoked = await _provision_probe_db(admin_url, probe_role, probe_pw, probe_db)
            env = _subprocess_env(probe_url, catalog_lock=True, role_suffix=suffix)
            env["FORCE_CATALOG_LOCK"] = "1"
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", _INIT_DB_SCRIPT],
                cwd=str(_REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=subprocess_timeout_sec(),
            )
            assert proc.returncode != 0
            combined = (proc.stdout or "") + (proc.stderr or "")
            assert "postgres" in combined.lower() or "permission" in combined.lower() or "connect" in combined.lower()
        except Exception:
            if public_revoked:
                await grant_public_connect_on_postgres(admin_url)
            raise
        finally:
            await _teardown_probe_db(
                admin_url,
                probe_role,
                probe_db,
                public_revoked,
                corpus_writer_role=writer_role,
                corpus_reader_role=reader_role,
                corpus_api_reader_role=api_reader_role,
            )
