"""Load, restore, and clear the section rows a re-extract gate has to see.

Do not delete ``statute_section`` rows and then promote. Read the live rows,
pass them as ``prior_sections`` to ``promote_statute_staging``, and delete
only after ``may_apply`` is true. A delete that already ran is still visible:
migration 028 stores the pre-delete text, and promotion gates on it.
``may_unquarantine`` stays on the gate decision. This module does not delete
``document_quarantine``.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.fetchers import sha256_text
from scraper.models import StatuteSection, StatuteSectionReextractPrior, StatuteSectionVersion
from scraper.statute_quality.section_reextract import _parseable_rows, reextract_before_sections


async def load_live_section_rows(db: AsyncSession, statute_id: UUID) -> list[dict[str, str]]:
    rows = (
        await db.execute(
            select(StatuteSection.section_number, StatuteSectionVersion.section_text)
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == statute_id)
        )
    ).all()
    return [{"section_number": num, "section_text": txt or ""} for num, txt in rows]


async def load_snapshot_section_rows(db: AsyncSession, statute_id: UUID) -> list[dict[str, str]]:
    rows = (
        await db.execute(
            select(
                StatuteSectionReextractPrior.section_number,
                StatuteSectionReextractPrior.section_text,
            ).where(StatuteSectionReextractPrior.statute_id == statute_id)
        )
    ).all()
    return [{"section_number": num, "section_text": txt or ""} for num, txt in rows]


async def before_rows_for_promote(
    db: AsyncSession,
    statute_id: UUID,
    prior_sections: Optional[Sequence[Any]],
) -> tuple[list[dict[str, str]], list[Any]]:
    """Live rows, and the rows the gate must use (prior, live, or snapshot)."""
    live = await load_live_section_rows(db, statute_id)
    snapshot: list[dict[str, str]] = []
    if not _parseable_rows(live):
        snapshot = await load_snapshot_section_rows(db, statute_id)
    return live, reextract_before_sections(live, prior_sections, snapshot)


async def clear_reextract_snapshot(db: AsyncSession, statute_id: UUID) -> None:
    await db.execute(
        delete(StatuteSectionReextractPrior).where(StatuteSectionReextractPrior.statute_id == statute_id)
    )


async def restore_section_rows(db: AsyncSession, statute_id: UUID, rows: Sequence[Any]) -> None:
    """Put captured section text back when a re-extract must not be applied."""
    order = 0
    for number, text in _parseable_rows(rows):
        order += 1
        sec = (
            await db.execute(
                select(StatuteSection).where(
                    StatuteSection.statute_id == statute_id,
                    StatuteSection.section_number == number,
                )
            )
        ).scalars().first()
        if sec is None:
            sec = StatuteSection(statute_id=statute_id, section_number=number, sort_key=order)
            db.add(sec)
            await db.flush()
        text_hash = sha256_text(" ".join(text.split()))
        ver = (
            await db.execute(
                select(StatuteSectionVersion).where(
                    StatuteSectionVersion.section_id == sec.id,
                    StatuteSectionVersion.text_hash == text_hash,
                )
            )
        ).scalars().first()
        if ver is None:
            count = (
                await db.execute(
                    select(func.count()).select_from(StatuteSectionVersion).where(
                        StatuteSectionVersion.section_id == sec.id
                    )
                )
            ).scalar() or 0
            ver = StatuteSectionVersion(
                section_id=sec.id,
                version_no=int(count) + 1,
                section_text=text,
                text_hash=text_hash,
                version_confidence=0.5,
            )
            db.add(ver)
            await db.flush()
        sec.current_version_id = ver.id
    await db.flush()
