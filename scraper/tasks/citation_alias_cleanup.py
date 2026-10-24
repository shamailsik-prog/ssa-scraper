"""Remove false non-primary citation aliases (body/headnote mentions) and requalify blocked staging rows."""

from __future__ import annotations

import csv
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal, run_async
from scraper.extractors import deterministic as det
from scraper.extractors.deterministic import citation_referred_in_text
from scraper.extractors.hybrid_extractor import load_court_directory
from scraper.extractors.validation import reconcile_judgment
from scraper.config import settings
from scraper.models import Citation, CorpusMetadata, Judgment, QuarantineQueue, ScraperStaging
from scraper.parsers.case_metadata import also_reported_citation_strings, judgment_caption_slice
from scraper.parsers.citation_extractor import extract_citations, normalise_citation
from scraper.tasks.promotion import promote_judgment_staging

logger = logging.getLogger(__name__)

SOURCE_NAME = "PakistanLawSite"
BACKUP_KEY = "false_citation_alias_cleanup_backup_{run_id}"
REQUALIFY_BACKUP_KEY = "false_citation_alias_cleanup_requalify_{run_id}"
REQUALIFY_JUDGMENT_BACKUP_KEY = "false_citation_alias_cleanup_requalify_judgments_{run_id}"
UNDO_PRIOR_KEY = "false_citation_alias_cleanup_undo_prior_{run_id}"
LATEST_RUN_KEY = "false_citation_alias_cleanup_latest_run"
ALREADY_BELONGS_SQL = "%already belongs to judgment%"


def _already_belongs_reason_clause():
    return func.coalesce(ScraperStaging.quarantine_reason, "").ilike(ALREADY_BELONGS_SQL)


def _citation_strings_before(text: str, end: int) -> Set[str]:
    raw = text or ""
    out: Set[str] = set()
    for c in extract_citations(raw):
        norm = c.get("normalized") or normalise_citation(c["raw"])
        if not norm:
            continue
        if c["span"][0] < end and not citation_referred_in_text(raw, c["span"][0], c["span"][1]):
            out.add(norm)
    return out


def caption_citation_strings(text: str) -> Set[str]:
    return _citation_strings_before(text, len(judgment_caption_slice(text or "")))


def legitimate_parallel_alias(alias: str, full_text: str, *, primary: Optional[str] = None) -> bool:
    """Genuine parallel: caption / also-reported line only (never headnote citation lists)."""
    raw = full_text or ""
    if not alias or not raw:
        return False
    return alias in caption_citation_strings(raw) or alias in also_reported_citation_strings(raw)


def alias_suspicion_reason(alias: str, full_text: str, *, primary: Optional[str] = None) -> Optional[str]:
    """None when the alias is a genuine parallel; else ref-only or not-in-text."""
    if legitimate_parallel_alias(alias, full_text, primary=primary):
        return None
    raw = full_text or ""
    mentions: List[bool] = []
    for c in extract_citations(raw):
        norm = c.get("normalized") or normalise_citation(c["raw"])
        if norm != alias:
            continue
        mentions.append(citation_referred_in_text(raw, c["span"][0], c["span"][1]))
    if not mentions:
        return "not in target text"
    if any(not is_ref for is_ref in mentions):
        return "body mention"
    return "ref-only mention"


# Back-compat for tests importing the private name
_alias_suspicion_reason = alias_suspicion_reason


async def _quarantined_staging_for_alias(db: AsyncSession, alias: str) -> List[str]:
    rows = (
        await db.execute(
            select(ScraperStaging.id).where(
                ScraperStaging.status == "quarantined",
                ScraperStaging.extracted_citation == alias,
                _already_belongs_reason_clause(),
            )
        )
    ).scalars().all()
    return [str(r) for r in rows]


async def scan_false_aliases(db: AsyncSession) -> Tuple[List[Dict[str, Any]], int, int]:
    """Return (suspicious rows, suspicious count, total non-primary alias count)."""
    suspicious: List[Dict[str, Any]] = []
    total_non_primary = 0
    last_id = None
    while True:
        q = (
            select(Citation, Judgment)
            .join(Judgment, Judgment.id == Citation.judgment_id)
            .where(Citation.is_primary.is_(False))
            .order_by(Citation.id)
            .limit(500)
        )
        if last_id is not None:
            q = q.where(Citation.id > last_id)
        batch = (await db.execute(q)).all()
        if not batch:
            break
        for cit, j in batch:
            last_id = cit.id
            total_non_primary += 1
            alias = cit.citation_string
            reason = alias_suspicion_reason(alias, j.full_text or "", primary=j.canonical_citation)
            if not reason:
                continue
            staging_ids = await _quarantined_staging_for_alias(db, alias)
            suspicious.append(
                {
                    "alias": alias,
                    "target": j.canonical_citation,
                    "target_judgment_id": str(j.id),
                    "citation_id": str(cit.id),
                    "reason": reason,
                    "quarantined_staging_ids": staging_ids,
                }
            )
    return suspicious, len(suspicious), total_non_primary


