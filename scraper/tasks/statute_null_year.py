"""Read-only inventory of visible statutes with null year_enacted.

Title re-extraction review only. This module has no --apply path and does not
quarantine or update rows. The ~36 main / ~18 free-sites population stays in place.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.corpus_visibility import statute_servable
from scraper.database import SessionLocal, run_async
from scraper.models import Statute

REASON_NULL_YEAR_TITLE_REVIEW = "null_year_enacted_title_review"


async def visible_null_year_inventory(db: AsyncSession, *, limit: Optional[int] = None) -> Dict[str, Any]:
    rows = (
        await db.execute(
            select(Statute)
            .where(statute_servable(), Statute.year_enacted.is_(None))
            .order_by(Statute.name.asc(), Statute.id.asc())
        )
    ).scalars().all()
    shown = rows if limit is None else rows[: max(0, int(limit))]
    statutes: List[Dict[str, Any]] = [
        {
            "id": str(st.id),
            "name": st.name,
            "jurisdiction": st.jurisdiction,
            "source_name": st.source_name,
            "source_url": st.source_url,
            "reason_code": REASON_NULL_YEAR_TITLE_REVIEW,
        }
        for st in shown
    ]
    return {
        "dry_run": True,
        "writes": 0,
        "reason_code": REASON_NULL_YEAR_TITLE_REVIEW,
        "count": len(rows),
        "shown": len(statutes),
        "statutes": statutes,
        "note": "Inventory only. Does not quarantine or update year_enacted.",
    }


async def run_report(*, limit: Optional[int] = None) -> Dict[str, Any]:
    async with SessionLocal() as db:
        return await visible_null_year_inventory(db, limit=limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List visible statutes with null year_enacted for title re-extraction review. "
            "Read-only: does not quarantine or update rows."
        )
    )
    parser.add_argument("--limit", type=int, default=500, help="Maximum rows to include in the report")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_async(run_report(limit=args.limit))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
