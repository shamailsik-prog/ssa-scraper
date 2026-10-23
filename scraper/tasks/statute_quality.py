"""Statute corpus quality CLI: dedupe, jurisdiction, OCR/section re-quarantine. Dry-run by default."""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.corpus_visibility import statute_servable
from scraper.database import SessionLocal, run_async
from scraper.models import CorpusMetadata, DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.parsers.assembly_pdf_sections import assembly_body_too_thin, is_assembly_source
from scraper.statute_quality.ocr_quality import statute_text_poor_ocr
from scraper.statute_quality.section_noise import filter_statute_quality_sections, is_section_body_noise
from scraper.statute_quality_jurisdiction import should_fix_federal_jurisdiction
from scraper.tasks.promotion import _normalized_statute_name_key

logger = logging.getLogger(__name__)

LATEST_RUN_KEY = "statute_quality_latest_run_id"
REASON_DUPLICATE = "statute_quality:duplicate_title_year"
REASON_JURISDICTION = "statute_quality:jurisdiction_fixed"
REASON_POOR_OCR = "statute_quality:poor_ocr"
REASON_THIN_ASSEMBLY = "statute_quality:thin_assembly_body"
REASON_NO_VALID_SECTIONS = "statute_quality:no_valid_sections"


def _undo_prior_latest_key(run_id: str) -> str:
    return f"statute_quality_undo:{run_id}:prior_latest"


def _undo_jurisdiction_key(run_id: str) -> str:
    return f"statute_quality_undo:{run_id}:jurisdiction"


@dataclass
class _StatuteRow:
    id: UUID
    name: str
    jurisdiction: str
    year_enacted: Optional[int]
    source_name: Optional[str]
    source_url: Optional[str]
    section_count: int
    text_chars: int


async def _visible_statute_ids(db: AsyncSession) -> Set[UUID]:
    rows = (await db.execute(select(Statute.id).where(statute_servable()))).scalars().all()
    return set(rows)


async def _statute_metrics(db: AsyncSession) -> Dict[UUID, Tuple[int, int]]:
    q = text(
        """
        SELECT ss.statute_id AS sid,
               COUNT(*)::int AS n,
               COALESCE(SUM(length(btrim(ssv.section_text))), 0)::bigint AS chars
        FROM statute_section ss
        JOIN statute_section_version ssv ON ssv.id = ss.current_version_id
        WHERE length(btrim(ssv.section_text)) > 0
        GROUP BY ss.statute_id
        """
    )
    out: Dict[UUID, Tuple[int, int]] = {}
    for row in (await db.execute(q)).all():
        out[row.sid] = (int(row.n), int(row.chars))
    return out


def _dedupe_group_key(st: Statute) -> Optional[Tuple[str, int, str]]:
    if st.year_enacted is None:
        return None
    name_key = (st.canonical_name_key or "").strip() or _normalized_statute_name_key(st.name)
    if not name_key:
        return None
    return (
        name_key,
        int(st.year_enacted),
        (st.jurisdiction or "").strip(),
    )


def _score_row(st: Statute, section_count: int, text_chars: int) -> Tuple[int, int, int]:
    prov = 1 if (st.source_name and st.source_url) else 0
    return (section_count, text_chars, prov)


async def find_duplicate_groups(db: AsyncSession) -> Dict[str, Any]:
    metrics = await _statute_metrics(db)
    visible = await _visible_statute_ids(db)
    statutes = (await db.execute(select(Statute).where(Statute.id.in_(visible)))).scalars().all()
    groups: Dict[Tuple[str, int, str], List[Statute]] = defaultdict(list)
    for st in statutes:
        key = _dedupe_group_key(st)
        if key is None:
            continue
        groups[key].append(st)
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    losers: List[UUID] = []
    winners: List[UUID] = []
    for _key, members in dup_groups.items():
        ranked = sorted(
            members,
            key=lambda s: _score_row(s, *metrics.get(s.id, (0, 0))),
            reverse=True,
        )
        winners.append(ranked[0].id)
        losers.extend(s.id for s in ranked[1:])
    return {
        "duplicate_groups": len(dup_groups),
        "statutes_to_quarantine": len(losers),
        "losers": losers,
        "winners": winners,
    }


