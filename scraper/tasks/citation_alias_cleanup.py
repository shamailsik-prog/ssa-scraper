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
from scraper.extractors.deterministic import referred_after_citation
from scraper.extractors.hybrid_extractor import load_court_directory
from scraper.extractors.validation import reconcile_judgment
from scraper.config import settings
from scraper.models import Citation, CorpusMetadata, Judgment, QuarantineQueue, ScraperStaging
from scraper.parsers.case_metadata import judgment_caption_slice
from scraper.parsers.citation_extractor import extract_citations, normalise_citation
from scraper.tasks.promotion import promote_judgment_staging

logger = logging.getLogger(__name__)

SOURCE_NAME = "PakistanLawSite"
BACKUP_KEY = "false_citation_alias_cleanup_backup_{run_id}"
UNDO_PRIOR_KEY = "false_citation_alias_cleanup_undo_prior_{run_id}"
LATEST_RUN_KEY = "false_citation_alias_cleanup_latest_run"
ALREADY_BELONGS = re.compile(r"citation .+ already belongs to judgment", re.I)


def caption_citation_strings(text: str) -> Set[str]:
    raw = text or ""
    end = len(judgment_caption_slice(raw))
    out: Set[str] = set()
    for c in extract_citations(raw):
        norm = c.get("normalized") or normalise_citation(c["raw"])
        if not norm:
            continue
        if c["span"][0] < end and referred_after_citation.match(raw, c["span"][1]) is None:
            out.add(norm)
    return out


def _alias_suspicion_reason(alias: str, full_text: str, caption: Set[str]) -> Optional[str]:
    if alias in caption:
        return None
    raw = full_text or ""
    if not raw:
        return "not in target text"
    found_in_text = False
    for c in extract_citations(raw):
        norm = c.get("normalized") or normalise_citation(c["raw"])
        if norm != alias:
            continue
        found_in_text = True
        if referred_after_citation.match(raw, c["span"][1]) is not None:
            return "ref-only mention"
        if c["span"][0] >= len(judgment_caption_slice(raw)):
            return "ref-only mention"
    if not found_in_text:
        return "not in target text"
    return "ref-only mention"


async def _quarantined_staging_for_alias(db: AsyncSession, alias: str) -> List[str]:
    pattern = f"%{alias}%already belongs to%"
    rows = (
        await db.execute(
            select(ScraperStaging.id).where(
                ScraperStaging.status == "quarantined",
                ScraperStaging.extracted_citation == alias,
                func.coalesce(ScraperStaging.quarantine_reason, "").ilike(pattern),
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
            caption = caption_citation_strings(j.full_text or "")
            reason = _alias_suspicion_reason(alias, j.full_text or "", caption)
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


async def _restore_backup(db: AsyncSession, run_id: str) -> int:
    row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == BACKUP_KEY.format(run_id=run_id)))).scalars().first()
    if row is None or not row.value:
        return 0
    from uuid import UUID

    restored = 0
    for item in json.loads(row.value):
        exists = (
            await db.execute(select(Citation).where(Citation.citation_string == item["citation_string"]))
        ).scalars().first()
        if exists is not None:
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
    return restored


async def requalify_already_belongs(db: AsyncSession, *, dry_run: bool, alias_strings: Set[str]) -> Dict[str, int]:
    from scraper.models import ScraperSource

    counts = {"scanned": 0, "promoted": 0, "duplicate": 0, "still_quarantined": 0}
    if not alias_strings:
        return counts
    directory = await load_court_directory(db)
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
    min_confidence = float((source.extraction_min_confidence if source else None) or settings.SGAI_DEFAULT_MIN_CONFIDENCE)
    rows = (
        await db.execute(
            select(ScraperStaging).where(
                ScraperStaging.status == "quarantined",
                ScraperStaging.extracted_citation.in_(list(alias_strings)),
                func.coalesce(ScraperStaging.quarantine_reason, "").ilike("%already belongs to judgment%"),
            )
        )
    ).scalars().all()
    for st in rows:
        counts["scanned"] += 1
        if dry_run:
            counts["promoted"] += 1
            continue
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
    return counts


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
            return {"undone": {"citations_restored": restored, "run_id": undo}}

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
                fieldnames=["alias", "target", "reason", "quarantined_staging_id", "citation_id", "target_judgment_id"],
            )
            writer.writeheader()
            for row in suspicious:
                staging = row["quarantined_staging_ids"]
                writer.writerow(
                    {
                        "alias": row["alias"],
                        "target": row["target"],
                        "reason": row["reason"],
                        "quarantined_staging_id": staging[0] if staging else "",
                        "citation_id": row["citation_id"],
                        "target_judgment_id": row["target_judgment_id"],
                    }
                )
        out["csv_path"] = str(path)

        if not apply:
            return out

        run_id = uuid.uuid4().hex
        citation_ids = [r["citation_id"] for r in suspicious]
        await _backup_citations(db, run_id, citation_ids)
        alias_strings = {r["alias"] for r in suspicious}
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
        requal = await requalify_already_belongs(db, dry_run=False, alias_strings=alias_strings)
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
