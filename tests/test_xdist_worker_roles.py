"""R5: every xdist worker database gets full ensure_roles (reader table grants), not CONNECT-only."""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys

import pytest

from scraper.config import settings

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _reader_select_subprocess(worker: str) -> tuple[int, str]:
    script = f"""
import asyncio
import os
# Subprocess must not inherit the parent xdist worker's rewritten DATABASE_URL / PYTEST_XDIST_WORKER.
os.environ["PYTEST_XDIST_WORKER"] = {worker!r}
os.environ["DATABASE_URL"] = "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper_test"
os.environ["_CORPUS_TEST_DATABASE_BASE"] = "legal_scraper_test"
os.environ["DATABASE_NULL_POOL"] = "1"
os.environ["SIKANDER_READER_PASSWORD"] = "readerpw"
os.environ["CORPUS_WRITER_PASSWORD"] = "writerpw"
from tests.xdist_db import apply_worker_database_url, apply_worker_redis_url, ensure_worker_database_exists
apply_worker_database_url()
apply_worker_redis_url()
from scraper.database import init_db, engine
from scraper.config import settings
from scraper.models import CONTRACT_TABLES
import asyncpg

async def main():
    await ensure_worker_database_exists(os.environ["DATABASE_URL"])
    await init_db()
    url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "")
    hostpart = url.split("@", 1)[1]
    dsn = f"postgresql://{{settings.SIKANDER_READER_ROLE}}:{{settings.SIKANDER_READER_PASSWORD.get_secret_value()}}@{{hostpart}}"
    conn = await asyncpg.connect(dsn)
    try:
        for t in CONTRACT_TABLES:
            await conn.fetch(f'SELECT * FROM "{{t}}" LIMIT 1')
    finally:
        await conn.close()
    await engine.dispose()

asyncio.run(main())
"""
    env = os.environ.copy()
    env["PYTEST_XDIST_WORKER"] = worker
    env["DATABASE_URL"] = "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper_test"
    env["_CORPUS_TEST_DATABASE_BASE"] = "legal_scraper_test"
    env["DATABASE_NULL_POOL"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


@pytest.mark.asyncio
async def test_settings_and_roles_reader_works_on_gw1_worker_database():
    """Same checks as test_settings_and_roles after full init_db on legal_scraper_test_gw1."""
    code, output = await asyncio.to_thread(_reader_select_subprocess, "gw1")
    assert code == 0, output
