"""Flag existing corpus rows with no sections or no text (B7). Dry-run by default; never deletes rows.

Prod: run without --apply for counts only (read-only). Test DB: use --apply in tests.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.models import CorpusMetadata

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
SNAPSHOT_KEY_PREFIX = "b7_quarantine_snapshot:"
LATEST_RUN_KEY = "b7_quarantine_latest_run_id"

_BAD_STATUTE_APPLY_WHERE = """
NOT EXISTS (
  SELECT 1
  FROM statute_section ss
  JOIN statute_section_version ssv ON ssv.id = ss.current_version_id
  WHERE ss.statute_id = statute.id
    AND length(btrim(ssv.section_text)) > 0
)
AND corpus_quarantine_reason IS NULL
"""

_BAD_JUDGMENT_APPLY_WHERE = """
(
  full_text IS NULL
  OR length(btrim(full_text)) < 140
)
AND corpus_quarantine_reason IS NULL
"""


def _snapshot_key(run_id: str) -> str:
    return f"{SNAPSHOT_KEY_PREFIX}{run_id}"


async def _load_snapshot(db: AsyncSession, run_id: str) -> Dict[str, Any]:
    row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _snapshot_key(run_id)))).scalars().first()
    if row is None or not row.value:
        raise ValueError(f"no quarantine snapshot for run_id={run_id}")
    return json.loads(row.value)


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


async def apply_quarantine(db: AsyncSession) -> Dict[str, Any]:
    run_id = uuid.uuid4().hex
    snapshot: Dict[str, Any] = {"statutes": {}, "judgments": {}}

    statute_rows = (
        await db.execute(
            text(
                f"""
                SELECT id::text, corpus_quarantined, corpus_quarantine_reason
                FROM statute
                WHERE {_BAD_STATUTE_APPLY_WHERE}
                """
            )
        )
    ).all()
    for row in statute_rows:
        snapshot["statutes"][row.id] = {
            "corpus_quarantined": bool(row.corpus_quarantined),
            "corpus_quarantine_reason": row.corpus_quarantine_reason,
        }

    judgment_rows = (
        await db.execute(
            text(
                f"""
                SELECT id::text, corpus_quarantined, corpus_quarantine_reason
                FROM judgment
                WHERE {_BAD_JUDGMENT_APPLY_WHERE}
                """
            )
        )
    ).all()
    for row in judgment_rows:
        snapshot["judgments"][row.id] = {
            "corpus_quarantined": bool(row.corpus_quarantined),
            "corpus_quarantine_reason": row.corpus_quarantine_reason,
        }

    if snapshot["statutes"]:
        ids = list(snapshot["statutes"].keys())
        await db.execute(
            text(
                """
                UPDATE statute
                SET corpus_quarantined = TRUE,
                    corpus_quarantine_reason = :reason
                WHERE id::text = ANY(:ids)
                """
            ),
            {"reason": STATUTE_QUARANTINE_REASON, "ids": ids},
        )
    if snapshot["judgments"]:
        ids = list(snapshot["judgments"].keys())
        await db.execute(
            text(
                """
                UPDATE judgment
                SET corpus_quarantined = TRUE,
                    corpus_quarantine_reason = :reason
                WHERE id::text = ANY(:ids)
                """
            ),
            {"reason": JUDGMENT_QUARANTINE_REASON, "ids": ids},
        )

    db.add(CorpusMetadata(key=_snapshot_key(run_id), value=json.dumps(snapshot)))
    latest = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
    if latest is None:
        db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
    else:
        latest.value = run_id
    await db.commit()
    return {
        "run_id": run_id,
        "statutes_updated": len(snapshot["statutes"]),
        "judgments_updated": len(snapshot["judgments"]),
    }


async def undo_quarantine(db: AsyncSession, run_id: str) -> Dict[str, int]:
    snapshot = await _load_snapshot(db, run_id)
    statutes_restored = 0
    judgments_restored = 0
    for sid, prior in snapshot.get("statutes", {}).items():
        await db.execute(
            text(
                """
                UPDATE statute
                SET corpus_quarantined = :q,
                    corpus_quarantine_reason = :reason
                WHERE id::text = :id
                """
            ),
            {
                "id": sid,
                "q": prior["corpus_quarantined"],
                "reason": prior["corpus_quarantine_reason"],
            },
        )
        statutes_restored += 1
    for jid, prior in snapshot.get("judgments", {}).items():
        await db.execute(
            text(
                """
                UPDATE judgment
                SET corpus_quarantined = :q,
                    corpus_quarantine_reason = :reason
                WHERE id::text = :id
                """
            ),
            {
                "id": jid,
                "q": prior["corpus_quarantined"],
                "reason": prior["corpus_quarantine_reason"],
            },
        )
        judgments_restored += 1
    await db.commit()
    return {"statutes_restored": statutes_restored, "judgments_restored": judgments_restored}


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

    parser = argparse.ArgumentParser(description="Dry-run or apply corpus quarantine for bad statute/judgment rows")
    parser.add_argument("--apply", action="store_true", help="Set corpus_quarantined on matching rows (not prod default)")
    parser.add_argument("--undo", metavar="RUN_ID", help="Restore rows touched by a prior --apply run_id")
    args = parser.parse_args(argv)
    out = run_async(run_quarantine_bad_documents(apply=args.apply, undo=args.undo))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
