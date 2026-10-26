"""Bundled gazette PDFs must yield only the target act.

A Senate upload can print several Acts under successive ``ACT No.`` lines.
The Service Tribunals Act, 1973 is the middle act of one such PDF (LXVII,
LXX, LXXI of 1973). Extraction must keep LXX and must not store the
neighbours' sections.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import func, select

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.fetchers import record_provenance, stage_statute
from scraper.models import DocumentQuarantine, ScraperSource, Statute, StatuteSection, StatuteSectionVersion
from scraper.tasks.promotion import promote_statute_staging

SERVICE_TRIBUNALS = "The Service Tribunals Act, 1973"
PDF_URL = "https://senate.gov.pk/uploads/documents/1660540467_508.pdf"
PIPE_URL = f"{PDF_URL}|LXX of 1973"

# Miniature of the three-act gazette. The bogus "59." line sits in the
# running footer, ahead of every ACT No., which is how "under Article 59"
# became a stored section.
BUNDLE = """
THE GAZETTE OF PAKISTAN
59. Printed under Article 59 of the Constitution as a gazette footer and not a section of any act in this part.

ACT No. LXVII of 1973
An Act further to amend the Life Insurance (Nationalisation) Act, 1972.
1. Short title.—This Act may be called the Life Insurance (Nationalisation) (Amendment) Act, 1973.
2. Amendment of section 4.—In the Life Insurance (Nationalisation) Act, 1972, in section 4, for the word insurer the word corporation shall be substituted throughout the vesting schedule and the compensation notice.
3. Amendment of section 11.—In the Life Insurance (Nationalisation) Act, 1972, section 11 shall be renumbered and the compensation board shall publish its award within ninety days.

ACT No. LXX of 1973
An Act to provide for the establishment of Service Tribunals.
1. Short title, extent and commencement.—This Act may be called the Service Tribunals Act, 1973.
2. Definitions.—In this Act, unless there is anything repugnant in the subject or context, Tribunal means a Service Tribunal established under this Act to hear appeals of civil servants.
3. Establishment of Tribunals.—The President may establish one or more Service Tribunals and each Tribunal shall have exclusive jurisdiction over service matters of civil servants.
4. Appeals.—A civil servant may prefer an appeal to the Tribunal within thirty days of the final order affecting the terms of service.
5. Powers.—The Tribunal shall have the powers of a civil court while trying a suit under the Code of Civil Procedure, 1908.
6. Decision.—The Tribunal shall decide the appeal within six months and record reasons in writing for every final order.
7. Review.—The Tribunal may review its decision on an application filed within thirty days of that decision.
8. Rules.—The Federal Government may make rules for carrying out the purposes of this Act and for the registry.
9. Repeal.—The Service Tribunals Ordinance, 1973, is hereby repealed.

