"""Dry-run year-section sweep must not write; --apply merges text back and leaves quarantine alone."""

from __future__ import annotations

import json

from scraper.models import (
    CorpusMetadata,
    DocumentQuarantine,
    EmbeddingQueue,
    Instrument,
    InstrumentSectionRelation,
    Statute,
    StatuteSection,
    StatuteSectionVersion,
)
from scraper.statute_quality.year_section_merge import plan_year_section_merges, source_host, spread_host_samples
from scraper.tasks.year_section_sweep import LATEST_RUN_KEY, build_parser, run_year_section_sweep
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
    assert plan["skipped_in_sequence"] == []
    assert "1860) shall be dealt with" in plan["merged_text_by_target"]["a"]
    assert plan["merged_text_by_target"]["a"].startswith("25. Offences")


def test_plan_keeps_year_headings_that_continue_the_act_sequence():
    sections = [
        {"id": "a", "section_number": "1798", "text": "1798. First rule.- This provision establishes the first operative rule of this code.", "sort_key": 1},
        {"id": "b", "section_number": "1799", "text": "1799. Second rule.- This provision establishes the second operative rule of this code.", "sort_key": 2},
        {"id": "c", "section_number": "1800", "text": "1800. Third rule.- This provision establishes the third operative rule of this code.", "sort_key": 3},
        {"id": "d", "section_number": "1801", "text": "1801. Fourth rule.- This provision establishes the fourth operative rule of this code.", "sort_key": 4},
    ]
    plan = plan_year_section_merges(sections)
    assert plan["merged"] == []
    assert plan["merged_text_by_target"] == {}
    assert {item["year_section_number"] for item in plan["skipped_in_sequence"]} == {"1800", "1801"}


def test_plan_still_merges_a_citation_year_that_sits_near_the_sequence():
    sections = [
        {"id": "p", "section_number": "1799", "text": "1799. Prior rule.- Tax is charged under the earlier Code (Act V of", "sort_key": 1},
        {"id": "y", "section_number": "1860", "text": "1860) and shall be assessed in the manner set out in this section.", "sort_key": 2},
        {"id": "n", "section_number": "1804", "text": "1804. Later rule.- This provision establishes the later operative rule of this code.", "sort_key": 3},
    ]
    plan = plan_year_section_merges(sections)
    assert plan["merged"] == [
        {"year_section_id": "y", "year_section_number": "1860", "into_section_id": "p", "into_section_number": "1799"}
    ]
    assert {item["year_section_number"] for item in plan["skipped_in_sequence"]} == {"1804"}
    assert "shall be assessed" in plan["merged_text_by_target"]["p"]


def test_samples_round_robin_across_hosts():
    items = [{"host": "law.gok.pk", "n": i} for i in range(5)]
    items.append({"host": "senate.gov.pk", "n": 0})
    spread = spread_host_samples(items, limit=3)
    assert [item["host"] for item in spread] == ["law.gok.pk", "senate.gov.pk", "law.gok.pk"]


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
    sequential = Statute(
        name="Long Sequential Code",
        jurisdiction="Federal",
        source_url="https://pas.gov.pk/acts/long-code.pdf",
        source_name="KPAssembly",
    )
    db.add_all([gok, senate, baloch, leading, sequential])
    await db.flush()
    await _add_section(db, gok.id, "25", "25. Offences under the Code (Act XLV of", 1)
    year = await _add_section(db, gok.id, "1860", "1860) shall be dealt with in that Code.", 2)
    await _add_section(db, gok.id, "26", "26. The Government may make rules for this Act.", 3)
    await _add_section(db, senate.id, "1", "1. Short title. This Act may be called the amending Act.", 1)
    await _add_section(db, senate.id, "1976", "1976) hereinafter referred to as the said Act.", 2)
    await _add_section(db, baloch.id, "4", "4. The rate of tax is amended as follows for the province.", 1)
    await _add_section(db, baloch.id, "2022", "2022) dt. 16.12.2001 shall stand repealed from that day.", 2)
    await _add_section(db, leading.id, "1898", "1898. This leading year section has no previous real section in the act.", 1)
    await _add_section(db, sequential.id, "1798", "1798. First rule.- This provision establishes the first operative rule of this code.", 1)
    await _add_section(db, sequential.id, "1799", "1799. Second rule.- This provision establishes the second operative rule of this code.", 2)
    seq_1800 = await _add_section(db, sequential.id, "1800", "1800. Third rule.- This provision establishes the third operative rule of this code.", 3)
    await _add_section(db, sequential.id, "1801", "1801. Fourth rule.- This provision establishes the fourth operative rule of this code.", 4)
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=baloch.id,
            reason="operator:hold",
            run_id="prior-hold",
        )
    )
    await db.commit()
    return {
        "gok": gok,
        "senate": senate,
        "baloch": baloch,
        "leading": leading,
        "sequential": sequential,
        "year": year,
        "seq_1800": seq_1800,
    }


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
    assert report["year_sections"] == 6
    assert report["merged"] == 3
    assert report["skipped_no_previous"] == 1
    assert report["skipped_in_sequence"] == 2
    assert report["by_host"]["pas.gov.pk"]["skipped_in_sequence"] == 2
    hosts_in_samples = {item["host"] for item in report["samples"]}
    assert "law.gok.pk" in hosts_in_samples
    assert "pas.gov.pk" in hosts_in_samples
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
    instrument = Instrument(type="amendment", full_text_hash="c" * 64, title="Note on section 1860")
    db.add(instrument)
    await db.flush()
    relation = InstrumentSectionRelation(
        source_instrument_id=instrument.id,
        target_statute_id=seeded["gok"].id,
        target_statute_section_id=seeded["year"].id,
        target_section_key="1860",
        amendment_operation="substitute",
        relation_phrase="substituted for",
        target_mention_raw="section 1860",
        span_start=0,
        span_end=12,
        evidence_snippet="section 1860 of the Code",
    )
    db.add(relation)
    await db.commit()

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

    sequential_numbers = set(
        (
            await db.execute(
                select(StatuteSection.section_number).where(StatuteSection.statute_id == seeded["sequential"].id)
            )
        ).scalars().all()
    )
    assert sequential_numbers == {"1798", "1799", "1800", "1801"}

    # The sweep commits on its own session. This session still holds the
    # pre-apply relation (expire_on_commit=False), so reload the row.
    linked = (
        await db.execute(
            select(InstrumentSectionRelation)
            .where(InstrumentSectionRelation.id == relation.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert linked.target_statute_section_id == target.id

    queued = (
        await db.execute(
            select(EmbeddingQueue).where(
                EmbeddingQueue.table_name == "statute_section",
                EmbeddingQueue.record_id == target.id,
            )
        )
    ).scalars().one()
    assert queued.status == "pending"

    latest = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().one()
    log_row = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == f"year_section_sweep_log:{latest.value}"))
    ).scalars().one()
    logged_numbers = {item["year_section_number"] for item in json.loads(log_row.value)["merges"]}
    assert "1860" in logged_numbers
    assert "1800" not in logged_numbers
    assert "1801" not in logged_numbers
