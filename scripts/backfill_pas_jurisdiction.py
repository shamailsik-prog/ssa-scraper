#!/usr/bin/env python3
"""Set pas.gov.pk statutes whose jurisdiction is not Sindh back to Sindh.

Dry-run by default. The live promotion path stored a missing SindhAssembly
jurisdiction as Federal. Older pas.gov.pk rows are already Sindh. The rows
this selects are the ones that are not.

On FS that set is two visible SindhAssembly acts from 2026:

  966d98fc-9a20-4e0a-aeb1-6e01a1f792a7  Iqra University (Amendment) Act 2026
  aa951b26-60c1-49de-a63f-d13a35523889  FIU at Karachi Act 2026

On MAIN the same shape is empty (every pas.gov.pk row is already Sindh).

The script lists matching rows, writes a backup JSONL, and writes rollback
SQL. It updates the database only with --apply, and only when --expect-count
and, when that count is not zero, --expect-ids match the rows just selected.
A second read under a row lock repeats that check before any UPDATE.

Do not point it at a database until a dry-run on that database has been read.
This file does not embed credentials and does not change TLS verification.

Dry-run:

  python scripts/backfill_pas_jurisdiction.py --backup pas_jurisdiction_backup.jsonl

FS apply (expect those two ids):

  python scripts/backfill_pas_jurisdiction.py --apply --expect-count 2 \\
    --expect-ids 966d98fc-9a20-4e0a-aeb1-6e01a1f792a7,aa951b26-60c1-49de-a63f-d13a35523889 \\
    --backup pas_jurisdiction_backup.jsonl

MAIN apply (expect no rows):

  python scripts/backfill_pas_jurisdiction.py --apply --expect-count 0 \\
    --backup pas_jurisdiction_backup.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal, run_async
from scraper.models import Statute
from scraper.statute_quality_jurisdiction import is_pas_source_url

SINDH_JURISDICTION = "Sindh"

# Operator precheck for the FS finding. Not a filter: selection is every
# pas.gov.pk / www.pas.gov.pk statute whose jurisdiction is not Sindh.
FS_PAS_FEDERAL_IDS = (
    "966d98fc-9a20-4e0a-aeb1-6e01a1f792a7",
    "aa951b26-60c1-49de-a63f-d13a35523889",
)

_OPS_EPILOG = f"""
Dry-run (default). Lists pas.gov.pk and www.pas.gov.pk statutes whose
jurisdiction is not Sindh, writes a backup JSONL and rollback SQL, and does
not update.

  python scripts/backfill_pas_jurisdiction.py --backup pas_jurisdiction_backup.jsonl

FS precheck is 2 rows, the 2026 SindhAssembly acts still stored as Federal:

  python scripts/backfill_pas_jurisdiction.py --apply --expect-count 2 \\
    --expect-ids {FS_PAS_FEDERAL_IDS[0]},{FS_PAS_FEDERAL_IDS[1]} \\
    --backup pas_jurisdiction_backup.jsonl

MAIN precheck is 0 rows (those acts are not on MAIN, and the pas.gov.pk rows
that are there are already Sindh):

  python scripts/backfill_pas_jurisdiction.py --apply --expect-count 0 \\
    --backup pas_jurisdiction_backup.jsonl
"""


class PrecheckError(RuntimeError):
    """Selected rows do not match the operator's count and id precheck."""


def parse_expect_ids(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def require_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise PrecheckError(f"statute id is not a UUID: {value!r}") from exc
    return str(parsed)


def backup_record(statute: Statute) -> dict[str, Any]:
    updated = statute.updated_at
    return {
        "id": str(statute.id),
        "name": statute.name,
        "jurisdiction": statute.jurisdiction,
        "source_name": statute.source_name,
        "source_url": statute.source_url,
        "updated_at": updated.isoformat() if updated is not None else None,
    }


def render_backup_jsonl(records: Sequence[dict[str, Any]]) -> str:
    lines = [json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def render_rollback_sql(records: Sequence[dict[str, Any]]) -> str:
    """SQL that restores jurisdiction and updated_at. A second run matches nothing."""
    lines = [
        "-- Rollback for pas.gov.pk jurisdiction backfill.",
        "-- Restores statute.jurisdiction and statute.updated_at from the backup.",
        "-- The jurisdiction predicate keeps a second run from changing a row again.",
        "BEGIN;",
    ]
    if not records:
        lines.append("-- no rows")
    for record in records:
        statute_id = require_uuid(str(record["id"]))
        jurisdiction = sql_literal(str(record["jurisdiction"]))
        updated = record.get("updated_at")
        assignments = f"jurisdiction = {jurisdiction}"
        if updated:
            assignments += f",\n    updated_at = TIMESTAMPTZ {sql_literal(str(updated))}"
        lines.append(
            "UPDATE statute\n"
            f"SET {assignments}\n"
            f"WHERE id = {sql_literal(statute_id)}\n"
            f"  AND jurisdiction = {sql_literal(SINDH_JURISDICTION)};"
        )
    lines.append("COMMIT;")
    lines.append("")
    return "\n".join(lines)


def evaluate_precheck(
    selected_ids: Sequence[str],
    *,
    expect_count: int,
    expect_ids: Sequence[str],
) -> None:
    selected = [require_uuid(statute_id) for statute_id in selected_ids]
    if expect_count < 0:
        raise PrecheckError("--expect-count must be zero or greater")
    if len(selected) != expect_count:
        raise PrecheckError(
            "precheck failed: "
            f"selected {len(selected)} row(s) {selected} "
            f"but --expect-count {expect_count}"
        )
    if expect_count == 0:
        if list(expect_ids):
            raise PrecheckError("--expect-count 0 does not take --expect-ids")
        return
    expected = [require_uuid(statute_id) for statute_id in expect_ids]
    if len(expected) != len(set(expected)):
        raise PrecheckError("--expect-ids contains a duplicate")
    if set(expected) != set(selected) or len(expected) != len(selected):
        raise PrecheckError(
            "precheck failed: "
            f"--expect-ids {expected} does not match selected ids {selected}"
        )


def is_pas_jurisdiction_target(source_url: Optional[str], jurisdiction: Optional[str]) -> bool:
    if not is_pas_source_url(source_url):
        return False
    return (jurisdiction or "").strip() != SINDH_JURISDICTION


async def select_targets(db: AsyncSession, *, lock: bool = False) -> list[Statute]:
    stmt = select(Statute).where(Statute.source_url.ilike("%pas.gov.pk%")).order_by(Statute.id)
    if lock:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt)).scalars().all()
    return [row for row in rows if is_pas_jurisdiction_target(row.source_url, row.jurisdiction)]