ACT No. LXXI of 1973
An Act to provide for federal supervision of curricula.
1. Short title.—This Act may be called the Federal Supervision of Curricula Act, 1973.
9. Repeal.—The Curriculum Ordinance, 1972, is hereby repealed and the Federal Supervision Board shall assume its records, staff, and pending curriculum proposals without further sanction.
"""

LIFE_INSURANCE_S2 = (
    "In the Life Insurance (Nationalisation) Act, 1972, in section 4, for the word insurer "
    "the word corporation shall be substituted throughout the vesting schedule and the compensation notice."
)
TRIBUNALS_S2 = (
    "Tribunal means a Service Tribunal established under this Act to hear appeals of civil servants."
)
CURRICULA_S9 = (
    "The Curriculum Ordinance, 1972, is hereby repealed and the Federal Supervision Board shall assume its records"
)


def _blob(extracted: dict) -> str:
    return "\n".join((section.get("section_text") or "") for section in extracted["sections"])


def _numbers(extracted: dict) -> set[str]:
    return {str(section.get("section_number")) for section in extracted["sections"]}


def test_middle_act_of_three_is_the_only_text_extracted_for_the_target():
    extracted = extract_statute_deterministic(
        html=None,
        text=BUNDLE,
        source_meta={
            "source_name": "Senate",
            "url": PIPE_URL,
            "act_title": SERVICE_TRIBUNALS,
        },
    )
    blob = _blob(extracted)
    numbers = _numbers(extracted)
    assert "Service Tribunals" in (extracted["statute_name"] or "")
    assert TRIBUNALS_S2 in blob
    assert "prefer an appeal to the Tribunal" in blob
    assert "Service Tribunals Ordinance, 1973" in blob
    assert LIFE_INSURANCE_S2 not in blob
    assert "compensation board shall publish" not in blob
    assert CURRICULA_S9 not in blob
    assert "59" not in numbers
    assert "4" in numbers
    assert extracted["field_evidence"].get("bundled_gazette_unresolved") is None


def test_title_alone_selects_the_middle_act_when_the_url_has_no_pipe_label():
    extracted = extract_statute_deterministic(
        html=None,
        text=BUNDLE,
        source_meta={
            "source_name": "Senate",
            "url": PDF_URL,
            "act_title": SERVICE_TRIBUNALS,
        },
    )
    blob = _blob(extracted)
    assert TRIBUNALS_S2 in blob
    assert LIFE_INSURANCE_S2 not in blob
    assert CURRICULA_S9 not in blob


@pytest.mark.parametrize(
    "source_name,url",
    [
        ("NationalAssembly", "https://na.gov.pk/uploads/documents/bundle.pdf|LXX of 1973"),
        ("AJKAssembly", "https://law.gok.pk/wp-content/uploads/bundle.pdf|LXX of 1973"),
    ],
)
def test_act_number_boundaries_isolate_the_middle_act_on_similar_hosts(source_name, url):
    extracted = extract_statute_deterministic(
        html=None,
        text=BUNDLE,
        source_meta={"source_name": source_name, "url": url, "act_title": SERVICE_TRIBUNALS},
    )
    blob = _blob(extracted)
    assert TRIBUNALS_S2 in blob
    assert LIFE_INSURANCE_S2 not in blob
    assert CURRICULA_S9 not in blob


def test_unresolved_bundle_stores_no_sections():
    extracted = extract_statute_deterministic(
        html=None,
        text=BUNDLE,
        source_meta={"source_name": "Senate", "url": PDF_URL},
    )
    assert extracted["sections"] == []
    assert extracted["field_evidence"].get("bundled_gazette_unresolved")


def test_pipe_label_and_title_that_name_different_acts_store_nothing():
    extracted = extract_statute_deterministic(
        html=None,
        text=BUNDLE,
        source_meta={
            "source_name": "Senate",
            "url": f"{PDF_URL}|LXVII of 1973",
            "act_title": SERVICE_TRIBUNALS,
        },
    )
    assert extracted["sections"] == []
    assert extracted["field_evidence"].get("bundled_gazette_unresolved")


AMENDMENT_CITING_EARLIER_ACT = """
ACT No. XII of 1980
An Act further to amend the Service Tribunals Act, 1973.
1. Short title.—This Act may be called the Service Tribunals (Amendment) Act, 1980.
2. Amendment of section 3.—In the Service Tribunals Act, 1973, in section 3, after the word President the words or a person authorised by him shall be inserted and the tribunal shall note that insertion in its register.
Act No. LXX of 1973.- In the Service Tribunals Act, 1973, in section 4, for the word thirty the word sixty shall be substituted and the appeal period shall be counted from the date of communication.
5. Savings.—Every appeal already filed under the unamended section shall continue and shall be decided under the law as it stood before this amendment came into force.
"""
SAVINGS = "Every appeal already filed under the unamended section shall continue"


def test_line_start_act_citation_with_pipe_label_does_not_drop_later_sections():
    extracted = extract_statute_deterministic(
        html=None,
        text=AMENDMENT_CITING_EARLIER_ACT,
        source_meta={
            "source_name": "Senate",
            "url": "https://senate.gov.pk/uploads/documents/amendment.pdf|XII of 1980",
            "act_title": "The Service Tribunals (Amendment) Act, 1980",
        },
    )
    blob = _blob(extracted)
    assert SAVINGS in blob
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]
    assert "person authorised by him" in blob


def test_line_start_act_citation_without_pipe_or_title_is_not_quarantined():
    extracted = extract_statute_deterministic(
        html=None,
        text=AMENDMENT_CITING_EARLIER_ACT,
        source_meta={
            "source_name": "Senate",
            "url": "https://senate.gov.pk/uploads/documents/amendment.pdf",
        },
    )
    assert extracted["sections"]
    assert SAVINGS in _blob(extracted)
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]


def test_next_line_in_the_citation_is_not_an_act_boundary():
    text = AMENDMENT_CITING_EARLIER_ACT.replace(
        "Act No. LXX of 1973.- In the Service Tribunals Act, 1973, in section 4, for the word thirty the word sixty shall be substituted and the appeal period shall be counted from the date of communication.",
        "Act No. LXX of 1973\nIn the Service Tribunals Act, 1973, in section 4, for the word thirty the word sixty shall be substituted and the appeal period shall be counted from the date of communication.",
    )
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "Senate",
            "url": "https://senate.gov.pk/uploads/documents/amendment.pdf|XII of 1980",
            "act_title": "The Service Tribunals (Amendment) Act, 1980",
        },
    )
    assert SAVINGS in _blob(extracted)
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]


WRAPPED_TITLE_BUNDLE = """
ACT No. LXVII of 1973
An Act to amend the Life Insurance (Nationalisation) Order, 1972.
1. Short title and commencement.—(1) This Act may be called
the Life Insurance (Nationalisation) (Amendment) Act, 1973.
2. General amendment.—In the Life Insurance (Nationalisation) Order, 1972, for the words Central Government the words Federal Government shall be substituted wherever they occur in that Order.

