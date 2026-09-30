"""Where every row of the PakistanLawSite citation grid stands (audit of 2026-09-30: ~815 of 20,568 rows unaccounted for).

Judgments are counted by identity (their own citation); the rest of the grid is classified from the staging
ledger by the reason it was set aside. What is left after both is `not_captured`: rows the walk has not been
able to preserve (unreachable detail page, duplicate grid rows, or not walked yet)."""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_CLASS_SQL = """
WITH q AS (
    SELECT DISTINCT ON (s.extracted_citation)
        s.extracted_citation AS cit, s.quarantine_reason AS reason, octet_length(s.raw_text) AS len
    FROM scraper_staging s
    WHERE s.source_name = 'PakistanLawSite' AND s.status = 'quarantined' AND s.extracted_citation IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM judgment j WHERE j.canonical_citation = s.extracted_citation)
    ORDER BY s.extracted_citation, s.created_at DESC
)
SELECT CASE
    WHEN reason LIKE 'citation % already belongs%' THEN 'citation_conflict'
    WHEN reason LIKE 'headnote_only%' OR reason LIKE '%notes_on_cases%' OR reason LIKE '%body_short%' OR reason LIKE '%body_missing%' THEN 'headnote_only_or_stub_page'
    WHEN reason LIKE 'subscription_chrome%' OR reason LIKE 'login_stub%' THEN 'chrome_or_login_page'
    WHEN reason LIKE 'no citation%' THEN 'no_citation'
    WHEN reason LIKE 'confidence%' AND coalesce(len, 0) < 3000 THEN 'error_or_empty_page'
    WHEN reason LIKE 'confidence%' THEN 'low_confidence_full_text'
    ELSE 'other'
END AS cls, count(*) AS n
FROM q GROUP BY 1
"""


async def grid_accounting(db: AsyncSession, *, total_rows: int) -> Dict[str, Any]:
    judgments = int((await db.execute(text("SELECT count(*) FROM judgment WHERE source_name = 'PakistanLawSite'"))).scalar() or 0)
    classes = {row[0]: int(row[1]) for row in (await db.execute(text(_CLASS_SQL))).all()}
    unresolved = sum(classes.values())
    review_open = int(
        (await db.execute(text("SELECT count(*) FROM quarantine_queue WHERE reviewed = false AND source_name = 'PakistanLawSite'"))).scalar() or 0
    )
    total = int(total_rows or 0)
    not_captured = max(0, total - judgments - unresolved) if total else None
    return {
        "grid_rows": total or None,
        "judgments": judgments,
        "set_aside_unresolved": unresolved,
        "set_aside_by_class": classes,
        "not_captured": not_captured,
        "accounted_pct": round(100.0 * (judgments + unresolved) / total, 2) if total else None,
        "review_queue_open": review_open,
    }
