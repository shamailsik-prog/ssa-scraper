"""Read-only pabalochistan consolidated-reprint report.

The report proposes the base-Act name and year from promoted section text
and flags a same-year stub whose name differs. It does not select
statutes_staging.raw_text or raw_html, and the CLI transaction is read-only.
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
    section_text_select,
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
    statements = (
        staging_rows_select(),
        statute_rows_select([uuid4()]),
        same_year_statutes_select([1975]),
        section_text_select([uuid4()]),
    )
    for stmt in statements:
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        names = {column.key.lower() for column in stmt.selected_columns}
        for blocked in _BLOCKED:
            assert blocked not in names
            assert blocked not in sql
    staging_names = {column.key for column in staging_rows_select().selected_columns}
    assert staging_names == {"id", "source_url", "promoted_to_id", "created_at"}
    section_names = {column.key for column in section_text_select([uuid4()]).selected_columns}
    assert section_names == {"statute_id", "sort_key", "section_number", "section_text"}


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


@pytest.mark.asyncio
async def test_report_lists_proposed_base_act_and_flags_a_differing_stub(db):
    page = CM_PAGE.read_text(encoding="utf-8")
    consolidated = Statute(
        name=CM_LISTING,
        jurisdiction="Balochistan",
        year_enacted=2014,
        source_name="BalochistanAssembly",
        source_url=CM_URL,
    )
    decoy = Statute(
        name="Unrelated Balochistan Reprint Placeholder Act, 1999",
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
    db.add_all([consolidated, decoy, stub, wildlife, amendment_statute])
    await db.flush()
    await _add_section(db, consolidated.id, page)
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
        source_url=decoy.source_url,
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
        raw_text=None,
        promoted_to_id=consolidated.id,
        status="promoted",
    )
    # The marker lives only on this row's raw_text. The report must not read it.
    body_only = StatutesStaging(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url="https://pabalochistan.gov.pk/storage/9/placeholder.pdf",
        provenance_id=decoy_provenance.id,
        content_hash="e" * 64,
        raw_text=page,
        promoted_to_id=decoy.id,
        status="promoted",
    )
    amendment = StatutesStaging(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=AMENDMENT_URL,
        provenance_id=amendment_provenance.id,
        content_hash="f" * 64,
        raw_text=None,
        promoted_to_id=amendment_statute.id,
        status="promoted",
    )
    db.add_all([staging, body_only, amendment])
    await db.flush()

    before = {
        consolidated.id: (consolidated.name, consolidated.year_enacted),
        stub.id: (stub.name, stub.year_enacted),
        wildlife.id: (wildlife.name, wildlife.year_enacted),
    }
    report = await consolidated_reprint_report(db)

    assert report["dry_run"] is True
    assert report["writes"] == 0
    assert report["consolidated_count"] == 1
    row = report["consolidated"][0]
    assert row["staging_id"] == str(staging.id)
    assert row["statute_id"] == str(consolidated.id)
    assert row["stored_name"] == CM_LISTING
    assert row["stored_year"] == 2014
    assert row["proposed_name"] == CM_HEADING
    assert row["proposed_year"] == 1975
    assert row["pab_amended_upto"] == "Amended upto 9th October, 2014"
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
