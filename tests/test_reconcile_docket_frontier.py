"""Regression for candidate truncation before docket matching."""

from datetime import date, datetime, timedelta, timezone

from scraper.models import Judgment
from scraper.tasks.promotion import _find_unreported_version_of_reported


async def test_old_docket_survives_260_newer_unrelated_rows(db):
    body = "Court order on the application. " * 30
    target = Judgment(
        canonical_citation="UNREPORTED SC Target 1234",
        docket_number="Civil Appeal No. 1234 of 2024",
        decision_date=date(2024, 5, 1),
        unreported=True,
        full_text=body,
        promoted_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
    )
    db.add(target)
    newer = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(260):
        db.add(Judgment(
            canonical_citation=f"UNREPORTED SC Other {i}",
            docket_number=f"Civil Appeal No. {i + 2000} of 2024",
            decision_date=date(2024, 5, 1),
            unreported=True,
            full_text="Different order.",
            promoted_at=newer + timedelta(seconds=i),
        ))
    await db.flush()
    match = await _find_unreported_version_of_reported(
        db,
        docket_number="Civil Appeal No 1234 of 2024",
        decision_date=date(2024, 5, 1),
        court=None,
        full_text=body,
    )
    assert match is not None and match.id == target.id
