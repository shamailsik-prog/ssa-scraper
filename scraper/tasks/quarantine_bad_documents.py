"""Flag bad corpus rows in document_quarantine (B7). Dry-run by default; never alters statute/judgment.

Prod: run without --apply for counts only (read-only). Test DB: use --apply in tests.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.models import CorpusMetadata, DocumentQuarantine, Judgment, Statute

logger = logging.getLogger(__name__)

STATUTE_QUARANTINE_REASON = "b7_bad_document:no_parseable_sections"
JUDGMENT_QUARANTINE_REASON = "b7_bad_document:empty_or_short_text"
MIN_JUDGMENT_TEXT_CHARS = 140
LATEST_RUN_KEY = "b7_quarantine_latest_run_id"


def _undo_prior_latest_key(run_id: str) -> str:
    return f"b7_quarantine_undo:{run_id}:prior_latest"

# Documented criteria for prod dry-run (read-only; no corpus_quarantined).
BAD_STATUTE_COUNT_SQL = """
SELECT COUNT(*)::bigint AS n
FROM statute s
WHERE NOT EXISTS (
    SELECT 1 FROM document_quarantine dq
    WHERE dq.doc_type = 'statute' AND dq.doc_id = s.id
  )
  AND NOT EXISTS (
    SELECT 1
    FROM statute_section ss
    JOIN statute_section_version ssv ON ssv.id = ss.current_version_id
    WHERE ss.statute_id = s.id
      AND length(btrim(ssv.section_text)) > 0
  )
"""

BAD_JUDGMENT_COUNT_SQL = f"""
SELECT COUNT(*)::bigint AS n
FROM judgment j
WHERE NOT EXISTS (
    SELECT 1 FROM document_quarantine dq
    WHERE dq.doc_type = 'judgment' AND dq.doc_id = j.id
  )
  AND (
    j.full_text IS NULL
    OR length(btrim(j.full_text)) < {MIN_JUDGMENT_TEXT_CHARS}
  )
"""

BAD_STATUTE_BY_TYPE_SQL = """
SELECT COALESCE(s.statute_type, '(null)') AS document_type, COUNT(*)::bigint AS n
FROM statute s
WHERE NOT EXISTS (
    SELECT 1 FROM document_quarantine dq
    WHERE dq.doc_type = 'statute' AND dq.doc_id = s.id
  )
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

BAD_JUDGMENT_BY_REPORTER_SQL = f"""
SELECT COALESCE(j.reporter, '(null)') AS document_type, COUNT(*)::bigint AS n
FROM judgment j
WHERE NOT EXISTS (
    SELECT 1 FROM document_quarantine dq
    WHERE dq.doc_type = 'judgment' AND dq.doc_id = j.id
  )
  AND (
    j.full_text IS NULL
    OR length(btrim(j.full_text)) < {MIN_JUDGMENT_TEXT_CHARS}
  )
GROUP BY COALESCE(j.reporter, '(null)')
ORDER BY n DESC
"""


async def _table_snapshot(db: AsyncSession) -> List[Dict[str, Any]]:
    rows = (
        await db.execute(
            select(DocumentQuarantine).order_by(
                DocumentQuarantine.doc_type,
                DocumentQuarantine.doc_id,
            )
        )
    ).scalars().all()
    return [
        {
            "doc_type": r.doc_type,
            "doc_id": str(r.doc_id),
            "reason": r.reason,
            "run_id": r.run_id,
        }
        for r in rows
    ]


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
        "min_judgment_text_chars": MIN_JUDGMENT_TEXT_CHARS,
    }


async def apply_quarantine(db: AsyncSession) -> Dict[str, Any]:
    run_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    statutes_inserted = 0
    judgments_inserted = 0

    statute_ids = (
        await db.execute(
            text(
                """
                SELECT s.id
                FROM statute s
                WHERE NOT EXISTS (
                    SELECT 1 FROM document_quarantine dq
                    WHERE dq.doc_type = 'statute' AND dq.doc_id = s.id
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM statute_section ss
                    JOIN statute_section_version ssv ON ssv.id = ss.current_version_id
                    WHERE ss.statute_id = s.id
                      AND length(btrim(ssv.section_text)) > 0
                  )
                """
            )
        )
    ).scalars().all()
    for sid in statute_ids:
        db.add(
            DocumentQuarantine(
                doc_type="statute",
                doc_id=sid,
                reason=STATUTE_QUARANTINE_REASON,
                run_id=run_id,
                quarantined_at=now,
            )
        )
        statutes_inserted += 1

    judgment_ids = (
        await db.execute(
            text(
                f"""
                SELECT j.id
                FROM judgment j
                WHERE NOT EXISTS (
                    SELECT 1 FROM document_quarantine dq
                    WHERE dq.doc_type = 'judgment' AND dq.doc_id = j.id
                  )
                  AND (
                    j.full_text IS NULL
                    OR length(btrim(j.full_text)) < {MIN_JUDGMENT_TEXT_CHARS}
                  )
                """
            )
        )
    ).scalars().all()
    for jid in judgment_ids:
        db.add(
            DocumentQuarantine(
                doc_type="judgment",
                doc_id=jid,
                reason=JUDGMENT_QUARANTINE_REASON,
                run_id=run_id,
                quarantined_at=now,
            )
        )
        judgments_inserted += 1

    latest = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
    prior_latest = latest.value if latest is not None else ""
    db.add(CorpusMetadata(key=_undo_prior_latest_key(run_id), value=prior_latest))
    if latest is None:
        db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
    else:
        latest.value = run_id
    await db.commit()
    return {
        "run_id": run_id,
        "statutes_updated": statutes_inserted,
        "judgments_updated": judgments_inserted,
    }


async def undo_quarantine(db: AsyncSession, run_id: str) -> Dict[str, int]:
    prior_row = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_prior_latest_key(run_id)))
    ).scalars().first()
    prior_latest = prior_row.value if prior_row is not None else None
    result = await db.execute(delete(DocumentQuarantine).where(DocumentQuarantine.run_id == run_id))
    n = int(result.rowcount or 0)
    latest = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
    if latest is not None and latest.value == run_id:
        if prior_latest:
            latest.value = prior_latest
        else:
            await db.delete(latest)
    if prior_row is not None:
        await db.delete(prior_row)
    await db.commit()
    return {"rows_removed": n}


async def run_quarantine_bad_documents(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
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
            result["undone"] = await undo_quarantine(db, undo)
        return result


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Dry-run or apply document_quarantine for bad statute/judgment rows")
    parser.add_argument("--apply", action="store_true", help="Insert document_quarantine rows (not prod default)")
    parser.add_argument("--undo", metavar="RUN_ID", help="Remove rows inserted by a prior --apply run_id")
    args = parser.parse_args(argv)
    out = run_async(run_quarantine_bad_documents(apply=args.apply, undo=args.undo))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
