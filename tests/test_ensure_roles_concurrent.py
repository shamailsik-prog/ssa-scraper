"""R4/R6: concurrent ensure_roles must serialize via advisory locks."""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PARALLEL_ENSURE_ROLES = 8


def _ensure_roles_subprocess() -> tuple[int, str]:
    script = """
import asyncio
import os
os.environ.setdefault("DATABASE_NULL_POOL", "1")
os.environ.setdefault("SIKANDER_READER_PASSWORD", "readerpw")
os.environ.setdefault("CORPUS_WRITER_PASSWORD", "writerpw")
from scraper.database import ensure_roles, engine
async def main():
    await ensure_roles()
    await engine.dispose()
asyncio.run(main())
"""
    env = os.environ.copy()
    env.setdefault("DATABASE_NULL_POOL", "1")
    if os.environ.get("DB_LOCK_ON_CATALOG"):
        env["DB_LOCK_ON_CATALOG"] = os.environ["DB_LOCK_ON_CATALOG"]
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


@pytest.mark.asyncio
async def test_eight_parallel_ensure_roles_all_succeed():
    results = await asyncio.gather(
        *[asyncio.to_thread(_ensure_roles_subprocess) for _ in range(_PARALLEL_ENSURE_ROLES)]
    )
    codes = [code for code, _ in results]
    assert codes == [0] * _PARALLEL_ENSURE_ROLES
