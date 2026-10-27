"""Inventory of sections whose text is a DD.MM.YYYY footnote. Report only."""

from scraper.models import CorpusMetadata, DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.tasks.amendment_date_section import build_parser, run_amendment_date_section_inventory
from sqlalchemy import func, select


def test_cli_defaults_to_report_only():
    assert build_parser().parse_args([]).apply is False
    assert build_parser().parse_args(["--apply"]).apply is True


async def _add_section(db, statute_id, num: str, text: str, sort_key: int, *, current: bool = True) -> StatuteSection:
    sec = StatuteSection(statute_id=statute_id, section_number=num, sort_key=sort_key, section_title=f"s.{num}")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(
        section_id=sec.id,
        version_no=1,
        section_text=text,
        text_hash=f"{statute_id}-{num}-{sort_key}".ljust(64, "b")[:64],
    )
    db.add(ver)
    await db.flush()
    if current:
        sec.current_version_id = ver.id
        await db.flush()
    return sec


async def _seed(db):
    bank = Statute(
        name="The Bank of Azad Jammu & Kashmir Act, 2005",
        jurisdiction="Azad Jammu and Kashmir",
        source_url="https://law.gok.pk/wp-content/uploads/bank-ajk-2005.pdf",
        source_name="AJKAssembly",
        needs_section_refetch=False,
    )
    senate = Statute(
        name="Sample Senate Amendment Act, 2010",
        jurisdiction="Federal",
        source_url="https://www.senate.gov.pk/uploads/acts/sample-2010.pdf",
        source_name="Senate",
        needs_section_refetch=False,
    )
    held = Statute(
        name="Held Bank Notes Act, 2004",
        jurisdiction="Azad Jammu and Kashmir",
        source_url="https://law.gok.pk/wp-content/uploads/held-notes.pdf",
        source_name="AJKAssembly",
        needs_section_refetch=False,
    )
    clean = Statute(
        name="Clean Capital Act, 2006",
        jurisdiction="Azad Jammu and Kashmir",
        source_url="https://law.gok.pk/wp-content/uploads/clean-capital.pdf",
        source_name="AJKAssembly",
        needs_section_refetch=False,
    )
    db.add_all([bank, senate, held, clean])
    await db.flush()
    date_11 = await _add_section(
        db,
        bank.id,
        "11",
        "11.10.2018. 278 Volume XI (2001-2010) proportion to the shares of the Bank shall stand revised.",
        2,
    )
    await _add_section(
        db,
        bank.id,
        "17",
        "17.02.2007. 2 In subsection (1) for the words the Bank the following words shall be read.",
        4,
    )
    await _add_section(
        db,
        bank.id,
        "10",
        "10. Shares of the Bank.- The authorised capital shall be divided among the subscribers of the Bank.",
        1,
    )
    # An older version that starts with a date is not the current text.
    historical = await _add_section(
        db,
        bank.id,
        "12",
        "12. General reserve.- The Bank shall maintain a general reserve from its annual profits.",
        3,
    )
    old = StatuteSectionVersion(
        section_id=historical.id,
        version_no=0,
        section_text="12.04.2011. 100 Volume XI historical footnote that is not the current text.",
        text_hash="historical-date-version".ljust(64, "c")[:64],
    )
    db.add(old)
    await _add_section(
        db,
        senate.id,
        "4",
        "4.05.2012. 12 Senate volume note that was stored as a section heading.",
        1,
    )
    await _add_section(
        db,
        held.id,
        "9",
        "9.01.2008. 40 Volume XI held footnote that must stay out of the visible inventory.",
        1,
    )
    await _add_section(
        db,
        clean.id,
        "3",
        "3. Commencement.- This Act shall come into force on the day it is published.",
        1,
    )
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=held.id,
            reason="operator:hold",
            run_id="prior-hold",
        )
    )
    await db.commit()
    return {"bank": bank, "senate": senate, "held": held, "date_11": date_11}


async def _text(db, section_id) -> str:
    sec = (await db.execute(select(StatuteSection).where(StatuteSection.id == section_id))).scalars().one()
    ver = (
        await db.execute(select(StatuteSectionVersion).where(StatuteSectionVersion.id == sec.current_version_id))
    ).scalars().one()
    return ver.section_text


async def test_inventory_reports_visible_date_rows_and_does_not_write(db):
    seeded = await _seed(db)
    before = await _text(db, seeded["date_11"].id)
    meta_before = (await db.execute(select(func.count()).select_from(CorpusMetadata))).scalar()

    report = await run_amendment_date_section_inventory(apply=False)

    assert report["dry_run"] is True
    assert report["apply"] is False
    assert report["writes"] is False
    assert report["section_rewrites"] == 0
    assert report["rows"] == 3
    assert report["statutes"] == 2
    assert report["by_host"]["law.gok.pk"] == {"rows": 2, "statutes": 1}
    assert report["by_host"]["senate.gov.pk"] == {"rows": 1, "statutes": 1}
    assert "law.gok.pk" not in {item["host"] for item in report["samples"] if item["statute_id"] == str(seeded["held"].id)}
    sample_ids = {item["statute_id"] for item in report["samples"]}
    assert str(seeded["bank"].id) in sample_ids
    assert str(seeded["held"].id) not in sample_ids
    assert "(19|20)" in report["query"]
    assert await _text(db, seeded["date_11"].id) == before
    meta_after = (await db.execute(select(func.count()).select_from(CorpusMetadata))).scalar()
    assert meta_after == meta_before
    quarantine = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == seeded["held"].id))
    ).scalars().one()
    assert quarantine.reason == "operator:hold"
    bank = (await db.execute(select(Statute).where(Statute.id == seeded["bank"].id))).scalars().one()
    assert bank.needs_section_refetch is False


async def test_apply_is_opt_in_and_does_not_rewrite_sections(db):
    seeded = await _seed(db)
    before = await _text(db, seeded["date_11"].id)
    quarantine_count = (await db.execute(select(func.count()).select_from(DocumentQuarantine))).scalar()

    report = await run_amendment_date_section_inventory(apply=True)

    assert report["apply"] is True
    assert report["dry_run"] is False
    assert report["writes"] is False
    assert report["section_rewrites"] == 0
    assert report["rows"] == 3
    assert await _text(db, seeded["date_11"].id) == before
    assert (await db.execute(select(func.count()).select_from(DocumentQuarantine))).scalar() == quarantine_count
    bank = (await db.execute(select(Statute).where(Statute.id == seeded["bank"].id))).scalars().one()
    assert bank.needs_section_refetch is False
