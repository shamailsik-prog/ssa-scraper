"""Re-key judgments whose stored citation disagrees with the header citation in preserved text."""

from __future__ import annotations

import csv
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal, run_async
from scraper.models import Citation, CorpusMetadata, Judgment, PlsSearchHarvestQuery, ScraperStaging
from scraper.parsers.header_citation import (
    citation_key,
    leading_header_citation_judgment,
    leading_header_citation_staging,
    mismatch_kind,
    rekey_actionable,
)
from scraper.pls_search_harvest_core import make_query_key, normalize_query_json
from scraper.tasks.promotion import _parse_citation_parts

logger = logging.getLogger(__name__)

SOURCE_NAME = "PakistanLawSite"
BACKUP_KEY = "citation_header_rekey_backup_{run_id}"
UNDO_LOG_KEY = "citation_header_rekey_undo_{run_id}"
REFETCH_KEY = "citation_header_rekey_refetch_{run_id}"


async def _citation_taken(db: AsyncSession, citation: str, *, exclude_judgment_id: Optional[UUID] = None) -> Optional[UUID]:
    cit = citation_key(citation)
    if not cit:
        return None
    row = (await db.execute(select(Citation).where(Citation.citation_string == cit))).scalars().first()
    if row is not None and (exclude_judgment_id is None or row.judgment_id != exclude_judgment_id):
        return row.judgment_id
    j = (await db.execute(select(Judgment).where(Judgment.canonical_citation == cit))).scalars().first()
    if j is not None and (exclude_judgment_id is None or j.id != exclude_judgment_id):
        return j.id
    return None


async def _queue_citation_refetch(db: AsyncSession, citation: str, *, reason: str) -> Optional[str]:
    cit = citation_key(citation)
    if not cit:
        return None
    q = normalize_query_json({"citation": cit})
    key = make_query_key(q)
    existing = (
        await db.execute(
            select(PlsSearchHarvestQuery).where(
                PlsSearchHarvestQuery.source_name == SOURCE_NAME,
                PlsSearchHarvestQuery.query_key == key,
            )
        )
    ).scalars().first()
    if existing is not None:
        return key
    db.add(
        PlsSearchHarvestQuery(
            source_name=SOURCE_NAME,
            query_key=key,
            query_json=q,
            status="pending",
            priority=50,
            split_reason=reason[:500],
        )
    )
    return key


def _judgment_row(j: Judgment, header: str, kind: str, action: str, detail: str = "") -> Dict[str, Any]:
    return {
        "kind": "judgment",
        "judgment_id": str(j.id),
        "stored": j.canonical_citation,
        "header": header,
        "mismatch": kind,
        "action": action,
        "detail": detail,
    }


async def scan_header_rekeys(db: AsyncSession) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    rows_out: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {
        "judgments_scanned": 0,
        "staging_scanned": 0,
        "aligned": 0,
        "would_rekey": 0,
        "conflict_refetch": 0,
        "weak_candidate": 0,
        "staging_would_rekey": 0,
    }
    last_id = None
    while True:
        q = (
            select(Judgment)
            .where(Judgment.source_name == SOURCE_NAME)
            .order_by(Judgment.id)
            .limit(400)
        )
        if last_id is not None:
            q = q.where(Judgment.id > last_id)
        batch = (await db.execute(q)).scalars().all()
        if not batch:
            break
        for j in batch:
            last_id = j.id
            counts["judgments_scanned"] += 1
            header = leading_header_citation_judgment(j.full_text or "")
            stored = citation_key(j.canonical_citation)
            if not header or not stored:
                continue
            if stored == header:
                counts["aligned"] += 1
                continue
            kind = mismatch_kind(stored, header) or "unknown"
            if not rekey_actionable(stored, header, j.full_text or ""):
                counts["weak_candidate"] += 1
                rows_out.append(_judgment_row(j, header, kind, "candidate_only"))
                continue
            owner = await _citation_taken(db, header, exclude_judgment_id=j.id)
            if owner is not None:
                counts["conflict_refetch"] += 1
                rows_out.append(_judgment_row(j, header, kind, "conflict_refetch", f"held_by={owner}"))
                continue
            counts["would_rekey"] += 1
            rows_out.append(_judgment_row(j, header, kind, "rekey"))

    st_last = None
    while True:
        sq = (
            select(ScraperStaging)
            .where(ScraperStaging.source_name == SOURCE_NAME, ScraperStaging.status.in_(["extracted", "promoted", "duplicate"]))
            .order_by(ScraperStaging.id)
            .limit(400)
        )
        if st_last is not None:
            sq = sq.where(ScraperStaging.id > st_last)
        batch = (await db.execute(sq)).scalars().all()
        if not batch:
            break
        for st in batch:
            st_last = st.id
            counts["staging_scanned"] += 1
            header = leading_header_citation_staging(st.raw_text or "")
            stored = citation_key(st.extracted_citation or "")
            if not header or not stored or stored == header:
                continue
            kind = mismatch_kind(stored, header) or "unknown"
            if not rekey_actionable(stored, header, st.raw_text or "", staging=True):
                counts["weak_candidate"] += 1
                continue
            counts["staging_would_rekey"] += 1
            rows_out.append(
                {
                    "kind": "staging",
                    "staging_id": str(st.id),
                    "stored": st.extracted_citation,
                    "header": header,
                    "mismatch": kind,
                    "action": "rekey_staging",
                }
            )
    return rows_out, counts


