"""Read-only inventory of pabalochistan consolidated reprints.

Lists each promoted pabalochistan statute once. The base-Act name and year
come from ``statutes_staging.raw_text`` for that statute. When several
staging rows share one ``promoted_to_id``, the chosen row is the newest
parsed heading (``created_at``, then ``id``). A row with no staging text
is chosen only when no row has a heading. Every other candidate row is
listed under ``duplicate_staging`` and is not a second proposal or a
second conflict. The report also flags any other statute of that same
year whose name differs (for example a ``Chief Minister and Ministers …
Act No. III of 1975`` stub beside the consolidated Chief Minister reprint).
The pair must share a jurisdiction. Plurals are folded before the
comparison (``salaries`` / ``salary``). A token subset counts only at five
or more content tokens. Otherwise the Jaccard overlap must be at least
0.75, or both titles must carry the same Act No. When the years agree, a
Jaccard overlap of at least 0.70 also counts if both titles share an
anchor token (speaker, deputy, chief, minister). A title that says
Amendment is not a conflict unless the reprint heading is itself an
amendment.

Promoted section text does not carry the ``Amended upto`` line, so it is
not the heading source. Metadata queries stay column-scoped.
``raw_text`` is selected by itself, and only for the candidate staging
ids. ``raw_html`` is not selected. A candidate with no staging text is
reported with reason ``no_staging_text`` instead of being dropped.

``run_report`` uses a dedicated connection, not the application pool.
That connection starts with ``default_transaction_read_only`` on, and the
report transaction is ``SET TRANSACTION READ ONLY``. The session flag is
turned off before the connection is closed, so it is not returned to a
pool still read-only. There is no ``--apply`` path.

``sikander_reader`` cannot select ``statutes_staging.raw_text`` (the table
is internal and the column is blocked). The command refuses that role,
and any role without the column privilege, before it reads staging.
Grants are not widened. Run it as the application database role.

    python -m scraper.tasks.pab_consolidated_report
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import Select

from scraper.config import settings
from scraper.database import connect_args, run_async
from scraper.models import Statute, StatutesStaging
from scraper.statute_quality.pab_consolidated import consolidated_base_heading, is_pabalochistan_source
from scraper.statute_quality.senate_integrity import _matched_token_count, _tokens, parse_act_number

# Conflict overlap is tighter than title_relation (subset, or Jaccard >= 0.55).
_JACCARD_MIN = 0.75
_ANCHOR_JACCARD_MIN = 0.70
_SUBSET_MIN_TOKENS = 5
_ANCHOR_TOKENS = frozenset({"speaker", "deputy", "chief", "minister"})

# Newest parsed heading wins. Missing text is chosen only when nothing parsed.
# Not-a-reprint rows are listed as duplicates and are never the choice.
STAGING_CHOICE = "newest_heading_then_created_at_then_id"
_HEADING_RANK = 1
_MISSING_TEXT_RANK = 0


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
    return select(
        Statute.id,
        Statute.name,
        Statute.year_enacted,
        Statute.jurisdiction,
        Statute.source_url,
    ).where(Statute.id.in_(list(statute_ids)))


def same_year_statutes_select(years: Sequence[int]) -> Select:
    return (
        select(
            Statute.id,
            Statute.name,
            Statute.year_enacted,
            Statute.jurisdiction,
            Statute.source_url,
        )
        .where(Statute.year_enacted.in_(list(years)))
        .order_by(Statute.name.asc(), Statute.id.asc())
    )


def _province(value: object) -> str:
    text = re.sub(r"[^a-z]+", " ", str(value or "").casefold()).strip()
    return re.sub(r"\s+province$", "", text)


def _amendment_title(name: str) -> bool:
    return re.search(r"(?i)\bamendment\b", name or "") is not None


def _fold_plural(token: str) -> str:
    """Light singular fold so ``salaries`` and ``salary`` are one token.

    ``ies`` becomes ``y``. A trailing ``s`` on a longer word is dropped.
    ``ss`` is left alone. This is only for the conflict comparison.
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _conflict_tokens(name: str) -> set[str]:
    return {_fold_plural(token) for token in _tokens(name)}


def _years_agree(proposed_year: object, other_year: object) -> bool:
    if proposed_year is None or other_year is None:
        return False
    return int(proposed_year) == int(other_year)


