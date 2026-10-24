"""court_id must agree with court_name (PLS foreign-court backfill follow-up)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import Court, Judgment
from scraper.parsers.foreign_court_registry import normalize_judgment_court_name


def court_labels_match(judgment_name: Optional[str], court_row_name: Optional[str]) -> bool:
    if not judgment_name or not court_row_name:
        return judgment_name == court_row_name
    a = normalize_judgment_court_name(judgment_name) or judgment_name
    b = normalize_judgment_court_name(court_row_name) or court_row_name
    return re.sub(r"\s+", " ", a).casefold() == re.sub(r"\s+", " ", b).casefold()


def judgment_court_fields_consistent(
    *,
    court_name: Optional[str],
    court_id: Optional[UUID],
    court_row: Optional[Court],
) -> bool:
    if court_id is None:
        return court_row is None
    if court_row is None:
        return False
    if court_row.id != court_id:
        return False
    return court_labels_match(court_name, court_row.name)


async def find_judgment_court_mismatches(db: AsyncSession, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    q = """
    SELECT j.id, j.canonical_citation, j.court_name, j.court_id, c.name AS court_row_name
    FROM judgment j
    LEFT JOIN court c ON c.id = j.court_id
    WHERE j.source_name = 'PakistanLawSite'
      AND j.court_name IS NOT NULL
      AND btrim(j.court_name) <> ''
      AND (
        j.court_id IS NULL
        OR c.name IS NULL
        OR lower(regexp_replace(btrim(j.court_name), '\\s+', ' ', 'g'))
           <> lower(regexp_replace(btrim(c.name), '\\s+', ' ', 'g'))
      )
    ORDER BY j.canonical_citation NULLS LAST
    """
    if limit is not None:
        q += f" LIMIT {int(limit)}"
    rows = (await db.execute(text(q))).all()
    out: List[Dict[str, Any]] = []
    for row in rows:
        if court_labels_match(row.court_name, row.court_row_name):
            continue
        out.append(
            {
                "id": str(row.id),
                "citation": row.canonical_citation or "",
                "court_name": row.court_name or "",
                "court_id": str(row.court_id) if row.court_id else None,
                "court_row_name": row.court_row_name or "",
            }
        )
    return out


async def consistency_report(db: AsyncSession, *, limit: Optional[int] = None) -> Dict[str, Any]:
    mismatches = await find_judgment_court_mismatches(db, limit=limit)
    return {"count": len(mismatches), "mismatches": mismatches}
