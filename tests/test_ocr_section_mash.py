"""OCR section-mash sweep and promotion gate.

FREE-SITES ``8d5b87b6`` (Balochistan Institute of Child Health Services Quetta
Act, Act No. XXI of 2025) is the failing fixture. It stays a MISMATCH until a
re-extract stores the real section numbers. Stored rows are 3, 8, 10, 11
(page/line numbers). The text of stored s.11 holds ss.28–30.

MAIN ``09cac1a4`` Industrial Statistics 1975 (s.7) is MATCH-likely.
MAIN ``648084fb`` PCSIR 1973 and FREE-SITES ``97bcaece`` IST Bahawalpur 2018
are the Senate OCR-garble PARTIAL class and must not be labelled as this defect.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import func, select

from scraper.document_validation import validate_statute_reconciled_json
from scraper.parsers.statute_contents import read_statute
from scraper.models import CorpusMetadata, DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.ocr_section_mash import (
    ACTION_NEEDS_REEXTRACT,
    ACTION_NONE,
    ACTION_QUARANTINE,
    REASON_FEW_SECTIONS_SCANNED,
    REASON_HEADINGS_ABOVE_STORED,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_OUT_OF_SCOPE,
    VERDICT_SUSPECT,
    classify_ocr_section_mash,
    pdf_lacks_text_layer,
)
from scraper.tasks.ocr_section_mash import run_ocr_section_mash
from tests.fixtures import scanned_pdf_bytes, text_pdf_bytes

BICHQ_NAME = "The Balochistan Institute of Child Health Services Quetta Act, Act No. XXI of 2025"
BICHQ_URL = "https://www.pabalochistan.gov.pk/notifications/Act-XXI-of-2025-child-health-quetta.pdf"
BICHQ_STORED_NUMBERS = ("3", "8", "10", "11")


def bichq_sections() -> list[dict]:
    """Shape of FREE-SITES 8d5b87b6. Do not renumber this fixture to make it MATCH."""
    mashed = "\n".join(
        [
            "11",
            "Page 11 of 12",
            "28. Functions of the Institute.—(1) The Institute shall provide child health services in Quetta.",
            "29. Power to make rules.—The Government may make rules to carry out the purposes of this Act.",
            "30. Repeal and savings.—The Ordinance is hereby repealed and anything done under it shall continue.",
        ]
    )
    return [
        {"section_number": "3", "section_text": "Page 3 of 12\n3\n"},
        {"section_number": "8", "section_text": "- 8 -\n8.\n"},
        {"section_number": "10", "section_text": "10\nPage 10 of 12\n"},
        {"section_number": "11", "section_text": mashed},
    ]


def _aligned(count: int) -> list[dict]:
    titles = (
        "Short title",
        "Definitions",
        "Collection of statistics",
        "Powers of the Director",
        "Returns",
        "Penalties",
        "Rules",
    )
    rows = []
    for index in range(1, count + 1):
        title = titles[index - 1] if index <= len(titles) else f"Provision {index}"
        rows.append(
            {
                "section_number": str(index),
                "section_text": f"{index}. {title}.—This section provides for {title.lower()} under this Act.",
            }
        )
    return rows


def test_bichq_fixture_stays_a_mismatch_until_reextracted():
    sections = bichq_sections()
    assert tuple(row["section_number"] for row in sections) == BICHQ_STORED_NUMBERS
    mashed = sections[-1]["section_text"]
    assert "28. Functions of the Institute" in mashed
    assert "30. Repeal and savings" in mashed
    verdict = classify_ocr_section_mash(
        sections,
        source_url=BICHQ_URL,
        source_name="BalochistanAssembly",
        scanned_source=True,
    )
    assert verdict.verdict == VERDICT_MISMATCH
    assert verdict.action == ACTION_QUARANTINE
    assert verdict.reason == REASON_HEADINGS_ABOVE_STORED
    assert verdict.stored_max == 11
    assert verdict.embedded_max == 30
    assert verdict.embedded_headings == (28, 29, 30)
    assert "ignored_page_or_line_numbers" in verdict.signals
    assert 3 not in verdict.embedded_headings
    assert 11 not in verdict.embedded_headings


def test_page_and_line_numbers_are_not_section_headings():
    sections = [
        {
            "section_number": "2",
            "section_text": "\n".join(
                [
                    "Page 2 of 9",
                    "2",
                    "- 4 -",
                    "8.",
                    "11. of 12",
                    "2. Definitions.—In this Act, Board means the Board of the Institute.",
                ]
            ),
        }
    ]
    verdict = classify_ocr_section_mash(sections, source_url=BICHQ_URL, source_name="BalochistanAssembly")
    assert verdict.verdict == VERDICT_MATCH
    assert verdict.embedded_headings == (2,)
    assert verdict.action == ACTION_NONE


def test_industrial_statistics_shape_matches():
    """MAIN 09cac1a4 is s.7 and MATCH-likely. Aligned headings must not be suspects."""
    verdict = classify_ocr_section_mash(
        _aligned(7),
        source_url="https://law.gok.pk/wp-content/uploads/industrial-statistics-1975.pdf",
        source_name="AJKAssembly",
        scanned_source=False,
    )
    assert verdict.verdict == VERDICT_MATCH
    assert verdict.reason == ""
    assert verdict.stored_count == 7
    assert verdict.embedded_max == 7


def test_amendment_that_quotes_one_later_section_is_not_a_mismatch():
    sections = [
        {
            "section_number": "1",
            "section_text": "1. Short title.—This Act may be called the Sample Amendment Act, 2025.",
        },
        {
            "section_number": "2",
            "section_text": (
                "2. Amendment of sections 20 and 21.—In the said Act, for sections 20 and 21 the following shall be substituted, namely:-\n"
                '"20. New fund.—The Fund shall be kept in a scheduled bank."\n'
                '"21. Accounts of the fund.—The accounts shall be audited each year."'
            ),
        },
    ]
    verdict = classify_ocr_section_mash(
        sections,
        source_url="https://www.pabalochistan.gov.pk/acts/sample-amendment.pdf",
        source_name="BalochistanAssembly",
    )
    assert verdict.verdict == VERDICT_MATCH
    assert REASON_HEADINGS_ABOVE_STORED not in verdict.reasons


CLEAN_CONTENTS_ACT = """
THE SAMPLE STATISTICS ACT, 1975

