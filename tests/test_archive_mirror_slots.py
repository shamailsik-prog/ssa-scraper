"""An archive is complete only when all three distinct judgment artifacts exist.

Duplicate PDFs or unrelated ledger objects must not conceal a missing
judgment.txt or metadata.json. No remote objects are modified by these tests.
"""

import pytest
from sqlalchemy import select

from scraper.models import ArchiveObject
from scraper.storage.archive import ArchiveMirror, judgment_prefix
from tests.test_mirror_drive_concurrency import _drive_target
from tests.test_public_security_pdf_archive import _promote_one


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffixes,complete",
    [
        (["original.pdf", "rendered_copy.pdf", "judgment.txt"], False),
        (["metadata.json", "judgment.txt", "unrelated.dat"], False),
        (["original.pdf", "rendered_copy.pdf", "metadata.json"], False),
        (["original.pdf", "judgment.txt", "metadata.json"], True),
        (["original.pdf", "rendered_copy.pdf", "judgment.txt", "metadata.json"], True),
    ],
)
async def test_distinct_archive_slots_not_raw_object_count(db, source, suffixes, complete):
    j = await _promote_one(db, source, "PLD 2024 SC 992")
    t = await _drive_target(db)
    prefix = judgment_prefix(j)
    db.add_all([
        ArchiveObject(
            target_id=t.id,
            judgment_id=j.id,
            object_key=f"{prefix}/{suffix}",
            content_hash="a" * 64,
            byte_size=5,
            document_kind="text",
            status="written",
            access_method="public",
        )
        for suffix in suffixes
    ])
    await db.commit()
    mirror = ArchiveMirror(db)
    lag = await mirror.lag(t)
    waiting = await mirror.batch_waiting(t)
    backlog = await mirror.backlog_judgments(t, limit=10)
    expected = 0 if complete else 1
    assert lag["judgments_total"] == 1
    assert lag["judgments_unmirrored"] == expected
    assert waiting["judgments"] == expected
    assert len(backlog) == expected
    statuses = (await db.execute(
        select(ArchiveObject.status).where(ArchiveObject.target_id == t.id)
    )).scalars().all()
    assert statuses == ["written"] * len(suffixes)
