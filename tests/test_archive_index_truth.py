"""Regression: archive index CSV must describe complete, successful judgment mirrors."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.models import ArchiveObject
from scraper.storage.archive import ArchiveMirror
from tests.test_mirror_drive_concurrency import _TrackingAdapter, _drive_target
from tests.test_public_security_pdf_archive import _promote_one


class _IndexCaptureAdapter(_TrackingAdapter):
    def __init__(self, *, fail_fragment: str = "", mismatch_text: bool = False) -> None:
        super().__init__()
        self.fail_fragment = fail_fragment
        self.mismatch_text = mismatch_text
        self.index_texts: list[str] = []
        self._index_guard = threading.Lock()

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self.fail_fragment and self.fail_fragment in key and key.endswith("judgment.txt"):
            raise RuntimeError("intentional judgment.txt upload failure")
        super().put(key, data, content_type)
        if key.startswith("_index/"):
            with self._index_guard:
                self.index_texts.append(data.decode("utf-8"))

    def exists(self, key: str) -> bool:
        if self.mismatch_text and key.endswith("judgment.txt"):
            return True
        return super().exists(key)

    def size(self, key: str) -> int:
        if self.mismatch_text and key.endswith("judgment.txt"):
            return 1
        return super().size(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 4])
async def test_failed_judgment_never_appears_in_archive_index(db, source, monkeypatch, concurrency):
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 0)
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", concurrency)
    monkeypatch.setattr("scraper.storage.archive.RETRY_DELAYS", ())
    await _promote_one(db, source, "PLD 2024 SC 971")
    t = await _drive_target(db)
    adapter = _IndexCaptureAdapter(fail_fragment="PLD_2024_SC_971")
    await ArchiveMirror(db, adapter_factory=lambda *_: adapter).mirror_pending(limit=1)
    await db.commit()
    assert adapter.index_texts == [], "failed judgment must not be indexed"
    failures = (await db.execute(select(ArchiveObject).where(
        ArchiveObject.target_id == t.id, ArchiveObject.status == "failed"
    ))).scalars().all()
    assert failures, "failure must remain visible in the ledger"


@pytest.mark.asyncio
async def test_parallel_index_contains_only_completed_judgments(db, source, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 0)
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", 4)
    monkeypatch.setattr("scraper.storage.archive.RETRY_DELAYS", ())
    await _promote_one(db, source, "PLD 2024 SC 971")
    await _promote_one(db, source, "PLD 2024 SC 972")
    await _drive_target(db)
    adapter = _IndexCaptureAdapter(fail_fragment="PLD_2024_SC_971")
    await ArchiveMirror(db, adapter_factory=lambda *_: adapter).mirror_pending(limit=2)
    await db.commit()
    assert len(adapter.index_texts) == 1
    assert "PLD 2024 SC 972" in adapter.index_texts[0]
    assert "PLD 2024 SC 971" not in adapter.index_texts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 4])
async def test_mismatched_object_never_appears_in_archive_index(db, source, monkeypatch, concurrency):
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 0)
    monkeypatch.setattr(settings, "MIRROR_DRIVE_CONCURRENCY", concurrency)
    await _promote_one(db, source, "PLD 2024 SC 973")
    t = await _drive_target(db)
    adapter = _IndexCaptureAdapter(mismatch_text=True)
    await ArchiveMirror(db, adapter_factory=lambda *_: adapter).mirror_pending(limit=1)
    await db.commit()
    assert adapter.index_texts == [], "mismatched object must not be indexed"
    mismatches = (await db.execute(select(ArchiveObject).where(
        ArchiveObject.target_id == t.id, ArchiveObject.status == "mismatch"
    ))).scalars().all()
    assert mismatches, "mismatch must remain visible in the ledger"