def _ids(rows: Sequence[Statute]) -> list[str]:
    return [str(row.id) for row in rows]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _summary(records: Sequence[dict[str, Any]], *, apply: bool, backup: Path, rollback_sql: Path) -> dict[str, Any]:
    return {
        "dry_run": not apply,
        "count": len(records),
        "ids": [record["id"] for record in records],
        "rows": list(records),
        "backup": str(backup),
        "rollback_sql": str(rollback_sql),
        "updated": len(records) if apply else 0,
        "target_jurisdiction": SINDH_JURISDICTION,
    }


async def run_backfill(
    db: AsyncSession,
    *,
    apply: bool,
    backup: Path,
    rollback_sql: Path,
    expect_count: Optional[int] = None,
    expect_ids: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """List or update pas.gov.pk rows whose jurisdiction is not Sindh.

    ``apply`` writes Sindh only after the precheck matches a second locked read.
    Dry-run writes the backup and rollback SQL and leaves the rows unchanged.
    """
    if apply and expect_count is None:
        raise PrecheckError("--apply requires --expect-count")
    selected = await select_targets(db)
    if expect_count is not None:
        evaluate_precheck(_ids(selected), expect_count=expect_count, expect_ids=list(expect_ids or []))
    if not apply:
        records = [backup_record(row) for row in selected]
        _write(backup, render_backup_jsonl(records))
        _write(rollback_sql, render_rollback_sql(records))
        return _summary(records, apply=False, backup=backup, rollback_sql=rollback_sql)

    locked = await select_targets(db, lock=True)
    if _ids(locked) != _ids(selected):
        raise PrecheckError(
            "precheck failed: the pas.gov.pk set changed before apply "
            f"(first {_ids(selected)}, locked {_ids(locked)})"
        )
    evaluate_precheck(_ids(locked), expect_count=expect_count or 0, expect_ids=list(expect_ids or []))
    records = [backup_record(row) for row in locked]
    _write(backup, render_backup_jsonl(records))
    _write(rollback_sql, render_rollback_sql(records))
    for row in locked:
        row.jurisdiction = SINDH_JURISDICTION
    summary = _summary(records, apply=True, backup=backup, rollback_sql=rollback_sql)
    await db.commit()
    return summary


def _rollback_path(backup: Path, explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    return backup.with_name(backup.name + ".rollback.sql")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill pas.gov.pk statute jurisdiction to Sindh. "
            "Dry-run unless --apply is passed with a matching row-count and id precheck."
        ),
        epilog=_OPS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Update matched rows to Sindh. Refused unless --expect-count matches the selected rows.",
    )
    parser.add_argument(
        "--expect-count",
        type=int,
        default=None,
        help="Required with --apply. FS is 2. MAIN is 0.",
    )
    parser.add_argument(
        "--expect-ids",
        default="",
        help="Comma-separated statute ids. Required when --expect-count is greater than 0.",
    )
    parser.add_argument("--backup", required=True, help="Backup JSONL path. Written on dry-run and before apply.")
    parser.add_argument(
        "--rollback-sql",
        default=None,
        help="Rollback SQL path. Defaults to the backup path plus .rollback.sql.",
    )
    args = parser.parse_args(argv)
    if args.apply and args.expect_count is None:
        parser.error("--apply requires --expect-count")
    if args.expect_count is not None and args.expect_count < 0:
        parser.error("--expect-count must be zero or greater")
    backup = Path(args.backup)
    rollback_sql = _rollback_path(backup, args.rollback_sql)

    async def _run() -> dict[str, Any]:
        async with SessionLocal() as db:
            try:
                return await run_backfill(
                    db,
                    apply=args.apply,
                    backup=backup,
                    rollback_sql=rollback_sql,
                    expect_count=args.expect_count,
                    expect_ids=parse_expect_ids(args.expect_ids),
                )
            except PrecheckError:
                await db.rollback()
                raise

    try:
        result = run_async(_run())
    except PrecheckError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
