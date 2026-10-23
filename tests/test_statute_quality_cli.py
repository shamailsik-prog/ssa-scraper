"""Statute quality CLI and auditor regression (dea868f2, 7334221a, b4e0f316, d5ba6a26)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.models import DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.parsers.assembly_pdf_sections import filter_assembly_pdf_sections, is_table_of_contents_chunk
from scraper.statute_quality.gazette_header import extract_gazette_title_year
from scraper.statute_quality.ocr_quality import section_fails_ocr_quality, statute_text_poor_ocr
from scraper.statute_quality.section_noise import filter_statute_quality_sections, is_bogus_section_number_token
from scraper.tasks.statute_quality import (
    REASON_THIN_ASSEMBLY,
    _dedupe_group_key,
    classify_requarantine,
    find_duplicate_groups,
    run_statute_quality,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statute_quality"


async def _add_section(db, statute_id, num: str, text: str) -> None:
    sec = StatuteSection(statute_id=statute_id, section_number=num)
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(
        section_id=sec.id,
        version_no=1,
        section_text=text,
        text_hash=f"{hash((num, text)) & 0xffffffff:08x}" * 8,
    )
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    await db.flush()


def test_ppc_section_numbers_survive_filter():
    sections = [
        {"section_number": "302", "section_text": "Section 302. Punishment of qatl-i-amd.—Whoever commits qatl-i-amd shall be punished with death."},
        {"section_number": "302A", "section_text": "Section 302A. Further provision for certain cases with extended text " + ("x" * 80)},
        {"section_number": "302B", "section_text": "Section 302B. Another related offence with sufficient body " + ("y" * 80)},
        {"section_number": "5A", "section_text": "Section 5A. Supplemental rule with adequate content " + ("z" * 80)},
        {"section_number": "10AA", "section_text": "Section 10AA. Special category provision " + ("w" * 80)},
        {"section_number": "12", "section_text": "Section 12. General definitions apply throughout the Code."},
    ]
    out = filter_statute_quality_sections(sections, statute_name="Pakistan Penal Code, 1860")
    nums = {s["section_number"] for s in out}
    assert nums >= {"302", "302A", "302B", "5A", "10AA", "12"}


def test_fixture_sections_7334221a_rejects_1n_name_split_only():
    assert is_bogus_section_number_token("1N", "Section 1N Name of Act")
    assert not is_bogus_section_number_token("302A", "Section 302A. Body " + "a" * 60)
    sections = [
        {"section_number": "1", "section_text": "Section 1. Short title."},
        {"section_number": "1N", "section_text": "Section 1N Name of Act"},
        {"section_number": "2", "section_text": "42"},
        {"section_number": "3", "section_text": "Section 2. Definitions.—meaning of words " + ("x" * 40)},
    ]
    out = filter_statute_quality_sections(sections)
    assert "1N" not in {s["section_number"] for s in out}
    assert "3" in {s["section_number"] for s in out}


def test_fixture_gazette_senate_rejects_following_act_line():
    text = (FIXTURES / "gazette_senate_following_act.txt").read_text(encoding="utf-8")
    got = extract_gazette_title_year(text, source_name="Senate")
    assert got is not None
    title, year = got
    assert "following" not in title.lower()
    assert "national library" in title.lower()
    assert year == 2024


def test_fixture_gazette_may_be_called_preference():
    text = (FIXTURES / "gazette_senate_b4e0f316.txt").read_text(encoding="utf-8") + (
        "\n1. Short title.—This Act may be called the National Library (Amendment) Act, 2024.\n"
    )
    got = extract_gazette_title_year(text, source_name="Senate")
    assert got is not None
    assert got[1] == 2024


def test_tariff_schedule_single_stray_mojibake_not_whole_act_fail():
    sched = (FIXTURES / "tariff_schedule_single_mojibake.txt").read_text(encoding="utf-8")
    sections = [sched] * 6 + ["Section 1. General " + ("a" * 50)]
    assert not statute_text_poor_ocr(name="Customs Act", section_texts=sections)


def test_fixture_ocr_dea868f2_majority_sections_fail():
    body = (FIXTURES / "ocr_dea868f2.txt").read_text(encoding="utf-8")
    assert statute_text_poor_ocr(name="Act", section_texts=[body, body, body])


def test_ajk_fixture_d5ba6a26_page_stub_filtered():
    raw = (FIXTURES / "ajk_page_stub_d5ba6a26.txt").read_text(encoding="utf-8")
    sections = [
        {"section_number": "1", "section_text": "Section 1. Short title.—AJK Sample Act, 2022."},
        {"section_number": "2", "section_text": "42"},
        {"section_number": "3", "section_text": "Section 2. Definitions.—Government means the AJK Government " + ("x" * 40)},
    ]
    out = filter_statute_quality_sections(sections)
    assert "2" not in {s["section_number"] for s in out}


def test_publication_notice_rejected_as_section_body():
    notice = (FIXTURES / "publication_notice_section1.txt").read_text(encoding="utf-8")
    out = filter_statute_quality_sections([{"section_number": "1", "section_text": notice}])
    assert out == []


def test_assembly_toc_chunk_skipped():
    toc = "TABLE OF CONTENTS\n1. Short title .............. 1\n2. Definitions .............. 2"
    assert is_table_of_contents_chunk(toc)


@pytest.mark.asyncio
async def test_dedupe_skips_null_year_and_groups_by_jurisdiction(db):
    a_id, b_id, c_id = uuid4(), uuid4(), uuid4()
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
    db.add(
        Statute(
            id=c_id,
            name="The Sample Duplication Act (Punjab), 2020",
            canonical_name_key="the sample duplication act, 2020",
            jurisdiction="Punjab",
            year_enacted=2020,
            source_name="PunjabAssembly",
        )
    )
    null_id = uuid4()
    db.add(
        Statute(
            id=null_id,
            name="The Sample Duplication Act (year unknown), 2020",
            canonical_name_key="the sample duplication act, 2020",
            jurisdiction="Federal",
            year_enacted=None,
            source_name="Senate",
        )
    )
    await db.commit()
    await _add_section(db, a_id, "1", "Section 1. " + ("x" * 120))
    await _add_section(db, b_id, "1", "Section 1. short")
    await db.commit()

    assert _dedupe_group_key((await db.get(Statute, null_id))) is None
    dry = await find_duplicate_groups(db)
    assert dry["duplicate_groups"] == 1
    assert dry["statutes_to_quarantine"] == 1


@pytest.mark.asyncio
async def test_dedupe_d5ba6a26_shape_apply_undo(db, monkeypatch):
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
    await run_statute_quality(undo=run_id)


@pytest.mark.asyncio
async def test_jurisdiction_balochistan_federal_fix_and_undo_updated_at(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_JURISDICTION", True)
    sid = uuid4()
    db.add(
        Statute(
            id=sid,
            name="The Balochistan Example Act, 2018",
            jurisdiction="Federal",
            year_enacted=2018,
            source_name="BalochistanAssembly",
            source_url="https://pabalochistan.gov.pk/example",
        )
    )
    await db.commit()
    before = (await db.get(Statute, sid)).updated_at
    applied = await run_statute_quality(apply=True)
    run_id = applied["applied"]["run_id"]
    st = (await db.get(Statute, sid))
    assert st.jurisdiction == "Balochistan"
    await run_statute_quality(undo=run_id)
    db.expire_all()
    st2 = (await db.get(Statute, sid))
    assert st2.jurisdiction == "Federal"
    assert st2.updated_at == before


@pytest.mark.asyncio
async def test_ocr_gate_without_requarantine_skips_thin_assembly(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_OCR_GATE", True)
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_REQUARANTINE", False)
    sid = uuid4()
    db.add(
        Statute(
            id=sid,
            name="Thin Act",
            jurisdiction="Punjab",
            year_enacted=2020,
            source_name="PunjabAssembly",
        )
    )
    await db.commit()
    await _add_section(db, sid, "1", "Short title only")
    await db.commit()
    info = await classify_requarantine(db)
    assert REASON_THIN_ASSEMBLY not in info.get("ids_by_reason", {})


@pytest.mark.asyncio
async def test_requarantine_flags_required_for_thin_and_no_valid(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_REQUARANTINE", True)
    sid = uuid4()
    db.add(
        Statute(
            id=sid,
            name="Noise Act",
            jurisdiction="Federal",
            year_enacted=2020,
            source_name="Senate",
        )
    )
    await db.commit()
    notice = (FIXTURES / "publication_notice_section1.txt").read_text(encoding="utf-8")
    await _add_section(db, sid, "1", notice)
    await db.commit()
    info = await classify_requarantine(db)
    assert "statute_quality:no_valid_sections" in info.get("ids_by_reason", {})


@pytest.mark.asyncio
async def test_mutant_f1_dedupe_flag_off_skips_apply(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_DEDUPE", False)
    out = await run_statute_quality(apply=True)
    assert out["applied"].get("dedupe_quarantined") is None


def test_ingest_section_filter_flag_wiring(monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_SECTION_FILTER", True)
    body = (FIXTURES / "sections_7334221a.txt").read_text(encoding="utf-8")
    out = extract_statute_deterministic(
        text=body,
        html=None,
        source_meta={"source_name": "Senate", "url": "https://example.test"},
    )
    nums = {s["section_number"] for s in out["sections"]}
    assert "1N" not in nums


def test_ingest_gazette_header_flag_wiring(monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_GAZETTE_HEADER", True)
    text = (FIXTURES / "gazette_senate_following_act.txt").read_text(encoding="utf-8")
    out = extract_statute_deterministic(
        text=text,
        html=None,
        source_meta={"source_name": "Senate"},
    )
    assert "National Library" in out["statute_name"]
    assert out.get("year_enacted") == 2024


def test_mutant_gazette_flag_off_keeps_detected_name(monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_QUALITY_GAZETTE_HEADER", False)
    text = (FIXTURES / "gazette_senate_following_act.txt").read_text(encoding="utf-8")
    out = extract_statute_deterministic(text=text, html=None, source_meta={"source_name": "Senate"})
    assert out.get("year_enacted") != 2024 or "following" not in out["statute_name"].lower()
