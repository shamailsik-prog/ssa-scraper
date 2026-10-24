"""Link PLS judgments with ``court_id`` NULL to ``court`` rows (dry-run / --apply / --undo)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.judgment_court_link_registry import (
    MANUAL_REVIEW_LABEL_KEYS,
    pls_court_label_key,
    resolve_null_court_id_link,
)
from scraper.models import CorpusMetadata, Judgment
from scraper.parsers.foreign_court_registry import ensure_foreign_court_row, ensure_link_court_row, resolve_court_row_for_name

logger = logging.getLogger(__name__)

LATEST_RUN_KEY = "judgment_court_link_latest_run_id"
RUN_SNAPSHOT_PREFIX = "judgment_court_link_undo:"


def _undo_snapshot_key(run_id: str) -> str:
    return f"{RUN_SNAPSHOT_PREFIX}{run_id}"


def _undo_prior_latest_key(run_id: str) -> str:
    return f"{RUN_SNAPSHOT_PREFIX}{run_id}:prior_latest"


async def _record_run(db: AsyncSession, run_id: str) -> None:
    prior = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
    prior_val = prior.value if prior is not None else None
    db.add(CorpusMetadata(key=_undo_prior_latest_key(run_id), value=prior_val or ""))
    row = prior
    if row is None:
        db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
    else:
        row.value = run_id


async def _null_court_id_judgment_ids(db: AsyncSession, *, limit: Optional[int] = None) -> List[UUID]:
    q = """
    SELECT j.id
    FROM judgment j
    WHERE j.source_name = 'PakistanLawSite'
      AND j.court_id IS NULL
      AND j.court_name IS NOT NULL
      AND btrim(j.court_name) <> ''
    ORDER BY j.canonical_citation NULLS LAST
    """
    if limit is not None:
        q += f" LIMIT {int(limit)}"
    return [row.id for row in (await db.execute(text(q))).all()]


async def _court_row_for_resolution(
    db: AsyncSession,
    resolution,
    *,
    create: bool,
) -> Optional[Any]:
    row = await resolve_court_row_for_name(db, resolution.target_court_name, create=False)
    if row is not None:
        return row
    if not create:
        return None
    row = await ensure_foreign_court_row(db, resolution.target_court_name)
    if row is not None:
        return row
    return await ensure_link_court_row(db, resolution.target_court_name)


async def find_court_id_links(db: AsyncSession, *, limit: Optional[int] = None, create: bool = False) -> Dict[str, Any]:
    ids = await _null_court_id_judgment_ids(db, limit=limit)
    links: List[Dict[str, str]] = []
    manual_review: List[Dict[str, str]] = []
    would_create: List[Dict[str, str]] = []
    skipped = 0
    for jid in ids:
        j = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().first()
        if j is None:
            continue
        resolution = resolve_null_court_id_link(j.court_name or "", j.full_text)
        if resolution is None:
            skipped += 1
            continue
        if resolution.manual_review:
            manual_review.append(
                {
                    "id": str(j.id),
                    "citation": j.canonical_citation or "",
                    "court_name": j.court_name or "",
                    "label_key": pls_court_label_key(j.court_name),
                }
            )
            continue
        court_row = await _court_row_for_resolution(db, resolution, create=create)
        if court_row is None:
            would_create.append(
                {
                    "id": str(j.id),
                    "citation": j.canonical_citation or "",
                    "court_name": j.court_name or "",
                    "target_court_name": resolution.target_court_name,
                    "reason": resolution.reason,
                }
            )
            continue
        links.append(
            {
                "id": str(j.id),
                "citation": j.canonical_citation or "",
                "court_name": j.court_name or "",
                "court_id_from": "",
                "court_id_to": str(court_row.id),
                "target_court_name": court_row.name,
                "reason": resolution.reason,
            }
        )
    return {
        "links": links,
        "count": len(links),
        "manual_review": manual_review,
        "manual_review_count": len(manual_review),
        "would_create": would_create,
        "would_create_count": len(would_create),
        "unresolved_skipped": skipped,
    }


async def apply_court_id_links(db: AsyncSession, run_id: str, links: List[Dict[str, str]]) -> Dict[str, int]:
    snapshot: List[Dict[str, str]] = []
    updated = 0
    for item in links:
        jid = UUID(item["id"])
        j = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().first()
        if j is None:
            continue
        new_id = UUID(item["court_id_to"])
        if j.court_id == new_id:
            continue
        snapshot.append(
            {
                "id": str(j.id),
                "court_id": "",
                "applied_court_id": str(new_id),
            }
        )
        j.court_id = new_id
        updated += 1
    db.add(CorpusMetadata(key=_undo_snapshot_key(run_id), value=json.dumps(snapshot)))
    return {"court_ids_updated": updated}


async def undo_run(db: AsyncSession, run_id: str) -> Dict[str, int]:
    snap_row = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_snapshot_key(run_id)))
    ).scalars().first()
    restored = 0
    if snap_row is not None and snap_row.value:
        for item in json.loads(snap_row.value):
            jid = UUID(item["id"])
            j = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().first()
            if j is None:
                continue
            applied_id = item.get("applied_court_id") or ""
            if applied_id and str(j.court_id or "") == applied_id:
                old_id = item.get("court_id") or ""
                j.court_id = UUID(old_id) if old_id else None
                restored += 1
        await db.delete(snap_row)
    prior_row = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_prior_latest_key(run_id)))
    ).scalars().first()
    prior_latest = prior_row.value if prior_row is not None else None
    latest = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
    if latest is not None and latest.value == run_id:
        if prior_latest:
            latest.value = prior_latest
        else:
            await db.delete(latest)
    if prior_row is not None:
        await db.delete(prior_row)
    await db.commit()
    return {"court_ids_restored": restored}


async def run_judgment_court_link(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    async with SessionLocal() as db:
        out: Dict[str, Any] = {
            "dry_run": not apply and not undo,
            "SSA_JUDGMENT_COURT_ID_BACKFILL": settings.SSA_JUDGMENT_COURT_ID_BACKFILL,
            "manual_review_label_keys": sorted(MANUAL_REVIEW_LABEL_KEYS),
        }
        if undo:
            out["undone"] = await undo_run(db, undo)
            return out
        info = await find_court_id_links(db, limit=limit, create=apply)
        out.update(info)
        if apply:
            if not settings.SSA_JUDGMENT_COURT_ID_BACKFILL:
                out["applied"] = {"skipped": "enable SSA_JUDGMENT_COURT_ID_BACKFILL"}
                return out
            run_id = uuid.uuid4().hex
            await _record_run(db, run_id)
            applied = await apply_court_id_links(db, run_id, info["links"])
            await db.commit()
            out["applied"] = {"run_id": run_id, **applied}
        return out


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Link PLS judgments with court_id NULL (dry-run default; --apply / --undo RUN_ID)",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--undo", metavar="RUN_ID")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    print(json.dumps(run_async(run_judgment_court_link(apply=args.apply, undo=args.undo, limit=args.limit)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
