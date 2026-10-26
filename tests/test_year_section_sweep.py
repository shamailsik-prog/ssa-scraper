"""Dry-run year-section sweep must not write; --apply merges text back and leaves quarantine alone."""

from __future__ import annotations

from scraper.models import DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.year_section_merge import plan_year_section_merges, source_host
from scraper.tasks.year_section_sweep import build_parser, run_year_section_sweep
from sqlalchemy import select


def test_cli_defaults_to_dry_run():
    assert build_parser().parse_args([]).apply is False
    assert build_parser().parse_args(["--apply"]).apply is True


def test_source_host_strips_www_and_blank():
    assert source_host("https://www.pabalochistan.gov.pk/storage/act.pdf") == "pabalochistan.gov.pk"
    assert source_host("https://law.gok.pk/acts/sample") == "law.gok.pk"
    assert source_host("") == "(none)"
    assert source_host(None) == "(none)"


def test_plan_merges_year_tail_into_previous_real_section_and_skips_leading():
    sections = [
        {"id": "a", "section_number": "25", "text": "25. Offences under the Code (Act XLV of", "sort_key": 1},
        {"id": "b", "section_number": "1860", "text": "1860) shall be dealt with in that Code.", "sort_key": 2},
        {"id": "c", "section_number": "26", "text": "26. Rules.", "sort_key": 3},
        {"id": "d", "section_number": "1882", "text": "1882. Leading year with no previous real section.", "sort_key": 0},
    ]
    # 1882 has the lowest sort key, so it is leading and must not be deleted by the plan.
    plan = plan_year_section_merges(sections)
    assert plan["merged"] == [{"year_section_id": "b", "year_section_number": "1860", "into_section_id": "a", "into_section_number": "25"}]
    assert plan["skipped_no_previous"] == [{"year_section_id": "d", "year_section_number": "1882"}]
    assert "1860) shall be dealt with" in plan["merged_text_by_target"]["a"]
    assert plan["merged_text_by_target"]["a"].startswith("25. Offences")


async def _add_section(db, statute_id, num: str, text: str, sort_key: int) -> StatuteSection:
    sec = StatuteSection(statute_id=statute_id, section_number=num, sort_key=sort_key, section_title=f"s.{num}")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(
        section_id=sec.id,
        version_no=1,
        section_text=text,
        text_hash=f"{num}-{sort_key}".ljust(64, "a"),
    )
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    await db.flush()
    return sec


async def _seed(db):
    gok = Statute(
        name="Gwadar Industrial Estates Development Authority Act",
        jurisdiction="Balochistan",
        source_url="https://law.gok.pk/wp-content/uploads/gwadar.pdf",
        source_name="AJKAssembly",
    )
    senate = Statute(
        name="Industrial Relations (Amendment) Act, 1976",
        jurisdiction="Federal",
        source_url="https://www.senate.gov.pk/uploads/acts/ir-1976.pdf",
        source_name="Senate",
    )
    baloch = Statute(
        name="Balochistan Finance Act, 2025",
        jurisdiction="Balochistan",
        source_url="https://pabalochistan.gov.pk/storage/finance-2025.pdf",
        source_name="BalochistanAssembly",
    )
    leading = Statute(
        name="Leading Year Act, 1898",
        jurisdiction="Federal",
        source_url="https://pakp.gov.pk/acts/leading.pdf",
        source_name="KPAssembly",
    )
    db.add_all([gok, senate, baloch, leading])
    await db.flush()
    await _add_section(db, gok.id, "25", "25. Offences under the Code (Act XLV of", 1)
    year = await _add_section(db, gok.id, "1860", "1860) shall be dealt with in that Code.", 2)
    await _add_section(db, gok.id, "26", "26. The Government may make rules for this Act.", 3)
    await _add_section(db, senate.id, "1", "1. Short title. This Act may be called the amending Act.", 1)
    await _add_section(db, senate.id, "1976", "1976) hereinafter referred to as the said Act.", 2)
    await _add_section(db, baloch.id, "4", "4. The rate of tax is amended as follows for the province.", 1)
    await _add_section(db, baloch.id, "2022", "2022) dt. 16.12.2001 shall stand repealed from that day.", 2)
    await _add_section(db, leading.id, "1898", "1898. This leading year section has no previous real section in the act.", 1)
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=baloch.id,
            reason="operator:hold",
            run_id="prior-hold",
        )
    )
    await db.commit()
    return {"gok": gok, "senate": senate, "baloch": baloch, "leading": leading, "year": year}


async def test_sweep_dry_run_reports_hosts_and_does_not_write(db):
    seeded = await _seed(db)
    before_year = (
        await db.execute(select(StatuteSection).where(StatuteSection.id == seeded["year"].id))
    ).scalars().one()
    before_text = (
        await db.execute(
            select(StatuteSectionVersion).where(StatuteSectionVersion.id == before_year.current_version_id)
        )
    ).scalars().one().section_text
    before_quarantine = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == seeded["baloch"].id))
    ).scalars().one()

    report = await run_year_section_sweep(apply=False)

    assert report["dry_run"] is True
    assert report["apply"] is False
    assert report["year_sections"] == 4
    assert report["merged"] == 3
    assert report["skipped_no_previous"] == 1
    assert report["by_host"]["law.gok.pk"]["merged"] == 1
    assert report["by_host"]["senate.gov.pk"]["year_sections"] == 1
    assert report["by_host"]["pabalochistan.gov.pk"]["statutes"] == 1
    assert report["by_host"]["pakp.gov.pk"]["skipped_no_previous"] == 1

    still = (
        await db.execute(select(StatuteSection).where(StatuteSection.id == seeded["year"].id))
    ).scalars().one()
    still_text = (
        await db.execute(select(StatuteSectionVersion).where(StatuteSectionVersion.id == still.current_version_id))
    ).scalars().one().section_text
    assert still_text == before_text
    quarantine = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == seeded["baloch"].id))
    ).scalars().one()
    assert quarantine.reason == before_quarantine.reason == "operator:hold"
    assert quarantine.run_id == "prior-hold"


async def test_sweep_apply_merges_text_and_does_not_unquarantine(db):
    seeded = await _seed(db)
    report = await run_year_section_sweep(apply=True)
    assert report["dry_run"] is False
    assert report["merged"] == 3

    numbers = (
        await db.execute(select(StatuteSection.section_number).where(StatuteSection.statute_id == seeded["gok"].id))
    ).scalars().all()
    assert "1860" not in numbers
    assert set(numbers) >= {"25", "26"}

    target = (
        await db.execute(
            select(StatuteSection).where(
                StatuteSection.statute_id == seeded["gok"].id,
                StatuteSection.section_number == "25",
            )
        )
    ).scalars().one()
    versions = (
        await db.execute(
            select(StatuteSectionVersion)
            .where(StatuteSectionVersion.section_id == target.id)
            .order_by(StatuteSectionVersion.version_no)
        )
    ).scalars().all()
    assert len(versions) == 2
    assert versions[0].section_text == "25. Offences under the Code (Act XLV of"
    current = next(v for v in versions if v.id == target.current_version_id)
    assert "1860) shall be dealt with" in current.section_text
    assert current.section_text.startswith("25. Offences")

    leading_numbers = (
        await db.execute(select(StatuteSection.section_number).where(StatuteSection.statute_id == seeded["leading"].id))
    ).scalars().all()
    assert leading_numbers == ["1898"]

    quarantine = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == seeded["baloch"].id))
    ).scalars().one()
    assert quarantine.reason == "operator:hold"
    assert quarantine.run_id == "prior-hold"
