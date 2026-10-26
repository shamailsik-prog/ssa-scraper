"""Merge bogus year section numbers back into the previous real section.

Dry-run by default. ``--apply`` writes. This does not insert or delete
``document_quarantine`` rows, so a quarantined statute stays hidden.

Operator steps: docs/YEAR_SECTION_SWEEP.md
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.fetchers import sha256_text
from scraper.models import (
    CorpusMetadata,
    EmbeddingQueue,
    InstrumentSectionRelation,
    Statute,
    StatuteSection,
    StatuteSectionVersion,
)
from scraper.statute_quality.year_section_merge import (
    YEAR_SECTION_NUMBER_SQL,
    plan_year_section_merges,
    source_host,
    spread_host_samples,
)

LATEST_RUN_KEY = "year_section_sweep_latest_run_id"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report bogus year section numbers (1800-2099) and, with --apply, "
            "merge each one's text into the previous real section. "
            "Default is dry-run and writes nothing. Run the dry-run before --apply. "
            "Does not unquarantine statutes. See docs/YEAR_SECTION_SWEEP.md."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write merges. Omit for a dry-run (no writes).",
    )
    return parser


def _empty_host() -> Dict[str, int]:
    return {
        "statutes": 0,
        "year_sections": 0,
        "merged": 0,
        "skipped_no_previous": 0,
        "skipped_empty": 0,
        "skipped_in_sequence": 0,
    }


def _log_key(run_id: str) -> str:
    return f"year_section_sweep_log:{run_id}"


async def _affected_statute_ids(db: AsyncSession) -> List[UUID]:
    rows = (
        await db.execute(
            select(StatuteSection.statute_id)
            .where(func.btrim(StatuteSection.section_number).op("~")(YEAR_SECTION_NUMBER_SQL))
            .distinct()
        )
    ).scalars().all()
    return list(rows)


async def _load_sections(db: AsyncSession, statute_ids: List[UUID]) -> Dict[UUID, List[Dict[str, Any]]]:
    if not statute_ids:
        return {}
    rows = (
        await db.execute(
            select(
                StatuteSection.id,
                StatuteSection.statute_id,
                StatuteSection.section_number,
                StatuteSection.sort_key,
                StatuteSection.created_at,
                StatuteSectionVersion.section_text,
            )
            .outerjoin(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id.in_(statute_ids))
        )
    ).all()
    grouped: Dict[UUID, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.statute_id].append(
            {
                "id": str(row.id),
                "section_number": row.section_number,
                "text": row.section_text or "",
                "sort_key": row.sort_key,
                "created_at": row.created_at,
            }
        )
    return grouped


async def _apply_plan(db: AsyncSession, plan: Dict[str, Any]) -> None:
    if not plan["merged"]:
        return
    target_ids = [UUID(item["into_section_id"]) for item in plan["merged"]]
    year_ids = [UUID(item["year_section_id"]) for item in plan["merged"]]
    targets = (
        await db.execute(select(StatuteSection).where(StatuteSection.id.in_(target_ids)))
    ).scalars().all()
    by_id = {str(sec.id): sec for sec in targets}
    for target_id, merged_text in plan["merged_text_by_target"].items():
        target = by_id[target_id]
        text_hash = sha256_text(" ".join(merged_text.split()))
        existing = (
            await db.execute(
                select(StatuteSectionVersion).where(
                    StatuteSectionVersion.section_id == target.id,
                    StatuteSectionVersion.text_hash == text_hash,
                )
            )
        ).scalars().first()
        if existing is None:
            current_no = (
                await db.execute(
                    select(func.max(StatuteSectionVersion.version_no)).where(
                        StatuteSectionVersion.section_id == target.id
                    )
                )
            ).scalar() or 0
            existing = StatuteSectionVersion(
                section_id=target.id,
                version_no=int(current_no) + 1,
                section_text=merged_text,
                text_hash=text_hash,
                version_confidence=0.5,
            )
            db.add(existing)
            await db.flush()
        target.current_version_id = existing.id
    await db.flush()
    for item in plan["merged"]:
        await db.execute(
            update(InstrumentSectionRelation)
            .where(InstrumentSectionRelation.target_statute_section_id == UUID(item["year_section_id"]))
            .values(target_statute_section_id=UUID(item["into_section_id"]))
        )
    await db.execute(
        delete(EmbeddingQueue).where(
            EmbeddingQueue.table_name == "statute_section",
            EmbeddingQueue.record_id.in_(year_ids),
        )
    )
    await db.execute(delete(StatuteSection).where(StatuteSection.id.in_(year_ids)))
    for target_id in {item["into_section_id"] for item in plan["merged"]}:
        await _requeue_section_embedding(db, UUID(target_id))


async def _requeue_section_embedding(db: AsyncSession, section_id: UUID) -> None:
    row = (
        await db.execute(
            select(EmbeddingQueue).where(
                EmbeddingQueue.table_name == "statute_section",
                EmbeddingQueue.record_id == section_id,
            )
        )
    ).scalars().first()
    if row is None:
        db.add(
            EmbeddingQueue(
                record_id=section_id,
                table_name="statute_section",
                access_method="public",
                status="pending",
                embedding_model=settings.EMBEDDING_MODEL,
                embedding_dimensions=settings.EMBEDDING_DIM,
            )
        )
        return
    row.status = "pending"
    row.attempts = 0
    row.error_message = None


async def run_year_section_sweep(*, apply: bool = False) -> Dict[str, Any]:
    """Count year-sections per source host. Write only when ``apply`` is true.

    Quarantine rows are never inserted or removed.
    """
    async with SessionLocal() as db:
        statute_ids = await _affected_statute_ids(db)
        statutes = (
            await db.execute(select(Statute).where(Statute.id.in_(statute_ids)))
        ).scalars().all() if statute_ids else []
        sections = await _load_sections(db, statute_ids)
        by_host: Dict[str, Dict[str, int]] = defaultdict(_empty_host)
        merged = 0
        skipped_no_previous = 0
        skipped_empty = 0
        skipped_in_sequence = 0
        year_sections = 0
        sample_rows: List[Dict[str, Any]] = []
        log_merges: List[Dict[str, Any]] = []
        text_by_section: Dict[str, str] = {
            section["id"]: section["text"]
            for group in sections.values()
            for section in group
        }
        for statute in statutes:
            plan = plan_year_section_merges(sections.get(statute.id, []))
            host = source_host(statute.source_url)
            bucket = by_host[host]
            n_years = (
                len(plan["merged"])
                + len(plan["skipped_no_previous"])
                + len(plan["skipped_empty"])
                + len(plan["skipped_in_sequence"])
            )
            if n_years == 0:
                continue
            bucket["statutes"] += 1
            bucket["year_sections"] += n_years
            bucket["merged"] += len(plan["merged"])
            bucket["skipped_no_previous"] += len(plan["skipped_no_previous"])
            bucket["skipped_empty"] += len(plan["skipped_empty"])
            bucket["skipped_in_sequence"] += len(plan["skipped_in_sequence"])
            year_sections += n_years
            merged += len(plan["merged"])
            skipped_no_previous += len(plan["skipped_no_previous"])
            skipped_empty += len(plan["skipped_empty"])
            skipped_in_sequence += len(plan["skipped_in_sequence"])
            sample_rows.append(
                {
                    "statute_id": str(statute.id),
                    "name": statute.name,
                    "host": host,
                    "merged": [
                        {"year": item["year_section_number"], "into": item["into_section_number"]}
                        for item in plan["merged"]
                    ],
                    "skipped_no_previous": [item["year_section_number"] for item in plan["skipped_no_previous"]],
                    "skipped_in_sequence": [item["year_section_number"] for item in plan["skipped_in_sequence"]],
                }
            )
            if apply:
                for item in plan["merged"]:
                    log_merges.append(
                        {
                            "statute_id": str(statute.id),
                            "year_section_id": item["year_section_id"],
                            "year_section_number": item["year_section_number"],
                            "into_section_id": item["into_section_id"],
                            "into_section_number": item["into_section_number"],
                            "text": text_by_section.get(item["year_section_id"], ""),
                        }
                    )
                await _apply_plan(db, plan)
        run_id = None
        if apply:
            run_id = uuid.uuid4().hex
            latest = (
                await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))
            ).scalars().first()
            if latest is None:
                db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
            else:
                latest.value = run_id
            db.add(
                CorpusMetadata(
                    key=_log_key(run_id),
                    value=json.dumps({"run_id": run_id, "merges": log_merges}),
                )
            )
            await db.commit()
        return {
            "dry_run": not apply,
            "apply": apply,
            "run_id": run_id,
            "statutes": sum(bucket["statutes"] for bucket in by_host.values()),
            "year_sections": year_sections,
            "merged": merged,
            "skipped_no_previous": skipped_no_previous,
            "skipped_empty": skipped_empty,
            "skipped_in_sequence": skipped_in_sequence,
            "by_host": {host: counts for host, counts in sorted(by_host.items())},
            "samples": spread_host_samples(sample_rows, limit=20),
            "note": (
                "Dry-run writes nothing. Read it on both MAIN and FREE-SITES before --apply on either. "
                "--apply merges only bogus year-sections (citation or off-sequence). "
                "skipped_in_sequence rows are kept. Citation links are re-pointed at the merge target, "
                "the target is re-queued for embedding, and the deleted year text is stored in "
                "corpus_metadata. document_quarantine is unchanged. "
                "Live-verify 10 repaired statutes before any release or unquarantine. "
                "See docs/YEAR_SECTION_SWEEP.md."
            ),
        }


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out = run_async(run_year_section_sweep(apply=args.apply))
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
