"""R4: concurrent ensure_roles must serialize via advisory locks (no contention retries)."""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys

import pytest

from scraper.database import ensure_roles_contention_retry_count, reset_ensure_roles_contention_retry_count_for_tests

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PARALLEL_ENSURE_ROLES = 8


def _ensure_roles_subprocess() -> tuple[int, str]:
    script = """
import asyncio
import os
os.environ.setdefault("DATABASE_NULL_POOL", "1")
os.environ.setdefault("SIKANDER_READER_PASSWORD", "readerpw")
os.environ.setdefault("CORPUS_WRITER_PASSWORD", "writerpw")
from scraper.database import ensure_roles, engine, ensure_roles_contention_retry_count
async def main():
    await ensure_roles()
    await engine.dispose()
    print("RETRIES=" + str(ensure_roles_contention_retry_count()))
asyncio.run(main())
"""
    env = os.environ.copy()
    env.setdefault("DATABASE_NULL_POOL", "1")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


@pytest.mark.asyncio
async def test_eight_parallel_ensure_roles_zero_contention_retries():
    reset_ensure_roles_contention_retry_count_for_tests()
    results = await asyncio.gather(
        *[asyncio.to_thread(_ensure_roles_subprocess) for _ in range(_PARALLEL_ENSURE_ROLES)]
    )
    codes = [code for code, _ in results]
    assert codes == [0] * _PARALLEL_ENSURE_ROLES
    for _, output in results:
        assert "RETRIES=0" in output
    assert ensure_roles_contention_retry_count() == 0
