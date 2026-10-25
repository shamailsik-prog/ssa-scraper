"""Re-queue crawl_frontier rows retired for missing %PDF on PDF-shaped judgment URLs.

Ops-only maintenance: dry-run by default. Does not run automatically on deploy.

After the HTML-judgment fix is live, recover the ~1,980 SindhHighCourt rows (Monitor
"SHC 1980" means **row count**, not calendar year 1980). Omit ``--year``:

    python -m scraper.tasks.public_frontier_requeue --source SindhHighCourt --dry-run
    python -m scraper.tasks.public_frontier_requeue --source SindhHighCourt --apply

Optional pilot on a small slice before full apply:

    python -m scraper.tasks.public_frontier_requeue --source SindhHighCourt --limit 50 --dry-run
    python -m scraper.tasks.public_frontier_requeue --source SindhHighCourt --limit 50 --apply

``--year`` only matches rows whose citation or URL **contains that digit string**
(e.g. ``--year 2024``). Do **not** pass ``--year 1980`` for the ~1,980-row recovery;
that would select only year-1980-looking citations and miss almost the entire batch.

Do not bulk-re-queue IslamabadHighCourt (404 URLs) or PeshawarHighCourt (406 backoff).
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from datetime import datetime, timezone
from uuid import UUID
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal, run_async
from scraper.models import CorpusMetadata, CrawlFrontier

logger = logging.getLogger(__name__)

LATEST_RUN_KEY = "public_frontier_pdf_miss_requeue_latest_run_id"
PDF_MISS_RETIRED_PREFIX = "missing %PDF signature"
PDF_MISS_RETIRED_EXACT = "missing %PDF signature for judgment document URL"


def _undo_snapshot_key(run_id: str) -> str:
    return f"public_frontier_pdf_miss_requeue_undo:{run_id}"


def _undo_prior_latest_key(run_id: str) -> str:
    return f"public_frontier_pdf_miss_requeue_undo:{run_id}:prior_latest"


def _row_matches_fresh_requeue_state(row: CrawlFrontier) -> bool:
    """True when the worker has not changed a row since --apply reset it to pending."""
    return (
        row.status == "pending"
        and int(row.attempts or 0) == 0
        and row.last_error is None
        and row.next_run_at is None
    )


async def _upsert_corpus_metadata(db: AsyncSession, key: str, value: str) -> None:
    row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == key))).scalars().first()
    if row is None:
        db.add(CorpusMetadata(key=key, value=value))
    else:
        row.value = value


def _matches_year(row: CrawlFrontier, year: Optional[int]) -> bool:
    """True when ``year`` is unset or its decimal string appears in citation or URL."""
    if year is None:
        return True
    needle = str(year)
    meta = row.query_json.get("meta") or {}
    citation = str(meta.get("citation") or "")
    if needle in citation:
        return True
    url = str(row.query_json.get("url") or "")
    return needle in url


def _candidate_query(source_name: str):
    return (
        select(CrawlFrontier)
        .where(
            CrawlFrontier.source_name == source_name,
            CrawlFrontier.tier == 0,
            CrawlFrontier.status == "retired",
            or_(
                CrawlFrontier.last_error.like(f"{PDF_MISS_RETIRED_PREFIX}%"),
                CrawlFrontier.last_error == PDF_MISS_RETIRED_EXACT,
            ),
        )
        .order_by(CrawlFrontier.created_at.asc())
    )


async def count_pdf_miss_retired(
    db: AsyncSession,
    *,
    source_name: str,
    year: Optional[int] = None,
    limit: Optional[int] = None,
) -> int:
    rows = list((await db.execute(_candidate_query(source_name))).scalars().all())
    if year is not None:
        rows = [r for r in rows if _matches_year(r, year)]
    if limit is not None:
        rows = rows[:limit]
    return len(rows)


async def requeue_pdf_miss_retired(
    db: AsyncSession,
    *,
    source_name: str,
    run_id: str,
    year: Optional[int] = None,
    limit: Optional[int] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    rows = list((await db.execute(_candidate_query(source_name))).scalars().all())
    if year is not None:
        rows = [r for r in rows if _matches_year(r, year)]
    if limit is not None:
        rows = rows[:limit]
    snapshot: List[Dict[str, Any]] = []
    for row in rows:
        snapshot.append(
            {
                "id": str(row.id),
                "status": row.status,
                "last_error": row.last_error,
                "attempts": row.attempts,
                "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
            }
        )
        if not dry_run:
            row.status = "pending"
            row.last_error = None
            row.attempts = 0
            row.next_run_at = None
    if not dry_run and snapshot:
        prior = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
        await _upsert_corpus_metadata(db, _undo_prior_latest_key(run_id), prior.value if prior else "")
        await _upsert_corpus_metadata(db, _undo_snapshot_key(run_id), json.dumps(snapshot))
        await _upsert_corpus_metadata(db, LATEST_RUN_KEY, run_id)
        await db.commit()
    return {"run_id": run_id, "matched": len(snapshot), "dry_run": dry_run, "source_name": source_name, "year": year}


async def undo_requeue_run(db: AsyncSession, run_id: str) -> Dict[str, int]:
    snap_row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_snapshot_key(run_id)))).scalars().first()
    restored = 0
    skipped = 0
    if snap_row is not None and snap_row.value:
        for item in json.loads(snap_row.value):
            row = (await db.execute(select(CrawlFrontier).where(CrawlFrontier.id == UUID(item["id"])))).scalars().first()
            if row is None:
                skipped += 1
                continue
            if not _row_matches_fresh_requeue_state(row):
                skipped += 1
                continue
            row.status = item["status"]
            row.last_error = item.get("last_error")
            row.attempts = int(item.get("attempts") or 0)
            nra = item.get("next_run_at")
            row.next_run_at = datetime.fromisoformat(nra) if nra else None
            restored += 1
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
    return {"restored": restored, "skipped": skipped}


async def run_public_frontier_requeue(
    *,
    source_name: str,
    year: Optional[int] = None,
    limit: Optional[int] = None,
    apply: bool = False,
    undo: Optional[str] = None,
) -> Dict[str, Any]:
    async with SessionLocal() as db:
        if undo:
            return {"undone": await undo_requeue_run(db, undo)}
        run_id = str(uuid.uuid4())
        matched = await count_pdf_miss_retired(db, source_name=source_name, year=year, limit=limit)
        result = await requeue_pdf_miss_retired(
            db,
            source_name=source_name,
            run_id=run_id,
            year=year,
            limit=limit,
            dry_run=not apply,
        )
        result["count"] = matched
        return result


_OPS_EPILOG = """
Examples (SindhHighCourt ~1,980-row PDF-miss recovery — no --year):

  python -m scraper.tasks.public_frontier_requeue --source SindhHighCourt --dry-run
  python -m scraper.tasks.public_frontier_requeue --source SindhHighCourt --apply