ACT No. LXX of 1973
An Act to provide for the establishment of Service Tribunals to exercise jurisdiction in respect of matters relating to the terms and conditions of service of civil servants.
1. Short title, commencement and application.—(1) This Act may be called
the Service Tribunals Act, 1973.
2. Definitions.—In this Act, unless there is anything repugnant in the subject or context, civil servant means a person who is or has been a civil servant within the meaning of the Civil Servants Act, 1973.
9. Repeal.—The Service Tribunals Ordinance, 1973, is hereby repealed.

ACT No. LXXI of 1973
An Act to regulate the appointment of persons to, and the terms and conditions of service of persons in, the service of Pakistan.
1. Short title, application and commencement.—(1) This Act may be called
the Civil Servants Act, 1973.
2. Definitions.—In this Act, unless there is anything repugnant, civil servant means a person who is a member of a civil service of the Federation and is recorded on the federal establishment roll.
"""


def test_shall_be_called_title_wrapped_mid_name_selects_the_middle_act_without_a_pipe():
    text = """
    ACT No. LXVII of 1973
    An Act to amend the Life Insurance (Nationalisation) Order, 1972.
    1. Short title.—This Act shall be called the Life Insurance (Nationalisation) (Amendment) Act, 1973.
    2. General amendment.—In the Life Insurance (Nationalisation) Order, 1972, for the words Central Government the words Federal Government shall be substituted wherever they occur in that Order.

    ACT No. LXX of 1973
    An Act to provide for the establishment of Service Tribunals.
    1. Short title.—This Act shall be called the Service
    Tribunals Act, 1973.
    2. Definitions.—In this Act Tribunal means a Service Tribunal established under this Act to hear appeals of civil servants from the departments of the Federation.
    9. Repeal.—The Service Tribunals Ordinance, 1973, is hereby repealed.

    ACT No. LXXI of 1973
    An Act to regulate the service of Pakistan.
    1. Short title.—This Act shall be called the Civil Servants Act, 1973.
    2. Definitions.—In this Act civil servant means a person who is a member of a civil service of the Federation and is recorded on the federal establishment roll.
    """
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={"source_name": "Senate", "url": PDF_URL, "act_title": SERVICE_TRIBUNALS},
    )
    blob = _blob(extracted)
    assert "Service Tribunals Ordinance, 1973" in blob
    assert "Federal Government shall be substituted" not in blob
    assert "federal establishment roll" not in blob
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]


def test_wrapped_short_title_selects_the_middle_act_without_a_pipe_label():
    """The real Service Tribunals short title breaks after 'may be called'."""
    extracted = extract_statute_deterministic(
        html=None,
        text=WRAPPED_TITLE_BUNDLE,
        source_meta={
            "source_name": "Senate",
            "url": PDF_URL,
            "act_title": SERVICE_TRIBUNALS,
        },
    )
    blob = _blob(extracted)
    assert "Service Tribunals Ordinance, 1973" in blob
    assert "Federal Government shall be substituted" not in blob
    assert "federal establishment roll" not in blob
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]


def test_scanned_act_number_and_year_on_the_next_line_are_three_boundaries():
    """A scan may wrap the year, drop 'of', or print r97J for 1973."""
    from scraper.statute_quality.senate_integrity import act_segments

    text = """
