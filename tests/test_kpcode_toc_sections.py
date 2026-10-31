"""kpcode lawDetails pages must store operative section text, not the contents list."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.fetchers import record_provenance, stage_statute
from scraper.models import QuarantineQueue, ScraperSource, Statute
from scraper.parsers.kpcode_sections import (
    kpcode_sections_are_toc_stubs,
    parse_kpcode_detail_html,
)
from scraper.tasks.promotion import promote_statute_staging

FIXTURE = Path(__file__).parent / "fixtures" / "kpcode_lawdetails_699_style.html"
DETAIL_URL = "https://kpcode.kp.gov.pk/homepage/lawDetails/699"
ACT_TITLE = "The Khyber Pakhtunkhwa Preemption Act, 1987"

TOC_ONLY_HTML = """
<html><body><div id="content">
<p>CONTENTS</p>
<p>5. Right of preemption.</p>
<p>6. Persons in whom the right of preemption vests.</p>
<p>7. Priorities in the right of preemption.</p>
</div></body></html>
"""


def _section(sections, number):
    return next(s for s in sections if str(s.get("section_number")) == number)


def test_lawdetails_fixture_joins_heading_to_operative_body():
    html = FIXTURE.read_text(encoding="utf-8")
    sections = parse_kpcode_detail_html(html, statute_name=ACT_TITLE)
    assert sections
    assert not kpcode_sections_are_toc_stubs(sections)
    section_five = _section(sections, "5")
    text = " ".join(section_five["section_text"].split())
    assert "shall arise in case of sale" in text
    assert text != "5. Right of preemption."
    assert "Received" not in section_five["section_text"]
    assert "received the assent" in _section(sections, "1")["section_text"].lower()


def test_toc_only_kpcode_html_is_a_stub_list():
    sections = parse_kpcode_detail_html(TOC_ONLY_HTML, statute_name=ACT_TITLE)
    assert len(sections) >= 3
    assert kpcode_sections_are_toc_stubs(sections)
    assert all(len(" ".join(s["section_text"].split())) < 120 for s in sections)


def test_kpcode_and_pakp_jurisdiction_is_khyber_pakhtunkhwa_not_federal():
    body = (
        "1. Short title.- This Act may be called the Example Provincial Act, 2020 and it extends to the Province.\n"
        "2. Definitions.- In this Act the words in this section have the meanings assigned for the Province.\n"
        "3. Extent.- This Act extends to the whole of the Province and comes into force at once on publication."
    )
    kpcode = extract_statute_deterministic(
        html=None,
        text=body,
        source_meta={
            "source_name": "KPAssembly",
            "url": "https://kpcode.kp.gov.pk/uploads/example-act.pdf",
            "detail_url": DETAIL_URL,
            "act_title": "Example Provincial Act, 2020",
        },
    )
    pakp = extract_statute_deterministic(
        html=None,
        text=body,
        source_meta={
            "source_name": "KPAssembly",
            "url": "https://pakp.gov.pk/wp-content/uploads/example-act.pdf",
            "act_title": "Example Provincial Act, 2020",
            "jurisdiction": "Federal",
        },
    )
    federal = extract_statute_deterministic(
        html=None,
        text=body,
        source_meta={
            "source_name": "PakistanCode",
            "url": "https://pakistancode.gov.pk/english/example",
            "act_title": "Example Provincial Act, 2020",
        },
    )
    assert kpcode["jurisdiction"] == "Khyber Pakhtunkhwa"
    assert pakp["jurisdiction"] == "Khyber Pakhtunkhwa"
    assert federal["jurisdiction"] == "Federal"
    assert all(s["jurisdiction"] == "Khyber Pakhtunkhwa" for s in kpcode["sections"])


def test_thin_html_falls_back_to_linked_pdf_body():
    pdf_text = """
CONTENTS
5. Right of preemption.
6. Persons in whom the right of preemption vests.
7. Priorities in the right of preemption.
5. (1) The right of pre-emption shall arise in case of sale of immovable property in the Province.
(2) Nothing contained in sub-section (1) shall prevent a court from holding that an alienation purporting to be other than a sale is in fact a sale.
6. The right of pre-emption shall vest firstly in shafi-sharik and secondly in shafi-khalit when no person in the prior class claims.
7. Where all the classes of pre-emptors claim together the prior class excludes the later class from the property sold.
"""
    parsed = extract_statute_deterministic(
        html=TOC_ONLY_HTML,
        text=pdf_text,
        source_meta={
            "source_name": "KPAssembly",
            "url": DETAIL_URL,
            "act_title": ACT_TITLE,
        },
    )
    assert "shall arise in case of sale" in _section(parsed["sections"], "5")["section_text"]
    assert not kpcode_sections_are_toc_stubs(parsed["sections"])
    assert parsed["jurisdiction"] == "Khyber Pakhtunkhwa"


def test_extract_kpcode_html_keeps_body_and_rejects_toc_only_shape():
    html = FIXTURE.read_text(encoding="utf-8")
    parsed = extract_statute_deterministic(
        html=html,
        text=None,
        source_meta={
            "source_name": "KPAssembly",
            "url": DETAIL_URL,
            "act_title": ACT_TITLE,
        },
    )
    section_five = _section(parsed["sections"], "5")
    assert "shall arise in case of sale" in section_five["section_text"]
    assert parsed["jurisdiction"] == "Khyber Pakhtunkhwa"

    stubs = extract_statute_deterministic(
        html=TOC_ONLY_HTML,
        text=None,
        source_meta={
            "source_name": "KPAssembly",
            "url": DETAIL_URL,
            "act_title": ACT_TITLE,
        },
    )
    assert kpcode_sections_are_toc_stubs(stubs["sections"])


@pytest.mark.asyncio
async def test_promotion_quarantines_kpcode_toc_stubs_before_upsert(db):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "KPAssembly"))
    ).scalars().first()
    assert source is not None
    prov = await record_provenance(
        db,
        source=source,
        url=DETAIL_URL,
        content=b"toc-only",
        content_kind="html",
    )
    staging = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=TOC_ONLY_HTML,
        raw_text="5. Right of preemption.",
        url=DETAIL_URL,
        kind="statute",
    )
    staging.status = "extracted"
    staging.reconciled_json = {
        "statute_name": ACT_TITLE,
        "year_enacted": 1987,
        "jurisdiction": "Federal",
        "sections": [
            {"section_number": "5", "section_title": "Right of preemption", "section_text": "5. Right of preemption."},
            {"section_number": "6", "section_title": "Persons", "section_text": "6. Persons in whom the right of preemption vests."},
            {"section_number": "7", "section_title": "Priorities", "section_text": "7. Priorities in the right of preemption."},
        ],
    }
    assert await promote_statute_staging(db, staging) == "quarantined"
    assert "kpcode_toc_stub" in (staging.quarantine_reason or "")
    assert (await db.execute(select(func.count()).select_from(Statute).where(Statute.name == ACT_TITLE))).scalar() == 0
    details = (
        await db.execute(select(QuarantineQueue).where(QuarantineQueue.statutes_staging_id == staging.id))
    ).scalars().one()
    assert (details.details or {}).get("reason_code") == "kpcode_toc_stub"