def reprint_names_conflict(
    proposed_name: str,
    proposed_jurisdiction: object,
    other_name: str,
    other_jurisdiction: object,
    *,
    proposed_year: object = None,
    other_year: object = None,
) -> bool:
    """True when the other statute is the same Act as this consolidated reprint.

    ``title_relation`` treats a token subset, or a Jaccard overlap of 0.55, as
    ``same`` and ignores province. That flags federal Speaker acts and
    one-token fragments. A conflict here needs the same jurisdiction.
    Plurals are folded first (``salaries`` / ``salary``). Then one of: a
    subset of at least five content tokens, a Jaccard overlap of at least
    0.75, the same Act No. when both titles carry one, or, when the years
    agree, a Jaccard overlap of at least 0.70 if both titles share an
    anchor token (speaker, deputy, chief, minister). A title that says
    Amendment is not the base Act, unless the reprint heading is itself an
    amendment. Differing Act numbers do not match.
    """
    proposed_province = _province(proposed_jurisdiction)
    other_province = _province(other_jurisdiction)
    if not proposed_province or proposed_province != other_province:
        return False
    if proposed_year is not None and other_year is not None and not _years_agree(proposed_year, other_year):
        return False
    if _amendment_title(other_name) and not _amendment_title(proposed_name):
        return False
    proposed_act = parse_act_number(proposed_name or "")
    other_act = parse_act_number(other_name or "")
    if proposed_act and other_act and proposed_act != other_act:
        return False
    if proposed_act and other_act and proposed_act == other_act:
        return True
    left = _conflict_tokens(proposed_name or "")
    right = _conflict_tokens(other_name or "")
    if not left or not right:
        return False
    matched = _matched_token_count(left, right)
    smaller = min(len(left), len(right))
    union = len(left) + len(right) - matched
    if union <= 0:
        return False
    jaccard = matched / union
    subset = matched == smaller and smaller >= _SUBSET_MIN_TOKENS
    shared_anchor = bool(left & right & _ANCHOR_TOKENS)
    anchor = shared_anchor and jaccard >= _ANCHOR_JACCARD_MIN and _years_agree(proposed_year, other_year)
    return subset or jaccard >= _JACCARD_MIN or anchor


def _same_year_conflicts(proposals: List[Dict[str, Any]], statutes: Sequence[Any]) -> List[Dict[str, Any]]:
    by_year: Dict[int, List[Any]] = defaultdict(list)
    for statute in statutes:
        if statute.year_enacted is not None:
            by_year[statute.year_enacted].append(statute)
    conflicts: List[Dict[str, Any]] = []
    for proposal in proposals:
        if proposal.get("proposed_year") is None or not proposal.get("proposed_name"):
            continue
        for statute in by_year.get(proposal["proposed_year"], []):
            statute_id = str(statute.id)
            if proposal["statute_id"] and statute_id == proposal["statute_id"]:
                continue
            if _names_equivalent(statute.name or "", proposal["proposed_name"]):
                continue
            if not reprint_names_conflict(
                proposal["proposed_name"],
                proposal.get("stored_jurisdiction"),
                statute.name or "",
                statute.jurisdiction,
                proposed_year=proposal.get("proposed_year"),
                other_year=statute.year_enacted,
            ):
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


class PabReportRoleError(RuntimeError):
    """The connected role must not run this report."""


def needed_report_role() -> str:
    """Application role that can SELECT statutes_staging.raw_text.

    That is the ``DATABASE_URL`` user. ``sikander_reader`` and
    ``corpus_api_reader`` are never that role: staging is internal and
    ``raw_text`` is a blocked column. This does not grant anything.
    """
    user = make_url(settings.DATABASE_URL).username or ""
    blocked = {settings.SIKANDER_READER_ROLE, settings.CORPUS_API_READER_ROLE}
    if user and user not in blocked:
        return user
    return (
        "the application database role (the DATABASE_URL user that can "
        "SELECT statutes_staging.raw_text)"
    )


def report_role_refusal(current_role: str, *, needed_role: str) -> str:
    return (
        f"pab_consolidated_report cannot run as {current_role}. "
        "statutes_staging.raw_text is not granted to sikander_reader "
        "(statutes_staging is internal and raw_text is a blocked column). "
        f"Run it as {needed_role}."
    )


