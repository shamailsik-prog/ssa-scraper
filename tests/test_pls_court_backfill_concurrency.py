"""Court-name backfill must not overwrite subsequent manual or automated corrections.

All tests use the repository DB fixture; no live corpus is modified.
"""
from uuid import uuid4

import pytest

from scraper.models import Judgment
from scraper.tasks.judgment_court_backfill import (
    _record_run,
    _undo_snapshot_key,
    apply_court_fixes,
    undo_run,
)
from sqlalchemy import select
from scraper.models import CorpusMetadata


async def _judgment(db, court="Supreme Court of Pakistan"):
    jid = uuid4()
    j = Judgment(
        id=jid,
        source_name="PakistanLawSite",
        court_name=court,
        canonical_citation=f"1990 MLD {str(jid.int)[-8:]}",
        full_text="1990 MLD 947\\n[Supreme Court of India]\\n" + "x" * 600,
        full_text_hash=jid.hex,
        access_method="rendered_copy",
    )
    db.add(j)
    await db.commit()
    return j


def _fix(j, old="Supreme Court of Pakistan", new="Supreme Court of India"):
    return {"id": str(j.id), "from": old, "to": new}


@pytest.mark.asyncio
async def test_stale_backfill_proposal_does_not_overwrite_manual_edit(db):
    j = await _judgment(db)
    j.court_name = "Sindh High Court"
    await db.commit()
    run_id = uuid4().hex
    await _record_run(db, run_id)
    assert await apply_court_fixes(db, run_id, [_fix(j)]) == 0
    await db.commit()
    await db.refresh(j)
    assert j.court_name == "Sindh High Court"
    snap = (await db.execute(select(CorpusMetadata).where(
        CorpusMetadata.key == _undo_snapshot_key(run_id)
    ))).scalars().one()
    assert snap.value == "[]"


@pytest.mark.asyncio
async def test_undo_refuses_to_erase_later_manual_correction(db):
    j = await _judgment(db)
    run_id = uuid4().hex
    await _record_run(db, run_id)
    assert await apply_court_fixes(db, run_id, [_fix(j)]) == 1
    await db.commit()
    j.court_name = "Lahore High Court"
    await db.commit()
    with pytest.raises(ValueError, match="changed or disappeared"):
        await undo_run(db, run_id)
    await db.refresh(j)
    assert j.court_name == "Lahore High Court"
    snap = (await db.execute(select(CorpusMetadata).where(
        CorpusMetadata.key == _undo_snapshot_key(run_id)
    ))).scalars().one_or_none()
    assert snap is not None  # failed undo must preserve rollback evidence


@pytest.mark.asyncio
async def test_older_run_cannot_undo_newer_run(db):
    j = await _judgment(db)
    first, second = uuid4().hex, uuid4().hex
    await _record_run(db, first)
    assert await apply_court_fixes(db, first, [_fix(j)]) == 1
    await db.commit()
    await _record_run(db, second)
    assert await apply_court_fixes(db, second, []) == 0
    await db.commit()
    with pytest.raises(ValueError, match="not the latest"):
        await undo_run(db, first)
    await db.refresh(j)
    assert j.court_name == "Supreme Court of India"


@pytest.mark.asyncio
async def test_undo_all_or_nothing_if_second_judgment_changed(db):
    first, second = await _judgment(db), await _judgment(db)
    run_id = uuid4().hex
    await _record_run(db, run_id)
    assert await apply_court_fixes(db, run_id, [_fix(first), _fix(second)]) == 2
    await db.commit()
    second.court_name = "Lahore High Court"
    await db.commit()
    with pytest.raises(ValueError, match="changed or disappeared"):
        await undo_run(db, run_id)
    await db.refresh(first)
    await db.refresh(second)
    assert first.court_name == "Supreme Court of India"
    assert second.court_name == "Lahore High Court"


@pytest.mark.asyncio
async def test_unchanged_latest_run_can_be_undone(db):
    j = await _judgment(db)
    run_id = uuid4().hex
    await _record_run(db, run_id)
    assert await apply_court_fixes(db, run_id, [_fix(j)]) == 1
    await db.commit()
    assert await undo_run(db, run_id) == {"court_names_restored": 1}
    await db.refresh(j)
    assert j.court_name == "Supreme Court of Pakistan"
