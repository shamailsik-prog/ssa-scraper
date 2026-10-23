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

    def ensure_tree(self, key: str) -> None:
        return None

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
async def test_drive_concurrency_builds_distinct_http_client_per_worker(db, source, monkeypatch):
    """Parallel uploads must not share one httplib2-backed Drive service across threads."""

    overlap_lock = threading.Lock()
    clients_in_flight: Dict[int, int] = {}
    shared_client_across_threads = False
    factory_calls = 0

    class _ClientBoundAdapter(_TrackingAdapter):
        def __init__(self, http_client: object) -> None:
            super().__init__()
            self.http_client = http_client

        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            nonlocal shared_client_across_threads
            tid = threading.get_ident()
            cid = id(self.http_client)
            with overlap_lock:
                for other_tid, other_cid in clients_in_flight.items():
                    if other_tid != tid and other_cid == cid:
                        shared_client_across_threads = True
                clients_in_flight[tid] = cid
            try:
                return super().put(key, data, content_type)
            finally:
                with overlap_lock:
                    clients_in_flight.pop(tid, None)

    def factory(_target_type, _config):
        nonlocal factory_calls
        factory_calls += 1
        return _ClientBoundAdapter(object())

    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    await _promote_many(db, source, 4)
    await _drive_target(db)

    await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=4)
    await db.commit()

    assert factory_calls in (4, 5), "fixed upload pool (4) plus optional index writer"
    assert not shared_client_across_threads, "same http client must not be used from multiple threads at once"


@pytest.mark.asyncio
async def test_drive_concurrency_creates_each_judgment_folder_once(db, source, monkeypatch):
    """Pre-flight ensure_tree + shared folder cache: one mkdir per judgment prefix under concurrency."""

    from collections import defaultdict

    folder_ensures: Dict[str, int] = defaultdict(int)

    class _FolderAdapter(_TrackingAdapter):
        def __init__(self, folder_cache: Dict[str, str]) -> None:
            super().__init__()
            self._folder_cache = folder_cache

        def ensure_tree(self, key: str) -> None:
            folder = key.rsplit("/", 1)[0]
            folder_ensures[folder] += 1
            self._folder_cache.setdefault(folder, folder)

        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            folder = key.rsplit("/", 1)[0]
            if folder not in self._folder_cache:
                folder_ensures[folder] += 1
                self._folder_cache[folder] = folder
            return super().put(key, data, content_type)

    shared_cache: Dict[str, str] = {"": "root"}

    def factory(_target_type, config):
        assert config.get("_drive_folder_cache") is shared_cache
        return _FolderAdapter(shared_cache)

    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    await _promote_many(db, source, 2)
    await _drive_target(db)
    await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=2)
    await db.commit()

    for prefix, count in folder_ensures.items():
        assert count <= 1, f"duplicate folder ensure for {prefix}: {count}"


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
async def test_drive_concurrency_time_budget_after_acquire_stops_inflight(db, source, monkeypatch):
    """Deadline ~0.3s ahead with 0.4s uploads: workers must not run past budget (mutant M15)."""

    class _SlowPutAdapter(_TrackingAdapter):
        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            time.sleep(0.4)
            return super().put(key, data, content_type)

    adapter = _SlowPutAdapter()

    def factory(_target_type, _config):
        return adapter

    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    await _promote_many(db, source, 3)
    await _drive_target(db)

    mirror = ArchiveMirror(db, adapter_factory=factory, deadline=time.monotonic() + 0.3)
    await mirror.mirror_pending(limit=3)
    await db.commit()
    assert mirror.out_of_time
    assert len(adapter.put_keys) < 9


