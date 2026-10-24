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
    reopen = sub.add_parser(
        "search-harvest-reopen-premature-done",
        help="Re-queue search harvest queries marked done after page 1 with unknown site total",
    )
    reopen.add_argument("--dry-run", action="store_true")
    run = sub.add_parser("search-harvest-run", help="Run one search harvest query (login session lock)")
    run.add_argument("--query-key", default="")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-pages", type=int, default=0)
    run.add_argument("--priority-gaps", action="store_true")
    sub.add_parser("reset-retired-frontier-no-reason", help="Reset never-run frontier rows that were retired with no last_error").add_argument("--dry-run", action="store_true")
    remap = sub.add_parser(
        "remap-search-form",
        help="Remap PakistanLawSite search form from /Login/Check Citation Search panel (ACTIVE slot)",
    )
    remap.add_argument("--dry-run", action="store_true")
    meta = sub.add_parser("backfill-metadata", help="Fill docket/petitioner/respondent/headnotes from the preserved text (only where empty)")
    meta.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    meta.add_argument("--limit", type=int, default=None)
    titles = sub.add_parser(
        "backfill-order-sheet-titles",
        help="Rewrite PakistanLawSite case_title from order-sheet header parties (dry-run by default)",
    )
    titles.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    titles.add_argument("--limit", type=int, default=None)
    pld = sub.add_parser("backfill-pld-citations", help="Relabel primary PLD citations missing a court token (dry-run by default)")
    pld.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    pld.add_argument("--limit", type=int, default=None)
    court_bf = sub.add_parser(
        "backfill-judgment-courts",
        help="Fix PLS court_name from header (foreign SC/HC mislabel, SC vs HC swap; dry-run by default)",
    )
    court_bf.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    court_bf.add_argument("--undo", metavar="RUN_ID", help="Restore court_name values from a prior --apply run")
    court_bf.add_argument("--limit", type=int, default=None)
    court_bf.add_argument(
        "--consistency-report",
        action="store_true",
        help="Report court_id vs court_name mismatches (read-only)",
    )
    court_link = sub.add_parser(
        "link-judgment-court-ids",
        help="Set court_id on PLS rows where court_id is NULL (dry-run by default)",
    )
    court_link.add_argument("--apply", action="store_true", help="Write court_id links (requires SSA_JUDGMENT_COURT_ID_BACKFILL)")
    court_link.add_argument("--undo", metavar="RUN_ID", help="Restore court_id from a prior --apply run")
    court_link.add_argument("--limit", type=int, default=None)
    jcc = sub.add_parser(
        "judgment-court-consistency-report",
        help="Report PLS judgments whose court_id row disagrees with court_name",
    )
    jcc.add_argument("--limit", type=int, default=None)
    req = sub.add_parser("requalify-quarantine", help="Re-run deterministic extraction on quarantined captures and promote the ones that now validate")
    req.add_argument("--apply", action="store_true", help="Promote/write changes (default is dry-run)")
    req.add_argument("--limit", type=int, default=2000)
    strip = sub.add_parser(
        "strip-urdu-headnote-mojibake",
        help="Strip flagged Urdu headnote ???? lines from preserved PLS text (dry-run by default)",
    )
    strip.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    strip.add_argument("--limit", type=int, default=None)
    flag = sub.add_parser(
        "backfill-flag-urdu-headnote-mojibake",
        help="Set quality_flag_urdu_headnote_mojibake on PLS rows with headnote-only ???? (dry-run by default)",
    )
    flag.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    flag.add_argument("--limit", type=int, default=None)
    aliases = sub.add_parser(
        "cleanup-false-citation-aliases",
        help="Dry-run scan of non-primary aliases from body cites; --apply removes them (with backup)",
    )
    aliases.add_argument("--apply", action="store_true", help="Delete suspicious aliases and requalify blocked staging (default dry-run)")
    aliases.add_argument("--undo", metavar="RUN_ID", help="Restore citations deleted by a prior --apply run_id")
    aliases.add_argument("--csv", dest="csv_path", default=None, help="CSV output path")
    sub.add_parser("grid-report", help="Classify every citation-grid row: judgment / set aside (by class) / not captured")
    hn = sub.add_parser(
        "scan-headnote-only-promoted",
        help="Report promoted PLS judgments with counsel/hearing metadata but no judgment body (dry-run by default)",
    )
    hn.add_argument("--apply", action="store_true", help="Insert document_quarantine rows for matches")
    hn.add_argument("--force", action="store_true", help="Allow --apply when SSA_PLS_HEADNOTE_BODY_GUARD is off")
    hn.add_argument("--undo", metavar="RUN_ID", default="", help="Remove quarantine rows from a prior --apply run")
    hn.add_argument("--limit", type=int, default=None)
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
    if args.command == "remap-search-form":
        from scraper.tasks.pls_remap_search_form import run_remap_search_form
        print(run_async(run_remap_search_form(dry_run=args.dry_run)))
        return 0
    if args.command == "backfill-metadata":
        from scraper.tasks.pls_backfill import run_backfill_metadata

        print(run_async(run_backfill_metadata(dry_run=not args.apply, limit=args.limit)))
        return 0
    if args.command == "backfill-order-sheet-titles":
        from scraper.tasks.pls_backfill import run_backfill_order_sheet_titles

        print(run_async(run_backfill_order_sheet_titles(dry_run=not args.apply, limit=args.limit)))
        return 0
    if args.command == "backfill-pld-citations":
        from scraper.tasks.pls_backfill import run_backfill_pld_citations

        print(run_async(run_backfill_pld_citations(dry_run=not args.apply, limit=args.limit)))
        return 0
    if args.command == "backfill-judgment-courts":
        from scraper.tasks.judgment_court_backfill import run_judgment_court_backfill

        print(
            run_async(
                run_judgment_court_backfill(
                    apply=args.apply,
                    undo=args.undo,
                    limit=args.limit,
                    consistency_report_only=args.consistency_report,
                )
            )
        )
        return 0
    if args.command == "link-judgment-court-ids":
        from scraper.tasks.judgment_court_link import run_judgment_court_link

        print(
            run_async(
                run_judgment_court_link(
                    apply=args.apply,
                    undo=args.undo,
                    limit=args.limit,
                )
            )
        )
        return 0
    if args.command == "judgment-court-consistency-report":
        from scraper.database import SessionLocal
        from scraper.judgment_court_consistency import consistency_report

        async def _report():
            async with SessionLocal() as db:
                return await consistency_report(db, limit=args.limit)

        print(run_async(_report()))
        return 0
    if args.command == "requalify-quarantine":
        from scraper.tasks.pls_backfill import run_requalify

        print(run_async(run_requalify(dry_run=not args.apply, limit=args.limit)))
        return 0
    if args.command == "strip-urdu-headnote-mojibake":
        from scraper.tasks.pls_backfill import run_strip_urdu_headnote_mojibake

        print(run_async(run_strip_urdu_headnote_mojibake(dry_run=not args.apply, limit=args.limit)))
        return 0
    if args.command == "cleanup-false-citation-aliases":
        from pathlib import Path

        from scraper.tasks.citation_alias_cleanup import run_citation_alias_cleanup

        out = run_async(
            run_citation_alias_cleanup(
                apply=args.apply,
                undo=args.undo,
                csv_path=Path(args.csv_path) if args.csv_path else None,
            )
        )
        if out.get("summary"):
            print(out["summary"])
        import json

        print(json.dumps(out, indent=2, default=str))
        return 0
    if args.command == "backfill-flag-urdu-headnote-mojibake":
        from scraper.tasks.pls_backfill import run_backfill_flag_urdu_headnote_mojibake

        print(run_async(run_backfill_flag_urdu_headnote_mojibake(dry_run=not args.apply, limit=args.limit)))
        return 0
    if args.command == "scan-headnote-only-promoted":
        import json

        from scraper.tasks.judgment_headnote_body_scan import run_headnote_body_scan

        undo = (args.undo or "").strip() or None
        print(
            json.dumps(
                run_async(run_headnote_body_scan(apply=args.apply, undo=undo, limit=args.limit, force=args.force)),
                indent=2,
                default=str,
            )
        )
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
    if args.command == "search-harvest-reopen-premature-done":
        from scraper.tasks.pls_search_harvest import cli_reopen_premature_done

        return cli_reopen_premature_done(["--dry-run"] if args.dry_run else [])
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
