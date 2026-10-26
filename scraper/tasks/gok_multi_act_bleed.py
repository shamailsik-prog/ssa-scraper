"""law.gok.pk multi-act bleed sweep. Dry-run by default.

Classifies statutes whose ``source_url`` host is law.gok.pk. ``--apply``
inserts ``document_quarantine`` rows for visible mismatches. It does not
rewrite statute names or section text, and it does not fetch PDFs.

Already-quarantined rows are classified again and left unchanged, so a second
``--apply`` is a no-op for rows this sweep (or an operator) already held.

    python -m scraper.tasks.gok_multi_act_bleed
    python -m scraper.tasks.gok_multi_act_bleed --apply
    python -m scraper.tasks.gok_multi_act_bleed --undo RUN_ID
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal, run_async
from scraper.models import CorpusMetadata, DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.gok_multi_act_bleed import (
    ACTION_QUARANTINE,
    REASON_BLEED,
    REASON_NAME_MISMATCH,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    classify_gok_multi_act,
    is_law_gok_source,
)

LATEST_RUN_KEY = "gok_multi_act_bleed_latest_run_id"


def _undo_prior_latest_key(run_id: str) -> str:
    return f"gok_multi_act_bleed_undo:{run_id}:prior_latest"


def _empty_counts() -> Dict[str, int]:
    return {VERDICT_MATCH: 0, VERDICT_MISMATCH: 0}


async def _load_candidates(db: AsyncSession, statute_id: Optional[UUID]) -> List[Statute]:
    stmt = select(Statute).where(Statute.source_url.ilike("%law.gok.pk%"))
    if statute_id is not None:
        stmt = stmt.where(Statute.id == statute_id)
    stmt = stmt.order_by(Statute.id)
    rows = (await db.execute(stmt)).scalars().all()
    return [row for row in rows if is_law_gok_source(row.source_url)]


async def _sections_by_statute(db: AsyncSession, statute_ids: Sequence[UUID]) -> Dict[UUID, List[dict]]:
    if not statute_ids:
        return {}
    rows = (
        await db.execute(
            select(
                StatuteSection.statute_id,
                StatuteSection.section_number,
                StatuteSection.section_title,
                StatuteSection.sort_key,
                StatuteSectionVersion.section_text,
            )
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id.in_(list(statute_ids)))
            .order_by(StatuteSection.sort_key.asc().nulls_last(), StatuteSection.section_number)
        )
    ).all()
    out: Dict[UUID, List[dict]] = {}
    for sid, number, title, sort_key, text in rows:
        out.setdefault(sid, []).append(
            {
                "section_number": str(number),
                "section_title": title or "",
                "section_text": text or "",
                "sort_key": sort_key,
            }
        )
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


def _row_payload(statute: Statute, verdict: Any, *, already_quarantined: bool) -> Dict[str, Any]:
    return {
        "statute_id": str(statute.id),
        "stored_name": statute.name,
        "source_url": statute.source_url,
        "fetch_url": verdict.fetch_url,
        "pipe_label": verdict.pipe_label,
        "verdict": verdict.verdict,
        "action": verdict.action,
        "reason": verdict.reason,
        "signals": list(verdict.signals),
        "already_quarantined": already_quarantined,
    }


async def _insert_quarantine(
    db: AsyncSession,
    statute: Statute,
    *,
    reason: str,
    run_id: str,
    now: datetime,
) -> bool:
    """Insert one quarantine row. Return False when a row already exists."""
    exists = (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id == statute.id,
            )
        )
    ).scalars().first()
    if exists is not None:
        return False
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=statute.id,
            reason=reason,
            run_id=run_id,
            quarantined_at=now,
        )
    )
    return True


async def undo_run(db: AsyncSession, run_id: str) -> Dict[str, int]:
    result = await db.execute(
        delete(DocumentQuarantine).where(
            DocumentQuarantine.run_id == run_id,
            or_(
                DocumentQuarantine.reason.like("multi_act_pdf_bleed:law.gok.pk%"),
                DocumentQuarantine.reason.like("name_pdf_mismatch:law.gok.pk%"),
            ),
        )
    )
    removed = int(result.rowcount or 0)
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
    return {"quarantine_rows_removed": removed}


async def run_gok_multi_act_bleed(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
    limit: Optional[int] = None,
    statute_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """Dry-run unless ``apply`` is true. ``undo`` deletes one run's quarantine rows."""
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    async with SessionLocal() as db:
        if undo:
            undone = await undo_run(db, undo)
            return {
                "dry_run": False,
                "applied": None,
                "undone": undone,
                "counts": _empty_counts(),
                "rows": [],
                "population": 0,
            }
        candidates = await _load_candidates(db, statute_id)
        if limit is not None:
            candidates = candidates[: max(0, limit)]
        ids = [row.id for row in candidates]
        sections_map = await _sections_by_statute(db, ids)
        quarantined = await _quarantined_statute_ids(db, ids)
        counts = _empty_counts()
        reason_counts = {REASON_BLEED: 0, REASON_NAME_MISMATCH: 0}
        rows: List[Dict[str, Any]] = []
        pending: List[tuple[Statute, Any]] = []
        for statute in candidates:
            sections = sections_map.get(statute.id, [])
            already = statute.id in quarantined
            verdict = classify_gok_multi_act(
                stored_name=statute.name,
                source_url=statute.source_url or "",
                sections=sections,
            )
            counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
            if verdict.reason in reason_counts:
                reason_counts[verdict.reason] += 1
            rows.append(_row_payload(statute, verdict, already_quarantined=already))
            if apply and not already and verdict.action == ACTION_QUARANTINE and verdict.reason:
                pending.append((statute, verdict))

        applied: Optional[Dict[str, Any]] = None
        if apply:
            run_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc)
            inserted = 0
            for statute, verdict in pending:
                if await _insert_quarantine(db, statute, reason=verdict.reason, run_id=run_id, now=now):
                    inserted += 1
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
            applied = {"run_id": run_id, "quarantined": inserted}
        return {
            "dry_run": not apply,
            "population": len(candidates),
            "counts": counts,
            "reason_counts": reason_counts,
            "rows": rows,
            "applied": applied,
        }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Classify law.gok.pk statutes for multi-act PDF bleed and name/PDF mismatch. "
            "Dry-run by default. --apply quarantines visible mismatches and does not "
            "rewrite statute names or section text."
        )
    )
    parser.add_argument("--apply", action="store_true", help="Insert document_quarantine rows for visible mismatches")
    parser.add_argument("--undo", metavar="RUN_ID", help="Remove quarantine rows inserted by one --apply run")
    parser.add_argument("--limit", type=int, default=None, help="Classify at most this many law.gok.pk rows")
    parser.add_argument("--statute-id", default=None, help="Classify one statute UUID")
    args = parser.parse_args(argv)
    statute_id = UUID(args.statute_id) if args.statute_id else None
    out = run_async(
        run_gok_multi_act_bleed(
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