@pytest.mark.asyncio
async def test_drive_preflight_429_counts_failure_and_continues_other_folders(db, source, monkeypatch):
    """Preflight ensure_tree rate limits must not abort mirror_pending; other folders still upload."""

    fail_folder_marker = "PLD_2024_SC_700"

    class _PreflightAdapter(_TrackingAdapter):
        def ensure_tree(self, key: str) -> None:
            folder = key.rsplit("/", 1)[0]
            if fail_folder_marker in folder:
                raise _HttpishError(429, "Too Many Requests", reason="rateLimitExceeded")
            return None

    adapters: List[_PreflightAdapter] = []

    def factory(_target_type, _config):
        ad = _PreflightAdapter()
        adapters.append(ad)
        return ad

    async def _fast_backoff():
        return None

    monkeypatch.setattr("scraper.storage.archive.drive_rate_limit_backoff", _fast_backoff)
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 2)
    await _promote_many(db, source, 2)
    t = await _drive_target(db)

    result = await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=2)
    await db.commit()

    summary = result["summary"]["drive"]
    assert summary["failed"] >= 1
    all_puts = [k for ad in adapters for k in ad.put_keys]
    assert all_puts, "non-doomed judgment folder should still upload"
    assert not any(fail_folder_marker in k for k in all_puts)
    assert any("PLD_2024_SC_701" in k for k in all_puts)


@pytest.mark.asyncio
async def test_drive_preflight_429_then_success_on_retry(db, source, monkeypatch):
    class _Retry429Adapter(_TrackingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self._rate_limit_hits = 1

        def ensure_tree(self, key: str) -> None:
            if self._rate_limit_hits > 0:
                self._rate_limit_hits -= 1
                raise _HttpishError(429, "Too Many Requests", reason="rateLimitExceeded")
            return None

    adapter = _Retry429Adapter()

    def factory(_target_type, _config):
        return adapter

    async def _fast_backoff():
        return None

    monkeypatch.setattr("scraper.storage.archive.drive_rate_limit_backoff", _fast_backoff)
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 2)
    await _promote_one(db, source, "PLD 2024 SC 755")
    await _drive_target(db)
    result = await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=1)
    await db.commit()
    assert result["summary"]["drive"]["failed"] == 0
    assert adapter.put_keys


@pytest.mark.asyncio
async def test_drive_preflight_budget_stop_not_counted_as_failure(db, source, monkeypatch):
    class _SlowSecondFolderAdapter(_TrackingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self._folder_count = 0

        def ensure_tree(self, key: str) -> None:
            self._folder_count += 1
            if self._folder_count > 1:
                time.sleep(0.4)
            return None

    adapter = _SlowSecondFolderAdapter()

    def factory(_target_type, _config):
        return adapter

    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 2)
    await _promote_many(db, source, 2)
    await _drive_target(db)
    mirror = ArchiveMirror(db, adapter_factory=factory, deadline=time.monotonic() + 0.08)
    result = await mirror.mirror_pending(limit=2)
    await db.commit()
    assert mirror.out_of_time
    assert result["summary"]["drive"]["failed"] == 0


@pytest.mark.asyncio
async def test_drive_shared_folder_cache_same_dict_for_pool_adapters(db, source, monkeypatch):
    """Mutant F1: every pool adapter must share one _drive_folder_cache dict."""

    seen_caches: List[Dict[str, str]] = []

    class _CacheProbeAdapter(_TrackingAdapter):
        def __init__(self, folder_cache: Dict[str, str]) -> None:
            super().__init__()
            self._folder_cache = folder_cache

        def ensure_tree(self, key: str) -> None:
            folder = key.rsplit("/", 1)[0]
            self._folder_cache.setdefault(folder, folder)

    def factory(_target_type, config):
        cache = config.get("_drive_folder_cache")
        if isinstance(cache, dict):
            seen_caches.append(cache)
            return _CacheProbeAdapter(cache)
        return _CacheProbeAdapter({"": "root"})

    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    await _promote_many(db, source, 2)
    await _drive_target(db)
    await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=2)
    await db.commit()

    assert len(seen_caches) >= 4
    assert len({id(c) for c in seen_caches}) == 1