async def _backup_judgment(db: AsyncSession, j: Judgment) -> Dict[str, Any]:
    cits = (await db.execute(select(Citation).where(Citation.judgment_id == j.id))).scalars().all()
    return {
        "judgment_id": str(j.id),
        "canonical_citation": j.canonical_citation,
        "citations": [
            {
                "id": str(c.id),
                "citation_string": c.citation_string,
                "is_primary": c.is_primary,
                "reporter": c.reporter,
                "year": c.year,
                "page": c.page,
            }
            for c in cits
        ],
    }


async def _apply_rekey_judgment(
    db: AsyncSession,
    j: Judgment,
    header: str,
    *,
    refetch_keys: Set[str],
    undo_actions: List[Dict[str, Any]],
) -> None:
    old = citation_key(j.canonical_citation)
    new = citation_key(header)
    backup = await _backup_judgment(db, j)
    undo_actions.append({"type": "judgment_rekey", "backup": backup, "new_citation": new})
    j.canonical_citation = new
    parts = _parse_citation_parts(new)
    j.reporter = parts.get("reporter")
    j.year = parts.get("year")
    j.page_number = parts.get("page")
    for c in (await db.execute(select(Citation).where(Citation.judgment_id == j.id))).scalars().all():
        if c.is_primary:
            c.citation_string = new
            c.raw_string = new
            c.reporter = parts.get("reporter")
            c.year = parts.get("year")
            c.page = parts.get("page")
    if old and old != new:
        if await _citation_taken(db, old) is None:
            key = await _queue_citation_refetch(db, old, reason=f"vacated_by_rekey:{j.id}")
            if key:
                refetch_keys.add(key)


