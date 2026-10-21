"""Per-xdist-worker PostgreSQL database naming and provisioning for isolated parallel tests."""

from __future__ import annotations

import os
import re
from urllib.parse import quote, unquote, urlparse, urlunparse


def xdist_worker_id() -> str | None:
    return os.environ.get("PYTEST_XDIST_WORKER")


def worker_database_name(base: str = "legal_scraper") -> str:
    worker = xdist_worker_id()
    if not worker:
        return base
    safe = re.sub(r"[^\w]+", "_", worker).strip("_") or "worker"
    return f"{base}_{safe}"


def _replace_url_database(url: str, database: str) -> str:
    parsed = urlparse(url)
    # postgresql+asyncpg://user:pass@host:port/dbname
    path = "/" + quote(database, safe="")
    return urlunparse(parsed._replace(path=path))


def apply_worker_database_url() -> str:
    """Point DATABASE_URL at this worker's database (mutates os.environ)."""
    current = os.environ.get("DATABASE_URL", "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper")
    db_name = worker_database_name()
    updated = _replace_url_database(current, db_name)
    os.environ["DATABASE_URL"] = updated
    return updated


def admin_database_url(database_url: str, admin_db: str = "postgres") -> str:
    return _replace_url_database(database_url, admin_db)


async def ensure_worker_database_exists(database_url: str) -> None:
    """Create the worker database if missing, then run init_db on it."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    db_name = urlparse(database_url).path.lstrip("/")
    db_name = unquote(db_name)
    if not db_name or db_name == "postgres":
        return

    admin_url = admin_database_url(database_url)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"),
                    {"n": db_name},
                )
            ).scalar()
            if not exists:
                safe = db_name.replace('"', "")
                await conn.execute(text(f'CREATE DATABASE "{safe}"'))
    finally:
        await admin_engine.dispose()
