"""
Read-only per-record corpus verifier. SELECTs contract tables as ``sikander_reader``; writes only
``verification.verification_results`` via the verifier role. Never mutates judgment/statute rows.
"""

from __future__ import annotations

import csv
import json
import logging
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from celery import shared_task
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import run_async
from scraper.models import Court, Judgment, Statute, StatuteSection, StatuteSectionVersion
from scraper.verification.fingerprint import compare_judgment_fingerprint, fingerprint_sample_pass, pls_harvest_slot_busy
from scraper.verification.rules import RULES_VERSION, verify_judgment_row, verify_statute_row
from scraper.verification.sessions import open_reader_session, open_verifier_writer_session

logger = logging.getLogger(__name__)

STREAM_JUDGMENTS = "judgments"
STREAM_STATUTES = "statutes"


async def _duplicate_judgment_keys(reader: AsyncSession) -> Tuple[Set[str], Set[str]]:
    cites = {
        row[0]
        for row in (
            await reader.execute(
                text(
                    """
                    SELECT canonical_citation FROM judgment
                    WHERE canonical_citation IS NOT NULL AND btrim(canonical_citation) <> ''
                    GROUP BY canonical_citation HAVING count(*) > 1
                    """
                )
            )
        ).all()
    }
    hashes = {
        row[0]
        for row in (
            await reader.execute(
                text(
                    """
                    SELECT full_text_hash FROM judgment
                    WHERE full_text_hash IS NOT NULL AND btrim(full_text_hash) <> ''
                    GROUP BY full_text_hash HAVING count(*) > 1
                    """
                )
            )
        ).all()
    }
    return cites, hashes


async def _duplicate_statute_names(reader: AsyncSession) -> Set[str]:
    return {
        (row[0] or "").strip().lower()
        for row in (
            await reader.execute(
                text(
                    """
                    SELECT lower(btrim(name)) FROM statute
                    WHERE name IS NOT NULL AND btrim(name) <> ''
                    GROUP BY lower(btrim(name)) HAVING count(*) > 1
                    """
                )
            )
        ).all()
    }


async def _court_map(reader: AsyncSession) -> Dict[UUID, Court]:
    return {c.id: c for c in (await reader.execute(select(Court))).scalars().all()}


async def _load_run_cursor(writer: AsyncSession, stream: str) -> Optional[UUID]:
    row = (
        await writer.execute(
            text("SELECT last_record_id FROM verification.verification_run_state WHERE stream = :s"),
            {"s": stream},
        )
    ).first()
    return UUID(str(row[0])) if row and row[0] else None


async def _save_run_cursor(writer: AsyncSession, stream: str, last_id: Optional[UUID]) -> None:
    await writer.execute(
        text(
            """
            INSERT INTO verification.verification_run_state (stream, last_record_id, rules_version, updated_at)
            VALUES (:stream, :last_id, :rules, NOW())
            ON CONFLICT (stream) DO UPDATE SET
                last_record_id = EXCLUDED.last_record_id,
                rules_version = EXCLUDED.rules_version,
                updated_at = NOW()
            """
        ),
        {"stream": stream, "last_id": str(last_id) if last_id else None, "rules": RULES_VERSION},
    )