@pytest.mark.asyncio
async def test_drive_preflight_runs_before_parallel_puts(db, source, monkeypatch):
    """Each upload ensures its folder (on demand) before put."""

    events: List[tuple[str, str]] = []

    class _OrderedAdapter(_TrackingAdapter):
        def ensure_tree(self, key: str) -> None:
            events.append(("ensure", key.rsplit("/", 1)[0]))

        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            events.append(("put", key))
            return super().put(key, data, content_type)

    shared = _OrderedAdapter()

    def factory(_target_type, _config):
        return shared

    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    await _promote_many(db, source, 2)
    await _drive_target(db)
    await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=2)
    await db.commit()

    assert any(e[0] == "ensure" for e in events)
    assert any(e[0] == "put" for e in events)
    first_put = next(i for i, e in enumerate(events) if e[0] == "put")
    first_ensure = next(i for i, e in enumerate(events) if e[0] == "ensure")
    assert first_ensure < first_put


@pytest.mark.asyncio
async def test_drive_concurrency_large_batch_starts_uploads_within_budget(db, source, monkeypatch):
    """At c=2, uploads must not wait for serial ensure_tree over the whole backlog slice."""

    class _SlowEnsureAdapter(_TrackingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.ensure_before_first_put = 0
            self._saw_put = False
            self.first_put_at: Optional[float] = None

        def ensure_tree(self, key: str) -> None:
            if not self._saw_put:
                self.ensure_before_first_put += 1
            time.sleep(0.02)

        def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
            if self.first_put_at is None:
                self.first_put_at = time.monotonic()
            self._saw_put = True
            return super().put(key, data, content_type)

    adapter = _SlowEnsureAdapter()

    def factory(_target_type, _config):
        return adapter

    monkeypatch.setattr(settings, "MIRROR_DRIVE_SERIAL_PREFLIGHT_MAX_FOLDERS", 0)
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 2)
    batch = 50
    await _promote_many(db, source, batch)
    await _drive_target(db)
    started = time.monotonic()
    await ArchiveMirror(db, adapter_factory=factory, deadline=started + 4.0).mirror_pending(limit=batch)
    await db.commit()

    assert adapter.first_put_at is not None
    # Serial preflight over 50 folders at 20ms each would be ~1s before any put; on-demand must be much sooner.
    assert adapter.first_put_at - started < 1.0
    assert adapter.ensure_before_first_put < batch // 2
    assert adapter.put_keys


@pytest.mark.asyncio
async def test_drive_adapter_pool_init_failure_records_target_and_continues(db, source, monkeypatch):
    pool_init_calls = 0
    upload_adapter = _TrackingAdapter()

    def factory(_target_type, config):
        nonlocal pool_init_calls
        if isinstance(config.get("_drive_folder_cache"), dict):
            pool_init_calls += 1
            if pool_init_calls == 1:
                raise RuntimeError("adapter init boom")
            return upload_adapter
        return _TrackingAdapter()

    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 2)
    await _promote_one(db, source, "PLD 2024 SC 760")
    t = await _drive_target(db)
    result = await ArchiveMirror(db, adapter_factory=factory).mirror_pending(limit=1)
    await db.commit()
    await db.refresh(t)
    assert upload_adapter.put_keys
    assert result["summary"]["drive"]["failed"] >= 1
    assert int(t.consecutive_failures or 0) == 1
    assert t.last_error


@pytest.mark.asyncio
async def test_drive_concurrency_respects_time_budget_before_acquire(db, source, tracking_drive, monkeypatch):
    adapter, factory = tracking_drive
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    await _promote_many(db, source, 5)
    await _drive_target(db)

    # Budget already expired: parallel path must not start new uploads.
    mirror = ArchiveMirror(db, adapter_factory=factory, deadline=time.monotonic() - 1.0)
    await mirror.mirror_pending(limit=5)
    await db.commit()
    assert mirror.out_of_time
    assert adapter.put_keys == []
