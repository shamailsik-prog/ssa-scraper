"""Per-xdist-worker PostgreSQL database naming and provisioning for isolated parallel tests."""

from __future__ import annotations

import os
import re
from urllib.parse import quote, unquote, urlparse, urlunparse

CORPUS_TEST_ALLOW_UNSAFE_DATABASE = "CORPUS_TEST_ALLOW_UNSAFE_DATABASE"
# Redis default installs expose logical databases 0–15.
_MAX_REDIS_LOGICAL_DB = 15


def xdist_worker_id() -> str | None:
    return os.environ.get("PYTEST_XDIST_WORKER")


def database_name_from_url(url: str) -> str:
    path = urlparse(url).path.lstrip("/")
    return unquote(path.split("?", 1)[0])


def is_safe_test_database_name(name: str) -> bool:
    if os.environ.get(CORPUS_TEST_ALLOW_UNSAFE_DATABASE, "").lower() in ("1", "true", "yes"):
        return True
    lower = name.lower()
    return lower.endswith("_test") or lower.startswith("test_")


def refuse_unsafe_database_url(url: str) -> None:
    name = database_name_from_url(url)
    if not name:
        raise RuntimeError("DATABASE_URL has no database name; tests refuse to run")
    if not is_safe_test_database_name(name):
        raise RuntimeError(
            f"DATABASE_URL database {name!r} does not look like a test database "
            f"(name must start with 'test_' or end with '_test', or set {CORPUS_TEST_ALLOW_UNSAFE_DATABASE}=1)"
        )


def refuse_unsafe_worker_database_name(base: str, worker_db: str) -> None:
    """Refuse derived xdist DB names that would point at a non-test base (e.g. legal_scraper_gw0)."""
    if not is_safe_test_database_name(base):
        raise RuntimeError(
            f"DATABASE_URL database {base!r} does not look like a test database "
            f"(name must start with 'test_' or end with '_test', or set {CORPUS_TEST_ALLOW_UNSAFE_DATABASE}=1)"
        )
    if worker_db == base:
        return
    worker = xdist_worker_id()
    expected = worker_database_name(base) if worker else base
    if worker_db != expected:
        raise RuntimeError(
            f"Derived worker database {worker_db!r} does not match expected name {expected!r} "
            f"for test base {base!r}"
        )


def worker_database_name(base: str) -> str:
    worker = xdist_worker_id()
    if not worker:
        return base
    safe = re.sub(r"[^\w]+", "_", worker).strip("_") or "worker"
    return f"{base}_{safe}"


def _replace_url_database(url: str, database: str) -> str:
    parsed = urlparse(url)
    path = "/" + quote(database, safe="")
    return urlunparse(parsed._replace(path=path))


def apply_worker_database_url() -> str:
    """Validate DATABASE_URL and, under xdist only, point it at this worker's database."""
    current = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper_test",
    )
    refuse_unsafe_database_url(current)
    base = database_name_from_url(current)
    if not xdist_worker_id():
        return current
    worker_db = worker_database_name(base)
    refuse_unsafe_worker_database_name(base, worker_db)
    updated = _replace_url_database(current, worker_db)
    os.environ["DATABASE_URL"] = updated
    return updated


def redis_url_db_index(redis_url: str) -> int:
    path = urlparse(redis_url).path
    if not path or path == "/":
        return 0
    return int(path.lstrip("/"))


def redis_logical_db_index_for_worker(worker: str, redis_url: str) -> int:
    """Map gwN to base+1+N where base is the REDIS_URL logical DB index."""
    m = re.match(r"^gw(\d+)$", worker)
    if not m:
        raise RuntimeError(
            f"pytest-xdist worker id {worker!r} is not gwN; cannot assign an isolated Redis logical database"
        )
    n = int(m.group(1))
    index = redis_url_db_index(redis_url) + 1 + n
    if index > _MAX_REDIS_LOGICAL_DB:
        raise RuntimeError(
            f"pytest-xdist worker {worker!r} needs Redis logical database {index} "
            f"(base {redis_url_db_index(redis_url)} + 1 + {n}), but only 0–{_MAX_REDIS_LOGICAL_DB} are available"
        )
    return index


def apply_worker_redis_url() -> str:
    """Under xdist, use a dedicated Redis logical DB per worker to avoid cross-worker key clashes."""
    current = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    worker = xdist_worker_id()
    if not worker:
        return current
    db_index = redis_logical_db_index_for_worker(worker, current)
    parsed = urlparse(current)
    updated = urlunparse(parsed._replace(path=f"/{db_index}"))
    os.environ["REDIS_URL"] = updated
    return updated


def admin_database_url(database_url: str, admin_db: str = "postgres") -> str:
    return _replace_url_database(database_url, admin_db)


def worker_database_names_created_this_session() -> list[str]:
    raw = os.environ.get("_CORPUS_XDIST_WORKER_DBS", "")
    return [n for n in raw.split("\n") if n]


def _remember_worker_database(name: str) -> None:
    existing = worker_database_names_created_this_session()
    if name not in existing:
        os.environ["_CORPUS_XDIST_WORKER_DBS"] = "\n".join(existing + [name])


async def ensure_worker_database_exists(database_url: str) -> None:
    """Create the worker database if missing."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    db_name = database_name_from_url(database_url)
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
                if xdist_worker_id():
                    _remember_worker_database(db_name)
    finally:
        await admin_engine.dispose()


async def drop_worker_databases_best_effort(database_url: str) -> None:
    """Drop xdist worker databases created in this session (best effort)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    if not xdist_worker_id():
        return
    db_name = database_name_from_url(database_url)
    base = os.environ.get("_CORPUS_TEST_DATABASE_BASE") or ""
    if not base or db_name == base or not db_name.startswith(f"{base}_"):
        return

    admin_url = admin_database_url(database_url)
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
    except Exception:
        pass
    finally:
        await admin_engine.dispose()
