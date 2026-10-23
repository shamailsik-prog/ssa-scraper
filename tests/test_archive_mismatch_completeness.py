"""Mismatch objects must never be counted as completed judgment mirrors.

The scheduler still treats mismatches as present to avoid overwriting
write-once archive objects; reporting must retain them as incomplete.
"""

import pytest
from sqlalchemy import select

from scraper.models import ArchiveObject
from scraper.storage.archive import ArchiveMirror
from tests.test_mirror_drive_concurrency import _drive_target
from tests.test_public_security_pdf_archive import _promote_one


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses,expected_incomplete,expected_backlog",
    [
        (["written", "written", "written"], 0, 0),
        (["mismatch", "mismatch", "mismatch"], 1, 0),
        (["written", "mismatch", "written"], 1, 0),
        (["written", "written", "missing"], 1, 1),
        (["failed", "failed", "failed"], 1, 1),
    ],
)
async def test_mismatch_never_claimed_complete(db, source, statuses, expected_incomplete, expected_backlog):
    j = await _promote_one(db, source, "PLD 2024 SC 991")
    t = await _drive_target(db)
    db.add_all([
        ArchiveObject(
            target_id=t.id,
            judgment_id=j.id,
            object_key=f"test/{j.id}/{index}",
            content_hash="a" * 64,
            byte_size=5,
            document_kind="text",
            status=status,
            access_method="public",
        )
        for index, status in enumerate(statuses)
    ])
    await db.commit()
    mirror = ArchiveMirror(db)
    lag = await mirror.lag(t)
    waiting = await mirror.batch_waiting(t)
    backlog = await mirror.backlog_judgments(t, limit=10)
    assert lag["judgments_total"] == 1
    assert lag["judgments_unmirrored"] == expected_incomplete
    assert waiting["judgments"] == expected_incomplete
    assert len(backlog) == expected_backlog
    ledger = (await db.execute(
        select(ArchiveObject.status).where(ArchiveObject.target_id == t.id)
    )).scalars().all()
    assert sorted(ledger) == sorted(statuses), "reporting must not mutate the archive ledger"
