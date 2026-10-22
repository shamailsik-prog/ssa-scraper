"""Flag existing corpus rows with no sections or no text (B7). Dry-run by default; never deletes rows.

Prod: run without --apply for counts only (read-only). Test DB: use --apply in tests.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async

logger = logging.getLogger(__name__)

# Documented criteria for prod dry-run (read-only SELECTs).
BAD_STATUTE_COUNT_SQL = """
SELECT COUNT(*)::bigint AS n
FROM statute s
WHERE COALESCE(s.corpus_quarantined, FALSE) IS NOT TRUE
  AND NOT EXISTS (
    SELECT 1
    FROM statute_section ss
    JOIN statute_section_version ssv ON ssv.id = ss.current_version_id
    WHERE ss.statute_id = s.id
      AND length(btrim(ssv.section_text)) > 0
  )
"""

BAD_JUDGMENT_COUNT_SQL = """
SELECT COUNT(*)::bigint AS n
FROM judgment j
WHERE COALESCE(j.corpus_quarantined, FALSE) IS NOT TRUE
  AND (
    j.full_text IS NULL
    OR length(btrim(j.full_text)) < 140
  )
"""

BAD_STATUTE_BY_TYPE_SQL = """
SELECT COALESCE(s.statute_type, '(null)') AS document_type, COUNT(*)::bigint AS n
FROM statute s
WHERE COALESCE(s.corpus_quarantined, FALSE) IS NOT TRUE
  AND NOT EXISTS (
    SELECT 1
    FROM statute_section ss
    JOIN statute_section_version ssv ON ssv.id = ss.current_version_id
    WHERE ss.statute_id = s.id
      AND length(btrim(ssv.section_text)) > 0
  )
GROUP BY COALESCE(s.statute_type, '(null)')
ORDER BY n DESC
"""

BAD_JUDGMENT_BY_REPORTER_SQL = """
SELECT COALESCE(j.reporter, '(null)') AS document_type, COUNT(*)::bigint AS n
FROM judgment j
WHERE COALESCE(j.corpus_quarantined, FALSE) IS NOT TRUE
  AND (
    j.full_text IS NULL
    OR length(btrim(j.full_text)) < 140
  )
GROUP BY COALESCE(j.reporter, '(null)')
ORDER BY n DESC
"""

STATUTE_QUARANTINE_REASON = "b7_bad_document:no_parseable_sections"
JUDGMENT_QUARANTINE_REASON = "b7_bad_document:empty_or_short_text"
STATUTE_QUARANTINE_PREFIX = "b7_bad_document:"


async def bad_document_counts(db: AsyncSession) -> Dict[str, Any]:
    statute_total = int((await db.execute(text(BAD_STATUTE_COUNT_SQL))).scalar() or 0)
    judgment_total = int((await db.execute(text(BAD_JUDGMENT_COUNT_SQL))).scalar() or 0)
    statute_by_type = [
        {"document_type": row.document_type, "count": int(row.n)}
        for row in (await db.execute(text(BAD_STATUTE_BY_TYPE_SQL))).all()
    ]
    judgment_by_type = [
        {"document_type": row.document_type, "count": int(row.n)}
        for row in (await db.execute(text(BAD_JUDGMENT_BY_REPORTER_SQL))).all()
    ]
    return {
        "statute_bad_rows": statute_total,
        "judgment_bad_rows": judgment_total,
        "by_document_type": {
            "statute": statute_by_type,
            "judgment": judgment_by_type,
        },
        "criteria_sql": {
            "statute_count": BAD_STATUTE_COUNT_SQL.strip(),
            "judgment_count": BAD_JUDGMENT_COUNT_SQL.strip(),
            "statute_by_type": BAD_STATUTE_BY_TYPE_SQL.strip(),
            "judgment_by_reporter": BAD_JUDGMENT_BY_REPORTER_SQL.strip(),
        },
        "min_judgment_text_chars": 140,
    }


async def apply_quarantine(db: AsyncSession) -> Dict[str, int]:
    st = await db.execute(
        text(
            """
            UPDATE statute
            SET corpus_quarantined = TRUE,
                corpus_quarantine_reason = :reason
            WHERE COALESCE(corpus_quarantined, FALSE) IS NOT TRUE
              AND NOT EXISTS (
                SELECT 1
                FROM statute_section ss
                JOIN statute_section_version ssv ON ssv.id = ss.current_version_id
                WHERE ss.statute_id = statute.id
                  AND length(btrim(ssv.section_text)) > 0
              )
            """
        ),
        {"reason": STATUTE_QUARANTINE_REASON},
    )
    jg = await db.execute(
        text(
            """
            UPDATE judgment
            SET corpus_quarantined = TRUE,
                corpus_quarantine_reason = :reason
            WHERE COALESCE(corpus_quarantined, FALSE) IS NOT TRUE
              AND (
                full_text IS NULL
                OR length(btrim(full_text)) < 140
              )
            """
        ),
        {"reason": JUDGMENT_QUARANTINE_REASON},
    )
    await db.commit()
    return {"statutes_updated": int(st.rowcount or 0), "judgments_updated": int(jg.rowcount or 0)}


async def undo_quarantine(db: AsyncSession) -> Dict[str, int]:
    st = await db.execute(
        text(
            """
            UPDATE statute
            SET corpus_quarantined = FALSE,
                corpus_quarantine_reason = NULL
            WHERE corpus_quarantined IS TRUE
              AND corpus_quarantine_reason LIKE :prefix
            """
        ),
        {"prefix": f"{STATUTE_QUARANTINE_PREFIX}%"},
    )
    jg = await db.execute(
        text(
            """
            UPDATE judgment
            SET corpus_quarantined = FALSE,
                corpus_quarantine_reason = NULL
            WHERE corpus_quarantined IS TRUE
              AND corpus_quarantine_reason LIKE :prefix
            """
        ),
        {"prefix": f"{STATUTE_QUARANTINE_PREFIX}%"},
    )
    await db.commit()
    return {"statutes_restored": int(st.rowcount or 0), "judgments_restored": int(jg.rowcount or 0)}


async def run_quarantine_bad_documents(
    *,
    apply: bool = False,
    undo: bool = False,
) -> Dict[str, Any]:
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    async with SessionLocal() as db:
        counts = await bad_document_counts(db)
        result: Dict[str, Any] = {
            "dry_run": not apply and not undo,
            "flag": "SSA_DOCUMENT_SAVE_VALIDATION",
            "validation_flag_on": bool(settings.SSA_DOCUMENT_SAVE_VALIDATION),
            **counts,
        }
        if apply:
            result["applied"] = await apply_quarantine(db)
        elif undo:
            result["undone"] = await undo_quarantine(db)
        return result


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Dry-run or apply corpus quarantine for bad statute/judgment rows")
    parser.add_argument("--apply", action="store_true", help="Set corpus_quarantined on matching rows (not prod default)")
    parser.add_argument("--undo", action="store_true", help="Clear quarantine flags set by this script (reason prefix b7_bad_document:)")
    args = parser.parse_args(argv)
    out = run_async(run_quarantine_bad_documents(apply=args.apply, undo=args.undo))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
