"""Read-only inventory of pabalochistan consolidated reprints.

Lists each promoted staging row and the base-Act name and year parsed
from that row's ``statutes_staging.raw_text``. Also flags any other
statute of that same year whose name differs (for example a ``Chief
Minister and Ministers … Act No. III of 1975`` stub beside the
consolidated Chief Minister reprint), so the canonical name can be
chosen before any retitle.

Promoted section text does not carry the ``Amended upto`` line, so it is
not the heading source. Metadata queries stay column-scoped.
``raw_text`` is selected by itself, and only for the candidate staging
ids. ``raw_html`` is not selected. A candidate with no staging text is
reported with reason ``no_staging_text`` instead of being dropped.
``run_report`` opens a read-only transaction
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
from scraper.models import Statute, StatutesStaging
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


def staging_raw_text_select(staging_ids: Sequence[UUID]) -> Select:
    """``raw_text`` only, and only for these candidate staging ids."""
    return select(StatutesStaging.id, StatutesStaging.raw_text).where(StatutesStaging.id.in_(list(staging_ids)))


def statute_rows_select(statute_ids: Sequence[UUID]) -> Select:
    return select(Statute.id, Statute.name, Statute.year_enacted, Statute.source_url).where(Statute.id.in_(list(statute_ids)))


def same_year_statutes_select(years: Sequence[int]) -> Select:
    return (
        select(Statute.id, Statute.name, Statute.year_enacted, Statute.source_url)
        .where(Statute.year_enacted.in_(list(years)))
        .order_by(Statute.name.asc(), Statute.id.asc())
    )


def _same_year_conflicts(proposals: List[Dict[str, Any]], statutes: Sequence[Any]) -> List[Dict[str, Any]]:
    by_year: Dict[int, List[Any]] = defaultdict(list)
    for statute in statutes:
        if statute.year_enacted is not None:
            by_year[statute.year_enacted].append(statute)
    conflicts: List[Dict[str, Any]] = []
    for proposal in proposals:
        if proposal.get("proposed_year") is None:
            continue
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


def _staging_text_missing(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


async def consolidated_reprint_report(db: AsyncSession) -> Dict[str, Any]:
    """Propose base-Act names for pabalochistan consolidated rows. Reads only."""
    staging_rows = (await db.execute(staging_rows_select())).all()
    candidates = [row for row in staging_rows if is_pabalochistan_source(row.source_url or "") and row.promoted_to_id is not None]
    promoted_ids = list({row.promoted_to_id for row in candidates})
    statutes_by_id: Dict[UUID, Any] = {}
    if promoted_ids:
        for statute in (await db.execute(statute_rows_select(promoted_ids))).all():
            statutes_by_id[statute.id] = statute
    raw_by_id: Dict[UUID, object] = {}
    if candidates:
        text_rows = (await db.execute(staging_raw_text_select([row.id for row in candidates]))).all()
        raw_by_id = {row.id: row.raw_text for row in text_rows}

    proposals: List[Dict[str, Any]] = []
    for row in candidates:
        stored = statutes_by_id.get(row.promoted_to_id)
        base = {
            "staging_id": str(row.id),
            "source_url": row.source_url,
            "statute_id": str(row.promoted_to_id),
            "stored_name": stored.name if stored is not None else None,
            "stored_year": stored.year_enacted if stored is not None else None,
        }
        raw_text = raw_by_id.get(row.id)
        if _staging_text_missing(raw_text):
            proposals.append(
                {
                    **base,
                    "proposed_name": None,
                    "proposed_year": None,
                    "pab_amended_upto": None,
                    "reason": "no_staging_text",
                }
            )
            continue
        heading = consolidated_base_heading(raw_text if isinstance(raw_text, str) else "")
        if heading is None:
            continue
        proposals.append(
            {
                **base,
                "proposed_name": heading.title,
                "proposed_year": heading.year,
                "pab_amended_upto": heading.marker,
                "reason": None,
            }
        )
    years = sorted({item["proposed_year"] for item in proposals if item["proposed_year"] is not None})
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
            "The heading is parsed from statutes_staging.raw_text for the "
            "candidate ids only. raw_html is not selected. A candidate with "
            "no staging text has proposed_name None and reason no_staging_text. "
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
            "their staging text proposes. Flags other statutes of that year whose name "
            "differs. Read-only: there is no --apply path and this command writes nothing."
        )
    )


def main(argv: Optional[List[str]] = None) -> int:
    build_parser().parse_args(argv)
    print(json.dumps(run_async(run_report()), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