async def find_jurisdiction_fixes(db: AsyncSession) -> Dict[str, Any]:
    visible = await _visible_statute_ids(db)
    rows = (await db.execute(select(Statute).where(Statute.id.in_(visible), Statute.jurisdiction == "Federal"))).scalars().all()
    fixes: List[Dict[str, Any]] = []
    for st in rows:
        new_j = should_fix_federal_jurisdiction(st.jurisdiction, st.source_name)
        if new_j:
            fixes.append({"id": str(st.id), "from": st.jurisdiction, "to": new_j, "source_name": st.source_name})
    return {"federal_mislabeled": len(fixes), "fixes": fixes}


async def _sections_for_statute(db: AsyncSession, statute_id: UUID) -> List[Dict[str, str]]:
    rows = (
        await db.execute(
            select(StatuteSection.section_number, StatuteSectionVersion.section_text)
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == statute_id)
        )
    ).all()
    return [
        {"section_number": str(num), "section_text": txt}
        for num, txt in rows
        if (txt or "").strip()
    ]


async def classify_requarantine(db: AsyncSession) -> Dict[str, Any]:
    visible = await _visible_statute_ids(db)
    statutes = (await db.execute(select(Statute).where(Statute.id.in_(visible)))).scalars().all()
    by_reason: Dict[str, List[str]] = defaultdict(list)
    for st in statutes:
        sections = await _sections_for_statute(db, st.id)
        texts = [s["section_text"] for s in sections]
        filtered = filter_statute_quality_sections(sections, statute_name=st.name)
        if settings.SSA_STATUTE_QUALITY_OCR_GATE:
            issues = statute_text_poor_ocr(name=st.name, section_texts=texts)
            if issues:
                by_reason[REASON_POOR_OCR].append(str(st.id))
                continue
        if settings.SSA_STATUTE_QUALITY_REQUARANTINE:
            if is_assembly_source(st.source_name):
                if assembly_body_too_thin([{"section_text": t} for t in texts]):
                    by_reason[REASON_THIN_ASSEMBLY].append(str(st.id))
                    continue
            if texts and not filtered:
                if all(is_section_body_noise(t) for t in texts):
                    by_reason[REASON_NO_VALID_SECTIONS].append(str(st.id))
    counts = {k: len(v) for k, v in by_reason.items()}
    return {"by_reason": counts, "ids_by_reason": dict(by_reason), "total": sum(counts.values())}


async def _record_run(db: AsyncSession, run_id: str) -> None:
    latest = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
    prior = latest.value if latest is not None else ""
    db.add(CorpusMetadata(key=_undo_prior_latest_key(run_id), value=prior))
    if latest is None:
        db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
    else:
        latest.value = run_id


async def apply_dedupe(db: AsyncSession, run_id: str) -> int:
    info = await find_duplicate_groups(db)
    now = datetime.now(timezone.utc)
    n = 0
    for sid in info["losers"]:
        exists = (
            await db.execute(
                select(DocumentQuarantine).where(
                    DocumentQuarantine.doc_type == "statute",
                    DocumentQuarantine.doc_id == sid,
                )
            )
        ).scalars().first()
        if exists is not None:
            continue
        db.add(
            DocumentQuarantine(
                doc_type="statute",
                doc_id=sid,
                reason=REASON_DUPLICATE,
                run_id=run_id,
                quarantined_at=now,
            )
        )
        n += 1
    return n


async def apply_jurisdiction(db: AsyncSession, run_id: str) -> int:
    info = await find_jurisdiction_fixes(db)
    snapshot: List[Dict[str, str]] = []
    n = 0
    for fix in info["fixes"]:
        sid = UUID(fix["id"])
        st = (await db.execute(select(Statute).where(Statute.id == sid))).scalars().first()
        if st is None:
            continue
        snapshot.append(
            {
                "id": str(st.id),
                "jurisdiction": st.jurisdiction,
                "updated_at": st.updated_at.isoformat() if st.updated_at else None,
            }
        )
        st.jurisdiction = fix["to"]
        n += 1
    db.add(CorpusMetadata(key=_undo_jurisdiction_key(run_id), value=json.dumps(snapshot)))
    return n


