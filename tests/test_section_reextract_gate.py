"""Re-extract PASS gate: a collapsed parse must not be applied or un-quarantined.

The Balochistan Animal Slaughter Control Act, 2025 re-extract kept the
distribution-list checks (no addressee row) and still went visible as one
section: number "2", the whole act, section 1 gone, 9 stored rows to 1.
"""

from __future__ import annotations

from scraper.statute_quality.section_reextract import (
    REEXTRACT_FOOTER_STRIP_MAX_DROP,
    REEXTRACT_MEGA_SECTION_CHARS,
    gate_section_reextract,
)


def _section(number: str, text: str) -> dict:
    return {"section_number": number, "section_text": text}


def _real(number: int, text: str = "The Government shall enforce this provision.") -> dict:
    return _section(str(number), f"{number}. {text}")


def _nine_real() -> list[dict]:
    return [_real(n) for n in range(1, 10)]


def test_nine_to_one_mega_section_fails():
    """9 stored rows to one numbered row holding the whole act."""
    body = "2. Extent through repeal and savings. " + ("The remaining provisions of this Act. " * 400)
    assert len(body) >= REEXTRACT_MEGA_SECTION_CHARS
    assert len(body) > 12000
    decision = gate_section_reextract(_nine_real(), [_section("2", body)])
    assert decision.verdict == "FAIL"
    assert decision.reason == "reextract_collapse_count"
    assert decision.section_count_before == 9
    assert decision.section_count_after == 1
    assert decision.may_apply is False
    assert decision.may_unquarantine is False


def test_footer_strip_that_keeps_count_roughly_stable_passes():
    """Dropping two addressee rows (9→7) is a footer strip, not a collapse."""
    before = _nine_real()
    before[4] = _section("5", "5. The Director General, Public Relations, Balochistan, Quetta.")
    before[5] = _section("6", "6. The System Analyst, Balochistan Provincial Assembly.")
    after = [row for row in before if row["section_number"] not in {"5", "6"}]
    decision = gate_section_reextract(before, after)
    assert decision.verdict == "PASS"
    assert decision.reason == ""
    assert decision.section_count_before == 9
    assert decision.section_count_after == 7
    assert decision.may_apply is True
    assert decision.may_unquarantine is True
    assert 9 - 7 <= REEXTRACT_FOOTER_STRIP_MAX_DROP


def test_single_section_over_threshold_fails_when_count_is_stable():
    before = [_real(1), _real(2)]
    mega = "2. " + ("x" * REEXTRACT_MEGA_SECTION_CHARS)
    after = [_real(1), _section("2", mega)]
    decision = gate_section_reextract(before, after)
    assert decision.verdict == "FAIL"
    assert decision.reason == "reextract_mega_section"
    assert decision.may_apply is False


def test_section_at_exact_threshold_fails_and_one_below_passes():
    before = [_real(1)]
    at_limit = "1. " + ("y" * (REEXTRACT_MEGA_SECTION_CHARS - 3))
    assert len(at_limit) == REEXTRACT_MEGA_SECTION_CHARS
    decision = gate_section_reextract(before, [_section("1", at_limit)])
    assert decision.verdict == "FAIL"
    assert decision.reason == "reextract_mega_section"

    under = at_limit[:-1]
    assert len(under) == REEXTRACT_MEGA_SECTION_CHARS - 1
    ok = gate_section_reextract(before, [_section("1", under)])
    assert ok.verdict == "PASS"


def test_whitespace_around_a_mega_section_still_fails():
    before = [_real(1), _real(2)]
    padded = "\n" + ("z" * REEXTRACT_MEGA_SECTION_CHARS) + " \n"
    decision = gate_section_reextract(before, [_real(1), _section("2", padded)])
    assert decision.reason == "reextract_mega_section"


def test_half_kept_passes_and_just_under_half_fails():
    """Strictly below half, and more than a few junk rows, is a collapse.

    9→5 keeps half (integer half rounds down via after*2 < before). 9→4 does not.
    """
    kept = gate_section_reextract(_nine_real(), [_real(n) for n in range(1, 6)])
    assert kept.verdict == "PASS"
    assert kept.section_count_after == 5

    dropped = gate_section_reextract(_nine_real(), [_real(n) for n in range(1, 5)])
    assert dropped.verdict == "FAIL"
    assert dropped.reason == "reextract_collapse_count"
    assert dropped.section_count_after == 4


def test_drop_of_at_most_footer_allowance_does_not_fail_on_count():
    """A 4→1 drop is three rows, the footer allowance, so count alone does not fail."""
    before = [_real(n) for n in range(1, 5)]
    decision = gate_section_reextract(before, [_real(1, "Short title stays.")])
    assert decision.verdict == "PASS"
    assert decision.section_count_before - decision.section_count_after == REEXTRACT_FOOTER_STRIP_MAX_DROP


def test_real_section_one_disappearing_fails_even_when_count_stays_near():
    before = [_real(n) for n in range(1, 5)]
    after = [_real(n) for n in range(2, 5)]
    decision = gate_section_reextract(before, after)
    assert decision.verdict == "FAIL"
    assert decision.reason == "reextract_missing_section_1"
    assert decision.may_unquarantine is False


def test_footer_only_section_one_may_disappear():
    before = [
        _section("1", "1. The Principal Secretary to Governor Balochistan, Quetta."),
        *[_real(n) for n in range(2, 10)],
    ]
    after = [_real(n) for n in range(2, 10)]
    decision = gate_section_reextract(before, after)
    assert decision.verdict == "PASS"
    assert decision.section_count_before == 9
    assert decision.section_count_after == 8


