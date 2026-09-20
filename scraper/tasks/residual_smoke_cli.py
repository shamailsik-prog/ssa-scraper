"""Operator CLI for citation/statute residual smoke checks."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from scraper.database import run_async
from scraper.tasks.promotion import reconcile_citation_statute_residual_smoke


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run citation/statute residual smoke checks in dry-run or apply mode."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run reconcile jobs before reporting before/after residual counts.",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help="Lookback window in hours for reconcile passes.",
    )
    parser.add_argument(
        "--instrument-limit",
        type=int,
        default=None,
        help="Instrument reconcile batch limit.",
    )
    parser.add_argument(
        "--judgment-batch-size",
        type=int,
        default=None,
        help="Judgment citation reconcile batch size.",
    )
    parser.add_argument(
        "--fail-on-increase",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail non-zero when unresolved residual counts increase (default: true).",
    )
    parser.add_argument(
        "--include-unresolved-breakdown",
        action="store_true",
        help="Include compact unresolved top-bucket breakdowns in the report output.",
    )
    parser.add_argument(
        "--unresolved-breakdown-top-n",
        type=int,
        default=None,
        help="Top-N unresolved buckets to include when breakdown is enabled.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    smoke_coro = reconcile_citation_statute_residual_smoke(
        run_reconcile=args.apply,
        lookback_hours=args.lookback_hours,
        instrument_limit=args.instrument_limit,
        judgment_batch_size=args.judgment_batch_size,
        fail_on_increase=args.fail_on_increase,
        include_unresolved_breakdown=args.include_unresolved_breakdown,
        unresolved_breakdown_top_n=args.unresolved_breakdown_top_n,
    )
    try:
        report = run_async(smoke_coro)
    except Exception as exc:
        smoke_coro.close()
        print(f"residual smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