async def _backup_citations(db: AsyncSession, run_id: str, citation_ids: List[str]) -> None:
    if not citation_ids:
        return
    from uuid import UUID

    rows = (
        await db.execute(select(Citation).where(Citation.id.in_([UUID(x) for x in citation_ids])))
    ).scalars().all()
    payload = [
        {
            "id": str(c.id),
            "judgment_id": str(c.judgment_id),
            "citation_string": c.citation_string,
            "raw_string": c.raw_string,
            "reporter": c.reporter,
            "year": c.year,
            "page": c.page,
            "is_primary": c.is_primary,
            "source_evidence": c.source_evidence,
        }
        for c in rows
    ]
    db.add(CorpusMetadata(key=BACKUP_KEY.format(run_id=run_id), value=json.dumps(payload)))


async def _restore_backup(db: AsyncSession, run_id: str) -> Dict[str, Any]:
    row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == BACKUP_KEY.format(run_id=run_id)))).scalars().first()
    if row is None or not row.value:
        return {"citations_restored": 0, "skipped_reclaimed": []}
    from uuid import UUID

    restored = 0
    skipped_reclaimed: List[str] = []
    for item in json.loads(row.value):
        exists = (
            await db.execute(select(Citation).where(Citation.citation_string == item["citation_string"]))
        ).scalars().first()
        if exists is not None:
            skipped_reclaimed.append(item["citation_string"])
            continue
        db.add(
            Citation(
                id=UUID(item["id"]),
                judgment_id=UUID(item["judgment_id"]),
                citation_string=item["citation_string"],
                raw_string=item.get("raw_string"),
                reporter=item.get("reporter"),
                year=item.get("year"),
                page=item.get("page"),
                is_primary=item.get("is_primary", False),
                source_evidence=item.get("source_evidence"),
            )
        )
        restored += 1
    await db.delete(row)
    return {"citations_restored": restored, "skipped_reclaimed": skipped_reclaimed}


def _judgment_backup_payload(j: Judgment, citations: List[Citation]) -> Dict[str, Any]:
    return {
        "id": str(j.id),
        "canonical_citation": j.canonical_citation,
        "case_title": j.case_title,
        "court_id": str(j.court_id) if j.court_id else None,
        "court_name": j.court_name,
        "judge_names": j.judge_names,
        "bench_size": j.bench_size,
        "bench_type": j.bench_type,
        "decision_date": j.decision_date.isoformat() if j.decision_date else None,
        "year": j.year,
        "reporter": j.reporter,
        "volume": j.volume,
        "page_number": j.page_number,
        "docket_number": j.docket_number,
        "docket_key": j.docket_key,
        "unreported": j.unreported,
        "proceeding_type": j.proceeding_type,
        "petitioner": j.petitioner,
        "respondent": j.respondent,
        "full_text": j.full_text,
        "full_text_hash": j.full_text_hash,
        "headnotes": j.headnotes,
        "summary": j.summary,
        "statutes_cited": j.statutes_cited,
        "citations_cited": j.citations_cited,
        "access_method": j.access_method,
        "source_name": j.source_name,
        "source_url": j.source_url,
        "source_provenance_id": str(j.source_provenance_id) if j.source_provenance_id else None,
        "original_document_id": str(j.original_document_id) if j.original_document_id else None,
        "has_original_pdf": j.has_original_pdf,
        "language": j.language,
        "confidence_score": j.confidence_score,
        "quality_flag_urdu_headnote_mojibake": j.quality_flag_urdu_headnote_mojibake,
        "headnote_stripped_full_text": j.headnote_stripped_full_text,
        "extraction_engine": j.extraction_engine,
        "citations": [
            {
                "id": str(c.id),
                "citation_string": c.citation_string,
                "raw_string": c.raw_string,
                "reporter": c.reporter,
                "year": c.year,
                "page": c.page,
                "is_primary": c.is_primary,
                "source_evidence": c.source_evidence,
            }
            for c in citations
        ],
    }


