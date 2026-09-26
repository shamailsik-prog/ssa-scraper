"""Idempotent PakistanLawSite maintenance commands."""

from __future__ import annotations

import argparse
import sys

from scraper.database import run_async
from scraper.tasks.pls_self_healing import reset_retired_pls_search_map_frontier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PakistanLawSite maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser(
        "reset-retired-frontier",
        help="Reset tier-1/2 frontier rows retired for 'search map cannot express' back to pending",
    )
    reset.add_argument("--dry-run", action="store_true", help="Count only; do not update")
    args = parser.parse_args(argv)
    if args.command == "reset-retired-frontier":
        if args.dry_run:
            from sqlalchemy import func, select

            from scraper.database import SessionLocal
            from scraper.models import CrawlFrontier

            async def count():
                async with SessionLocal() as db:
                    n = (
                        await db.execute(
                            select(func.count())
                            .select_from(CrawlFrontier)
                            .where(
                                CrawlFrontier.source_name == "PakistanLawSite",
                                CrawlFrontier.status == "retired",
                                CrawlFrontier.last_error.ilike("%search map cannot express%"),
                            )
                        )
                    ).scalar()
                    return int(n or 0)

            print({"would_reset": run_async(count())})
            return 0
        result = run_async(reset_retired_pls_search_map_frontier())
        print(result)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
