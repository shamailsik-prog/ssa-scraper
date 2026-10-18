"""Read-only Drive/archive mismatch audit (no writes to remote storage)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import func, select

from scraper.database import SessionLocal, run_async
from scraper.models import ArchiveObject, ArchiveTarget, Judgment

logger = logging.getLogger(__name__)


async def summarize_archive_health() -> Dict[str, Any]:
    async with SessionLocal() as db:
        targets = (await db.execute(select(ArchiveTarget))).scalars().all()
        by_target: List[Dict[str, Any]] = []
        for t in targets:
            failed = int(
                (await db.execute(select(func.count()).select_from(ArchiveObject).where(ArchiveObject.target_id == t.id, ArchiveObject.status == "failed"))).scalar()
                or 0
            )
            mismatch = int(
                (
                    await db.execute(
                        select(func.count()).select_from(ArchiveObject).where(
                            ArchiveObject.target_id == t.id, ArchiveObject.status == "mismatch"
                        )
                    )
                ).scalar()
                or 0
            )
            missing = int(
                (
                    await db.execute(
                        select(func.count()).select_from(ArchiveObject).where(
                            ArchiveObject.target_id == t.id, ArchiveObject.status == "missing"
                        )
                    )
                ).scalar()
                or 0
            )
            by_target.append(
                {
                    "name": t.name,
                    "type": t.target_type,
                    "enabled": t.enabled,
                    "objects_written": t.objects_written,
                    "objects_failed": t.objects_failed,
                    "failed_rows": failed,
                    "mismatch_rows": mismatch,
                    "missing_rows": missing,
                    "last_ok_at": t.last_ok_at.isoformat() if t.last_ok_at else None,
                }
            )
        mirrored_ids = select(ArchiveObject.judgment_id).where(ArchiveObject.judgment_id.isnot(None))
        unmirrored = int(
            (
                await db.execute(
                    select(func.count()).select_from(Judgment).where(
                        Judgment.source_name == "PakistanLawSite", Judgment.id.notin_(mirrored_ids)
                    )
                )
            ).scalar()
            or 0
        )
        return {"targets": by_target, "pls_judgments_without_archive_object": unmirrored}


def main() -> None:
    import json

    print(json.dumps(run_async(summarize_archive_health()), indent=2, default=str))


if __name__ == "__main__":
    main()
