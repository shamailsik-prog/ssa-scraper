"""Backfill judgment court_name and court_id when PLS grid defaulted to SC Pakistan or header disagrees."""

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
from scraper.judgment_court_consistency import (
    consistency_report,
    court_labels_match,
    find_judgment_court_mismatches,
)
from scraper.models import CorpusMetadata, Judgment
from scraper.parsers.foreign_court_registry import (
    foreign_seed_canonical_names,
    normalize_judgment_court_name,
    resolve_court_row_for_name,
)
from scraper.parsers.pls_court_resolution import foreign_court_mislabel_sql_predicate, proposed_court_name

logger = logging.getLogger(__name__)

LATEST_RUN_KEY = "judgment_court_backfill_latest_run_id"
RUN_SNAPSHOT_PREFIX = "judgment_court_backfill_undo:"


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


async def _candidate_judgment_ids(db: AsyncSession, *, limit: Optional[int] = None) -> List[UUID]:
    q = f"""
    SELECT j.id
    FROM judgment j
    WHERE j.source_name = 'PakistanLawSite'
      AND j.full_text IS NOT NULL
      AND length(btrim(j.full_text)) > 0
      AND (
        ({foreign_court_mislabel_sql_predicate()})
        OR (
          j.court_name ILIKE 'Supreme Court of Pakistan'
          AND left(j.full_text, 400) ~* '(?m)^\\s*IN\\s+THE\\s+HIGH\\s+COURT'
        )
        OR (
          j.court_name ILIKE '%high court%'
          AND left(j.full_text, 400) ~* '(?m)^\\s*IN\\s+THE\\s+SUPREME\\s+COURT\\s+OF\\s+PAKISTAN'
        )
        OR (
          j.court_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM court c
            WHERE c.id = j.court_id
              AND NOT (
                lower(regexp_replace(btrim(j.court_name), '\\s+', ' ', 'g'))
                = lower(regexp_replace(btrim(c.name), '\\s+', ' ', 'g'))
              )
          )
        )
        OR j.court_name ILIKE '%ANDHRA%PRADESH%'
      )
    ORDER BY j.canonical_citation NULLS LAST
    """
    if limit is not None:
        q += f" LIMIT {int(limit)}"
    return [row.id for row in (await db.execute(text(q))).all()]


async def _build_fix_for_judgment(db: AsyncSession, j: Judgment, *, create: bool) -> Optional[Dict[str, str]]:
    stored = j.court_name or ""
    proposed_name = proposed_court_name(stored, j.full_text, citation=j.canonical_citation)
    if proposed_name and proposed_name != stored:
        target_label = proposed_name
    else:
        target_label = normalize_judgment_court_name(stored) or stored
    if not target_label:
        return None
    court_row = await resolve_court_row_for_name(db, target_label, create=create)
    would_create = court_row is None and not create
    if court_row is None and create:
        court_row = await resolve_court_row_for_name(db, stored, create=create)
    if court_row is None:
        if would_create:
            return {
                "id": str(j.id),
                "citation": j.canonical_citation or "",
                "court_name_from": stored,
                "court_name_to": target_label,
                "court_id_from": str(j.court_id) if j.court_id else "",
                "court_id_to": "",
                "would_create_court": "1",
            }
        return None
    final_name = court_row.name
    name_change = final_name != stored
    id_change = j.court_id != court_row.id
    if not name_change and not id_change:
        return None
    if not name_change and id_change and not court_labels_match(stored, final_name):
        return None
    foreign_targets = foreign_seed_canonical_names()
    if name_change and final_name not in foreign_targets:
        name_change = False
    return {
        "id": str(j.id),
        "citation": j.canonical_citation or "",
        "court_name_from": stored,
        "court_name_to": final_name if name_change else stored,
        "court_id_from": str(j.court_id) if j.court_id else "",
        "court_id_to": str(court_row.id),
    }


async def find_court_fixes(db: AsyncSession, *, limit: Optional[int] = None, create: bool = False) -> Dict[str, Any]:
    ids = await _candidate_judgment_ids(db, limit=limit)
    fixes: List[Dict[str, str]] = []
    would_create: List[Dict[str, str]] = []
    for jid in ids:
        j = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().first()
        if j is None:
            continue
        fix = await _build_fix_for_judgment(db, j, create=create)
        if not fix:
            continue
        if fix.get("would_create_court"):
            would_create.append(fix)
        fixes.append(fix)
    return {"fixes": fixes, "count": len(fixes), "would_create": would_create, "would_create_count": len(would_create)}