def test_section_one_label_variants_count_as_real():
    before = [_section("Section 1", "1. This Act may be called the Example Act, 2025.")]
    after = [_section("s. 1", "1. This Act may be called the Example Act, 2025.")]
    assert gate_section_reextract(before, after).verdict == "PASS"

    renamed = gate_section_reextract(before, [_real(2)])
    assert renamed.reason == "reextract_missing_section_1"


def test_blank_rows_do_not_count_and_first_ingest_has_no_stored_body():
    before = _nine_real() + [_section("10", "   "), "not-a-section"]
    after = _nine_real()
    decision = gate_section_reextract(before, after)
    assert decision.verdict == "PASS"
    assert decision.section_count_before == 9
    assert decision.section_count_after == 9

    first = gate_section_reextract([], [_section("2", "2. " + ("q" * REEXTRACT_MEGA_SECTION_CHARS))])
    assert first.verdict == "PASS"
    assert first.section_count_before == 0
    assert first.may_apply is True


def test_more_sections_than_stored_passes():
    decision = gate_section_reextract([_real(1)], [_real(1), _real(2), _real(3)])
    assert decision.verdict == "PASS"


async def test_promote_does_not_apply_or_unquarantine_a_collapsed_reextract(db):
    """Stored 9-section act must stay quarantined when the candidate is one mega row."""
    from sqlalchemy import func, select

    from scraper.models import DocumentQuarantine, QuarantineQueue, Statute, StatuteSection, StatuteSectionVersion
    from scraper.tasks.promotion import promote_statute_staging
    from tests.test_b3_statute_promotion import _make_statute_staging
    from tests.test_year_section_sweep import _add_section

    name = "THE BALOCHISTAN ANIMAL SLAUGHTER CONTROL ACT, 2025"
    statute = Statute(
        name=name,
        jurisdiction="Balochistan",
        year_enacted=2025,
        needs_section_refetch=True,
        source_name="BalochistanAssembly",
    )
    db.add(statute)
    await db.flush()
    originals = {}
    for number in range(1, 10):
        text = f"{number}. The Government shall enforce provision {number} of this Act."
        await _add_section(db, statute.id, str(number), text, number)
        originals[str(number)] = text
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=statute.id,
            reason="distribution_list_footer_as_sections",
            run_id="reextract-gate-test",
        )
    )
    await db.flush()
    mega = "2. Extent through repeal and savings. " + ("The remaining provisions of this Act. " * 400)
    staging = await _make_statute_staging(
        db,
        {
            "statute_name": name,
            "year_enacted": 2025,
            "jurisdiction": "Balochistan",
            "sections": [_section("2", mega)],
        },
    )
    assert await promote_statute_staging(db, staging) == "quarantined"
    assert staging.quarantine_reason == "section_reextract:reextract_collapse_count"
    details = (
        await db.execute(select(QuarantineQueue).where(QuarantineQueue.statutes_staging_id == staging.id))
    ).scalars().one()
    assert (details.details or {}).get("reason_code") == "reextract_collapse_count"
    assert (details.details or {}).get("section_count_before") == 9
    assert (details.details or {}).get("section_count_after") == 1
    await db.refresh(statute)
    assert statute.needs_section_refetch is True
    still_hidden = (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id == statute.id,
            )
        )
    ).scalars().one()
    assert still_hidden.reason == "distribution_list_footer_as_sections"
    stored = (
        await db.execute(
            select(StatuteSection.section_number, StatuteSectionVersion.section_text)
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == statute.id)
        )
    ).all()
    assert {num: text for num, text in stored} == originals
    assert (
        await db.execute(select(func.count()).select_from(StatuteSection).where(StatuteSection.statute_id == statute.id))
    ).scalar() == 9


async def test_promote_applies_a_stable_footer_strip_and_leaves_quarantine(db):
    """A 9→7 footer strip may be written. This path does not delete document_quarantine."""
    from sqlalchemy import select

    from scraper.models import DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
    from scraper.tasks.promotion import promote_statute_staging
    from tests.test_b3_statute_promotion import _make_statute_staging
    from tests.test_year_section_sweep import _add_section

    name = "The Example Footer Strip Act, 2024"
    statute = Statute(
        name=name,
        jurisdiction="Balochistan",
        year_enacted=2024,
        needs_section_refetch=True,
        source_name="BalochistanAssembly",
    )
    db.add(statute)
    await db.flush()
    for number in range(1, 10):
        if number == 5:
            text = "5. The Director General, Public Relations, Balochistan, Quetta."
        elif number == 6:
            text = "6. The System Analyst, Balochistan Provincial Assembly."
        else:
            text = f"{number}. The Government shall enforce provision {number} of this Act."
        await _add_section(db, statute.id, str(number), text, number)
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=statute.id,
            reason="distribution_list_footer_as_sections",
            run_id="reextract-gate-footer",
        )
    )
    await db.flush()
    incoming = []
    for number in range(1, 10):
        if number in {5, 6}:
            continue
        incoming.append(_real(number, f"The Government shall enforce provision {number} of this Act, as re-read."))
    staging = await _make_statute_staging(
        db,
        {
            "statute_name": name,
            "year_enacted": 2024,
            "jurisdiction": "Balochistan",
            "sections": incoming,
        },
    )
    assert await promote_statute_staging(db, staging) in ("promoted", "duplicate")
    await db.refresh(statute)
    assert statute.needs_section_refetch is False
    still_hidden = (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id == statute.id,
            )
        )
    ).scalars().one()
    assert still_hidden.reason == "distribution_list_footer_as_sections"
    current = (
        await db.execute(
            select(StatuteSectionVersion.section_text)
            .join(StatuteSection, StatuteSection.current_version_id == StatuteSectionVersion.id)
            .where(StatuteSection.statute_id == statute.id, StatuteSection.section_number == "1")
        )
    ).scalar_one()
    assert "as re-read" in current