async def assert_report_role(db: AsyncSession) -> None:
    """Refuse reader roles before any staging query. Read-only catalog check."""
    current = str((await db.execute(text("SELECT current_user"))).scalar() or "")
    blocked = {settings.SIKANDER_READER_ROLE, settings.CORPUS_API_READER_ROLE}
    can_read = False
    if current not in blocked:
        try:
            can_read = bool(
                (
                    await db.execute(
                        text(
                            "SELECT has_column_privilege("
                            "current_user, 'statutes_staging', 'raw_text', 'SELECT'"
                            ")"
                        )
                    )
                ).scalar()
            )
        except DBAPIError:
            can_read = False
            await db.rollback()
    if current in blocked or not can_read:
        raise PabReportRoleError(report_role_refusal(current, needed_role=needed_report_role()))


def _parse_staging_text(raw_text: object) -> Dict[str, Any]:
    if _staging_text_missing(raw_text):
        return {
            "proposed_name": None,
            "proposed_year": None,
            "pab_amended_upto": None,
            "reason": "no_staging_text",
            "rank": _MISSING_TEXT_RANK,
        }
    heading = consolidated_base_heading(raw_text if isinstance(raw_text, str) else "")
    if heading is None:
        return {
            "proposed_name": None,
            "proposed_year": None,
            "pab_amended_upto": None,
            "reason": "not_consolidated",
            "rank": -1,
        }
    return {
        "proposed_name": heading.title,
        "proposed_year": heading.year,
        "pab_amended_upto": heading.marker,
        "reason": None,
        "rank": _HEADING_RANK,
    }


def _created_at(row: Any) -> datetime:
    created = row.created_at
    if isinstance(created, datetime):
        if created.tzinfo is None:
            return created.replace(tzinfo=timezone.utc)
        return created
    return datetime.min.replace(tzinfo=timezone.utc)


def _duplicate_payload(row: Any, parsed: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "staging_id": str(row.id),
        "created_at": _created_at(row).isoformat(),
        "source_url": row.source_url,
        "proposed_name": parsed["proposed_name"],
        "proposed_year": parsed["proposed_year"],
        "pab_amended_upto": parsed["pab_amended_upto"],
        "reason": parsed["reason"],
    }


