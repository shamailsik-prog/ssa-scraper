"""Batch transfer: Google Drive receives judgments in batches of about ARCHIVE_BATCH_BYTES, not file by file."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.models import ArchiveObject, ArchiveTarget, Judgment
from scraper.storage.adapters import LocalPathAdapter
from scraper.storage.archive import ArchiveMirror
from tests.test_public_security_pdf_archive import _promote_one


@pytest.fixture
def drive(tmp_path):
    """A google_drive target whose writes land in a local folder, so the test sees what was transferred."""
    root = tmp_path / "drive"

    def factory(_target_type, _config):
        return LocalPathAdapter({"path": str(root)})

    return root, factory


@pytest.fixture(autouse=True)
def _fresh_cutoffs():
    from scraper.tasks.chain import _redis

    def clear():
        for key in _redis().scan_iter("corpus:archive_batch_cutoff:*"):
            _redis().delete(key)

    clear()
    yield
    clear()


async def _drive_target(db):
    t = ArchiveTarget(name="drive", target_type="google_drive", root_path="SSA")
    db.add(t)
    await db.commit()
    return t


async def _written_judgments(db, t) -> set:
    rows = (await db.execute(select(ArchiveObject.judgment_id).where(ArchiveObject.target_id == t.id, ArchiveObject.status == "written"))).scalars().all()
    return {r for r in rows if r is not None}


async def test_drive_waits_until_a_batch_has_built_up(db, source, drive, monkeypatch):
    _root, factory = drive
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 10 * 1024**2)  # far more than two judgments
    j = await _promote_one(db, source, "PLD 2024 SC 601")
    t = await _drive_target(db)

    mirror = ArchiveMirror(db, adapter_factory=factory)
    await mirror.mirror_pending()
    await db.commit()

    assert await _written_judgments(db, t) == set()  # nothing went up file by file
    waiting = mirror.summary["drive"]["batch_waiting"]
    assert waiting["judgments"] == 1 and waiting["threshold_mb"] == 10.0
    assert j is not None


async def test_a_full_batch_goes_up_in_one_transfer_and_then_closes(db, source, drive, monkeypatch):
    _root, factory = drive
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 1)  # any waiting judgment fills the batch
    a = await _promote_one(db, source, "PLD 2024 SC 602")
    b = await _promote_one(db, source, "PLD 2024 SC 603")
    t = await _drive_target(db)

    mirror = ArchiveMirror(db, adapter_factory=factory)
    await mirror.mirror_pending()
    await db.commit()
    assert await _written_judgments(db, t) == {a.id, b.id}
    assert mirror._get_cutoff(t) is not None  # still open until a run sees the snapshot complete

    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 10 * 1024**2)
    await ArchiveMirror(db, adapter_factory=factory).mirror_pending()
    await db.commit()
    assert mirror._get_cutoff(t) is None  # batch closed; the next one waits for the threshold again


async def test_an_open_batch_takes_only_its_snapshot(db, source, drive, monkeypatch):
    _root, factory = drive
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 1)
    first = await _promote_one(db, source, "PLD 2024 SC 604")
    t = await _drive_target(db)
    mirror = ArchiveMirror(db, adapter_factory=factory)
    mirror._set_cutoff(t, datetime.now(timezone.utc) - timedelta(seconds=1))  # batch opened a moment ago
    later = await _promote_one(db, source, "PLD 2024 SC 605")
    later.promoted_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    first.promoted_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db.commit()

    await mirror.mirror_pending()
    await db.commit()
    assert await _written_judgments(db, t) == {first.id}  # the newer judgment waits for the next batch


async def test_a_slow_trickle_still_goes_up_after_the_maximum_wait(db, source, drive, monkeypatch):
    _root, factory = drive
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 10 * 1024**2)
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_MAX_WAIT_HOURS", 24.0)
    j = await _promote_one(db, source, "PLD 2024 SC 606")
    j.promoted_at = datetime.now(timezone.utc) - timedelta(hours=25)
    t = await _drive_target(db)

    await ArchiveMirror(db, adapter_factory=factory).mirror_pending()
    await db.commit()
    assert await _written_judgments(db, t) == {j.id}


async def test_targets_that_are_not_batched_still_receive_every_record(db, source, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 10 * 1024**2)
    j = await _promote_one(db, source, "PLD 2024 SC 607")
    t = ArchiveTarget(name="local", target_type="local_path", root_path=str(tmp_path / "local"))
    db.add(t)
    await db.commit()

    await ArchiveMirror(db).mirror_pending()
    await db.commit()
    assert await _written_judgments(db, t) == {j.id}


async def test_zero_turns_batching_off(db, source, drive, monkeypatch):
    _root, factory = drive
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 0)
    j = await _promote_one(db, source, "PLD 2024 SC 608")
    t = await _drive_target(db)

    await ArchiveMirror(db, adapter_factory=factory).mirror_pending()
    await db.commit()
    assert await _written_judgments(db, t) == {j.id}
    assert (await db.execute(select(Judgment))).scalars().first() is not None


async def test_a_fresh_drive_target_building_its_first_batch_raises_no_lag_alarm(db, source, drive, monkeypatch):
    """A new server's Drive has written nothing yet while its first batch builds up: that is not lag."""
    _root, factory = drive
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 10 * 1024**2)
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_MAX_WAIT_HOURS", 24.0)
    j = await _promote_one(db, source, "PLD 2024 SC 609")
    await _drive_target(db)
    mirror = ArchiveMirror(db, adapter_factory=factory)

    assert await mirror.alert_on_lag() == []  # waiting 0 h of the allowed 24 + 3

    j.promoted_at = datetime.now(timezone.utc) - timedelta(hours=30)  # the batch should have gone by now
    await db.commit()
    assert await mirror.alert_on_lag() == ["ARCHIVE_MIRROR_LAG:drive"]


async def test_an_old_backlog_raises_no_lag_alarm_while_drive_is_actively_receiving(db, source, drive, monkeypatch):
    """Draining an old backlog (e.g. Drive connected after weeks of collecting) is progress, not lag."""
    _root, factory = drive
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_BYTES", 1)
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_MAX_WAIT_HOURS", 24.0)
    await _promote_one(db, source, "PLD 2024 SC 610")
    t = await _drive_target(db)
    mirror = ArchiveMirror(db, adapter_factory=factory)
    await mirror.mirror_pending()
    await db.commit()
    assert await _written_judgments(db, t)  # Drive wrote something just now

    old = await _promote_one(db, source, "PLD 2024 SC 611")
    old.promoted_at = datetime.now(timezone.utc) - timedelta(hours=30)
    await db.commit()
    assert await mirror.alert_on_lag() == []
