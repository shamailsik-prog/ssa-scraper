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
    plan = sub.add_parser("search-harvest-plan", help="Seed resumable PLS search gap harvest queries")
    plan.add_argument("--dry-run", action="store_true")
    gap = sub.add_parser("search-harvest-gap-report", help="JSON gap report (site total vs rows_known)")
    gap.add_argument("--limit", type=int, default=200)
    run = sub.add_parser("search-harvest-run", help="Run one search harvest query (login session lock)")
    run.add_argument("--query-key", default="")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-pages", type=int, default=0)
    run.add_argument("--priority-gaps", action="store_true")
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
    if args.command == "search-harvest-plan":
        from scraper.tasks.pls_search_harvest import cli_plan

        return cli_plan(["--dry-run"] if args.dry_run else [])
    if args.command == "search-harvest-gap-report":
        from scraper.tasks.pls_search_harvest import cli_gap_report

        return cli_gap_report(["--limit", str(args.limit)])
    if args.command == "search-harvest-run":
        from scraper.tasks.pls_search_harvest import cli_run

        argv_run = []
        if args.query_key:
            argv_run.extend(["--query-key", args.query_key])
        if args.dry_run:
            argv_run.append("--dry-run")
        if args.max_pages:
            argv_run.extend(["--max-pages", str(args.max_pages)])
        if args.priority_gaps:
            argv_run.append("--priority-gaps")
        return cli_run(argv_run or None)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