async def run_citation_header_rekey(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
    csv_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    async with SessionLocal() as db:
        if undo:
            return await _undo_run(db, undo)

        plan, counts = await scan_header_rekeys(db)
        if limit is not None:
            plan = [r for r in plan if r.get("action") in ("rekey", "conflict_refetch", "rekey_staging")][: int(limit)]

        path = csv_path or Path(f"citation_header_rekey_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv")
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["kind", "judgment_id", "staging_id", "stored", "header", "mismatch", "action", "detail"],
            )
            writer.writeheader()
            for row in plan:
                writer.writerow({k: row.get(k, "") for k in writer.fieldnames})

        out: Dict[str, Any] = {"dry_run": not apply, "counts": counts, "csv_path": str(path), "planned": len(plan)}
        if not apply:
            return out

        run_id = uuid.uuid4().hex
        undo_actions: List[Dict[str, Any]] = []
        refetch_keys: Set[str] = set()
        applied = {"rekeyed": 0, "conflict_refetch": 0, "staging_updated": 0}

        for row in plan:
            if row.get("action") == "rekey":
                j = (await db.execute(select(Judgment).where(Judgment.id == UUID(row["judgment_id"])))).scalars().first()
                if j is None:
                    continue
                await _apply_rekey_judgment(db, j, row["header"], refetch_keys=refetch_keys, undo_actions=undo_actions)
                applied["rekeyed"] += 1
            elif row.get("action") == "conflict_refetch":
                key = await _queue_citation_refetch(
                    db,
                    row["header"],
                    reason=f"header_rekey_conflict:{row.get('judgment_id')}",
                )
                if key:
                    refetch_keys.add(key)
                key2 = await _queue_citation_refetch(db, row["stored"], reason=f"ambiguous_stored:{row.get('judgment_id')}")
                if key2:
                    refetch_keys.add(key2)
                applied["conflict_refetch"] += 1
            elif row.get("action") == "rekey_staging":
                st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == UUID(row["staging_id"])))).scalars().first()
                if st is None:
                    continue
                old = st.extracted_citation
                st.extracted_citation = citation_key(row["header"])
                undo_actions.append({"type": "staging_rekey", "staging_id": str(st.id), "old": old, "new": st.extracted_citation})
                applied["staging_updated"] += 1

        db.add(CorpusMetadata(key=BACKUP_KEY.format(run_id=run_id), value=json.dumps(undo_actions)))
        db.add(
            CorpusMetadata(
                key=UNDO_LOG_KEY.format(run_id=run_id),
                value=json.dumps({"actions": undo_actions, "refetch_keys": sorted(refetch_keys)}),
            )
        )
        db.add(CorpusMetadata(key=REFETCH_KEY.format(run_id=run_id), value=json.dumps(sorted(refetch_keys))))
        await db.commit()
        out["applied"] = {**applied, "run_id": run_id, "refetch_queries": len(refetch_keys)}
        return out


async def _undo_run(db: AsyncSession, run_id: str) -> Dict[str, Any]:
    row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == BACKUP_KEY.format(run_id=run_id)))).scalars().first()
    if row is None or not row.value:
        return {"undone": False, "reason": "backup missing", "run_id": run_id}
    restored = 0
    for item in json.loads(row.value):
        if item.get("type") == "judgment_rekey":
            backup = item["backup"]
            j = (await db.execute(select(Judgment).where(Judgment.id == UUID(backup["judgment_id"])))).scalars().first()
            if j is None:
                continue
            j.canonical_citation = backup["canonical_citation"]
            for cit in backup.get("citations") or []:
                c = (await db.execute(select(Citation).where(Citation.id == UUID(cit["id"])))).scalars().first()
                if c is None:
                    continue
                c.citation_string = cit["citation_string"]
                c.is_primary = cit.get("is_primary", False)
                c.reporter = cit.get("reporter")
                c.year = cit.get("year")
                c.page = cit.get("page")
            restored += 1
        elif item.get("type") == "staging_rekey":
            st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == UUID(item["staging_id"])))).scalars().first()
            if st is not None:
                st.extracted_citation = item.get("old")
                restored += 1
    ref_row = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == REFETCH_KEY.format(run_id=run_id)))
    ).scalars().first()
    removed_refetch = 0
    if ref_row and ref_row.value:
        for key in json.loads(ref_row.value):
            q = (
                await db.execute(
                    select(PlsSearchHarvestQuery).where(
                        PlsSearchHarvestQuery.source_name == SOURCE_NAME,
                        PlsSearchHarvestQuery.query_key == key,
                        PlsSearchHarvestQuery.status == "pending",
                    )
                )
            ).scalars().first()
            if q is not None:
                await db.delete(q)
                removed_refetch += 1
    for key in (BACKUP_KEY.format(run_id=run_id), UNDO_LOG_KEY.format(run_id=run_id), REFETCH_KEY.format(run_id=run_id)):
        meta = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == key))).scalars().first()
        if meta is not None:
            await db.delete(meta)
    await db.commit()
    return {"undone": True, "run_id": run_id, "judgments_restored": restored, "refetch_removed": removed_refetch}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Dry-run header citation re-key (default); --apply to write; --undo RUN_ID")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--undo", metavar="RUN_ID")
    parser.add_argument("--csv", dest="csv_path", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    out = run_async(
        run_citation_header_rekey(
            apply=args.apply,
            undo=args.undo,
            csv_path=Path(args.csv_path) if args.csv_path else None,
            limit=args.limit,
        )
    )
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
