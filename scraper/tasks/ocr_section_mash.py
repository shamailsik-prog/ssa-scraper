"""OCR section-mash sweep. Dry-run by default.

Lists visible statutes whose stored section numbers sit far below the ``N.``
headings in their section text, or that have very few sections and a preserved
PDF with no text layer. ``--apply`` quarantines heading mismatches and marks
the scanned few-section rows for re-extract. It does not rewrite section
numbers or section text, and it does not treat OCR page or line numbers as
section numbers.

Senate-sourced rows are skipped. That partial OCR garble is a different class.

Do not pass ``--apply`` until Monitor has reviewed a dry-run on each corpus.

    python -m scraper.tasks.ocr_section_mash
    python -m scraper.tasks.ocr_section_mash --apply
    python -m scraper.tasks.ocr_section_mash --undo RUN_ID
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.corpus_visibility import statute_servable
from scraper.database import SessionLocal, run_async
from scraper.fetchers import read_raw
from scraper.models import CorpusMetadata, DocumentQuarantine, SourceProvenance, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.ocr_section_mash import (
    ACTION_NEEDS_REEXTRACT,
    ACTION_QUARANTINE,
    FEW_STORED_SECTIONS,
    REASON_FEW_SECTIONS_SCANNED,
    REASON_HEADINGS_ABOVE_STORED,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_SUSPECT,
    OcrSectionMashVerdict,
    classify_ocr_section_mash,
    pdf_lacks_text_layer,
)
from scraper.statute_quality.senate_integrity import is_pdf_document_url, is_senate_sourced, split_statute_source_url

logger = logging.getLogger(__name__)

LATEST_RUN_KEY = "ocr_section_mash_latest_run_id"
ScannedProbe = Callable[[Statute], Optional[bool]]


def _undo_prior_latest_key(run_id: str) -> str:
    return f"ocr_section_mash_undo:{run_id}:prior_latest"


def _undo_refetch_key(run_id: str) -> str:
    return f"ocr_section_mash_undo:{run_id}:refetch"


def _empty_counts() -> Dict[str, int]:
    return {VERDICT_MATCH: 0, VERDICT_MISMATCH: 0, VERDICT_SUSPECT: 0}


def _empty_reasons() -> Dict[str, int]:
    return {REASON_HEADINGS_ABOVE_STORED: 0, REASON_FEW_SECTIONS_SCANNED: 0}


async def _recorded_pdf_bytes(db: AsyncSession, fetch_url: str) -> Optional[bytes]:
    """Preserved public PDF bytes for this URL. Never downloads."""
    prov = (
        await db.execute(
            select(SourceProvenance)
            .where(
                SourceProvenance.source_url == fetch_url,
                SourceProvenance.content_kind == "pdf",
                SourceProvenance.access_method == "public",
                SourceProvenance.raw_ref.isnot(None),
            )
            .order_by(SourceProvenance.fetched_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if prov is None or not prov.raw_ref:
        return None
    try:
        data = read_raw(prov.raw_ref)
    except OSError as exc:
        logger.warning("ocr section mash recorded bytes unreadable for %s: %s", fetch_url, exc)
        return None
    if not data.startswith(b"%PDF"):
        return None
    return data


async def _scanned_from_recorded_pdf(db: AsyncSession, statute: Statute) -> Optional[bool]:
    fetch_url, _label = split_statute_source_url(statute.source_url)
    if not fetch_url or not is_pdf_document_url(fetch_url):
        return None
    data = await _recorded_pdf_bytes(db, fetch_url)
    if not data:
        return None
    try:
        return pdf_lacks_text_layer(data)
    except Exception as exc:
        logger.warning("ocr section mash text-layer check failed for %s: %s", fetch_url, exc)
        return None


async def _load_visible(db: AsyncSession, statute_id: Optional[UUID]) -> List[Statute]:
    stmt = select(Statute).where(statute_servable())
    if statute_id is not None:
        stmt = stmt.where(Statute.id == statute_id)
    stmt = stmt.order_by(Statute.id)
    return list((await db.execute(stmt)).scalars().all())


async def _sections_by_statute(db: AsyncSession, statute_ids: Sequence[UUID]) -> Dict[UUID, List[dict]]:
    if not statute_ids:
        return {}
    rows = (
        await db.execute(
            select(
                StatuteSection.statute_id,
                StatuteSection.section_number,
                StatuteSectionVersion.section_text,
            )
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id.in_(list(statute_ids)))
            .order_by(StatuteSection.sort_key.asc().nulls_last(), StatuteSection.section_number)
        )
    ).all()
    out: Dict[UUID, List[dict]] = {}
    for sid, number, text in rows:
        out.setdefault(sid, []).append({"section_number": str(number), "section_text": text or ""})
    return out


async def _quarantined_statute_ids(db: AsyncSession, statute_ids: Sequence[UUID]) -> set[UUID]:
    if not statute_ids:
        return set()
    rows = (
        await db.execute(
            select(DocumentQuarantine.doc_id).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id.in_(list(statute_ids)),
            )
        )
    ).scalars().all()
    return set(rows)


def _row_payload(statute: Statute, verdict: OcrSectionMashVerdict, *, already_quarantined: bool) -> Dict[str, Any]:
    return {
        "statute_id": str(statute.id),
        "stored_name": statute.name,
        "source_url": statute.source_url,
        "source_name": statute.source_name,
        "verdict": verdict.verdict,
        "action": verdict.action,
        "reason": verdict.reason,
        "reasons": list(verdict.reasons),
        "signals": list(verdict.signals),
        "stored_count": verdict.stored_count,
        "stored_max": verdict.stored_max,
        "embedded_max": verdict.embedded_max,
        "embedded_headings": list(verdict.embedded_headings),
        "already_quarantined": already_quarantined,
    }


async def undo_run(db: AsyncSession, run_id: str) -> Dict[str, int]:
    result = await db.execute(
        delete(DocumentQuarantine).where(
            DocumentQuarantine.run_id == run_id,
            DocumentQuarantine.reason.like("ocr_section_mash:%"),
        )
    )
    removed = int(result.rowcount or 0)
    restored = 0
    snap = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_refetch_key(run_id)))
    ).scalars().first()
    if snap is not None and snap.value:
        for item in json.loads(snap.value):
            sid = UUID(item["id"])
            statute = (await db.execute(select(Statute).where(Statute.id == sid))).scalars().first()
            if statute is not None:
                statute.needs_section_refetch = bool(item.get("needs_section_refetch"))
                restored += 1
        await db.delete(snap)
    prior_row = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_prior_latest_key(run_id)))
    ).scalars().first()
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
    return {"quarantine_rows_removed": removed, "refetch_flags_restored": restored}


async def run_ocr_section_mash(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
    limit: Optional[int] = None,
    statute_id: Optional[UUID] = None,
    scanned_probe: Optional[ScannedProbe] = None,
) -> Dict[str, Any]:
    """Dry-run unless ``apply`` is true. ``undo`` reverses one apply run.

    ``scanned_probe`` overrides the preserved-PDF text-layer check. The default
    probe reads recorded bytes only and does not download a PDF.
    """
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    async with SessionLocal() as db:
        if undo:
            undone = await undo_run(db, undo)
            return {
                "dry_run": False,
                "applied": None,
                "undone": undone,
                "population": 0,
                "skipped_senate": 0,
                "suspects": 0,
                "counts": _empty_counts(),
                "reason_counts": _empty_reasons(),
                "rows": [],
            }
        visible = await _load_visible(db, statute_id)
        if limit is not None:
            visible = visible[: max(0, limit)]
        considered: List[Statute] = []
        skipped_senate = 0
        for statute in visible:
            if is_senate_sourced(statute.source_url, statute.source_name):
                skipped_senate += 1
                continue
            considered.append(statute)
        ids = [row.id for row in considered]
        sections_map = await _sections_by_statute(db, ids)
        quarantined = await _quarantined_statute_ids(db, ids)
        counts = _empty_counts()
        reason_counts = _empty_reasons()
        rows: List[Dict[str, Any]] = []
        pending_quarantine: List[tuple[Statute, OcrSectionMashVerdict]] = []
        pending_refetch: List[Statute] = []
        for statute in considered:
            sections = sections_map.get(statute.id, [])
            scanned: Optional[bool] = None
            if scanned_probe is not None:
                scanned = scanned_probe(statute)
            elif len(sections) <= FEW_STORED_SECTIONS:
                scanned = await _scanned_from_recorded_pdf(db, statute)
            verdict = classify_ocr_section_mash(
                sections,
                source_url=statute.source_url or "",
                source_name=statute.source_name or "",
                scanned_source=scanned,
            )
            counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
            for reason in verdict.reasons:
                if reason in reason_counts:
                    reason_counts[reason] += 1
            already = statute.id in quarantined
            if verdict.verdict in (VERDICT_MISMATCH, VERDICT_SUSPECT):
                rows.append(_row_payload(statute, verdict, already_quarantined=already))
            if not apply or already:
                continue
            if verdict.action == ACTION_QUARANTINE and verdict.reason:
                pending_quarantine.append((statute, verdict))
            elif verdict.action == ACTION_NEEDS_REEXTRACT:
                pending_refetch.append(statute)

        applied: Optional[Dict[str, Any]] = None
        if apply:
            run_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc)
            inserted = 0
            for statute, verdict in pending_quarantine:
                exists = (
                    await db.execute(
                        select(DocumentQuarantine).where(
                            DocumentQuarantine.doc_type == "statute",
                            DocumentQuarantine.doc_id == statute.id,
                        )
                    )
                ).scalars().first()
                if exists is not None:
                    continue
                db.add(
                    DocumentQuarantine(
                        doc_type="statute",
                        doc_id=statute.id,
                        reason=verdict.reason,
                        run_id=run_id,
                        quarantined_at=now,
                    )
                )
                inserted += 1
            refetch_snapshot: List[Dict[str, Any]] = []
            reextract = 0
            for statute in pending_refetch:
                refetch_snapshot.append(
                    {"id": str(statute.id), "needs_section_refetch": bool(statute.needs_section_refetch)}
                )
                statute.needs_section_refetch = True
                reextract += 1
            if refetch_snapshot:
                db.add(CorpusMetadata(key=_undo_refetch_key(run_id), value=json.dumps(refetch_snapshot)))
            latest = (
                await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))
            ).scalars().first()
            prior_latest = latest.value if latest is not None else ""
            db.add(CorpusMetadata(key=_undo_prior_latest_key(run_id), value=prior_latest))
            if latest is None:
                db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
            else:
                latest.value = run_id
            await db.commit()
            applied = {"run_id": run_id, "quarantined": inserted, "reextract": reextract}
        return {
            "dry_run": not apply,
            "population": len(considered),
            "skipped_senate": skipped_senate,
            "suspects": len(rows),
            "counts": counts,
            "reason_counts": reason_counts,
            "rows": rows,
            "applied": applied,
        }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "List visible statutes with OCR section-number mash or very few sections "
            "from a scanned PDF. Dry-run by default. Do not pass --apply until Monitor "
            "has reviewed the dry-run. --apply quarantines heading mismatches and marks "
            "scanned few-section rows for re-extract. It does not rewrite section numbers."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Quarantine heading mismatches and mark scanned few-section rows for re-extract",
    )
    parser.add_argument("--undo", metavar="RUN_ID", help="Remove one --apply run's quarantine rows and refetch flags")
    parser.add_argument("--limit", type=int, default=None, help="Classify at most this many visible statutes")
    parser.add_argument("--statute-id", default=None, help="Classify one statute UUID")
    args = parser.parse_args(argv)
    statute_id = UUID(args.statute_id) if args.statute_id else None
    out = run_async(
        run_ocr_section_mash(
            apply=args.apply,
            undo=args.undo,
            limit=args.limit,
            statute_id=statute_id,
        )
    )
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
