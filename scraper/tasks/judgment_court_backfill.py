"""Backfill judgment court_name when PLS grid defaulted to SC Pakistan or SC/HC header disagrees."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.models import CorpusMetadata, Judgment
from scraper.parsers.pls_court_resolution import foreign_court_mislabel_sql_predicate, proposed_court_name

logger = logging.getLogger(__name__)

LATEST_RUN_KEY = "judgment_court_backfill_latest_run_id"
RUN_SNAPSHOT_PREFIX = "judgment_court_backfill_undo:"


def _undo_snapshot_key(run_id: str) -> str:
    return f"{RUN_SNAPSHOT_PREFIX}{run_id}"


def _undo_prior_latest_key(run_id: str) -> str:
    return f"{RUN_SNAPSHOT_PREFIX}{run_id}:prior_latest"


async def _record_run(db: AsyncSession, run_id: str) -> None:
    prior = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY).with_for_update())).scalars().first()
    prior_val = prior.value if prior is not None else None
    db.add(CorpusMetadata(key=_undo_prior_latest_key(run_id), value=prior_val or ""))
    row = prior
    if row is None:
        db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
    else:
        row.value = run_id


async def find_court_fixes(db: AsyncSession, *, limit: Optional[int] = None) -> Dict[str, Any]:
    if not settings.SSA_PLS_COURT_RESOLUTION:
        return {"disabled": True, "fixes": []}
    q = f"""
    SELECT j.id, j.court_name, j.canonical_citation
    FROM judgment j
    WHERE j.source_name = 'PakistanLawSite'
      AND j.full_text IS NOT NULL
      AND length(btrim(j.full_text)) > 0
      AND (
        ({foreign_court_mislabel_sql_predicate()})
        OR (
          j.court_name ILIKE '%Supreme Court of Pakistan%'
          AND left(j.full_text, 8000) ~* '(?i)in\\s+the\\s+high\\s+court'
        )
        OR (
          j.court_name ILIKE '%high court%'
          AND left(j.full_text, 8000) ~* '(?i)in\\s+the\\s+supreme\\s+court\\s+of\\s+pakistan'
        )
      )
    ORDER BY j.canonical_citation NULLS LAST
    """
    if limit is not None:
        q += f" LIMIT {int(limit)}"
    rows = (await db.execute(text(q))).all()
    fixes: List[Dict[str, str]] = []
    for row in rows:
        jid = row.id
        stored = row.court_name
        j = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().first()
        if j is None:
            continue
        proposed = proposed_court_name(stored, j.full_text)
        if proposed and proposed != stored:
            fixes.append(
                {
                    "id": str(j.id),
                    "citation": j.canonical_citation or "",
                    "from": stored or "",
                    "to": proposed,
                }
            )
    return {"fixes": fixes, "count": len(fixes)}


async def apply_court_fixes(db: AsyncSession, run_id: str, fixes: List[Dict[str, str]]) -> int:
    """Apply only proposals whose original court is still unchanged.

    Lock each row so a backfill cannot overwrite a concurrent curator's edit.
    Snapshot the *applied* value as well, for conditional, non-destructive undo.
    """
    snapshot: List[Dict[str, str]] = []
    n = 0
    for fix in fixes:
        jid = UUID(fix["id"])
        j = (await db.execute(
            select(Judgment).where(Judgment.id == jid).with_for_update()
        )).scalars().first()
        if j is None or j.source_name != "PakistanLawSite":
            continue
        if (j.court_name or "") != fix["from"] or (j.court_name or "") == fix["to"]:
            continue  # stale proposal or already corrected; never overwrite
        snapshot.append({
            "id": str(j.id),
            "court_name": j.court_name or "",
            "applied_court_name": fix["to"],
        })
        j.court_name = fix["to"]
        n += 1
    db.add(CorpusMetadata(key=_undo_snapshot_key(run_id), value=json.dumps(snapshot)))
    return n


async def undo_run(db: AsyncSession, run_id: str) -> Dict[str, int]:
    """Undo only the latest run, and only if no court was edited afterwards.

    Validate the entire snapshot under row locks before changing anything:
    one conflicting or missing judgment must not cause a partial rollback.
    """
    latest = (await db.execute(
        select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY).with_for_update()
    )).scalars().first()
    if latest is None or latest.value != run_id:
        raise ValueError("court backfill undo refused: run is not the latest applied run")
    snap_row = (await db.execute(
        select(CorpusMetadata).where(CorpusMetadata.key == _undo_snapshot_key(run_id)).with_for_update()
    )).scalars().first()
    prior_row = (await db.execute(
        select(CorpusMetadata).where(CorpusMetadata.key == _undo_prior_latest_key(run_id)).with_for_update()
    )).scalars().first()
    if snap_row is None or not snap_row.value or prior_row is None:
        raise ValueError("court backfill undo refused: complete run snapshot is unavailable")
    snapshot = json.loads(snap_row.value)
    to_restore = []
    for item in snapshot:
        if "applied_court_name" not in item:
            raise ValueError("court backfill undo refused: snapshot lacks applied-value provenance")
        jid = UUID(item["id"])
        j = (await db.execute(
            select(Judgment).where(Judgment.id == jid).with_for_update()
        )).scalars().first()
        if j is None or j.court_name != item["applied_court_name"]:
            raise ValueError(f"court backfill undo refused: judgment {jid} changed or disappeared")
        to_restore.append((j, item["court_name"] or None))
    for j, old_court in to_restore:
        j.court_name = old_court
    await db.delete(snap_row)
    if prior_row.value:
        latest.value = prior_row.value
    else:
        await db.delete(latest)
    await db.delete(prior_row)
    await db.commit()
    return {"court_names_restored": len(to_restore)}


async def run_judgment_court_backfill(*, apply: bool = False, undo: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    async with SessionLocal() as db:
        out: Dict[str, Any] = {
            "dry_run": not apply and not undo,
            "SSA_PLS_COURT_RESOLUTION": settings.SSA_PLS_COURT_RESOLUTION,
        }
        if undo:
            out["undone"] = await undo_run(db, undo)
            return out
        info = await find_court_fixes(db, limit=limit)
        out.update(info)
        if apply:
            if not settings.SSA_PLS_COURT_RESOLUTION:
                out["applied"] = {"skipped": "SSA_PLS_COURT_RESOLUTION disabled"}
                return out
            run_id = uuid.uuid4().hex
            await _record_run(db, run_id)
            applied_n = await apply_court_fixes(db, run_id, info["fixes"])
            await db.commit()
            out["applied"] = {"run_id": run_id, "court_names_updated": applied_n}
        return out


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="PLS court_name backfill (dry-run default; --apply / --undo RUN_ID)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--undo", metavar="RUN_ID")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    print(json.dumps(run_async(run_judgment_court_backfill(apply=args.apply, undo=args.undo, limit=args.limit)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
