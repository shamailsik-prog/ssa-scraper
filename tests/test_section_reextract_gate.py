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


def _definition_catalogue(extra: str = "") -> str:
    """Section 2 as a run of definition clauses, padded past the mega-section cap."""
    clauses = "\n".join(
        f'({number}) "term{number}" means the meaning this Act assigns to term {number};'
        for number in range(1, 24)
    )
    # Clause (2) printed with the opening parenthesis read as a digit.
    clauses = clauses.replace(
        '(2) "term2" means the meaning this Act assigns to term 2;',
        '12) "term2" means the meaning this Act assigns to term 2;',
        1,
    )
    body = (
        "2. Definitions.—In this Act, unless there is anything repugnant in the subject "
        "or context,—\n"
        + clauses
        + "\n"
        + extra
    )
    if len(body) < REEXTRACT_MEGA_SECTION_CHARS:
        body += "\n" + ("The same meaning applies for the rest of this definition. " * 80)
    return body


def test_definition_catalogue_over_the_cap_is_not_a_mega_section():
    """A real definitions section can pass 5000 characters without swallowing the act."""
    catalogue = _definition_catalogue()
    assert len(catalogue) >= REEXTRACT_MEGA_SECTION_CHARS
    after = [
        _real(1, "This Act may be called the Example Urban Planning Act, 1975."),
        _section("2", catalogue),
        _real(3, "The Government shall establish the Board under this Act."),
        _real(12, "The Board may regulate development in a provincial control area."),
    ]
    decision = gate_section_reextract([_real(1), _real(2), _real(3)], after)
    assert decision.verdict == "PASS"
    assert decision.reason == ""
    assert decision.may_apply is True


def test_definition_clauses_stored_as_section_12_still_fail_the_mega_gate():
    """The same catalogue under number 12 is the OCR split, not section 2."""
    catalogue = _definition_catalogue().replace("2. Definitions", "12) ", 1)
    assert len(catalogue) >= REEXTRACT_MEGA_SECTION_CHARS
    after = [
        _real(1),
        _section("2", "2. Definitions.—In this Act, unless the context otherwise requires,—"),
        _section("12", catalogue),
        _real(3),
    ]
    decision = gate_section_reextract([_real(1), _real(2), _real(3), _real(12)], after)
    assert decision.verdict == "FAIL"
    assert decision.reason == "reextract_mega_section"


def test_definitions_that_swallowed_a_later_section_still_fail():
    """Extent-through-repeal glued onto section 2 is still a mega section."""
    swallowed = _definition_catalogue(
        "\n3. Establishment.—The Government shall establish the Board.\n"
        "51. Repeal.—The earlier Ordinance is hereby repealed.\n"
    )
    assert len(swallowed) >= REEXTRACT_MEGA_SECTION_CHARS
    decision = gate_section_reextract(
        [_real(1), _real(2), _real(3)],
        [_real(1), _section("2", swallowed)],
    )
    assert decision.verdict == "FAIL"
    assert decision.reason == "reextract_mega_section"


def test_definitions_that_swallowed_a_heading_without_a_period_still_fail():
    """``3 Establishment`` with no period is still a later section inside section 2."""
    swallowed = _definition_catalogue(
        "\n3 Establishment.—The Government shall establish the Board to perform the functions of this Act.\n"
    )
    assert len(swallowed) >= REEXTRACT_MEGA_SECTION_CHARS
    assert "3 Establishment" in swallowed
    decision = gate_section_reextract(
        [_real(1), _real(2), _real(3)],
        [_real(1), _section("2", swallowed), _real(12)],
    )
    assert decision.verdict == "FAIL"
    assert decision.reason == "reextract_mega_section"


def _dense_definition_catalogue(minimum_chars: int) -> str:
    """Definitions clauses repeated until the body reaches ``minimum_chars``.

    Repeating clauses keeps the quoted-clause density legal, so a failure is
    the size cap and not a thin catalogue.
    """
    block = "\n".join(
        f'({number}) "term{number}" means the meaning this Act assigns to term {number};'
        for number in range(1, 24)
    )
    block = block.replace(
        '(2) "term2" means the meaning this Act assigns to term 2;',
        '12) "term2" means the meaning this Act assigns to term 2;',
        1,
    )
    body = (
        "2. Definitions.—In this Act, unless there is anything repugnant in the subject "
        "or context,—\n"
        + block
    )
    extra = 0
    while len(body) < minimum_chars:
        extra += 1
        body += "\n" + block.replace('"term', f'"extra{extra}term')
    return body