async def _backup_requalify_judgments(db: AsyncSession, run_id: str, records: List[Dict[str, Any]]) -> None:
    from uuid import UUID

    judgment_ids = [
        UUID(item["promoted_to_id"])
        for item in records
        if item.get("result") == "promoted" and item.get("promoted_to_id")
    ]
    if not judgment_ids:
        return
    payload: List[Dict[str, Any]] = []
    for jid in judgment_ids:
        j = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().first()
        if j is None:
            continue
        cits = (await db.execute(select(Citation).where(Citation.judgment_id == jid))).scalars().all()
        payload.append(_judgment_backup_payload(j, list(cits)))
    if payload:
        db.add(
            CorpusMetadata(
                key=REQUALIFY_JUDGMENT_BACKUP_KEY.format(run_id=run_id),
                value=json.dumps(payload),
            )
        )


async def _delete_requalify_judgments(db: AsyncSession, run_id: str) -> int:
    from uuid import UUID

    row = (
        await db.execute(
            select(CorpusMetadata).where(CorpusMetadata.key == REQUALIFY_JUDGMENT_BACKUP_KEY.format(run_id=run_id))
        )
    ).scalars().first()
    if row is None or not row.value:
        return 0
    deleted = 0
    for item in json.loads(row.value):
        j = (await db.execute(select(Judgment).where(Judgment.id == UUID(item["id"])))).scalars().first()
        if j is None:
            continue
        await db.delete(j)
        deleted += 1
    return deleted


async def _undo_requalify(db: AsyncSession, run_id: str) -> Dict[str, int]:
    from uuid import UUID

    judgments_deleted = await _delete_requalify_judgments(db, run_id)
    row = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == REQUALIFY_BACKUP_KEY.format(run_id=run_id)))
    ).scalars().first()
    if row is None or not row.value:
        return {"staging_rows_reverted": 0, "judgments_deleted": judgments_deleted}
    reverted = 0
    for item in json.loads(row.value):
        st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == UUID(item["staging_id"])))).scalars().first()
        if st is None:
            continue
        st.status = item.get("prior_status") or "quarantined"
        st.quarantine_reason = item.get("prior_quarantine_reason")
        st.promoted_to_id = UUID(item["prior_promoted_to_id"]) if item.get("prior_promoted_to_id") else None
        reverted += 1
    await db.delete(row)
    return {"staging_rows_reverted": reverted, "judgments_deleted": judgments_deleted}


async def requalify_already_belongs(
    db: AsyncSession, *, dry_run: bool, alias_strings: Set[str]
) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    from scraper.models import ScraperSource

    counts: Dict[str, int] = {
        "scanned": 0,
        "promoted": 0,
        "duplicate": 0,
        "still_quarantined": 0,
        "would_promote": 0,
        "would_duplicate": 0,
        "would_still_quarantine": 0,
    }
    records: List[Dict[str, Any]] = []
    if not alias_strings:
        return counts, records
    directory = await load_court_directory(db)
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
    min_confidence = float((source.extraction_min_confidence if source else None) or settings.SGAI_DEFAULT_MIN_CONFIDENCE)
    rows = (
        await db.execute(
            select(ScraperStaging).where(
                ScraperStaging.status == "quarantined",
                ScraperStaging.extracted_citation.in_(list(alias_strings)),
                _already_belongs_reason_clause(),
            )
        )
    ).scalars().all()
    for st in rows:
        counts["scanned"] += 1
        prior_status = st.status
        prior_quarantine_reason = st.quarantine_reason
        prior_promoted_to_id = st.promoted_to_id
        text = st.raw_text or ""
        meta = {"citation": st.extracted_citation, "title": st.extracted_title, "court": st.extracted_court, "url": st.source_url}
        dj = det.extract_judgment_deterministic(html=None, text=text, source_meta=meta)
        out = reconcile_judgment(
            deterministic=dj,
            ai=None,
            raw_text=text,
            court_directory=directory,
            min_confidence=min_confidence,
            source_name=st.source_name,
        )
        if dry_run:
            if out.quarantine:
                counts["would_still_quarantine"] += 1
                continue
            nested = await db.begin_nested()
            try:
                reconciled = dict(out.data or {})
                st.reconciled_json = reconciled
                st.confidence_score = out.confidence
                st.validation_errors = out.errors
                st.status = "extracted"
                st.quarantine_reason = None
                await db.flush()
                result = await promote_judgment_staging(db, st, force=True)
                if result == "duplicate":
                    counts["would_duplicate"] += 1
                elif result == "promoted":
                    counts["would_promote"] += 1
                else:
                    counts["would_still_quarantine"] += 1
            finally:
                await nested.rollback()
                await db.refresh(st)
            continue
        if out.quarantine:
            counts["still_quarantined"] += 1
            continue
        reconciled = dict(out.data or {})
        st.reconciled_json = reconciled
        st.confidence_score = out.confidence
        st.validation_errors = out.errors
        st.status = "extracted"
        st.quarantine_reason = None
        await db.flush()
        result = await promote_judgment_staging(db, st)
        await db.commit()
        if result in ("promoted", "duplicate"):
            counts[result] += 1
            records.append(
                {
                    "staging_id": str(st.id),
                    "prior_status": prior_status,
                    "prior_quarantine_reason": prior_quarantine_reason,
                    "prior_promoted_to_id": str(prior_promoted_to_id) if prior_promoted_to_id else None,
                    "result": result,
                    "promoted_to_id": str(st.promoted_to_id) if st.promoted_to_id else None,
                }
            )
            items = (
                await db.execute(
                    select(QuarantineQueue).where(QuarantineQueue.staging_id == st.id, QuarantineQueue.reviewed.is_(False))
                )
            ).scalars().all()
            for item in items:
                item.reviewed = True
                item.reviewed_by = "system"
                item.reviewed_at = datetime.now(timezone.utc)
                item.resolution = "requalified"
                item.resolution_notes = f"false alias cleanup: {result}"
            if items:
                await db.commit()
        else:
            counts["still_quarantined"] += 1
    return counts, records


