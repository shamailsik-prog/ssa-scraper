"""
Test fixtures. The settings singleton is built at import time, so the environment is prepared
here before any `scraper` module is imported.

Requires a reachable PostgreSQL 16 with pgvector and a Redis (DATABASE_URL / REDIS_URL;
defaults match docker-compose run on localhost). Every test runs against a truncated database.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

from cryptography.fernet import Fernet

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="corpus-test-"))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from tests.xdist_db import apply_worker_database_url  # noqa: E402

apply_worker_database_url()
os.environ["ENCRYPTION_KEY"] = os.environ.get("ENCRYPTION_KEY") or Fernet.generate_key().decode()
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["SIKANDER_READER_PASSWORD"] = os.environ.get("SIKANDER_READER_PASSWORD") or "readerpw"
os.environ["CORPUS_WRITER_PASSWORD"] = os.environ.get("CORPUS_WRITER_PASSWORD") or "writerpw"
os.environ["ENVIRONMENT"] = "chambers"
os.environ["ALLOW_LOGIN_SCRAPING"] = "true"
os.environ["APP_ENV"] = "development"
os.environ["DEBUG"] = "true"
os.environ["RAW_STORAGE_PATH"] = str(_TMP / "raw")
os.environ["PDF_STORAGE_PATH"] = str(_TMP / "live")
os.environ["STATE_STORAGE_PATH"] = str(_TMP / "state")
os.environ["ARCHIVE_LOCAL_PATH"] = ""
os.environ["SGAI_API_KEY"] = ""
# Connector tests exercise every source's code, including the two retired in production.
os.environ["RETIRED_SOURCES"] = ""
os.environ["SGAI_LOCAL_LLM_BASE_URL"] = ""
os.environ["SGAI_LOCAL_LLM_MODEL"] = ""
os.environ["SGAI_DAILY_CREDIT_CAP"] = ""
os.environ["SGAI_TIMEOUT_SECONDS"] = "2"
os.environ["SGAI_MAX_RETRIES"] = "0"
os.environ["SCRAPER_DELAY_MIN"] = "0"
os.environ["SCRAPER_DELAY_MAX"] = "0"
os.environ["SCRAPER_RETRY_ATTEMPTS"] = "1"
os.environ["OPENAI_API_KEY"] = ""
os.environ["RECONNECT_SECONDS"] = "30"
os.environ["CORPUS_CHAIN_ENABLED"] = "false"  # tests that exercise the chain switch it on
os.environ["DISPATCH_DEDUPE_ENABLED"] = "false"  # tests that exercise dispatch dedupe switch it on
for k in ("PLS_USER", "PLS_PASS", "PLS_USER_B", "PLS_PASS_B"):
    os.environ.pop(k, None)
if not os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH"):
    _pw_root = pathlib.Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    _candidates = sorted(_pw_root.glob("chromium-*/chrome-linux/chrome")) + sorted(_pw_root.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
    if _candidates:
        os.environ["PLAYWRIGHT_EXECUTABLE_PATH"] = str(_candidates[-1])

import asyncio  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402

from scraper.config import settings  # noqa: E402
from scraper.database import (  # noqa: E402
    TEST_DB_ADVISORY_LOCK_ID,
    Base,
    SessionLocal,
    engine,
    init_db,
    seed_data,
)
from scraper.models import ScraperSource  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from tests.fixtures import FixtureServer  # noqa: E402

TMP = _TMP


async def _ensure_judgment_case_hash_includes_unreported(conn) -> None:
    """Migration downgrade tests can leave a 4-column unique; B2/models require unreported in the key."""
    row = await conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conname = 'uq_judgment_case_text_hash'"
        )
    )
    defn = (row.scalar() or "").lower()
    if defn and "unreported" not in defn:
        import importlib

        mig = importlib.import_module("migrations.020_judgment_case_hash_unreported_scope")
        await mig.upgrade(conn)


@pytest.fixture(scope="session", autouse=True)
def _initialised_database():
    from tests.xdist_db import ensure_worker_database_exists

    asyncio.run(ensure_worker_database_exists(os.environ["DATABASE_URL"]))
    asyncio.run(init_db())
    asyncio.run(engine.dispose())
    yield


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    """Truncate every table except schema_migrations, then re-seed. Per-xdist-worker DB names avoid
    cross-worker contention; session advisory lock serialises truncate+seed on the shared default DB."""
    from tests.xdist_db import worker_database_name

    tables = [t for t in Base.metadata.sorted_tables if t.name != "schema_migrations"]
    truncate_sql = text("TRUNCATE " + ", ".join(f'"{t.name}"' for t in tables) + " RESTART IDENTITY CASCADE")

    async def _truncate_and_seed() -> None:
        await engine.dispose(close=False)
        async with engine.begin() as conn:
            await conn.execute(truncate_sql)
            await _ensure_judgment_case_hash_includes_unreported(conn)
        await seed_data()

    if worker_database_name() != "legal_scraper":
        await _truncate_and_seed()
    else:
        from tests.xdist_db import admin_database_url

        lock_engine = create_async_engine(admin_database_url(settings.DATABASE_URL), poolclass=NullPool)
        async with lock_engine.connect() as lock_conn:
            await lock_conn.execute(
                text("SELECT pg_advisory_lock(:k)"),
                {"k": TEST_DB_ADVISORY_LOCK_ID},
            )
            try:
                await _truncate_and_seed()
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": TEST_DB_ADVISORY_LOCK_ID},
                )
        await lock_engine.dispose()
    from scraper.extractors.scrapegraph_base import LOCAL_BREAKER, MANAGED_BREAKER
    from scraper.security import reset_robots_cache

    MANAGED_BREAKER.reset()
    LOCAL_BREAKER.reset()
    reset_robots_cache()
    yield


@pytest_asyncio.fixture
async def db(clean_db):
    async with SessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def source(db):
    from sqlalchemy import select

    s = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "SupremeCourt"))).scalars().first()
    s.allow_list = ["127.0.0.1", "localhost"]
    s.respect_robots = True
    await db.commit()
    return s


@pytest_asyncio.fixture
async def login_source(db):
    from sqlalchemy import select

    s = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    s.allow_list = ["127.0.0.1", "localhost", "www.pakistanlawsite.com"]
    await db.commit()
    return s


@pytest.fixture
def fixture_server():
    srv = FixtureServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def admin_headers():
    return {"X-API-Key": "test-admin-key"}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from scraper.main import app

    with TestClient(app) as c:
        yield c