def test_definition_catalogue_around_twelve_thousand_chars_fails_the_size_cap():
    """A pure definitions section past the catalogue cap is still a mega section."""
    catalogue = _dense_definition_catalogue(12_000)
    assert len(catalogue) >= 12_000
    decision = gate_section_reextract(
        [_real(1), _real(2), _real(3)],
        [
            _real(1, "This Act may be called the Example Urban Planning Act, 1975."),
            _section("2", catalogue),
            _real(3, "The Government shall establish the Board under this Act."),
        ],
    )
    assert decision.verdict == "FAIL"
    assert decision.reason == "reextract_mega_section"


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


def test_cleared_stored_body_uses_snapshot_or_explicit_prior_rows():
    """A wiped stored body is not a first ingest when pre-delete rows exist."""
    from scraper.statute_quality.section_reextract import reextract_before_sections

    stored: list[dict] = []
    prior = _nine_real()
    snapshot = _nine_real()
    assert reextract_before_sections(stored, None, snapshot) == snapshot
    assert reextract_before_sections(stored, prior, []) == prior
    assert reextract_before_sections(stored, [], snapshot) == snapshot
    assert reextract_before_sections([], None, []) == []
    assert reextract_before_sections(_nine_real(), None, []) == _nine_real()


def test_partial_live_rows_merge_with_capture_and_prior():
    """Leaving section 1 in place must not hide captured sections 2–9."""
    from scraper.statute_quality.section_reextract import reextract_before_sections

    live = [_section("1", "1. Live short title stays.")]
    snapshot = [_section("1", "1. Captured short title."), *[_real(n) for n in range(2, 10)]]
    merged = reextract_before_sections(live, None, snapshot)
    by_number = {row["section_number"]: row["section_text"] for row in merged}
    assert set(by_number) == {str(n) for n in range(1, 10)}
    assert by_number["1"] == "1. Live short title stays."

    prior = [_section("2", "2. Prior text wins over the capture.")]
    with_prior = reextract_before_sections(live, prior, snapshot)
    prior_by_number = {row["section_number"]: row["section_text"] for row in with_prior}
    assert prior_by_number["1"] == "1. Live short title stays."
    assert prior_by_number["2"] == "2. Prior text wins over the capture."
    assert prior_by_number["3"].startswith("3.")


async def _seed_baloch_sections(db, statute, texts: dict[str, str]):
    from tests.test_year_section_sweep import _add_section

    for number, text in texts.items():
        await _add_section(db, statute.id, number, text, int(number))


def _real_act_texts() -> dict[str, str]:
    return {
        str(number): f"{number}. The Government shall enforce provision {number} of this Act."
        for number in range(1, 10)
    }


async def _delete_sections(db, statute_id, *, clear_snapshot: bool):
    from sqlalchemy import text

    await db.execute(
        text("DELETE FROM statute_section WHERE statute_id = :id"),
        {"id": statute_id},
    )
    if clear_snapshot:
        await db.execute(
            text("DELETE FROM statute_section_reextract_prior WHERE statute_id = :id"),
            {"id": statute_id},
        )
    await db.flush()
    db.expire_all()


async def test_delete_before_promote_without_prior_rows_fails(db):
    """Deleting section rows then promoting must not skip the collapse gate."""
    from sqlalchemy import func, select

    from scraper.models import DocumentQuarantine, QuarantineQueue, Statute, StatuteSection, StatuteSectionVersion
    from scraper.tasks.promotion import promote_statute_staging
    from tests.test_b3_statute_promotion import _make_statute_staging

    name = "THE BALOCHISTAN ANIMAL SLAUGHTER CONTROL ACT, 2025 DELETED"
    originals = _real_act_texts()
    statute = Statute(
        name=name,
        jurisdiction="Balochistan",
        year_enacted=2025,
        needs_section_refetch=True,
        source_name="BalochistanAssembly",
    )
    db.add(statute)
    await db.flush()
    await _seed_baloch_sections(db, statute, originals)
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=statute.id,
            reason="distribution_list_footer_as_sections",
            run_id="reextract-delete-before",
        )
    )
    await db.flush()
    await _delete_sections(db, statute.id, clear_snapshot=False)
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


