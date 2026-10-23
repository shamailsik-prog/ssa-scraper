"""Google Drive mirror upload concurrency (MIRROR_DRIVE_CONCURRENCY)."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest
from sqlalchemy import func, select

from scraper.config import settings
from scraper.models import ArchiveObject, ArchiveTarget, Judgment
from scraper.storage.archive import ArchiveMirror, _AdaptiveConcurrency
from tests.test_public_security_pdf_archive import _promote_one


class _TrackingAdapter:
    """Records concurrent put() depth for concurrency tests."""

    def __init__(self, *, fail_rate_limit_times: int = 0) -> None:
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.put_keys: List[str] = []
        self._fail_rate_limit_times = fail_rate_limit_times
        self.rate_limit_halve_events = 0

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with self._lock:
            if self._fail_rate_limit_times > 0:
                self._fail_rate_limit_times -= 1
                raise RuntimeError("429 Too Many Requests rateLimitExceeded")
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(0.04)
            with self._lock:
                self.put_keys.append(key)
        finally:
            with self._lock:
                self.in_flight -= 1

    def exists(self, key: str) -> bool:
        return False

    def size(self, key: str) -> int:
        return 0

    def check(self) -> Dict[str, Any]:
        return {"ok": True}


@pytest.fixture(autouse=True)
def _no_batch_gate(monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 0)


@pytest.fixture
def tracking_drive():
    adapter = _TrackingAdapter()

    def factory(_target_type, _config):
        return adapter

    return adapter, factory


async def _drive_target(db):
    t = ArchiveTarget(name="drive", target_type="google_drive", root_path="SSA")
    db.add(t)
    await db.commit()
    return t


async def _promote_many(db, source, n: int) -> None:
    for i in range(n):
        await _promote_one(db, source, f"PLD 2024 SC {700 + i}")


@pytest.mark.asyncio
async def test_drive_concurrency_uploads_in_parallel_without_duplicate_ledger_rows(db, source, tracking_drive, monkeypatch):
    adapter, factory = tracking_drive
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    await _promote_many(db, source, 3)
    t = await _drive_target(db)

    mirror = ArchiveMirror(db, adapter_factory=factory)
    await mirror.mirror_pending(limit=3)
    await db.commit()

    assert adapter.max_in_flight >= 2
    keys = (await db.execute(select(ArchiveObject.object_key).where(ArchiveObject.target_id == t.id))).scalars().all()
    assert len(keys) == len(set(keys))
    assert len(adapter.put_keys) == len(set(adapter.put_keys))
    written = int((await db.execute(select(func.count()).select_from(ArchiveObject).where(ArchiveObject.target_id == t.id, ArchiveObject.status == "written"))).scalar() or 0)
    assert written == len(keys)
    assert written >= 9  # 3 judgments × 3 objects each


@pytest.mark.asyncio
async def test_drive_concurrency_one_matches_sequential_depth(db, source, tracking_drive, monkeypatch):
    adapter, factory = tracking_drive
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 1)
    await _promote_many(db, source, 2)
    await _drive_target(db)

    await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=2)
    await db.commit()
    assert adapter.max_in_flight == 1


@pytest.mark.asyncio
async def test_drive_rate_limit_triggers_backoff_and_halves_concurrency(db, source, tracking_drive, monkeypatch):
    adapter, factory = tracking_drive
    adapter._fail_rate_limit_times = 2
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    halved: list[int] = []
    original_halve = _AdaptiveConcurrency.halve

    def _halve(self):
        n = original_halve(self)
        halved.append(n)
        return n

    monkeypatch.setattr(_AdaptiveConcurrency, "halve", _halve)
    sleeps: list[float] = []

    async def _fake_backoff():
        sleeps.append(1.0)

    monkeypatch.setattr("scraper.storage.archive.drive_rate_limit_backoff", _fake_backoff)

    await _promote_one(db, source, "PLD 2024 SC 750")
    await _drive_target(db)
    await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=1)
    await db.commit()

    assert sleeps, "expected rate-limit backoff"
    assert halved and all(n <= 2 for n in halved)
    assert len(adapter.put_keys) >= 3
