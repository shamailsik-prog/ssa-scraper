"""13 October 2026: about 11,000 held court judgments were released into the corpus at once and the backup
backlog grew instead of shrinking. A run that took a full backlog slice stopped and waited up to 30 minutes for
Beat, and every target re-rendered each judgment's PDF for itself."""

from __future__ import annotations

from sqlalchemy import select

from scraper.models import ArchiveObject, ArchiveTarget
from scraper.storage import archive as archive_mod
from scraper.storage.archive import ArchiveMirror
from tests.test_pls_durable_fixes import _mk_judgments


async def _judgments_on(db, name):
    db.expire_all()
    t = (await db.execute(select(ArchiveTarget).where(ArchiveTarget.name == name))).scalars().first()
    rows = (
        await db.execute(
            select(ArchiveObject.judgment_id).where(ArchiveObject.target_id == t.id, ArchiveObject.status == "written", ArchiveObject.judgment_id.isnot(None))
        )
    ).scalars().all()
    return len(set(rows))


async def test_a_run_that_fills_its_backlog_slice_queues_the_next_slice(db, tmp_path, monkeypatch):
    from scraper.config import settings
    from scraper.tasks import archive_mirror, chain

    await _mk_judgments(db, 8)
    db.add(ArchiveTarget(name="local", target_type="local_path", root_path=str(tmp_path / "a")))
    await db.commit()
    requested = []
    monkeypatch.setattr(chain, "request_mirror", lambda: requested.append(1) or True)
    monkeypatch.setattr(settings, "MIRROR_BACKLOG_PER_RUN", 3)

    result = await archive_mirror.mirror_pending(limit=2)
    assert result["out_of_time"] is False
    assert result["more_pending"] is True and requested == [1]  # more is owed: the next slice starts now
    assert await _judgments_on(db, "local") == 5

    result = await archive_mirror.mirror_pending(limit=2)
    assert await _judgments_on(db, "local") == 8
    # the second slice found only the 3 left: nothing more is owed, so no follow-up is queued
    assert result["more_pending"] is False and requested == [1]


async def test_each_judgment_is_rendered_once_per_run_for_every_target(db, tmp_path, monkeypatch):
    await _mk_judgments(db, 4)
    db.add(ArchiveTarget(name="local", target_type="local_path", root_path=str(tmp_path / "a")))
    db.add(ArchiveTarget(name="local_export", target_type="local_path", root_path=str(tmp_path / "b")))
    await db.commit()
    renders = []
    real_render = archive_mod.render_judgment_pdf_bytes

    def counting_render(meta, text):
        renders.append(meta["citation"])
        return real_render(meta, text)

    monkeypatch.setattr(archive_mod, "render_judgment_pdf_bytes", counting_render)
    await ArchiveMirror(db).mirror_pending(limit=10, backlog_limit=10)
    await db.commit()
    assert await _judgments_on(db, "local") == 4 and await _judgments_on(db, "local_export") == 4
    assert sorted(renders) == sorted(set(renders)) and len(renders) == 4  # one render each, not one per target


async def test_local_disks_lead_a_run_even_when_google_drive_waited_longer(db, tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    db.add(ArchiveTarget(name="google_drive", target_type="google_drive", root_path="x", last_ok_at=now - timedelta(hours=5)))
    db.add(ArchiveTarget(name="local_export", target_type="local_path", root_path=str(tmp_path / "b"), last_ok_at=now - timedelta(hours=1)))
    db.add(ArchiveTarget(name="local", target_type="local_path", root_path=str(tmp_path / "a"), last_ok_at=now - timedelta(hours=2)))
    await db.commit()
    mirror = ArchiveMirror(db)
    assert [t.name for t in await mirror.targets()] == ["local", "local_export", "google_drive"]
    # reconcile keeps its own fairness order, untouched
    assert {t.name for t in await mirror.targets(for_reconcile=True)} == {"local", "local_export", "google_drive"}