async def run_citation_alias_cleanup(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
    csv_path: Optional[Path] = None,
) -> Dict[str, Any]:
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    async with SessionLocal() as db:
        if undo:
            restored = await _restore_backup(db, undo)
            reverted = await _undo_requalify(db, undo)
            prior = (
                await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == UNDO_PRIOR_KEY.format(run_id=undo)))
            ).scalars().first()
            latest = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
            if latest is not None and latest.value == undo:
                if prior and prior.value:
                    latest.value = prior.value
                else:
                    await db.delete(latest)
            if prior is not None:
                await db.delete(prior)
            await db.commit()
            return {"undone": {**restored, **reverted, "run_id": undo}}

        suspicious, n_suspicious, total_non_primary = await scan_false_aliases(db)
        out: Dict[str, Any] = {
            "dry_run": not apply,
            "summary": f"{n_suspicious} suspicious of {total_non_primary} non-primary aliases",
            "suspicious_count": n_suspicious,
            "non_primary_alias_count": total_non_primary,
        }
        path = csv_path or Path(f"false_citation_aliases_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv")
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["alias", "target", "reason", "quarantined_staging_ids", "citation_id", "target_judgment_id"],
            )
            writer.writeheader()
            for row in suspicious:
                staging = row["quarantined_staging_ids"]
                writer.writerow(
                    {
                        "alias": row["alias"],
                        "target": row["target"],
                        "reason": row["reason"],
                        "quarantined_staging_ids": ";".join(staging),
                        "citation_id": row["citation_id"],
                        "target_judgment_id": row["target_judgment_id"],
                    }
                )
        out["csv_path"] = str(path)
        alias_strings = {r["alias"] for r in suspicious}
        requal_preview, _ = await requalify_already_belongs(db, dry_run=True, alias_strings=alias_strings)
        out["requalify_preview"] = requal_preview

        if not apply:
            return out

        run_id = uuid.uuid4().hex
        citation_ids = [r["citation_id"] for r in suspicious]
        await _backup_citations(db, run_id, citation_ids)
        if citation_ids:
            from uuid import UUID

            await db.execute(delete(Citation).where(Citation.id.in_([UUID(x) for x in citation_ids])))
        prior_latest = (
            await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))
        ).scalars().first()
        db.add(
            CorpusMetadata(
                key=UNDO_PRIOR_KEY.format(run_id=run_id),
                value=prior_latest.value if prior_latest is not None else "",
            )
        )
        if prior_latest is None:
            db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
        else:
            prior_latest.value = run_id
        await db.commit()
        requal, requal_records = await requalify_already_belongs(db, dry_run=False, alias_strings=alias_strings)
        if requal_records:
            db.add(CorpusMetadata(key=REQUALIFY_BACKUP_KEY.format(run_id=run_id), value=json.dumps(requal_records)))
            await _backup_requalify_judgments(db, run_id, requal_records)
            await db.commit()
        out["applied"] = {"run_id": run_id, "aliases_removed": len(citation_ids), "requalify": requal}
        return out


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Dry-run false citation alias cleanup (default); --apply to delete with backup; --undo RUN_ID to restore"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--undo", metavar="RUN_ID")
    parser.add_argument("--csv", dest="csv_path", default=None, help="Output CSV path (dry-run and apply)")
    args = parser.parse_args(argv)
    out = run_async(
        run_citation_alias_cleanup(
            apply=args.apply,
            undo=args.undo,
            csv_path=Path(args.csv_path) if args.csv_path else None,
        )
    )
    summary = out.get("summary")
    if summary:
        print(summary)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