async def consolidated_reprint_report(db: AsyncSession) -> Dict[str, Any]:
    """Propose one base-Act name per promoted pabalochistan statute. Reads only."""
    await assert_report_role(db)
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

    grouped: Dict[UUID, List[Any]] = defaultdict(list)
    for row in candidates:
        grouped[row.promoted_to_id].append(row)

    proposal_order: List[tuple] = []
    for promoted_id, group in grouped.items():
        parsed_rows = [(row, _parse_staging_text(raw_by_id.get(row.id))) for row in group]
        eligible = [pair for pair in parsed_rows if pair[1]["rank"] >= 0]
        if not eligible:
            continue
        chosen_row, chosen = max(eligible, key=lambda pair: (pair[1]["rank"], _created_at(pair[0]), pair[0].id))
        duplicates = [
            _duplicate_payload(row, parsed)
            for row, parsed in sorted(
                (pair for pair in parsed_rows if pair[0].id != chosen_row.id),
                key=lambda pair: (_created_at(pair[0]), pair[0].id),
                reverse=True,
            )
        ]
        stored = statutes_by_id.get(promoted_id)
        proposal_order.append(
            (
                (_created_at(chosen_row), chosen_row.id),
                {
                    "staging_id": str(chosen_row.id),
                    "source_url": chosen_row.source_url,
                    "statute_id": str(promoted_id),
                    "stored_name": stored.name if stored is not None else None,
                    "stored_year": stored.year_enacted if stored is not None else None,
                    "stored_jurisdiction": stored.jurisdiction if stored is not None else None,
                    "proposed_name": chosen["proposed_name"],
                    "proposed_year": chosen["proposed_year"],
                    "pab_amended_upto": chosen["pab_amended_upto"],
                    "reason": chosen["reason"],
                    "staging_choice": STAGING_CHOICE,
                    "duplicate_staging_count": len(duplicates),
                    "duplicate_staging": duplicates,
                },
            )
        )
    proposal_order.sort(key=lambda item: item[0])
    proposals = [item[1] for item in proposal_order]
    years = sorted({item["proposed_year"] for item in proposals if item["proposed_year"] is not None})
    same_year: List[Any] = []
    if years:
        same_year = list((await db.execute(same_year_statutes_select(years))).all())
    conflicts = _same_year_conflicts(proposals, same_year)
    duplicate_staging_count = sum(item["duplicate_staging_count"] for item in proposals)
    return {
        "dry_run": True,
        "writes": 0,
        "consolidated_count": len(proposals),
        "conflict_count": len(conflicts),
        "duplicate_staging_count": duplicate_staging_count,
        "consolidated": proposals,
        "same_year_name_conflicts": conflicts,
        "note": (
            "Read-only. Does not retitle or merge rows. "
            "The heading is parsed from statutes_staging.raw_text for the "
            "candidate ids only. raw_html is not selected. A candidate with "
            "no staging text has proposed_name None and reason no_staging_text. "
            "Several staging rows for one promoted statute produce one proposal. "
            "The choice is the newest parsed heading (created_at, then id). "
            "A missing-text row is chosen only when no heading parsed. "
            "Other rows are listed in duplicate_staging and are not extra conflicts. "
            "A same-year conflict needs the same jurisdiction. A token subset "
            "must have at least five content tokens, or the Jaccard overlap "
            "must be at least 0.75, or both titles must carry the same Act No. "
            "salaries and salary are the same token. When the years agree, "
            "Jaccard of at least 0.70 also counts if both titles share "
            "speaker, deputy, chief, or minister. "
            "Titles that say Amendment are excluded unless the reprint heading "
            "is itself an amendment. Differing Act numbers are not conflicts. "
            "A same-year stub with a different name, such as "
            "'Chief Minister and Ministers … Act No. III of 1975' beside the "
            "consolidated Chief Minister reprint, is listed so the canonical "
            "name can be chosen before any retitle. "
            f"Run this as {needed_report_role()}, not sikander_reader."
        ),
    }


async def begin_read_only(db: AsyncSession) -> None:
    """Make this transaction read-only. Does not set a session default.

    ``SET TRANSACTION READ ONLY`` ends when the transaction ends.
    ``default_transaction_read_only`` would stay on the connection after
    commit and poison a pooled checkout. ``run_report`` sets that flag
    only on a dedicated connection and clears it before close.
    """
    await db.execute(text("SET TRANSACTION READ ONLY"))


def _report_connect_args() -> dict:
    args = connect_args()
    server = dict(args.get("server_settings") or {})
    server["default_transaction_read_only"] = "on"
    return {**args, "server_settings": server}


async def _release_report_connection(db: AsyncSession) -> None:
    """Clear read-only session state before this connection can be reused.

    Rollback first so a failed statement does not block the RESET. The
    ``SET ... off`` is committed: a later rollback would undo it and leave
    the flag on.
    """
    try:
        await db.rollback()
    except Exception:
        await db.invalidate()
        return
    try:
        await db.execute(text("SET default_transaction_read_only = off"))
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        await db.invalidate()


def open_report_engine():
    """Dedicated engine. The application ``SessionLocal`` pool is not used."""
    return create_async_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,
        connect_args=_report_connect_args(),
    )


async def run_report() -> Dict[str, Any]:
    """Read the inventory on a connection that is not the application pool."""
    report_engine = open_report_engine()
    factory = async_sessionmaker(report_engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as db:
            try:
                await begin_read_only(db)
                return await consolidated_reprint_report(db)
            finally:
                await _release_report_connection(db)
    finally:
        await report_engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "List pabalochistan consolidated reprints and the base-Act name and year "
            "their staging text proposes. One proposal per promoted statute; extra "
            "staging rows are reported as duplicates. Flags other statutes of that "
            "year whose name differs. Read-only: there is no --apply path and this "
            "command writes nothing. Refuses sikander_reader. Run it as the "
            "application database role, which can SELECT statutes_staging.raw_text."
        )
    )


def main(argv: Optional[List[str]] = None) -> int:
    build_parser().parse_args(argv)
    print(json.dumps(run_async(run_report()), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