async def test_delete_versions_before_promote_without_prior_rows_fails(db):
    """Deleting current section versions first must not skip the collapse gate."""
    from sqlalchemy import select, text

    from scraper.models import Statute, StatuteSection, StatuteSectionVersion
    from scraper.tasks.promotion import promote_statute_staging
    from tests.test_b3_statute_promotion import _make_statute_staging

    name = "THE BALOCHISTAN ANIMAL SLAUGHTER CONTROL ACT, 2025 VERSIONS"
    originals = _real_act_texts()
    statute = Statute(
        name=name,
        jurisdiction="Balochistan",
        year_enacted=2025,
        needs_section_refetch=True,
        source_name="BalochistanAssembly",
    )
    db.add(statute)
    await db.flush()
    await _seed_baloch_sections(db, statute, originals)
    await db.execute(
        text(
            "DELETE FROM statute_section_version WHERE section_id IN "
            "(SELECT id FROM statute_section WHERE statute_id = :id)"
        ),
        {"id": statute.id},
    )
    await db.flush()
    db.expire_all()
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
    stored = (
        await db.execute(
            select(StatuteSection.section_number, StatuteSectionVersion.section_text)
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == statute.id)
        )
    ).all()
    assert {num: text for num, text in stored} == originals
    assert len(stored) == 9


async def test_delete_before_promote_with_prior_rows_nine_to_one_fails(db):
    """Explicit pre-delete rows still fail the 9→1 collapse after the rows are gone."""
    from sqlalchemy import select

    from scraper.models import DocumentQuarantine, QuarantineQueue, Statute, StatuteSection, StatuteSectionVersion
    from scraper.tasks.promotion import promote_statute_staging
    from tests.test_b3_statute_promotion import _make_statute_staging

    name = "THE BALOCHISTAN ANIMAL SLAUGHTER CONTROL ACT, 2025 PRIOR"
    originals = _real_act_texts()
    prior = [_section(number, text) for number, text in originals.items()]
    statute = Statute(
        name=name,
        jurisdiction="Balochistan",
        year_enacted=2025,
        needs_section_refetch=True,
        source_name="BalochistanAssembly",
    )
    db.add(statute)
    await db.flush()
    await _seed_baloch_sections(db, statute, originals)
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=statute.id,
            reason="distribution_list_footer_as_sections",
            run_id="reextract-prior-rows",
        )
    )
    await db.flush()
    await _delete_sections(db, statute.id, clear_snapshot=True)
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
    assert await promote_statute_staging(db, staging, prior_sections=prior) == "quarantined"
    details = (
        await db.execute(select(QuarantineQueue).where(QuarantineQueue.statutes_staging_id == staging.id))
    ).scalars().one()
    assert (details.details or {}).get("reason_code") == "reextract_collapse_count"
    assert (details.details or {}).get("section_count_before") == 9
    stored = (
        await db.execute(
            select(StatuteSection.section_number, StatuteSectionVersion.section_text)
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == statute.id)
        )
    ).all()
    assert {num: text for num, text in stored} == originals
    assert all("Extent through repeal" not in text for _, text in stored)


async def test_delete_before_promote_stable_footer_strip_passes(db):
    """A 9→7 footer strip still passes when the gate sees the pre-delete rows."""
    from sqlalchemy import func, select

    from scraper.models import (
        DocumentQuarantine,
        Statute,
        StatuteSection,
        StatuteSectionReextractPrior,
        StatuteSectionVersion,
    )
    from scraper.statute_quality.section_reextract import gate_section_reextract, reextract_before_sections
    from scraper.tasks.promotion import promote_statute_staging
    from tests.test_b3_statute_promotion import _make_statute_staging

    name = "The Example Footer Strip After Delete Act, 2024"
    originals = _real_act_texts()
    originals["5"] = "5. The Director General, Public Relations, Balochistan, Quetta."
    originals["6"] = "6. The System Analyst, Balochistan Provincial Assembly."
    prior = [_section(number, text) for number, text in originals.items()]
    incoming = [
        _real(number, f"The Government shall enforce provision {number} of this Act, as re-read.")
        for number in range(1, 10)
        if number not in {5, 6}
    ]
    decision = gate_section_reextract(reextract_before_sections([], prior, []), incoming)
    assert decision.verdict == "PASS"
    assert decision.may_apply is True
    assert decision.section_count_before == 9
    assert decision.section_count_after == 7
    statute = Statute(
        name=name,
        jurisdiction="Balochistan",
        year_enacted=2024,
        needs_section_refetch=True,
        source_name="BalochistanAssembly",
    )
    db.add(statute)
    await db.flush()
    await _seed_baloch_sections(db, statute, originals)
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=statute.id,
            reason="distribution_list_footer_as_sections",
            run_id="reextract-footer-after-delete",
        )
    )
    await db.flush()
    await _delete_sections(db, statute.id, clear_snapshot=False)
    staging = await _make_statute_staging(
        db,
        {
            "statute_name": name,
            "year_enacted": 2024,
            "jurisdiction": "Balochistan",
            "sections": incoming,
        },
    )
    assert await promote_statute_staging(db, staging, prior_sections=prior) in ("promoted", "duplicate")
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
    stored = (
        await db.execute(
            select(StatuteSection.section_number, StatuteSectionVersion.section_text)
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == statute.id)
        )
    ).all()
    by_number = {num: text for num, text in stored}
    assert set(by_number) == {"1", "2", "3", "4", "7", "8", "9"}
    assert "as re-read" in by_number["1"]
    leftover = (
        await db.execute(
            select(func.count())
            .select_from(StatuteSectionReextractPrior)
            .where(StatuteSectionReextractPrior.statute_id == statute.id)
        )
    ).scalar()
    assert leftover == 0


