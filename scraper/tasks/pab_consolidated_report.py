"""Read-only inventory of pabalochistan consolidated reprints.

Lists each staging row whose first page carries an ``Amended upto`` or
``(as amended)`` marker, with the base-Act name and year that heading
proposes. Also flags any other statute of that same year whose name
differs (for example a ``Chief Minister and Ministers … Act No. III of
1975`` stub beside the consolidated Chief Minister reprint), so the
canonical name can be chosen before any retitle.

This command never writes. It has no ``--apply`` path.

    python -m scraper.tasks.pab_consolidated_report
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal, run_async
from scraper.models import Statute, StatutesStaging
from scraper.statute_quality.pab_consolidated import consolidated_base_heading, is_pabalochistan_source
from scraper.statute_quality.senate_integrity import title_relation


def _names_equivalent(left: str, right: str) -> bool:
    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip().casefold()

    return norm(left) == norm(right)


def _stored_year(value: object) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


async def _stored_identity(db: AsyncSession, row: StatutesStaging) -> Dict[str, Any]:
    if row.promoted_to_id is not None:
        statute = await db.get(Statute, row.promoted_to_id)
        if statute is not None:
            return {
                "statute_id": str(statute.id),
                "stored_name": statute.name,
                "stored_year": statute.year_enacted,
            }
    reconciled = row.reconciled_json if isinstance(row.reconciled_json, dict) else {}
    stored_name = reconciled.get("statute_name")
    return {
        "statute_id": None,
        "stored_name": stored_name if isinstance(stored_name, str) else None,
        "stored_year": _stored_year(reconciled.get("year_enacted")),
    }


def _same_year_conflicts(proposals: List[Dict[str, Any]], statutes: List[Statute]) -> List[Dict[str, Any]]:
    by_year: Dict[int, List[Statute]] = defaultdict(list)
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


async def consolidated_reprint_report(db: AsyncSession) -> Dict[str, Any]:
    """Propose base-Act names for pabalochistan consolidated rows. Reads only."""
    rows = (
        await db.execute(
            select(StatutesStaging)
            .where(StatutesStaging.source_url.ilike("%pabalochistan.gov.pk%"))
            .order_by(StatutesStaging.created_at.asc(), StatutesStaging.id.asc())
        )
    ).scalars().all()
    proposals: List[Dict[str, Any]] = []
    for row in rows:
        if not is_pabalochistan_source(row.source_url or ""):
            continue
        heading = consolidated_base_heading(row.raw_text or "")
        if heading is None:
            continue
        stored = await _stored_identity(db, row)
        proposals.append(
            {
                "staging_id": str(row.id),
                "source_url": row.source_url,
                "proposed_name": heading.title,
                "proposed_year": heading.year,
                "pab_amended_upto": heading.marker,
                **stored,
            }
        )
    years = sorted({item["proposed_year"] for item in proposals})
    statutes: List[Statute] = []
    if years:
        statutes = list(
            (
                await db.execute(
                    select(Statute).where(Statute.year_enacted.in_(years)).order_by(Statute.name.asc(), Statute.id.asc())
                )
            ).scalars().all()
        )
    conflicts = _same_year_conflicts(proposals, statutes)
    return {
        "dry_run": True,
        "writes": 0,
        "consolidated_count": len(proposals),
        "conflict_count": len(conflicts),
        "consolidated": proposals,
        "same_year_name_conflicts": conflicts,
        "note": (
            "Read-only. Does not retitle or merge rows. "
            "A same-year stub with a different name, such as "
            "'Chief Minister and Ministers … Act No. III of 1975' beside the "
            "consolidated Chief Minister reprint, is listed so the canonical "
            "name can be chosen before any retitle."
        ),
    }


async def run_report() -> Dict[str, Any]:
    async with SessionLocal() as db:
        return await consolidated_reprint_report(db)


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "List pabalochistan consolidated reprints and the base-Act name and year "
            "their first page proposes. Flags other statutes of that year whose name "
            "differs. Read-only: there is no --apply path and this command writes nothing."
        )
    )


def main(argv: Optional[List[str]] = None) -> int:
    build_parser().parse_args(argv)
    print(json.dumps(run_async(run_report()), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
