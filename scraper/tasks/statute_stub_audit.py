"""Dry-run audit of sectionless statute stubs (SSA_STATUTE_NO_MENTION_STUBS companion CLI).

Reports current corpus metrics against the 2026-10-18 auditor baselines. Does not delete rows.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.audit_baselines import AUDIT_2026_10_18, CORE_ACT_PATTERNS, statute_counts_as_complete
from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.models import Statute, StatuteSection, StatuteSectionVersion
from scraper.parsers.statute_parser import is_short_title_clause, looks_like_fragment_name


def _looks_like_clause_fragment(name: str) -> bool:
    lowered = (name or "").strip().lower()
    if lowered.startswith("means the ") or " further to amend " in f" {lowered} ":
        return True
    if lowered.startswith("further to amend"):
        return True
    return is_short_title_clause(name) or looks_like_fragment_name(name)


def _looks_like_garbage_name(name: str) -> bool:
    raw = name or ""
    if not raw:
        return True
    if any(sig in raw for sig in ("JFIF", "%PDF", "\x00", "\xff\xd8")):
        return True
    printable = sum(1 for ch in raw if ch.isprintable())
    return printable / max(len(raw), 1) < 0.85


async def statute_stub_report(db: AsyncSession) -> Dict[str, Any]:
    total = int((await db.execute(select(func.count()).select_from(Statute))).scalar() or 0)
    statutes = (await db.execute(select(Statute))).scalars().all()
    without_sections = 0
    with_any = 0
    complete = 0
    no_year = 0
    fragment_names = 0
    garbage_names = 0
    names_lower: Dict[str, int] = {}
    core: list[Dict[str, Any]] = []

    for st in statutes:
        names_lower[st.name.lower()] = names_lower.get(st.name.lower(), 0) + 1
        if not st.year_enacted:
            no_year += 1
        if _looks_like_clause_fragment(st.name):
            fragment_names += 1
        if _looks_like_garbage_name(st.name):
            garbage_names += 1
        section_count = int(
            (await db.execute(select(func.count()).select_from(StatuteSection).where(StatuteSection.statute_id == st.id))).scalar()
            or 0
        )
        with_text = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(StatuteSection)
                    .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
                    .where(
                        StatuteSection.statute_id == st.id,
                        func.length(func.btrim(StatuteSectionVersion.section_text)) > 0,
                    )
                )
            ).scalar()
            or 0
        )
        if section_count == 0:
            without_sections += 1
        else:
            with_any += 1
        if statute_counts_as_complete(section_count=section_count, sections_with_text=with_text):
            complete += 1

    duplicate_names = sum(1 for n, c in names_lower.items() if c > 1)

    for pattern in CORE_ACT_PATTERNS:
        hits = [s for s in statutes if pattern.lower() in (s.name or "").lower()]
        complete_hits = 0
        for st in hits:
            section_count = int(
                (await db.execute(select(func.count()).select_from(StatuteSection).where(StatuteSection.statute_id == st.id))).scalar()
                or 0
            )
            with_text = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(StatuteSection)
                        .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
                        .where(
                            StatuteSection.statute_id == st.id,
                            func.length(func.btrim(StatuteSectionVersion.section_text)) > 0,
                        )
                    )
                ).scalar()
                or 0
            )
            if statute_counts_as_complete(section_count=section_count, sections_with_text=with_text):
                complete_hits += 1
        core.append({"pattern": pattern, "rows": len(hits), "complete": complete_hits})

    baseline = AUDIT_2026_10_18
    return {
        "flag": "SSA_STATUTE_NO_MENTION_STUBS",
        "flag_on": bool(settings.SSA_STATUTE_NO_MENTION_STUBS),
        "current": {
            "statutes_total": total,
            "statutes_without_sections": without_sections,
            "statutes_with_any_sections": with_any,
            "statutes_complete": complete,
            "statutes_no_year": no_year,
            "statutes_duplicate_names": duplicate_names,
            "statutes_fragment_names": fragment_names,
            "statutes_garbage_names": garbage_names,
        },
        "auditor_baseline_2026_10_18": {
            "statutes_total": baseline["statutes_total"],
            "statutes_without_sections": baseline["statutes_without_sections"],
            "statutes_with_any_sections": baseline["statutes_with_any_sections"],
            "statutes_no_year": baseline["statutes_no_year"],
            "statutes_duplicate_names": baseline["statutes_duplicate_names"],
        },
        "core_acts": core,
        "dry_run": True,
        "note": "Completeness requires at least one section with body text. This CLI never deletes rows.",
    }


async def run_report(**_kw) -> Dict[str, Any]:
    async with SessionLocal() as db:
        return await statute_stub_report(db)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Dry-run statute stub / completeness report")
    parser.parse_args(argv)
    print(json.dumps(run_async(run_report()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
