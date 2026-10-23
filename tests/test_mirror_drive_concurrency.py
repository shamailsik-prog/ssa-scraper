"""Google Drive mirror upload concurrency (MIRROR_DRIVE_CONCURRENCY)."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import func, select

from scraper.config import settings
from scraper.models import ArchiveObject, ArchiveTarget, Judgment
from scraper.storage.archive import (
    ArchiveMirror,
    _AdaptiveConcurrency,
    is_drive_rate_limited,
)
from tests.test_public_security_pdf_archive import _promote_one


class _HttpishError(Exception):
    def __init__(self, status: int, message: str = "", reason: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.resp = type("R", (), {"status": status})()
        self.status_code = status
        self.reason = reason


class _TrackingAdapter:
    """Records concurrent put() depth for concurrency tests."""

    def __init__(
        self,
        *,
        fail_rate_limit_times: int = 0,
        fail_forbidden_times: int = 0,
        fail_permanent_key: Optional[str] = None,
    ) -> None:
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.put_keys: List[str] = []
        self._fail_rate_limit_times = fail_rate_limit_times
        self._fail_forbidden_times = fail_forbidden_times
        self._fail_permanent_key = fail_permanent_key
        self.rate_limit_halve_events = 0

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with self._lock:
            if self._fail_permanent_key and key == self._fail_permanent_key:
                raise RuntimeError("adapter permanent failure for test")
            if self._fail_rate_limit_times > 0:
                self._fail_rate_limit_times -= 1
                raise _HttpishError(429, "Too Many Requests", reason="rateLimitExceeded")
            if self._fail_forbidden_times > 0:
                self._fail_forbidden_times -= 1
                raise _HttpishError(403, "Forbidden: access denied")
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


async def _written_ledger_count(db, t) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(ArchiveObject).where(
                    ArchiveObject.target_id == t.id,
                    ArchiveObject.status == "written",
                )
            )
        ).scalar()
        or 0
    )


async def _promote_many(db, source, n: int) -> None:
    for i in range(n):
        await _promote_one(db, source, f"PLD 2024 SC {700 + i}")


def test_is_drive_rate_limited_ignores_citation_with_429():
    assert not is_drive_rate_limited(RuntimeError("PLD 2024 SC 429"))
    assert is_drive_rate_limited(_HttpishError(429, "Too Many Requests", reason="rateLimitExceeded"))
    assert not is_drive_rate_limited(_HttpishError(403, "Forbidden: quota"))


def test_adaptive_concurrency_halve_floor_is_one():
    lim = _AdaptiveConcurrency(1)
    assert lim.halve() == 1
    assert lim.limit == 1


@pytest.mark.asyncio
async def test_drive_concurrency_uploads_in_parallel_without_duplicate_ledger_rows(db, source, tracking_drive, monkeypatch):
    adapter, factory = tracking_drive
    concurrency = 4
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", concurrency)
    await _promote_many(db, source, 3)
    t = await _drive_target(db)

    mirror = ArchiveMirror(db, adapter_factory=factory)
    await mirror.mirror_pending(limit=3)
    await db.commit()

    await db.refresh(t)
    assert adapter.max_in_flight >= 2
    assert adapter.max_in_flight <= concurrency
    keys = (await db.execute(select(ArchiveObject.object_key).where(ArchiveObject.target_id == t.id))).scalars().all()
    assert len(keys) == len(set(keys))
    assert len(adapter.put_keys) == len(set(adapter.put_keys))
    written = await _written_ledger_count(db, t)
    assert written == len(keys)
    assert written >= 9  # 3 judgments × 3 objects each
    assert int(t.objects_written or 0) == written


@pytest.mark.asyncio
async def test_drive_concurrency_one_matches_sequential_depth(db, source, tracking_drive, monkeypatch):
    adapter, factory = tracking_drive
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 1)
    await _promote_many(db, source, 2)
    t = await _drive_target(db)

    await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=2)
    await db.commit()
    assert adapter.max_in_flight == 1
    await db.refresh(t)
    written = await _written_ledger_count(db, t)
    assert int(t.objects_written or 0) == written
    assert written >= 6


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

    assert len(sleeps) == 2, "expected one backoff per rate-limit retry"
    assert halved == [2, 1]
    assert len(adapter.put_keys) >= 3


@pytest.mark.asyncio
async def test_drive_forbidden_403_without_rate_limit_does_not_halve(db, source, tracking_drive, monkeypatch):
    adapter, factory = tracking_drive
    adapter._fail_forbidden_times = 1
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    halved: list[int] = []
    original_halve = _AdaptiveConcurrency.halve

    def _halve(self):
        n = original_halve(self)
        halved.append(n)
        return n

    monkeypatch.setattr(_AdaptiveConcurrency, "halve", _halve)
    await _promote_one(db, source, "PLD 2024 SC 751")
    await _drive_target(db)
    await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=1)
    await db.commit()
    assert halved == []


@pytest.mark.asyncio
async def test_drive_concurrency_failed_upload_persists_failed_ledger_row(db, source, tracking_drive, monkeypatch):
    adapter, factory = tracking_drive
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    await _promote_one(db, source, "PLD 2024 SC 752")
    t = await _drive_target(db)
    adapter._fail_permanent_key = None

    class _FailOnceAdapter(_TrackingAdapter):
        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            if key.endswith("judgment.txt"):
                raise RuntimeError("simulated upload failure")
            return super().put(key, data, content_type)

    fail_adapter = _FailOnceAdapter()

    def factory(_target_type, _config):
        return fail_adapter

    await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=1)
    await db.commit()

    failed = (
        await db.execute(
            select(ArchiveObject).where(
                ArchiveObject.target_id == t.id,
                ArchiveObject.status == "failed",
            )
        )
    ).scalars().all()
    assert len(failed) >= 1


def test_mirror_drive_concurrency_capped_at_sixteen():
    with pytest.raises(Exception):
        settings.model_validate({**settings.model_dump(), "MIRROR_DRIVE_CONCURRENCY": 17})


@pytest.mark.asyncio
async def test_drive_concurrency_respects_time_budget_before_acquire(db, source, tracking_drive, monkeypatch):
    adapter, factory = tracking_drive
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    await _promote_many(db, source, 5)
    await _drive_target(db)

    mirror = ArchiveMirror(db, adapter_factory=factory, deadline=time.monotonic() + 0.05)
    await mirror.mirror_pending(limit=5)
    await db.commit()
    assert mirror.out_of_time
    assert len(adapter.put_keys) < 15
