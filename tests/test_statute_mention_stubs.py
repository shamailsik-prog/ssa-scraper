from __future__ import annotations

import pytest
from sqlalchemy import func, select

from scraper.audit_baselines import AUDIT_2026_10_18, statute_counts_as_complete
from scraper.config import settings
from scraper.models import Statute, StatuteSection, StatuteSectionVersion
from scraper.tasks.promotion import _resolve_statute_link
from scraper.tasks.statute_stub_audit import _looks_like_clause_fragment, _looks_like_garbage_name, statute_stub_report


def test_auditor_baseline_records_10303_sectionless_statutes():
    assert AUDIT_2026_10_18["statutes_total"] == 12_618
    assert AUDIT_2026_10_18["statutes_without_sections"] == 10_303
    assert AUDIT_2026_10_18["statutes_with_any_sections"] == 2_300
    assert AUDIT_2026_10_18["statutes_no_year"] == 588
    assert AUDIT_2026_10_18["statutes_duplicate_names"] == 79


def test_completeness_gate_requires_sections_with_text():
    assert not statute_counts_as_complete(section_count=0, sections_with_text=0)
    assert not statute_counts_as_complete(section_count=12, sections_with_text=0)
    assert statute_counts_as_complete(section_count=1, sections_with_text=1)


def test_clause_and_garbage_name_detectors():
    assert _looks_like_clause_fragment("means the Income Tax Act")
    assert _looks_like_clause_fragment("further to amend the Customs Act")
    assert not _looks_like_clause_fragment("Pakistan Penal Code, 1860")
    assert _looks_like_garbage_name("JFIF\x00\x00binary")
    assert _looks_like_garbage_name("%PDF-1.4 junk")
    assert not _looks_like_garbage_name("Sales Tax Act, 1990")


@pytest.mark.asyncio
async def test_mention_link_creates_sectionless_stub_when_flag_off(db):
    created = await _resolve_statute_link(
        db,
        canonical_name="means the Foo Act",
        source_name="GazetteOfPakistan",
        source_url=None,
        jurisdiction="Federal",
    )
    assert created is not None
    n = (
        await db.execute(select(func.count()).select_from(StatuteSection).where(StatuteSection.statute_id == created.id))
    ).scalar()
    assert int(n or 0) == 0


@pytest.mark.asyncio
async def test_mention_link_does_not_create_stub_when_flag_on(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_NO_MENTION_STUBS", True)
    created = await _resolve_statute_link(
        db,
        canonical_name="means the Foo Act",
        source_name="Senate",
        source_url=None,
        jurisdiction="Federal",
    )
    assert created is None
    rows = (await db.execute(select(Statute).where(Statute.name == "means the Foo Act"))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_mention_link_reuses_existing_complete_statute(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_NO_MENTION_STUBS", True)
    statute = Statute(name="Pakistan Penal Code, 1860", jurisdiction="Federal", year_enacted=1860, source_name="PakistanCode")
    db.add(statute)
    await db.flush()
    sec = StatuteSection(statute_id=statute.id, section_number="302", section_title="Qatl-i-amd")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="Whoever commits qatl-i-amd...", text_hash="a" * 64)
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    await db.commit()

    linked = await _resolve_statute_link(
        db,
        canonical_name="Pakistan Penal Code, 1860",
        source_name="GazetteOfPakistan",
        source_url=None,
        jurisdiction="Federal",
    )
    assert linked is not None
    assert linked.id == statute.id


@pytest.mark.asyncio
async def test_stub_audit_report_counts_sectionless_rows(db):
    stub = Statute(name="means the Foo Act", jurisdiction="Federal", source_name="NationalAssembly")
    complete = Statute(name="Qanun-e-Shahadat Order, 1984", jurisdiction="Federal", year_enacted=1984, source_name="PakistanCode")
    db.add_all([stub, complete])
    await db.flush()
    sec = StatuteSection(statute_id=complete.id, section_number="3", section_title="Evidence")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="All facts that...", text_hash="b" * 64)
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    await db.commit()

    report = await statute_stub_report(db)
    assert report["current"]["statutes_without_sections"] >= 1
    assert report["current"]["statutes_complete"] >= 1
    assert report["auditor_baseline_2026_10_18"]["statutes_without_sections"] == 10_303
    assert report["dry_run"] is True