Pilot before full apply:

  python -m scraper.tasks.public_frontier_requeue --source SindhHighCourt --limit 50 --dry-run
  python -m scraper.tasks.public_frontier_requeue --source SindhHighCourt --limit 50 --apply

Optional substring filter (not for the ~1,980-row recovery):

  python -m scraper.tasks.public_frontier_requeue --source SindhHighCourt --year 2024 --dry-run
"""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-queue frontier rows retired for missing %PDF on judgment document URLs (dry-run default)",
        epilog=_OPS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", required=True, help="Scraper source name (e.g. SindhHighCourt)")
    parser.add_argument(
        "--year",
        type=int,
        metavar="YEAR",
        help=(
            "Optional: only rows whose citation or URL contains this year substring "
            "(e.g. 2024). Do not use for the ~1,980-row SindhHighCourt recovery (omit --year)."
        ),
    )
    parser.add_argument("--limit", type=int, help="Max rows to touch (use for a small pilot before --apply)")
    parser.add_argument("--dry-run", action="store_true", help="Count only (default when --apply omitted)")
    parser.add_argument("--apply", action="store_true", help="Reset matched rows to pending and write undo backup")
    parser.add_argument("--undo", metavar="RUN_ID", help="Restore rows from a prior --apply run_id")
    args = parser.parse_args(argv)
    if args.apply and args.undo:
        parser.error("pass only one of --apply or --undo")
    out = run_async(
        run_public_frontier_requeue(
            source_name=args.source,
            year=args.year,
            limit=args.limit,
            apply=args.apply,
            undo=args.undo,
        )
    )
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