1. Short title.—This Act may be called the Sample Statistics Act, 1975 and it extends to the whole of the territory described in the preamble of this Act.

2. Definitions.—In this Act, unless there is anything repugnant in the subject or context, Director means the Director of Statistics appointed under this Act.

3. Rules.—The Government may make rules for carrying the purposes of this Act into effect and shall publish those rules in the official Gazette.
"""

AMENDING_CONTENTS_ACT = """
THE SAMPLE AMENDMENT ACT, 2025

1. Short title.—This Act may be called the Sample Amendment Act, 2025 and it shall come into force at once throughout the province.

2. Insertion of new sections.—In the said Act, after section 27, the following new sections shall be inserted, namely:-
28. Functions of the Institute.—(1) The Institute shall provide child health services in Quetta and may hold property for that purpose.
29. Power to make rules.—The Government may make rules to carry out the purposes of this Act and shall notify them.
30. Repeal and savings.—The Ordinance is hereby repealed and anything done under it shall continue in force.
"""


def test_statute_contents_headingless_body_stays_match():
    """statute_contents strips ``N. Title.—``. A clean body with no own heading is still a match."""
    parsed = read_statute(CLEAN_CONTENTS_ACT, "The Sample Statistics Act, 1975")
    sections = parsed["sections"]
    assert [row["section_number"] for row in sections] == ["1", "2", "3"]
    assert not sections[0]["section_text"].startswith("1.")
    assert "Short title" not in sections[0]["section_text"]
    verdict = classify_ocr_section_mash(
        sections,
        source_url="https://law.gok.pk/wp-content/uploads/sample-statistics-1975.pdf",
        source_name="AJKAssembly",
    )
    assert verdict.verdict == VERDICT_MATCH
    assert verdict.reason == ""
    assert REASON_HEADINGS_ABOVE_STORED not in verdict.reasons
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": "The Sample Statistics Act, 1975", "sections": sections},
        raw_text="",
        source_name="AJKAssembly",
        source_url="https://law.gok.pk/wp-content/uploads/sample-statistics-1975.pdf",
    )
    assert ok is True
    assert reason == ""


def test_amending_act_inserted_sections_stay_match_without_own_heading():
    """Auditor probe: s.2 inserts ss.28–30 and statute_contents has already dropped ``2.``."""
    parsed = read_statute(AMENDING_CONTENTS_ACT, "The Sample Amendment Act, 2025")
    sections = parsed["sections"]
    inserted = next(row for row in sections if row["section_number"] == "2")
    assert not inserted["section_text"].startswith("2.")
    assert "28. Functions of the Institute" in inserted["section_text"]
    assert "30. Repeal and savings" in inserted["section_text"]
    verdict = classify_ocr_section_mash(
        sections,
        source_url="https://www.pabalochistan.gov.pk/acts/sample-amendment-2025.pdf",
        source_name="BalochistanAssembly",
    )
    assert verdict.verdict == VERDICT_MATCH
    assert verdict.action == ACTION_NONE
    assert REASON_HEADINGS_ABOVE_STORED not in verdict.reasons
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "The Sample Amendment Act, 2025",
            "source_name": "BalochistanAssembly",
            "source_url": "https://www.pabalochistan.gov.pk/acts/sample-amendment-2025.pdf",
            "sections": sections,
        },
        raw_text="",
    )
    assert ok is True
    assert reason == ""


def test_quoted_and_substituted_insertions_are_not_headings():
    sections = [
        {
            "section_number": "1",
            "section_text": "This Act may be called the Sample Substitution Act, 2024 and it shall come into force at once.",
        },
        {
            "section_number": "2",
            "section_text": (
                "In the said Act, for sections 28, 29 and 30, the following shall be substituted, namely:-\n"
                '"28. Functions of the Institute.—(1) The Institute shall provide child health services."\n'
                '"29. Power to make rules.—The Government may make rules for the Institute."\n'
                '"30. Repeal and savings.—The Ordinance is hereby repealed."'
            ),
        },
    ]
    verdict = classify_ocr_section_mash(
        sections,
        source_url="https://www.pabalochistan.gov.pk/acts/sample-substitution-2024.pdf",
        source_name="BalochistanAssembly",
    )
    assert verdict.verdict == VERDICT_MATCH
    assert 28 not in verdict.embedded_headings
    assert 30 not in verdict.embedded_headings
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": "The Sample Substitution Act, 2024", "sections": sections},
        raw_text="",
        source_name="BalochistanAssembly",
    )
    assert ok is True
    assert reason == ""


def test_one_higher_heading_is_not_a_wide_margin():
    sections = [
        {
            "section_number": "4",
            "section_text": "28. Repeal.—The Ordinance is hereby repealed.",
        }
    ]
    verdict = classify_ocr_section_mash(sections, source_url=BICHQ_URL, source_name="BalochistanAssembly")
    assert verdict.verdict == VERDICT_MATCH


def test_senate_ocr_garble_is_not_this_defect():
    """PCSIR and IST Bahawalpur stay on the Senate PARTIAL path."""
    pcsir = classify_ocr_section_mash(
        bichq_sections(),
        source_url="https://www.senate.gov.pk/uploads/documents/pcsir-act-1973.pdf",
        source_name="Senate",
        scanned_source=True,
    )
    ist = classify_ocr_section_mash(
        _aligned(5),
        source_url="https://senate.gov.pk/uploads/documents/ist-bahawalpur-2018.pdf|XV",
        source_name="Senate",
    )
    assert pcsir.verdict == VERDICT_OUT_OF_SCOPE
    assert ist.verdict == VERDICT_OUT_OF_SCOPE
    assert pcsir.action == ACTION_NONE
    assert REASON_HEADINGS_ABOVE_STORED not in pcsir.reasons
    assert REASON_FEW_SECTIONS_SCANNED not in pcsir.reasons


def test_few_sections_from_a_scanned_source_are_suspects_without_a_heading_mismatch():
    verdict = classify_ocr_section_mash(
        _aligned(3),
        source_url=BICHQ_URL,
        source_name="BalochistanAssembly",
        scanned_source=True,
    )
    assert verdict.verdict == VERDICT_SUSPECT
    assert verdict.reason == REASON_FEW_SECTIONS_SCANNED
    assert verdict.action == ACTION_NEEDS_REEXTRACT
    assert verdict.stored_count == 3


def test_few_sections_without_a_scanned_signal_stay_match():
    unknown = classify_ocr_section_mash(_aligned(3), source_url=BICHQ_URL, source_name="BalochistanAssembly")
    typed = classify_ocr_section_mash(
        _aligned(4),
        source_url=BICHQ_URL,
        source_name="BalochistanAssembly",
        scanned_source=False,
    )
    assert unknown.verdict == VERDICT_MATCH
    assert typed.verdict == VERDICT_MATCH


def test_validation_gate_rejects_bichq_shape_and_accepts_aligned_text():
    rejected, reason = validate_statute_reconciled_json(
        {
            "statute_name": BICHQ_NAME,
            "source_url": BICHQ_URL,
            "source_name": "BalochistanAssembly",
            "sections": bichq_sections(),
        },
        raw_text="",
    )
    assert rejected is False
    assert reason == REASON_HEADINGS_ABOVE_STORED
    accepted, accepted_reason = validate_statute_reconciled_json(
        {"statute_name": "Industrial Statistics Act, 1975", "sections": _aligned(7)},
        raw_text="",
    )
    assert accepted is True
    assert accepted_reason == ""


def test_validation_gate_does_not_reject_senate_sources():
    ok, reason = validate_statute_reconciled_json(
        {"statute_name": "Pakistan Council of Scientific and Industrial Research Act, 1973", "sections": bichq_sections()},
        raw_text="",
        source_name="Senate",
        source_url="https://www.senate.gov.pk/uploads/documents/pcsir-act-1973.pdf",
    )
    assert ok is True
    assert reason == ""


def test_pdf_layer_helper_flags_scans_and_keeps_typed_pdfs():
    assert pdf_lacks_text_layer(scanned_pdf_bytes()) is True
    typed = text_pdf_bytes(
        "\n".join(
            f"{index}. Short title of the Typed Text Act, 2024, with a real text layer."
            for index in range(1, 8)
        )
    )
    assert pdf_lacks_text_layer(typed) is False


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _add_statute(
    db,
    *,
    name: str,
    url: str,
    sections: list[dict],
    source_name: str = "BalochistanAssembly",
    jurisdiction: str = "Balochistan",
):
    statute = Statute(
        name=name,
        jurisdiction=jurisdiction,
        source_name=source_name,
        source_url=url,
        year_enacted=2025,
    )
    db.add(statute)
    await db.flush()
    for index, sec in enumerate(sections):
        row = StatuteSection(
            statute_id=statute.id,
            section_number=sec["section_number"],
            section_title=sec.get("section_title"),
            sort_key=index,
        )
        db.add(row)
        await db.flush()
        ver = StatuteSectionVersion(
            section_id=row.id,
            version_no=1,
            section_text=sec["section_text"],
            text_hash=_text_hash(f"{statute.id}:{sec['section_number']}:{sec['section_text']}"),
        )
        db.add(ver)
        await db.flush()
        row.current_version_id = ver.id
    await db.commit()
    return statute


async def _section_numbers(db, statute_id) -> list[str]:
    rows = (
        await db.execute(
            select(StatuteSection.section_number)
            .where(StatuteSection.statute_id == statute_id)
            .order_by(StatuteSection.sort_key)
        )
    ).scalars().all()
    return [str(num) for num in rows]


@pytest.mark.asyncio
async def test_dry_run_lists_bichq_and_writes_nothing(db):
    bichq = await _add_statute(db, name=BICHQ_NAME, url=BICHQ_URL, sections=bichq_sections())
    await _add_statute(
        db,
        name="The Azad Jammu and Kashmir Industrial Statistics Act, 1975",
        url="https://law.gok.pk/wp-content/uploads/industrial-statistics-1975.pdf",
        sections=_aligned(7),
        source_name="AJKAssembly",
        jurisdiction="AJK",
    )
    await _add_statute(
        db,
        name="Pakistan Council of Scientific and Industrial Research Act, 1973",
        url="https://www.senate.gov.pk/uploads/documents/pcsir-act-1973.pdf",
        sections=bichq_sections(),
        source_name="Senate",
        jurisdiction="Federal",
    )
    before = (await db.execute(select(func.count()).select_from(DocumentQuarantine))).scalar_one()
    report = await run_ocr_section_mash(scanned_probe=lambda _statute: None)
    after = (await db.execute(select(func.count()).select_from(DocumentQuarantine))).scalar_one()
    assert after == before
    assert report["dry_run"] is True
    assert report["applied"] is None
    assert report["population"] == 2
    assert report["skipped_senate"] == 1
    assert report["suspects"] == 1
    assert report["reason_counts"][REASON_HEADINGS_ABOVE_STORED] == 1
    row = report["rows"][0]
    assert row["statute_id"] == str(bichq.id)
    assert row["verdict"] == VERDICT_MISMATCH
    assert row["reason"] == REASON_HEADINGS_ABOVE_STORED
    assert await _section_numbers(db, bichq.id) == list(BICHQ_STORED_NUMBERS)


@pytest.mark.asyncio
async def test_apply_quarantines_mismatch_and_flags_scanned_few_without_renumbering(db):
    bichq = await _add_statute(db, name=BICHQ_NAME, url=BICHQ_URL, sections=bichq_sections())
    short = await _add_statute(
        db,
        name="The Balochistan Sample Notification Act, 2024",
        url="https://www.pabalochistan.gov.pk/notifications/sample-2024.pdf",
        sections=_aligned(3),
    )
    bichq_id = bichq.id
    short_id = short.id

    def probe(statute: Statute):
        if statute.id == short_id:
            return True
        return None

    report = await run_ocr_section_mash(apply=True, scanned_probe=probe)
    assert report["dry_run"] is False
    assert report["applied"]["quarantined"] == 1
    assert report["applied"]["reextract"] == 1
    db.expire_all()
    assert await _section_numbers(db, bichq_id) == list(BICHQ_STORED_NUMBERS)
    held = (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id == bichq_id,
            )
        )
    ).scalars().one()
    assert held.reason == REASON_HEADINGS_ABOVE_STORED
    refreshed_bichq = (await db.execute(select(Statute).where(Statute.id == bichq_id))).scalars().one()
    refreshed_short = (await db.execute(select(Statute).where(Statute.id == short_id))).scalars().one()
    assert refreshed_bichq.needs_section_refetch is False
    assert refreshed_short.needs_section_refetch is True
    short_hold = (
        await db.execute(
            select(DocumentQuarantine).where(DocumentQuarantine.doc_id == short_id)
        )
    ).scalars().first()
    assert short_hold is None

    again = await run_ocr_section_mash(apply=True, scanned_probe=probe)
    assert again["applied"]["quarantined"] == 0
    assert again["applied"]["reextract"] == 1

    db.expire_all()
    undone = await run_ocr_section_mash(undo=report["applied"]["run_id"])
    db.expire_all()
    assert undone["undone"]["quarantine_rows_removed"] == 1
    assert undone["undone"]["refetch_flags_restored"] == 1
    still = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == bichq_id))
    ).scalars().first()
    assert still is None
    restored = (await db.execute(select(Statute).where(Statute.id == short_id))).scalars().one()
    assert restored.needs_section_refetch is False
    latest = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == "ocr_section_mash_latest_run_id"))
    ).scalars().first()
    assert latest is None or latest.value != report["applied"]["run_id"]
