"""Merge bogus year section numbers back into the previous real section.

Dry-run by default. ``--apply`` writes. This does not insert or delete
``document_quarantine`` rows, so a quarantined statute stays hidden.

Operator steps: docs/YEAR_SECTION_SWEEP.md
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal, run_async
from scraper.fetchers import sha256_text
from scraper.models import EmbeddingQueue, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.year_section_merge import (
    YEAR_SECTION_NUMBER_SQL,
    plan_year_section_merges,
    source_host,
)


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
    return {"statutes": 0, "year_sections": 0, "merged": 0, "skipped_no_previous": 0, "skipped_empty": 0}


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
    await db.execute(
        delete(EmbeddingQueue).where(
            EmbeddingQueue.table_name == "statute_section",
            EmbeddingQueue.record_id.in_(year_ids),
        )
    )
    await db.execute(delete(StatuteSection).where(StatuteSection.id.in_(year_ids)))


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
        year_sections = 0
        samples: List[Dict[str, Any]] = []
        for statute in statutes:
            plan = plan_year_section_merges(sections.get(statute.id, []))
            host = source_host(statute.source_url)
            bucket = by_host[host]
            n_years = len(plan["merged"]) + len(plan["skipped_no_previous"]) + len(plan["skipped_empty"])
            if n_years == 0:
                continue
            bucket["statutes"] += 1
            bucket["year_sections"] += n_years
            bucket["merged"] += len(plan["merged"])
            bucket["skipped_no_previous"] += len(plan["skipped_no_previous"])
            bucket["skipped_empty"] += len(plan["skipped_empty"])
            year_sections += n_years
            merged += len(plan["merged"])
            skipped_no_previous += len(plan["skipped_no_previous"])
            skipped_empty += len(plan["skipped_empty"])
            if len(samples) < 20:
                samples.append(
                    {
                        "statute_id": str(statute.id),
                        "name": statute.name,
                        "host": host,
                        "merged": [
                            {"year": item["year_section_number"], "into": item["into_section_number"]}
                            for item in plan["merged"]
                        ],
                        "skipped_no_previous": [item["year_section_number"] for item in plan["skipped_no_previous"]],
                    }
                )
            if apply:
                await _apply_plan(db, plan)
        if apply:
            await db.commit()
        return {
            "dry_run": not apply,
            "apply": apply,
            "statutes": sum(bucket["statutes"] for bucket in by_host.values()),
            "year_sections": year_sections,
            "merged": merged,
            "skipped_no_previous": skipped_no_previous,
            "skipped_empty": skipped_empty,
            "by_host": {host: counts for host, counts in sorted(by_host.items())},
            "samples": samples,
            "note": (
                "Dry-run writes nothing. --apply merges year-section text into the previous "
                "real section and removes only those merged year rows. document_quarantine is unchanged. "
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