async def test_partial_delete_before_promote_still_fails_collapse(db):
    """Deleting 8 of 9 sections must not shrink the gate's before count to the survivor."""
    from sqlalchemy import func, select, text

    from scraper.models import (
        DocumentQuarantine,
        QuarantineQueue,
        Statute,
        StatuteSection,
        StatuteSectionVersion,
    )
    from scraper.tasks.promotion import promote_statute_staging
    from tests.test_b3_statute_promotion import _make_statute_staging

    name = "THE BALOCHISTAN ANIMAL SLAUGHTER CONTROL ACT, 2025 PARTIAL"
    originals = _real_act_texts()
    statute = Statute(
        name=name,
        jurisdiction="Balochistan",
        year_enacted=2025,
        needs_section_refetch=True,
        source_name="BalochistanAssembly",
    )
    db.add(statute)
    await db.flush()
    await _seed_baloch_sections(db, statute, originals)
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=statute.id,
            reason="distribution_list_footer_as_sections",
            run_id="reextract-partial-delete",
        )
    )
    await db.flush()
    await db.execute(
        text("DELETE FROM statute_section WHERE statute_id = :id AND section_number <> '1'"),
        {"id": statute.id},
    )
    await db.flush()
    db.expire_all()
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
            select(StatuteSection.section_number, StatuteSectionVersion.section_text, StatuteSection.sort_key)
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == statute.id)
        )
    ).all()
    assert {num: text for num, text, _sort in stored} == originals
    assert {num: sort for num, _text, sort in stored} == {str(n): n for n in range(1, 10)}


async def test_orphan_reextract_capture_is_purged_and_live_statute_capture_stays(db):
    """Captures for a deleted statute go away. A time-to-live sweep of the rest is deferred."""
    import uuid

    from sqlalchemy import func, select

    from scraper.models import Statute, StatuteSectionReextractPrior
    from scraper.statute_quality.section_reextract_apply import purge_orphan_reextract_captures

    statute = Statute(name="Capture Keep Act, 2024", jurisdiction="Balochistan", year_enacted=2024)
    db.add(statute)
    await db.flush()
    kept_id = uuid.uuid4()
    orphan_id = uuid.uuid4()
    db.add(
        StatuteSectionReextractPrior(
            section_id=kept_id,
            statute_id=statute.id,
            section_number="1",
            section_text="1. This capture still belongs to a statute.",
            sort_key=1,
        )
    )
    db.add(
        StatuteSectionReextractPrior(
            section_id=orphan_id,
            statute_id=uuid.uuid4(),
            section_number="2",
            section_text="2. The statute row is already gone.",
            sort_key=2,
        )
    )
    await db.flush()
    await purge_orphan_reextract_captures(db)
    remaining = (
        await db.execute(select(StatuteSectionReextractPrior.section_id))
    ).scalars().all()
    assert remaining == [kept_id]
    assert (
        await db.execute(select(func.count()).select_from(StatuteSectionReextractPrior))
    ).scalar() == 1