async def apply_court_fixes(db: AsyncSession, run_id: str, fixes: List[Dict[str, str]]) -> Dict[str, int]:
    snapshot: List[Dict[str, str]] = []
    names = 0
    ids = 0
    for fix in fixes:
        if not fix.get("court_id_to") and settings.SSA_JUDGMENT_COURT_ID_BACKFILL:
            target = fix.get("court_name_to") or fix.get("court_name_from") or ""
            row = await resolve_court_row_for_name(db, target, create=True)
            if row is not None:
                fix = {**fix, "court_id_to": str(row.id)}
        if not fix.get("court_id_to"):
            continue
        jid = UUID(fix["id"])
        j = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().first()
        if j is None:
            continue
        prior_name = j.court_name or ""
        prior_id = str(j.court_id) if j.court_id else ""
        applied_name = False
        applied_id = False
        foreign_targets = foreign_seed_canonical_names()
        if (
            fix.get("court_name_to")
            and prior_name != fix["court_name_to"]
            and settings.SSA_PLS_COURT_RESOLUTION
            and fix["court_name_to"] in foreign_targets
        ):
            j.court_name = fix["court_name_to"]
            names += 1
            applied_name = True
        new_id = UUID(fix["court_id_to"]) if fix.get("court_id_to") else None
        if settings.SSA_JUDGMENT_COURT_ID_BACKFILL and new_id and j.court_id != new_id:
            j.court_id = new_id
            ids += 1
            applied_id = True
        if not applied_name and not applied_id:
            continue
        snapshot.append(
            {
                "id": str(j.id),
                "court_name": prior_name,
                "court_id": prior_id,
                "applied_court_name": fix["court_name_to"] if applied_name else "",
                "applied_court_id": fix["court_id_to"] if applied_id else "",
            }
        )
    db.add(CorpusMetadata(key=_undo_snapshot_key(run_id), value=json.dumps(snapshot)))
    return {"court_names_updated": names, "court_ids_updated": ids}


async def undo_run(db: AsyncSession, run_id: str) -> Dict[str, int]:
    snap_row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_snapshot_key(run_id)))).scalars().first()
    restored_names = 0
    restored_ids = 0
    if snap_row is not None and snap_row.value:
        for item in json.loads(snap_row.value):
            jid = UUID(item["id"])
            j = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().first()
            if j is None:
                continue
            applied_name = item.get("applied_court_name") or item.get("applied_to") or ""
            applied_id = item.get("applied_court_id") or ""
            if applied_name and (j.court_name or "") == applied_name:
                j.court_name = item.get("court_name") or None
                restored_names += 1
            if applied_id and applied_id.strip() and str(j.court_id or "") == applied_id:
                old_id = item.get("court_id") or ""
                j.court_id = UUID(old_id) if old_id else None
                restored_ids += 1
        await db.delete(snap_row)
    prior_row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_prior_latest_key(run_id)))).scalars().first()
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
    return {"court_names_restored": restored_names, "court_ids_restored": restored_ids}


async def run_judgment_court_backfill(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
    limit: Optional[int] = None,
    consistency_report_only: bool = False,
) -> Dict[str, Any]:
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    async with SessionLocal() as db:
        out: Dict[str, Any] = {
            "dry_run": not apply and not undo and not consistency_report_only,
            "SSA_PLS_COURT_RESOLUTION": settings.SSA_PLS_COURT_RESOLUTION,
            "SSA_JUDGMENT_COURT_ID_BACKFILL": settings.SSA_JUDGMENT_COURT_ID_BACKFILL,
        }
        if consistency_report_only:
            out["consistency"] = await consistency_report(db, limit=limit)
            return out
        if undo:
            out["undone"] = await undo_run(db, undo)
            return out
        info = await find_court_fixes(db, limit=limit, create=apply and settings.SSA_JUDGMENT_COURT_ID_BACKFILL)
        out.update(info)
        report = await consistency_report(db, limit=limit)
        out["null_court_id_count"] = report["null_court_id_count"]
        out["real_mismatch_count"] = report["real_mismatch_count"]
        if apply:
            if not settings.SSA_PLS_COURT_RESOLUTION and not settings.SSA_JUDGMENT_COURT_ID_BACKFILL:
                out["applied"] = {"skipped": "enable SSA_PLS_COURT_RESOLUTION or SSA_JUDGMENT_COURT_ID_BACKFILL"}
                return out
            run_id = uuid.uuid4().hex
            await _record_run(db, run_id)
            if settings.SSA_JUDGMENT_COURT_ID_BACKFILL:
                info = await find_court_fixes(db, limit=limit, create=True)
            applied = await apply_court_fixes(db, run_id, info["fixes"])
            await db.commit()
            out["applied"] = {"run_id": run_id, **applied}
        return out


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="PLS court_name/court_id backfill (dry-run default; --apply / --undo RUN_ID / --consistency-report)",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--undo", metavar="RUN_ID")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--consistency-report", action="store_true", help="Report court_id vs court_name mismatches only")
    args = parser.parse_args(argv)
    print(
        json.dumps(
            run_async(
                run_judgment_court_backfill(
                    apply=args.apply,
                    undo=args.undo,
                    limit=args.limit,
                    consistency_report_only=args.consistency_report,
                )
            ),
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