async def _upsert_result(
    writer: AsyncSession,
    *,
    record_kind: str,
    record_id: UUID,
    outcome,
    fingerprint_status: Optional[str] = None,
    fingerprint_detail: Optional[str] = None,
) -> None:
    await writer.execute(
        text(
            """
            INSERT INTO verification.verification_results (
                record_kind, record_id, canonical_key, journal_year, verdict, reason_codes,
                rules_version, checked_at, source_fingerprint_status, source_fingerprint_detail
            ) VALUES (
                :kind, :rid, :key, :year, :verdict, CAST(:reasons AS jsonb), :rules, NOW(), :fp_status, :fp_detail
            )
            ON CONFLICT (record_kind, record_id, rules_version) DO UPDATE SET
                canonical_key = EXCLUDED.canonical_key,
                journal_year = EXCLUDED.journal_year,
                verdict = EXCLUDED.verdict,
                reason_codes = EXCLUDED.reason_codes,
                checked_at = EXCLUDED.checked_at,
                source_fingerprint_status = EXCLUDED.source_fingerprint_status,
                source_fingerprint_detail = EXCLUDED.source_fingerprint_detail
            """
        ),
        {
            "kind": record_kind,
            "rid": str(record_id),
            "key": outcome.canonical_key,
            "year": outcome.journal_year,
            "verdict": outcome.verdict,
            "reasons": json.dumps(outcome.reason_codes),
            "rules": RULES_VERSION,
            "fp_status": fingerprint_status,
            "fp_detail": (fingerprint_detail or "")[:1000] or None,
        },
    )


async def verify_judgments_batch(
    *,
    limit: Optional[int] = None,
    fingerprint: bool = True,
) -> Dict[str, Any]:
    if not settings.SSA_CORPUS_RECORD_VERIFIER:
        return {"skipped": "SSA_CORPUS_RECORD_VERIFIER disabled"}
    batch = int(limit or settings.CORPUS_VERIFIER_BATCH_SIZE)
    reader = await open_reader_session()
    writer = await open_verifier_writer_session()
    try:
        dup_cites, dup_hashes = await _duplicate_judgment_keys(reader)
        courts = await _court_map(reader)
        after_id = await _load_run_cursor(writer, STREAM_JUDGMENTS)
        q = select(Judgment).order_by(Judgment.id)
        if after_id is not None:
            q = q.where(Judgment.id > after_id)
        rows = list((await reader.execute(q.limit(batch))).scalars().all())
        if not rows and after_id is not None:
            rows = list((await reader.execute(select(Judgment).order_by(Judgment.id).limit(batch))).scalars().all())
            after_id = None
        processed = 0
        last_id = after_id
        harvest_busy = await pls_harvest_slot_busy()
        for j in rows:
            court_row = courts.get(j.court_id) if j.court_id else None
            outcome = verify_judgment_row(j, court_row=court_row, duplicate_citations=dup_cites, duplicate_hashes=dup_hashes)
            fp_status = None
            fp_detail = None
            need_fp = fingerprint and (outcome.verdict == "FAIL" or fingerprint_sample_pass())
            if need_fp and j.source_url and j.full_text:
                if (j.source_name or "") == "PakistanLawSite" and harvest_busy:
                    fp_status, fp_detail = "skipped", "pls harvest slot busy"
                else:
                    fp_status, fp_detail = "skipped", "fingerprint deferred to spot_check worker"
            await _upsert_result(
                writer,
                record_kind="judgment",
                record_id=j.id,
                outcome=outcome,
                fingerprint_status=fp_status,
                fingerprint_detail=fp_detail,
            )
            processed += 1
            last_id = j.id
        if rows:
            await _save_run_cursor(writer, STREAM_JUDGMENTS, last_id)
        await writer.commit()
        return {"judgments_processed": processed, "resumed_after": str(after_id) if after_id else None}
    finally:
        await reader.close()
        await writer.close()


async def _statute_sections(reader: AsyncSession, statute_id: UUID) -> List[Dict[str, str]]:
    rows = (
        await reader.execute(
            select(StatuteSection.section_number, StatuteSectionVersion.section_text)
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == statute_id)
        )
    ).all()
    return [{"section_number": str(num), "section_text": txt or ""} for num, txt in rows]