def _tariff_polluted_finance_act_before() -> list[dict]:
    """The eleven Finance Act 1985 rows: rate lines and HS rows, no real section."""
    return [
        _section("1", "1. Where the taxable income does not exceed Rs. 25,000 the tax shall be five percent of the income."),
        _section("2", "2. Where the taxable income exceeds Rs. 25,000 but does not exceed Rs. 50,000 the tax shall be the amount specified."),
        _section("4", "4. Where the taxable income exceeds Rs. 50,000 but does not exceed Rs. 100,000 the tax shall be computed under this clause."),
        _section("33", "33.03 Synthetic organic colouring matter, whether or not chemically defined, and preparations."),
        _section("78", "78. 38BO5 Chromite sulphate used in the dyeing and tanning process under this schedule of the Act."),
        _section("32", "32.03 Synthetic organic colouring matter and preparations based on the colouring matter."),
        _section("40", "40.11 New pneumatic tyres, of rubber, of a kind used on motor cars under this schedule."),
        _section("51", "51.07 Yarn of combed wool, not put up for retail sale, containing eighty five percent of wool."),
        _section("56", "56.01 Synthetic staple fibres, not carded, combed or otherwise processed for spinning."),
        _section("03", "03) Cuprammonium hydroxide solution used in the manufacture of cuprammonium rayon under this schedule."),
        _section("73", "73.15 Chain and parts thereof, of iron or steel, per metric ton under this schedule of the Act."),
    ]


def _finance_act_without_definitions() -> list[dict]:
    rate = "1. Where the taxable income does not exceed Rs. 25,000 the tax shall be five percent of the income.\n"
    amendment = (
        "4. Amendment of Ordinance XXXI of 1979.— In the Income Tax Ordinance, 1979 "
        "the following further amendment shall be made, namely the rate schedule hereunder.\n"
        + rate
        + ("The rate in this schedule applies to the income of the year. " * 80)
    )
    assert len(amendment) >= REEXTRACT_MEGA_SECTION_CHARS
    return [
        _section("1", "1. Short title and extent.— This Act may be called the Finance Act, 1985."),
        _section("2", "2. Amendment of Act XV of 1963.— In the Wealth Tax Act, 1963 the following further amendment shall be made."),
        _section("3", "3. Amendment of Act IV of 1969.— In the Customs Act, 1969 the First Schedule shall be amended."),
        _section("4", amendment),
        _section("5", "5. Iqra surcharge on imported goods.— A surcharge to be called the Iqra surcharge shall be levied on imported goods."),
    ]


def test_tariff_rows_are_not_the_collapse_baseline_for_a_finance_act():
    """A correct five-section amending Act must not fail against eleven schedule rows.

    Finance Act 1985 has no Definitions section. That absence is not a failure.
    """
    before = _tariff_polluted_finance_act_before()
    after = _finance_act_without_definitions()
    assert all("Definitions" not in row["section_text"] for row in after)
    decision = gate_section_reextract(before, after)
    assert decision.verdict == "PASS"
    assert decision.reason == ""
    assert decision.may_apply is True
    assert decision.section_count_before == 0
    assert decision.section_count_after == 5


def test_tariff_only_before_set_still_rejects_a_mega_section_without_a_schedule():
    """Ignoring schedule rows does not let one long non-schedule section through."""
    before = _tariff_polluted_finance_act_before()
    mega = "2. Extent through repeal and savings. " + ("The remaining provisions of this Act. " * 400)
    assert len(mega) >= REEXTRACT_MEGA_SECTION_CHARS
    decision = gate_section_reextract(before, [_section("2", mega)])
    assert decision.verdict == "FAIL"
    assert decision.reason == "reextract_mega_section"
    assert decision.section_count_before == 0


def test_schedule_inside_an_amending_section_may_pass_the_mega_cut():
    """The rate schedule lives in the amending section. A later real heading still fails."""
    before = _tariff_polluted_finance_act_before()
    after = _finance_act_without_definitions()
    decision = gate_section_reextract(before, after)
    assert decision.verdict == "PASS"

    swallowed = after[3]["section_text"] + (
        "\n6. Short title of the next Act.— This Act may be called the Other Finance Act, 1986 "
        "and it extends to the whole of Pakistan.\n"
    )
    assert len(swallowed) >= REEXTRACT_MEGA_SECTION_CHARS
    glued = gate_section_reextract(before, [_section("4", swallowed)])
    assert glued.verdict == "FAIL"
    assert glued.reason == "reextract_mega_section"
