"""Read-only pabalochistan consolidated-reprint report.

The report proposes the base-Act name and year from the candidate row's
statutes_staging.raw_text and flags a same-year stub whose name differs.
Metadata queries stay column-scoped. raw_text is a separate select of that
one column for the candidate ids. The CLI transaction is read-only.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from scraper.database import SessionLocal
from scraper.models import SourceProvenance, Statute, StatuteSection, StatuteSectionVersion, StatutesStaging
from scraper.tasks.pab_consolidated_report import (
    begin_read_only,
    build_parser,
    consolidated_reprint_report,
    same_year_statutes_select,
    staging_raw_text_select,
    staging_rows_select,
    statute_rows_select,
)

from tests.test_pab_consolidated_heading import (
    AMENDMENT_PAGE,
    AMENDMENT_URL,
    CM_HEADING,
    CM_LISTING,
    CM_PAGE,
    CM_URL,
)

STUB_NAME = (
    "Chief Minister and Ministers (Salaries, Allowances and Privileges) Act No. III of 1975"
)
WILDLIFE_NAME = "The Balochistan Wildlife Protection Act, 1975"
_BLOCKED = ("raw_text", "raw_html", "reconciled_json", "deterministic_json", "ai_json")


def test_report_cli_has_no_apply_switch():
    parser = build_parser()
    assert all(getattr(action, "dest", None) != "apply" for action in parser._actions)
    with pytest.raises(SystemExit):
        parser.parse_args(["--apply"])


def test_report_queries_are_column_scoped():
    metadata = (
        staging_rows_select(),
        statute_rows_select([uuid4()]),
        same_year_statutes_select([1975]),
    )
    for stmt in metadata:
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        names = {column.key.lower() for column in stmt.selected_columns}
        for blocked in _BLOCKED:
            assert blocked not in names
            assert blocked not in sql
    staging_names = {column.key for column in staging_rows_select().selected_columns}
    assert staging_names == {"id", "source_url", "promoted_to_id", "created_at"}
    candidate_id = uuid4()
    raw_stmt = staging_raw_text_select([candidate_id])
    raw_names = {column.key for column in raw_stmt.selected_columns}
    raw_sql = str(raw_stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert raw_names == {"id", "raw_text"}
    assert "raw_html" not in raw_sql
    assert "reconciled_json" not in raw_sql
    assert candidate_id.hex in raw_sql


@pytest.mark.asyncio
async def test_run_report_transaction_is_read_only():
    async with SessionLocal() as db:
        await begin_read_only(db)
        mode = (await db.execute(text("SHOW transaction_read_only"))).scalar()
        assert mode == "on"
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await db.execute(text("CREATE TEMP TABLE pab_ro_probe (id int)"))
        await db.rollback()


async def _add_section(db, statute_id, body: str) -> None:
    section = StatuteSection(statute_id=statute_id, section_number="1", sort_key=1)
    db.add(section)
    await db.flush()
    version = StatuteSectionVersion(
        section_id=section.id,
        version_no=1,
        section_text=body,
        text_hash=f"{statute_id.hex[:16]}{'ab' * 24}"[:64],
    )
    db.add(version)
    await db.flush()
    section.current_version_id = version.id
    await db.flush()


SHORT_TITLE = (
    "1. Short title. This Act may be called the Balochistan Chief Minister "
    "and Provincial Ministers (Salaries, Allowances and Privileges) Act, 1975.\n"
)


@pytest.mark.asyncio
async def test_report_lists_proposed_base_act_and_flags_a_differing_stub(db):
    page = CM_PAGE.read_text(encoding="utf-8")
    assert "amended upto" not in SHORT_TITLE.lower()
    assert "amended upto" in page.lower()
    consolidated = Statute(
        name=CM_LISTING,
        jurisdiction="Balochistan",
        year_enacted=2014,
        source_name="BalochistanAssembly",
        source_url=CM_URL,
    )
    missing_text = Statute(
        name="Balochistan Staging Text Missing Act, 1999",
        jurisdiction="Balochistan",
        year_enacted=1999,
        source_name="BalochistanAssembly",
        source_url="https://pabalochistan.gov.pk/storage/9/placeholder.pdf",
    )
    stub = Statute(
        name=STUB_NAME,
        jurisdiction="Balochistan",
        year_enacted=1975,
        source_name="BalochistanAssembly",
        source_url="https://pabalochistan.gov.pk/storage/1/act-iii-1975.pdf",
    )
    wildlife = Statute(
        name=WILDLIFE_NAME,
        jurisdiction="Balochistan",
        year_enacted=1975,
        source_name="PakistanCode",
        source_url="https://pakistancode.gov.pk/english/UY2FqaJw1-apaUY2Fqa-ap%3D%3D-sg-jjjjjjjjjjjjj",
    )
    amendment_statute = Statute(
        name="THE BALOCHISTAN JUDICIAL ACADEMY (AMENDMENT) ACT, 2025. ACT NO. VII OF 2026",
        jurisdiction="Balochistan",
        year_enacted=2025,
        source_name="BalochistanAssembly",
        source_url=AMENDMENT_URL,
    )
    db.add_all([consolidated, missing_text, stub, wildlife, amendment_statute])
    await db.flush()
    # Live rows keep the cutoff in staging raw_text. Section 1 is only the short title.
    await _add_section(db, consolidated.id, SHORT_TITLE)
    await _add_section(db, missing_text.id, page)
    await _add_section(db, amendment_statute.id, AMENDMENT_PAGE.read_text(encoding="utf-8"))

    provenance = SourceProvenance(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=CM_URL,
        content_hash="a" * 64,
        content_kind="text",
        byte_size=len(page),
    )
    decoy_provenance = SourceProvenance(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=missing_text.source_url,
        content_hash="b" * 64,
        content_kind="text",
        byte_size=len(page),
    )
    amendment_provenance = SourceProvenance(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=AMENDMENT_URL,
        content_hash="c" * 64,
        content_kind="text",
        byte_size=10,
    )
    db.add_all([provenance, decoy_provenance, amendment_provenance])
    await db.flush()

    staging = StatutesStaging(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=CM_URL,
        provenance_id=provenance.id,
        content_hash="d" * 64,
        raw_text=page,
        promoted_to_id=consolidated.id,
        status="promoted",
    )
    # The first page is in section text only. Missing staging text must not use it.
    no_text = StatutesStaging(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url="https://pabalochistan.gov.pk/storage/9/placeholder.pdf",
        provenance_id=decoy_provenance.id,
        content_hash="e" * 64,
        raw_text=None,
        promoted_to_id=missing_text.id,
        status="promoted",
    )
    amendment = StatutesStaging(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=AMENDMENT_URL,
        provenance_id=amendment_provenance.id,
        content_hash="f" * 64,
        raw_text=AMENDMENT_PAGE.read_text(encoding="utf-8"),
        promoted_to_id=amendment_statute.id,
        status="promoted",
    )
    db.add_all([staging, no_text, amendment])
    await db.flush()

    before = {
        consolidated.id: (consolidated.name, consolidated.year_enacted),
        stub.id: (stub.name, stub.year_enacted),
        wildlife.id: (wildlife.name, wildlife.year_enacted),
    }
    report = await consolidated_reprint_report(db)

    assert report["dry_run"] is True
    assert report["writes"] == 0
    assert report["consolidated_count"] == 2
    by_id = {item["staging_id"]: item for item in report["consolidated"]}
    row = by_id[str(staging.id)]
    assert row["statute_id"] == str(consolidated.id)
    assert row["stored_name"] == CM_LISTING
    assert row["stored_year"] == 2014
    assert row["proposed_name"] == CM_HEADING
    assert row["proposed_year"] == 1975
    assert row["pab_amended_upto"] == "Amended upto 9th October, 2014"
    assert row["reason"] is None
    missing = by_id[str(no_text.id)]
    assert missing["proposed_name"] is None
    assert missing["proposed_year"] is None
    assert missing["pab_amended_upto"] is None
    assert missing["reason"] == "no_staging_text"
    assert str(amendment.id) not in by_id
    assert report["conflict_count"] == 1
    conflict = report["same_year_name_conflicts"][0]
    assert conflict["stub_id"] == str(stub.id)
    assert conflict["stub_name"] == STUB_NAME
    assert conflict["stub_year"] == 1975
    assert conflict["proposed_name"] == CM_HEADING
    assert conflict["proposed_year"] == 1975
    assert WILDLIFE_NAME not in {item["stub_name"] for item in report["same_year_name_conflicts"]}
    assert (consolidated.name, consolidated.year_enacted) == before[consolidated.id]
    assert (stub.name, stub.year_enacted) == before[stub.id]
    assert (wildlife.name, wildlife.year_enacted) == before[wildlife.id]
