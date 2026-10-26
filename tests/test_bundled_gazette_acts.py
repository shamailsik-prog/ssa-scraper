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
