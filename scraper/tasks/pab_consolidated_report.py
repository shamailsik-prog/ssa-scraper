"""Read-only inventory of pabalochistan consolidated reprints.

Lists each promoted staging row whose section text carries an ``Amended
upto`` or ``(as amended)`` marker, with the base-Act name and year that
heading proposes. Also flags any other statute of that same year whose
name differs (for example a ``Chief Minister and Ministers … Act No. III
of 1975`` stub beside the consolidated Chief Minister reprint), so the
canonical name can be chosen before any retitle.

The queries list columns. ``statutes_staging.raw_text`` and ``raw_html``
are not selected: the reader role has no grant on those columns. The
heading is parsed from ``statute_section_version.section_text`` of the
promoted statute. ``run_report`` opens a read-only transaction
(``default_transaction_read_only`` and ``SET TRANSACTION READ ONLY``).
There is no ``--apply`` path.

    python -m scraper.tasks.pab_consolidated_report
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from scraper.database import SessionLocal, run_async
from scraper.models import Statute, StatuteSection, StatuteSectionVersion, StatutesStaging
from scraper.statute_quality.pab_consolidated import consolidated_base_heading, is_pabalochistan_source
from scraper.statute_quality.senate_integrity import title_relation


def _names_equivalent(left: str, right: str) -> bool:
    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip().casefold()

    return norm(left) == norm(right)


def staging_rows_select() -> Select:
    """pabalochistan staging metadata. No document body and no extraction JSON."""
    return (
        select(
            StatutesStaging.id,
            StatutesStaging.source_url,
            StatutesStaging.promoted_to_id,
            StatutesStaging.created_at,
        )
        .where(StatutesStaging.source_url.ilike("%pabalochistan.gov.pk%"))
        .order_by(StatutesStaging.created_at.asc(), StatutesStaging.id.asc())
    )


def statute_rows_select(statute_ids: Sequence[UUID]) -> Select:
    return select(Statute.id, Statute.name, Statute.year_enacted, Statute.source_url).where(Statute.id.in_(list(statute_ids)))


def same_year_statutes_select(years: Sequence[int]) -> Select:
    return (
        select(Statute.id, Statute.name, Statute.year_enacted, Statute.source_url)
        .where(Statute.year_enacted.in_(list(years)))
        .order_by(Statute.name.asc(), Statute.id.asc())
    )


def section_text_select(statute_ids: Sequence[UUID]) -> Select:
    """Current section text for the promoted statutes. Not staging raw_text."""
    return (
        select(
            StatuteSection.statute_id,
            StatuteSection.sort_key,
            StatuteSection.section_number,
            StatuteSectionVersion.section_text,
        )
        .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
        .where(StatuteSection.statute_id.in_(list(statute_ids)))
        .order_by(
            StatuteSection.statute_id.asc(),
            StatuteSection.sort_key.asc(),
            StatuteSection.section_number.asc(),
        )
    )


def _same_year_conflicts(proposals: List[Dict[str, Any]], statutes: Sequence[Any]) -> List[Dict[str, Any]]:
    by_year: Dict[int, List[Any]] = defaultdict(list)
    for statute in statutes:
        if statute.year_enacted is not None:
            by_year[statute.year_enacted].append(statute)
    conflicts: List[Dict[str, Any]] = []
    for proposal in proposals:
        for statute in by_year.get(proposal["proposed_year"], []):
            statute_id = str(statute.id)
            if proposal["statute_id"] and statute_id == proposal["statute_id"]:
                continue
            if _names_equivalent(statute.name or "", proposal["proposed_name"]):
                continue
            if title_relation(proposal["proposed_name"], statute.name or "") != "same":
                continue
            conflicts.append(
                {
                    "proposed_name": proposal["proposed_name"],
                    "proposed_year": proposal["proposed_year"],
                    "consolidated_staging_id": proposal["staging_id"],
                    "consolidated_statute_id": proposal["statute_id"],
                    "stub_id": statute_id,
                    "stub_name": statute.name,
                    "stub_year": statute.year_enacted,
                    "stub_source_url": statute.source_url,
                }
            )
    conflicts.sort(key=lambda item: (item["proposed_name"], item["stub_name"], item["stub_id"]))
    return conflicts


def _section_bodies(rows: Sequence[Any]) -> Dict[UUID, str]:
    grouped: Dict[UUID, List[str]] = defaultdict(list)
    for row in rows:
        text_value = row.section_text or ""
        if text_value.strip():
            grouped[row.statute_id].append(text_value)
    return {statute_id: "\n".join(parts) for statute_id, parts in grouped.items()}


async def consolidated_reprint_report(db: AsyncSession) -> Dict[str, Any]:
    """Propose base-Act names for pabalochistan consolidated rows. Reads only."""
    staging_rows = (await db.execute(staging_rows_select())).all()
    candidates = [row for row in staging_rows if is_pabalochistan_source(row.source_url or "") and row.promoted_to_id is not None]
    promoted_ids = list({row.promoted_to_id for row in candidates})
    statutes_by_id: Dict[UUID, Any] = {}
    bodies: Dict[UUID, str] = {}
    if promoted_ids:
        for statute in (await db.execute(statute_rows_select(promoted_ids))).all():
            statutes_by_id[statute.id] = statute
        bodies = _section_bodies((await db.execute(section_text_select(promoted_ids))).all())

    proposals: List[Dict[str, Any]] = []
    for row in candidates:
        heading = consolidated_base_heading(bodies.get(row.promoted_to_id, ""))
        if heading is None:
            continue
        stored = statutes_by_id.get(row.promoted_to_id)
        proposals.append(
            {
                "staging_id": str(row.id),
                "source_url": row.source_url,
                "proposed_name": heading.title,
                "proposed_year": heading.year,
                "pab_amended_upto": heading.marker,
                "statute_id": str(row.promoted_to_id),
                "stored_name": stored.name if stored is not None else None,
                "stored_year": stored.year_enacted if stored is not None else None,
            }
        )
    years = sorted({item["proposed_year"] for item in proposals})
    same_year: List[Any] = []
    if years:
        same_year = list((await db.execute(same_year_statutes_select(years))).all())
    conflicts = _same_year_conflicts(proposals, same_year)
    return {
        "dry_run": True,
        "writes": 0,
        "consolidated_count": len(proposals),
        "conflict_count": len(conflicts),
        "consolidated": proposals,
        "same_year_name_conflicts": conflicts,
        "note": (
            "Read-only. Does not retitle or merge rows. "
            "Heading text is the promoted statute's section text; "
            "statutes_staging.raw_text and raw_html are not selected. "
            "A same-year stub with a different name, such as "
            "'Chief Minister and Ministers … Act No. III of 1975' beside the "
            "consolidated Chief Minister reprint, is listed so the canonical "
            "name can be chosen before any retitle."
        ),
    }


async def begin_read_only(db: AsyncSession) -> None:
    """Make this transaction read-only before any query.

    ``default_transaction_read_only`` keeps a later transaction on the same
    connection read-only. ``SET TRANSACTION READ ONLY`` covers the
    transaction that runs the report, including for a read-only role.
    """
    await db.execute(text("SET default_transaction_read_only = on"))
    await db.execute(text("SET TRANSACTION READ ONLY"))


async def run_report() -> Dict[str, Any]:
    async with SessionLocal() as db:
        await begin_read_only(db)
        return await consolidated_reprint_report(db)


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "List pabalochistan consolidated reprints and the base-Act name and year "
            "their section text proposes. Flags other statutes of that year whose name "
            "differs. Read-only: there is no --apply path and this command writes nothing."
        )
    )


def main(argv: Optional[List[str]] = None) -> int:
    build_parser().parse_args(argv)
    print(json.dumps(run_async(run_report()), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
