"""9 October 2026: one mirror run walks the targets in turn against a single 15-minute budget. With a backlog the
first targets spent it all, and the "local" target, always last, wrote nothing for hours (90,512 objects against
~100,000 on the others). Targets now run least-recently-completed first, so a starved target leads the next run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from scraper.models import ArchiveObject, ArchiveTarget
from scraper.storage.archive import ArchiveMirror


async def test_targets_run_least_recently_completed_first(db, tmp_path):
    await db.execute(delete(ArchiveObject))
    await db.execute(delete(ArchiveTarget))
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            ArchiveTarget(name="google_drive", target_type="local_path", root_path=str(tmp_path / "g"), last_ok_at=now - timedelta(minutes=5)),
            ArchiveTarget(name="local_export", target_type="local_path", root_path=str(tmp_path / "e"), last_ok_at=now - timedelta(minutes=30)),
            ArchiveTarget(name="local", target_type="local_path", root_path=str(tmp_path / "l"), last_ok_at=now - timedelta(hours=2)),
            ArchiveTarget(name="new_target", target_type="local_path", root_path=str(tmp_path / "n"), last_ok_at=None),
            ArchiveTarget(name="off", target_type="local_path", root_path=str(tmp_path / "o"), enabled=False),
        ]
    )
    await db.flush()
    names = [t.name for t in await ArchiveMirror(db).targets()]
    assert names == ["new_target", "local", "local_export", "google_drive"]
    await db.rollback()
