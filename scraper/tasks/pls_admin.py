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
    ext = sub.add_parser("search-harvest-plan-extended", help="Seed court/judge/statute/keyword/party x year queries")
    ext.add_argument("--dry-run", action="store_true")
    ext.add_argument("--keyword", action="append", default=[])
    ext.add_argument("--party", action="append", default=[])
    snow = sub.add_parser("search-harvest-snowball", help="Queue citations found inside downloaded judgments that are not yet in the corpus")
    snow.add_argument("--dry-run", action="store_true")
    snow.add_argument("--limit", type=int, default=2000)
    sub.add_parser("search-harvest-completeness", help="Completeness proof: queue state, capped jobs, page-continuity holes, known-citation test")
    run = sub.add_parser("search-harvest-run", help="Run one search harvest query (login session lock)")
    run.add_argument("--query-key", default="")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-pages", type=int, default=0)
    run.add_argument("--priority-gaps", action="store_true")
    sub.add_parser("reset-retired-frontier-no-reason", help="Reset never-run frontier rows that were retired with no last_error").add_argument("--dry-run", action="store_true")
    meta = sub.add_parser("backfill-metadata", help="Fill docket/petitioner/respondent/headnotes from the preserved text (only where empty)")
    meta.add_argument("--dry-run", action="store_true")
    meta.add_argument("--limit", type=int, default=None)
    req = sub.add_parser("requalify-quarantine", help="Re-run deterministic extraction on quarantined captures and promote the ones that now validate")
    req.add_argument("--dry-run", action="store_true")
    req.add_argument("--limit", type=int, default=2000)
    sub.add_parser("grid-report", help="Classify every citation-grid row: judgment / set aside (by class) / not captured")
    args = parser.parse_args(argv)
    if args.command == "reset-retired-frontier-no-reason":
        from scraper.database import SessionLocal
        from scraper.tasks.pls_self_healing import reset_retired_pls_frontier_without_reason_db

        async def go():
            async with SessionLocal() as db:
                n = await reset_retired_pls_frontier_without_reason_db(db, dry_run=args.dry_run)
                if not args.dry_run:
                    await db.commit()
                return n

        print({"would_reset" if args.dry_run else "reset": run_async(go())})
        return 0
    if args.command == "backfill-metadata":
        from scraper.tasks.pls_backfill import run_backfill_metadata

        print(run_async(run_backfill_metadata(dry_run=args.dry_run, limit=args.limit)))
        return 0
    if args.command == "requalify-quarantine":
        from scraper.tasks.pls_backfill import run_requalify

        print(run_async(run_requalify(dry_run=args.dry_run, limit=args.limit)))
        return 0
    if args.command == "grid-report":
        import json

        from sqlalchemy import select as sa_select

        from scraper.database import SessionLocal
        from scraper.models import ScraperSource
        from scraper.pls_accounting import grid_accounting

        async def report():
            async with SessionLocal() as db:
                src = (await db.execute(sa_select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
                cfg = (src.config_json or {}) if src else {}
                from scraper.pls_grid_health import grid_total_rows

                total = grid_total_rows(cfg)
                return await grid_accounting(db, total_rows=total)

        print(json.dumps(run_async(report()), indent=2))
        return 0
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
    if args.command == "search-harvest-plan-extended":
        from scraper.tasks.pls_search_harvest import cli_extended_plan

        return cli_extended_plan((["--dry-run"] if args.dry_run else []) + [x for k in args.keyword for x in ("--keyword", k)] + [x for p_ in args.party for x in ("--party", p_)])
    if args.command == "search-harvest-snowball":
        from scraper.tasks.pls_search_harvest import cli_snowball

        return cli_snowball((["--dry-run"] if args.dry_run else []) + ["--limit", str(args.limit)])
    if args.command == "search-harvest-completeness":
        from scraper.tasks.pls_search_harvest import cli_completeness

        return cli_completeness([])
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
