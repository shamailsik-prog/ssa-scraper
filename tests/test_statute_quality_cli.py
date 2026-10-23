"""Statute quality CLI: dedupe, jurisdiction, re-quarantine (regression shapes dea868f2, 7334221a, b4e0f316, d5ba6a26)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.models import DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.gazette_header import extract_gazette_title_year
from scraper.statute_quality.ocr_quality import statute_text_poor_ocr
from scraper.statute_quality.section_noise import filter_statute_quality_sections, is_bogus_section_number_token
from scraper.tasks.statute_quality import run_statute_quality

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statute_quality"


async def _add_section(db, statute_id, num: str, text: str) -> None:
    sec = StatuteSection(statute_id=statute_id, section_number=num)
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(
        section_id=sec.id,
        version_no=1,
        section_text=text,
        text_hash=f"{hash(text) & 0xffffffff:08x}" * 8,
    )
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    await db.flush()


@pytest.mark.asyncio
async def test_fixture_ocr_dea868f2_detects_garbled_text():
    body = (FIXTURES / "ocr_dea868f2.txt").read_text(encoding="utf-8")
    issues = statute_text_poor_ocr(name="The Punjab Special Act, 2019", section_texts=[body])
    assert "mojibake" in issues or "high_nonword_ratio" in issues


@pytest.mark.asyncio
async def test_fixture_sections_7334221a_rejects_1n_and_page_number():
    raw = (FIXTURES / "sections_7334221a.txt").read_text(encoding="utf-8")
    assert is_bogus_section_number_token("1N")
    sections = [
        {"section_number": "1", "section_text": "Section 1. Short title."},
        {"section_number": "1N", "section_text": "Section 1N Name of Act"},
        {"section_number": "2", "section_text": "42"},
        {"section_number": "3", "section_text": "Section 2. Definitions.—meaning of words"},
    ]
    out = filter_statute_quality_sections(sections)
    nums = {s["section_number"] for s in out}
    assert "1N" not in nums
    assert "3" in nums


@pytest.mark.asyncio
async def test_fixture_gazette_senate_b4e0f316_extracts_title_year():
    text = (FIXTURES / "gazette_senate_b4e0f316.txt").read_text(encoding="utf-8")
    got = extract_gazette_title_year(text, source_name="Senate")
    assert got is not None
    title, year = got
    assert "national library" in title.lower()
    assert year == 2024


@pytest.mark.asyncio
async def test_dedupe_d5ba6a26_shape_keeps_best_statute(db, monkeypatch):
    """Two visible statutes, same normalized title+year+jurisdiction → quarantine the weaker."""
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_DEDUPE", True)
    a_id = uuid4()
    b_id = uuid4()
    db.add(
        Statute(
            id=a_id,
            name="The Sample Duplication Act, 2020",
            canonical_name_key="the sample duplication act, 2020",
            jurisdiction="Federal",
            year_enacted=2020,
            source_name="Senate",
            source_url="https://example.test/a",
        )
    )
    db.add(
        Statute(
            id=b_id,
            name="THE SAMPLE DUPLICATION ACT, 2020",
            canonical_name_key="the sample duplication act, 2020",
            jurisdiction="Federal",
            year_enacted=2020,
            source_name="Senate",
        )
    )
    await db.commit()
    await _add_section(db, a_id, "1", "Section 1. " + ("x" * 120))
    await _add_section(db, a_id, "2", "Section 2. " + ("y" * 120))
    await _add_section(db, b_id, "1", "Section 1. short")
    await db.commit()

    dry = await run_statute_quality(apply=False)
    assert dry["dedupe"]["duplicate_groups"] == 1
    assert dry["dedupe"]["statutes_to_quarantine"] == 1

    applied = await run_statute_quality(apply=True)
    run_id = applied["applied"]["run_id"]
    q = (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.run_id == run_id,
                DocumentQuarantine.reason == "statute_quality:duplicate_title_year",
            )
        )
    ).scalars().all()
    assert len(q) == 1
    assert q[0].doc_id == b_id

    undone = await run_statute_quality(undo=run_id)
    assert undone["undone"]["quarantine_rows_removed"] == 1


@pytest.mark.asyncio
async def test_jurisdiction_fix_assembly_federal_to_province(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_JURISDICTION", True)
    sid = uuid4()
    db.add(
        Statute(
            id=sid,
            name="The KP Example Act, 2018",
            jurisdiction="Federal",
            year_enacted=2018,
            source_name="KPAssembly",
            source_url="https://pakp.gov.pk/example",
        )
    )
    await db.commit()

    dry = await run_statute_quality(apply=False)
    assert dry["jurisdiction"]["federal_mislabeled"] == 1

    applied = await run_statute_quality(apply=True)
    run_id = applied["applied"]["run_id"]
    st = (await db.execute(select(Statute).where(Statute.id == sid))).scalars().first()
    assert st.jurisdiction == "Khyber Pakhtunkhwa"

    await run_statute_quality(undo=run_id)
    db.expire_all()
    st2 = (await db.execute(select(Statute).where(Statute.id == sid))).scalars().first()
    assert st2.jurisdiction == "Federal"