ACT No. LXVII oF r97J
An Act to amend the Life Insurance (Nationalisation) Order, 1972.
1. Short title.—This Act may be called the Life Insurance (Nationalisation) (Amendment) Act, 1973.
2. Amendment.—In the Life Insurance (Nationalisation) Order, 1972, for the words Central Government the words Federal Government shall be substituted.

ACT No LXX oF
1973
An Act to provide for the establishment of Service Tribunals.
1. Short title.—This Act may be called the Service Tribunals Act, 1973.
2. Definitions.—Tribunal means a Service Tribunal established under this Act.

ACT No. LXXI
1973
An Act to regulate the service of Pakistan.
1. Short title.—This Act may be called the Civil Servants Act, 1973.
2. Definitions.—Civil servant means a member of a civil service of the Federation.
"""
    segments = act_segments(text)
    assert [segment.act_number for segment in segments] == [(67, 1973), (70, 1973), (71, 1973)]


def test_garbled_year_uses_the_neighbouring_heading_or_the_pipe():
    """An unreadable year token takes the year the other headings, or the pipe, already have."""
    from scraper.statute_quality.senate_integrity import act_segments

    text = """
ACT No. LXVII oF qqqq
An Act to amend the Life Insurance (Nationalisation) Order, 1972.
1. Short title.—This Act may be called the Life Insurance (Nationalisation) (Amendment) Act, 1973.

ACT No. LXX of 1973
An Act to provide for the establishment of Service Tribunals.
1. Short title.—This Act may be called the Service Tribunals Act, 1973.

ACT No. LXXI of qqqq
An Act to regulate the service of Pakistan.
1. Short title.—This Act may be called the Civil Servants Act, 1973.
"""
    segments = act_segments(text)
    assert [segment.act_number for segment in segments] == [(67, 1973), (70, 1973), (71, 1973)]

    only_pipe = """
ACT No. LXVII oF qqqq
An Act to amend the Life Insurance (Nationalisation) Order, 1972.
1. Short title.—This Act may be called the Life Insurance (Nationalisation) (Amendment) Act, 1973.