async def apply_requarantine(db: AsyncSession, run_id: str) -> int:
    info = await classify_requarantine(db)
    now = datetime.now(timezone.utc)
    n = 0
    for reason, ids in info.get("ids_by_reason", {}).items():
        for sid_s in ids:
            sid = UUID(sid_s)
            exists = (
                await db.execute(
                    select(DocumentQuarantine).where(
                        DocumentQuarantine.doc_type == "statute",
                        DocumentQuarantine.doc_id == sid,
                    )
                )
            ).scalars().first()
            if exists is not None:
                continue
            db.add(
                DocumentQuarantine(
                    doc_type="statute",
                    doc_id=sid,
                    reason=reason,
                    run_id=run_id,
                    quarantined_at=now,
                )
            )
            n += 1
    return n


async def undo_run(db: AsyncSession, run_id: str) -> Dict[str, int]:
    removed = int((await db.execute(delete(DocumentQuarantine).where(DocumentQuarantine.run_id == run_id))).rowcount or 0)
    jur_row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_jurisdiction_key(run_id)))).scalars().first()
    if jur_row is not None and jur_row.value:
        for item in json.loads(jur_row.value):
            sid = UUID(item["id"])
            st = (await db.execute(select(Statute).where(Statute.id == sid))).scalars().first()
            if st is not None:
                st.jurisdiction = item["jurisdiction"]
                if item.get("updated_at"):
                    st.updated_at = datetime.fromisoformat(item["updated_at"])
        await db.delete(jur_row)
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
    return {"quarantine_rows_removed": removed}


async def run_statute_quality(*, apply: bool = False, undo: Optional[str] = None) -> Dict[str, Any]:
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    async with SessionLocal() as db:
        counts: Dict[str, Any] = {
            "dry_run": not apply and not undo,
            "flags": {
                "SSA_STATUTE_QUALITY_DEDUPE": settings.SSA_STATUTE_QUALITY_DEDUPE,
                "SSA_STATUTE_QUALITY_JURISDICTION": settings.SSA_STATUTE_QUALITY_JURISDICTION,
                "SSA_STATUTE_QUALITY_OCR_GATE": settings.SSA_STATUTE_QUALITY_OCR_GATE,
                "SSA_STATUTE_QUALITY_REQUARANTINE": settings.SSA_STATUTE_QUALITY_REQUARANTINE,
            },
        }
        counts["dedupe"] = await find_duplicate_groups(db)
        counts["jurisdiction"] = await find_jurisdiction_fixes(db)
        counts["requarantine"] = await classify_requarantine(db)
        if undo:
            counts["undone"] = await undo_run(db, undo)
            return counts
        if apply:
            run_id = uuid.uuid4().hex
            applied: Dict[str, Any] = {"run_id": run_id}
            await _record_run(db, run_id)
            if settings.SSA_STATUTE_QUALITY_DEDUPE:
                applied["dedupe_quarantined"] = await apply_dedupe(db, run_id)
            if settings.SSA_STATUTE_QUALITY_JURISDICTION:
                applied["jurisdiction_updated"] = await apply_jurisdiction(db, run_id)
            if settings.SSA_STATUTE_QUALITY_OCR_GATE or settings.SSA_STATUTE_QUALITY_REQUARANTINE:
                applied["requarantine_rows"] = await apply_requarantine(db, run_id)
            await db.commit()
            counts["applied"] = applied
        return counts


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Statute quality dry-run counts; --apply to quarantine/fix with flags on")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--undo", metavar="RUN_ID")
    args = parser.parse_args(argv)
    out = run_async(run_statute_quality(apply=args.apply, undo=args.undo))
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
