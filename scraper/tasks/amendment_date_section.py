"""Inventory sections whose current text opens with a footnote date.

Dry-run by default. ``--apply`` is accepted and still writes nothing: the real
section body is not in the date row, so rewriting the section would not restore
it. Re-extract from the source PDF after the splitter fix. This does not
quarantine, does not set ``needs_section_refetch``, and does not merge text.

This is not the 1800–2099 year-as-section sweep.

Operator steps: docs/AMENDMENT_DATE_SECTION.md
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.corpus_visibility import statute_servable
from scraper.database import SessionLocal, run_async
from scraper.models import Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.amendment_date_section import AMENDMENT_DATE_SECTION_TEXT_SQL
from scraper.statute_quality.year_section_merge import source_host, spread_host_samples


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report visible statute sections whose current text starts with a "
            "footnote date DD.MM.YYYY (for example 11.10.2018). "
            "Default is report-only and writes nothing. "
            "--apply is opt-in and also writes nothing: it does not rewrite "
            "section text, quarantine, or set needs_section_refetch. "
            "The real section body is not in the date row; re-extract the source PDF. "
            "This is not the 1800-2099 year-as-section sweep. "
            "See docs/AMENDMENT_DATE_SECTION.md."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Accepted so the flag stays opt-in. Does not write. "
            "Does not rewrite sections, quarantine, or mark refetch."
        ),
    )
    return parser


async def _visible_date_rows(db: AsyncSession) -> List[Dict[str, Any]]:
    rows = (
        await db.execute(
            select(
                Statute.id,
                Statute.name,
                Statute.source_url,
                StatuteSection.section_number,
                StatuteSectionVersion.section_text,
            )
            .join(StatuteSection, StatuteSection.statute_id == Statute.id)
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(statute_servable())
            .where(StatuteSectionVersion.section_text.op("~")(AMENDMENT_DATE_SECTION_TEXT_SQL))
            .order_by(Statute.id, StatuteSection.sort_key, StatuteSection.section_number)
        )
    ).all()
    found: List[Dict[str, Any]] = []
    for row in rows:
        text = row.section_text or ""
        found.append(
            {
                "statute_id": str(row.id),
                "name": row.name,
                "host": source_host(row.source_url),
                "section_number": str(row.section_number or ""),
                "text_prefix": text[:160],
            }
        )
    return found


def _by_host(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    statutes: Dict[str, set] = defaultdict(set)
    counts: Dict[str, Dict[str, int]] = {}
    for row in rows:
        host = row["host"]
        bucket = counts.setdefault(host, {"rows": 0, "statutes": 0})
        bucket["rows"] += 1
        statutes[host].add(row["statute_id"])
    for host, bucket in counts.items():
        bucket["statutes"] = len(statutes[host])
    return {host: counts[host] for host in sorted(counts)}


async def run_amendment_date_section_inventory(*, apply: bool = False) -> Dict[str, Any]:
    """Count visible date-headed sections. Never writes, including when ``apply`` is true."""
    async with SessionLocal() as db:
        rows = await _visible_date_rows(db)
    return {
        "dry_run": not apply,
        "apply": apply,
        "writes": False,
        "section_rewrites": 0,
        "rows": len(rows),
        "statutes": len({row["statute_id"] for row in rows}),
        "by_host": _by_host(rows),
        "samples": spread_host_samples(rows, limit=20),
        "query": AMENDMENT_DATE_SECTION_TEXT_SQL,
        "note": (
            "Report only. Visible statutes, current section text, POSIX "
            f"{AMENDMENT_DATE_SECTION_TEXT_SQL}. "
            "--apply does not rewrite section text, does not insert document_quarantine, "
            "and does not set needs_section_refetch. "
            "The real section body is not in the date row (free-sites 1032ddb0 lost ss.11, 17, and 23 this way). "
            "Re-extract the source PDF; the splitter now refuses a DD.MM.YYYY line as a heading. "
            "A bare year line such as 2018. is the separate year-as-section sweep, not this inventory. "
            "See docs/AMENDMENT_DATE_SECTION.md."
        ),
    }


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out = run_async(run_amendment_date_section_inventory(apply=args.apply))
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