ACT No. LXX oF qqqq
An Act to provide for the establishment of Service Tribunals.
1. Short title.—This Act may be called the Service Tribunals Act, 1973.
2. Definitions.—Tribunal means a Service Tribunal established under this Act to hear appeals.
"""
    from scraper.statute_quality.bundled_gazette import isolate_bundled_act

    isolation = isolate_bundled_act(only_pipe, target_name=SERVICE_TRIBUNALS, source_url=PIPE_URL)
    assert isolation.status == "isolated"
    assert isolation.act_label == "LXX of 1973"
    assert isolation.act_year == 1973


def test_real_service_tribunals_scan_isolates_lxx_and_flags_act_bleed():
    from pathlib import Path

    from scraper.statute_quality.bundled_gazette import (
        REASON_BLEED,
        VERDICT_MISMATCH,
        classify_bundled_gazette,
        isolate_bundled_act,
    )

    text = Path("tests/fixtures/statute_quality/senate_1660540467_508.txt").read_text()
    isolation = isolate_bundled_act(text, target_name=SERVICE_TRIBUNALS, source_url=PIPE_URL)
    assert isolation.status == "isolated"
    assert isolation.act_label == "LXX of 1973"
    assert "Life Insurance" not in isolation.text
    assert "Insurancc" not in isolation.text

    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={"source_name": "Senate", "url": PIPE_URL, "act_title": SERVICE_TRIBUNALS},
    )
    blob = _blob(extracted)
    assert "Life Insurance" not in blob
    assert "Insurancc" not in blob
    assert "Federal Government shall be substituted" not in blob
    assert "59" not in _numbers(extracted)
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]
    assert extracted["sections"]

    def _slice(marker: str) -> str:
        at = text.index(marker)
        return text[at : at + 280]

    mixed = classify_bundled_gazette(
        stored_name=SERVICE_TRIBUNALS,
        source_url=PIPE_URL,
        sections=[
            {"section_number": "2", "section_text": _slice("Life Insurance (Nationalisation) Order")},
            {"section_number": "9", "section_text": _slice("Service Tribunals Ordinance")},
            {"section_number": "1", "section_text": _slice("the Civil Servants Act, 1973")},
        ],
        pdf_text=text,
        pdf_available=True,
    )
    assert mixed.verdict == VERDICT_MISMATCH
    assert mixed.reason == REASON_BLEED


def test_conciliation_courts_fixture_still_cuts_at_the_later_act_heading():
    """MAIN 86215821 is a title-heading bleed, not an ACT No. bundle."""
    from scraper.statute_quality.bundled_gazette import isolate_bundled_act
    from scraper.statute_quality.gok_multi_act_bleed import (
        REASON_BLEED,
        VERDICT_MISMATCH,
        classify_gok_multi_act,
        truncate_at_second_act_heading,
    )
    from tests.test_gok_multi_act_bleed import (
        CONCILIATION_NAME,
        CONCILIATION_URL,
        TWO_ACT_PDF,
        _mirpur_bleed_sections,
    )

    isolation = isolate_bundled_act(
        TWO_ACT_PDF,
        target_name=CONCILIATION_NAME,
        source_url=CONCILIATION_URL,
    )
    assert isolation.status == "single"
    assert "Headquarters of the Authority" in isolation.text
    cut = truncate_at_second_act_heading(isolation.text, CONCILIATION_NAME)
    assert "Conciliation Court" in cut
    assert "Headquarters of the Authority" not in cut
    extracted = extract_statute_deterministic(
        html=None,
        text=TWO_ACT_PDF,
        source_meta={
            "source_name": "AJKAssembly",
            "url": CONCILIATION_URL.split("|", 1)[0],
            "act_title": CONCILIATION_NAME,
        },
    )
    blob = _blob(extracted)
    assert "Conciliation Court" in blob
    assert "Headquarters of the Authority" not in blob
    verdict = classify_gok_multi_act(
        stored_name=CONCILIATION_NAME,
        source_url=CONCILIATION_URL,
        sections=_mirpur_bleed_sections(),
    )
    assert verdict.verdict == VERDICT_MISMATCH
    assert verdict.reason == REASON_BLEED


def test_single_act_pdf_is_unchanged():
    text = """
    ACT No. LXX of 1973
    1. Short title.—This Act may be called the Service Tribunals Act, 1973.
    2. Definitions.—In this Act Tribunal means a Service Tribunal established under this Act to hear appeals of civil servants.
    """
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={"source_name": "Senate", "url": PIPE_URL, "act_title": SERVICE_TRIBUNALS},
    )
    assert TRIBUNALS_S2.split(" to hear")[0] in _blob(extracted)
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]


def test_classify_mixed_sections_as_bleed_and_a_clean_middle_act_as_match():
    from scraper.statute_quality.bundled_gazette import (
        REASON_BLEED,
        VERDICT_MATCH,
        VERDICT_MISMATCH,
        classify_bundled_gazette,
    )

    mixed = classify_bundled_gazette(
        stored_name=SERVICE_TRIBUNALS,
        source_url=PIPE_URL,
        sections=[
            {"section_number": "2", "section_text": "2. Amendment of section 4.—" + LIFE_INSURANCE_S2},
            {"section_number": "2", "section_text": "2. Definitions.—" + TRIBUNALS_S2},
            {"section_number": "9", "section_text": "9. Repeal.—" + CURRICULA_S9},
        ],
        pdf_text=BUNDLE,
        pdf_available=True,
    )
    assert mixed.verdict == VERDICT_MISMATCH
    assert mixed.reason == REASON_BLEED
    assert mixed.action == "quarantine"

    clean = classify_bundled_gazette(
        stored_name=SERVICE_TRIBUNALS,
        source_url=PIPE_URL,
        sections=[
            {"section_number": "2", "section_text": "2. Definitions.—" + TRIBUNALS_S2},
            {
                "section_number": "4",
                "section_text": (
                    "4. Appeals.—A civil servant may prefer an appeal to the Tribunal within "
                    "thirty days of the final order affecting the terms of service."
                ),
            },
        ],
        pdf_text=BUNDLE,
        pdf_available=True,
    )
    assert clean.verdict == VERDICT_MATCH
    assert clean.reason is None


def test_apply_flag_defaults_off():
    from scraper.tasks.bundled_gazette_acts import build_parser

    args = build_parser().parse_args([])
    assert args.apply is False
    assert args.undo is None
    assert args.skip_fetch is False


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _add_statute(db, *, name: str, url: str, source_name: str, sections: list[dict]):
    statute = Statute(
        name=name,
        jurisdiction="Federal" if source_name == "Senate" else "AJK",
        source_name=source_name,
        source_url=url,
        year_enacted=1973,
        needs_section_refetch=False,
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


@pytest.mark.asyncio
async def test_dry_run_reports_the_mixed_id_and_writes_nothing(db):
    from scraper.tasks.bundled_gazette_acts import run_bundled_gazette_acts

    mixed = await _add_statute(
        db,
        name=SERVICE_TRIBUNALS,
        url=PIPE_URL,
        source_name="Senate",
        sections=[
            {"section_number": "2", "section_text": "2. Amendment of section 4.—" + LIFE_INSURANCE_S2},
            {"section_number": "6", "section_text": "6. Decision.—The Tribunal shall decide the appeal within six months and record reasons in writing for every final order."},
            {"section_number": "9", "section_text": "9. Repeal.—" + CURRICULA_S9},
        ],
    )
    report = await run_bundled_gazette_acts(
        apply=False,
        fetch_pdf=lambda url: _async_pdf(),
        extract_text=lambda _data: BUNDLE,
    )
    assert report["dry_run"] is True
    assert report["applied"] is None
    row = next(item for item in report["rows"] if item["statute_id"] == str(mixed.id))
    assert row["verdict"] == "MISMATCH"
    assert row["reason"] == "bundled_gazette_act_bleed"
    assert (await db.execute(select(func.count()).select_from(DocumentQuarantine))).scalar() == 0
    await db.refresh(mixed)
    assert mixed.needs_section_refetch is False


async def _async_pdf():
    return b"%PDF-1.4\nbundled"


@pytest.mark.asyncio
async def test_apply_quarantines_once_and_undo_removes_only_that_reason(db):
    from scraper.statute_quality.bundled_gazette import REASON_BLEED
    from scraper.tasks.bundled_gazette_acts import run_bundled_gazette_acts

    mixed = await _add_statute(
        db,
        name=SERVICE_TRIBUNALS + " row",
        url=PIPE_URL,
        source_name="Senate",
        sections=[
            {"section_number": "2", "section_text": "2. Amendment of section 4.—" + LIFE_INSURANCE_S2},
            {"section_number": "3", "section_text": "3. Establishment of Tribunals.—The President may establish one or more Service Tribunals and each Tribunal shall have exclusive jurisdiction over service matters of civil servants."},
            {"section_number": "9", "section_text": "9. Repeal.—" + CURRICULA_S9},
        ],
    )
    original = (
        await db.execute(
            select(StatuteSectionVersion.section_text)
            .join(StatuteSection, StatuteSection.current_version_id == StatuteSectionVersion.id)
            .where(StatuteSection.statute_id == mixed.id, StatuteSection.section_number == "2")
        )
    ).scalar_one()
    first = await run_bundled_gazette_acts(
        apply=True,
        fetch_pdf=lambda url: _async_pdf(),
        extract_text=lambda _data: BUNDLE,
    )
    assert first["dry_run"] is False
    run_id = first["applied"]["run_id"]
    assert first["applied"]["quarantined"] == 1
    held = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == mixed.id))
    ).scalars().one()
    assert held.reason == REASON_BLEED
    assert held.run_id == run_id
    second = await run_bundled_gazette_acts(
        apply=True,
        fetch_pdf=lambda url: _async_pdf(),
        extract_text=lambda _data: BUNDLE,
        include_quarantined=True,
    )
    assert second["applied"]["quarantined"] == 0
    assert (
        await db.execute(select(func.count()).select_from(DocumentQuarantine).where(DocumentQuarantine.doc_id == mixed.id))
    ).scalar() == 1
    still = (
        await db.execute(
            select(StatuteSectionVersion.section_text)
            .join(StatuteSection, StatuteSection.current_version_id == StatuteSectionVersion.id)
            .where(StatuteSection.statute_id == mixed.id, StatuteSection.section_number == "2")
        )
    ).scalar_one()
    assert still == original
    await db.refresh(mixed)
    assert mixed.needs_section_refetch is False
    undone = await run_bundled_gazette_acts(undo=run_id)
    assert undone["undone"]["quarantine_rows_removed"] == 1
    assert (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == mixed.id))
    ).scalars().first() is None


@pytest.mark.asyncio
async def test_unresolved_bundle_quarantines_staging_without_marking_refetch(db):
    statute = Statute(name=SERVICE_TRIBUNALS, jurisdiction="Federal", needs_section_refetch=False)
    db.add(statute)
    await db.flush()
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "Senate"))).scalars().first()
    assert source is not None
    prov = await record_provenance(
        db,
        source=source,
        url=PDF_URL,
        content=b"bundle",
        content_kind="text",
    )
    staging = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=BUNDLE,
        url=PDF_URL,
        kind="statute",
    )
    staging.status = "extracted"
    staging.reconciled_json = {
        "statute_name": SERVICE_TRIBUNALS,
        "year_enacted": 1973,
        "jurisdiction": "Federal",
        "sections": [
            {"section_number": "2", "section_title": "Amendment", "section_text": LIFE_INSURANCE_S2},
        ],
        "field_evidence": {"bundled_gazette_unresolved": "3 ACT No. boundaries"},
    }
    assert await promote_statute_staging(db, staging) == "quarantined"
    assert "bundled_gazette_unresolved" in (staging.quarantine_reason or "")
    await db.refresh(statute)
    assert statute.needs_section_refetch is False
    assert (
        await db.execute(select(func.count()).select_from(StatuteSection).where(StatuteSection.statute_id == statute.id))
    ).scalar() == 0
