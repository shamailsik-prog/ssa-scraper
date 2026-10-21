"""PakistanLawSite case-metadata backfill."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scraper.models import Judgment
from scraper.tasks.pls_backfill import backfill_case_metadata


@pytest.mark.asyncio
async def test_backfill_sets_docket_key_when_filling_docket_number(db):
    text = """1990 C L C 1205
[Peshawar]
Before Muhammad Bashir Khan Jehangiri, J
Mst. KAUSAR BIBI---Petitioner
versus
MUHAMMAD MUSHTAQ and 6 others---Respondents
Civil Revision No.329 of 1989, decided on 22nd November, 1989.
(a) Civil Procedure Code (V of 1908)---S. 12(2)---Fraud.

JUDGMENT
The facts are these.
"""
    j = Judgment(
        canonical_citation="PLS backfill docket",
        full_text=text,
        full_text_hash="a" * 64,
        source_name="PakistanLawSite",
        docket_number=None,
        docket_key=None,
    )
    db.add(j)
    await db.commit()
    counts = await backfill_case_metadata(db, batch=10, limit=1, dry_run=False)
    await db.commit()
    assert counts["docket_number"] == 1
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert refreshed.docket_number == "Civil Revision No.329 of 1989"
    assert refreshed.docket_key == "civilrevisionno.329of1989"