async def verify_statutes_batch(*, limit: Optional[int] = None) -> Dict[str, Any]:
    if not settings.SSA_CORPUS_RECORD_VERIFIER:
        return {"skipped": "SSA_CORPUS_RECORD_VERIFIER disabled"}
    batch = int(limit or settings.CORPUS_VERIFIER_BATCH_SIZE)
    reader = await open_reader_session()
    writer = await open_verifier_writer_session()
    try:
        dup_names = await _duplicate_statute_names(reader)
        after_id = await _load_run_cursor(writer, STREAM_STATUTES)
        q = select(Statute).order_by(Statute.id)
        if after_id is not None:
            q = q.where(Statute.id > after_id)
        statutes = list((await reader.execute(q.limit(batch))).scalars().all())
        if not statutes and after_id is not None:
            statutes = list((await reader.execute(select(Statute).order_by(Statute.id).limit(batch))).scalars().all())
            after_id = None
        processed = 0
        last_id = after_id
        for st in statutes:
            sections = await _statute_sections(reader, st.id)
            outcome = verify_statute_row(st, sections, duplicate_names=dup_names)
            await _upsert_result(writer, record_kind="statute", record_id=st.id, outcome=outcome)
            processed += 1
            last_id = st.id
        if statutes:
            await _save_run_cursor(writer, STREAM_STATUTES, last_id)
        await writer.commit()
        return {"statutes_processed": processed, "resumed_after": str(after_id) if after_id else None}
    finally:
        await reader.close()
        await writer.close()


async def run_verifier_tick(**kwargs) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["judgments"] = await verify_judgments_batch(**kwargs)
    out["statutes"] = await verify_statutes_batch(limit=kwargs.get("limit"))
    return out


async def build_nightly_report(output_dir: Optional[str] = None) -> Dict[str, Any]:
    writer = await open_verifier_writer_session()
    try:
        reason_rows = (
            await writer.execute(
                text(
                    """
                    SELECT verdict, journal_year, jsonb_array_elements_text(reason_codes) AS reason, count(*)::int
                    FROM verification.verification_results
                    WHERE rules_version = :rules
                    GROUP BY verdict, journal_year, reason
                    ORDER BY count DESC
                    """
                ),
                {"rules": RULES_VERSION},
            )
        ).all()
        by_reason: Counter[str] = Counter()
        by_year: Dict[Optional[int], Counter[str]] = defaultdict(Counter)
        for verdict, year, reason, n in reason_rows:
            if verdict == "FAIL":
                by_reason[reason] += n
                by_year[year][reason] += n
        fails = (
            await writer.execute(
                text(
                    """
                    SELECT record_kind, record_id, canonical_key, journal_year, reason_codes, checked_at
                    FROM verification.verification_results
                    WHERE rules_version = :rules AND verdict = 'FAIL'
                    ORDER BY checked_at DESC
                    """
                ),
                {"rules": RULES_VERSION},
            )
        ).all()
        out_dir = Path(output_dir or settings.CORPUS_VERIFIER_REPORT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        csv_path = out_dir / f"verification_fails_{stamp}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["record_kind", "record_id", "canonical_key", "journal_year", "reason_codes", "checked_at"])
            for row in fails:
                w.writerow([row[0], row[1], row[2], row[3], json.dumps(row[4]), row[5].isoformat() if row[5] else ""])
        summary = {
            "rules_version": RULES_VERSION,
            "fail_reason_counts": dict(by_reason),
            "fail_by_journal_year": {str(k): dict(v) for k, v in by_year.items()},
            "fail_csv": str(csv_path),
            "fail_rows": len(fails),
        }
        (out_dir / f"verification_summary_{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary
    finally:
        await writer.close()


@shared_task(name="scraper.tasks.corpus_verifier.corpus_verifier_tick", queue="maintenance")
def corpus_verifier_tick(limit: Optional[int] = None) -> Dict[str, Any]:
    return run_async(run_verifier_tick(limit=limit, fingerprint=True))


@shared_task(name="scraper.tasks.corpus_verifier.corpus_verifier_nightly_report", queue="maintenance")
def corpus_verifier_nightly_report() -> Dict[str, Any]:
    return run_async(build_nightly_report())
