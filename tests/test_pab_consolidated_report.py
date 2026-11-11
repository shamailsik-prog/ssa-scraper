"""Read-only pabalochistan consolidated-reprint report.

The report proposes the base-Act name and year from the candidate row's
statutes_staging.raw_text and flags a same-year stub whose name differs.
Metadata queries stay column-scoped. raw_text is a separate select of that
one column for the candidate ids. The CLI transaction is read-only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scraper.config import settings
from scraper.database import AsyncSession, SessionLocal, _quote_ident
from scraper.models import SourceProvenance, Statute, StatuteSection, StatuteSectionVersion, StatutesStaging
from scraper.tasks import pab_consolidated_report as report_mod
from scraper.tasks.pab_consolidated_report import (
    begin_read_only,
    build_parser,
    consolidated_reprint_report,
    reprint_names_conflict,
    run_report,
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
    SPEAKER_HEADING,
    SPEAKER_PAGE,
    SPEAKER_URL,
)

FEDERAL_SPEAKER = (
    "The Balochistan Speaker's and Deputy Speaker's "
    "(Salaries, Allowances and Privileges) Act, 1975"
)
FEDERAL_CHAIRMAN = "The Chairman and the Speaker (Salaries, Allowances and Privileges) Act, 1975"
MEMBERS_BASE = "THE BALOCHISTAN ASSEMBLY MEMBERS (SALARIES AND ALLOWANCES) ACT, 1975"
MEMBERS_ACT_II = (
    "The Balochistan Assembly Members (Salaries and Allowances) "
    "(Amendment) Act No. II of 1975"
)
MEMBERS_ACT_VIII = (
    "The Balochistan Assembly Members (Salaries and Allowances) "
    "(Amendment) Act No. VIII of 1975"
)
TRUNCATED_TITLE = "Privileges), Act, 1975"

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
    statute_names = {column.key for column in statute_rows_select([uuid4()]).selected_columns}
    same_year_names = {column.key for column in same_year_statutes_select([1975]).selected_columns}
    assert "jurisdiction" in statute_names
    assert "jurisdiction" in same_year_names
    assert "raw_text" not in statute_names
    assert "raw_text" not in same_year_names
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
        # The session GUC must not stay on after the report transaction ends.
        # SET TRANSACTION READ ONLY is transaction-scoped; default_transaction_read_only is not.
        default_mode = (await db.execute(text("SHOW default_transaction_read_only"))).scalar()
        assert default_mode == "off"
        await db.execute(text("CREATE TEMP TABLE pab_ro_probe_ok (id int)"))
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
    assert row["duplicate_staging"] == []
    assert row["duplicate_staging_count"] == 0
    assert row["staging_choice"] == "newest_heading_then_created_at_then_id"
    assert report["duplicate_staging_count"] == 0


def _staging(db_ready_id, *, url: str, provenance_id, raw_text: str | None, created_at: datetime, content_hash: str):
    return StatutesStaging(
        id=db_ready_id,
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=url,
        provenance_id=provenance_id,
        content_hash=content_hash,
        raw_text=raw_text,
        promoted_to_id=None,
        status="promoted",
        created_at=created_at,
    )


async def _provenance(db, *, url: str, content_hash: str) -> SourceProvenance:
    provenance = SourceProvenance(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=url,
        content_hash=content_hash,
        content_kind="text",
        byte_size=1,
    )
    db.add(provenance)
    await db.flush()
    return provenance


@pytest.mark.asyncio
async def test_duplicate_staging_rows_choose_newest_and_list_the_rest(db):
    """Several statutes_staging rows for one promoted id become one proposal.

    The chosen row is the newest qualifying row (created_at, then id). A newer
    row that is not a consolidated reprint does not hide an older heading.
    Conflicts are counted once. Every other candidate is listed.
    """
    statute = Statute(
        name=CM_LISTING,
        jurisdiction="Balochistan",
        year_enacted=2014,
        source_name="BalochistanAssembly",
        source_url=CM_URL,
    )
    stub = Statute(
        name=STUB_NAME,
        jurisdiction="Balochistan",
        year_enacted=1975,
        source_name="BalochistanAssembly",
        source_url="https://pabalochistan.gov.pk/storage/1/act-iii-1975.pdf",
    )
    db.add_all([statute, stub])
    await db.flush()
    cm_prov = await _provenance(db, url=CM_URL, content_hash="1" * 64)
    speaker_prov = await _provenance(db, url=SPEAKER_URL, content_hash="2" * 64)
    amendment_prov = await _provenance(db, url=AMENDMENT_URL, content_hash="3" * 64)
    missing_prov = await _provenance(db, url=CM_URL + "?missing=1", content_hash="4" * 64)

    older_heading = UUID("00000000-0000-4000-8000-000000000001")
    newer_heading = UUID("00000000-0000-4000-8000-000000000002")
    newest_amendment = UUID("00000000-0000-4000-8000-000000000003")
    older_missing = UUID("00000000-0000-4000-8000-000000000004")
    rows = [
        _staging(
            older_heading,
            url=SPEAKER_URL,
            provenance_id=speaker_prov.id,
            raw_text=SPEAKER_PAGE.read_text(encoding="utf-8"),
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            content_hash="5" * 64,
        ),
        _staging(
            newer_heading,
            url=CM_URL,
            provenance_id=cm_prov.id,
            raw_text=CM_PAGE.read_text(encoding="utf-8"),
            created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            content_hash="6" * 64,
        ),
        _staging(
            newest_amendment,
            url=AMENDMENT_URL,
            provenance_id=amendment_prov.id,
            raw_text=AMENDMENT_PAGE.read_text(encoding="utf-8"),
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            content_hash="7" * 64,
        ),
        _staging(
            older_missing,
            url=CM_URL + "?missing=1",
            provenance_id=missing_prov.id,
            raw_text=None,
            created_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
            content_hash="8" * 64,
        ),
    ]
    for row in rows:
        row.promoted_to_id = statute.id
    db.add_all(rows)
    await db.flush()

    report = await consolidated_reprint_report(db)

    assert report["consolidated_count"] == 1
    assert report["duplicate_staging_count"] == 3
    proposal = report["consolidated"][0]
    assert proposal["staging_id"] == str(newer_heading)
    assert proposal["staging_choice"] == "newest_heading_then_created_at_then_id"
    assert proposal["proposed_name"] == CM_HEADING
    assert proposal["proposed_year"] == 1975
    assert proposal["duplicate_staging_count"] == 3
    listed = proposal["duplicate_staging"]
    assert [item["staging_id"] for item in listed] == [
        str(newest_amendment),
        str(older_heading),
        str(older_missing),
    ]
    by_dup = {item["staging_id"]: item for item in listed}
    assert by_dup[str(newest_amendment)]["reason"] == "not_consolidated"
    assert by_dup[str(newest_amendment)]["proposed_name"] is None
    assert by_dup[str(older_heading)]["reason"] is None
    assert by_dup[str(older_heading)]["proposed_name"] == SPEAKER_HEADING
    assert by_dup[str(older_heading)]["proposed_year"] == 1975
    assert by_dup[str(older_missing)]["reason"] == "no_staging_text"
    assert by_dup[str(older_missing)]["proposed_name"] is None
    assert report["conflict_count"] == 1
    assert report["same_year_name_conflicts"][0]["consolidated_staging_id"] == str(newer_heading)
    assert report["same_year_name_conflicts"][0]["stub_id"] == str(stub.id)


@pytest.mark.asyncio
async def test_duplicate_staging_tie_breaks_on_id(db):
    statute = Statute(
        name="Balochistan Duplicate Tie Act, 1975",
        jurisdiction="Balochistan",
        year_enacted=2014,
        source_name="BalochistanAssembly",
        source_url=CM_URL,
    )
    db.add(statute)
    await db.flush()
    low_prov = await _provenance(db, url=SPEAKER_URL, content_hash="a" * 64)
    high_prov = await _provenance(db, url=CM_URL, content_hash="b" * 64)
    same_time = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    low_id = UUID("00000000-0000-4000-8000-00000000000a")
    high_id = UUID("00000000-0000-4000-8000-00000000000b")
    assert high_id > low_id
    low = _staging(
        low_id,
        url=SPEAKER_URL,
        provenance_id=low_prov.id,
        raw_text=SPEAKER_PAGE.read_text(encoding="utf-8"),
        created_at=same_time,
        content_hash="c" * 64,
    )
    high = _staging(
        high_id,
        url=CM_URL,
        provenance_id=high_prov.id,
        raw_text=CM_PAGE.read_text(encoding="utf-8"),
        created_at=same_time,
        content_hash="d" * 64,
    )
    low.promoted_to_id = statute.id
    high.promoted_to_id = statute.id
    db.add_all([low, high])
    await db.flush()

    report = await consolidated_reprint_report(db)

    assert report["consolidated_count"] == 1
    proposal = report["consolidated"][0]
    assert proposal["staging_id"] == str(high_id)
    assert proposal["proposed_name"] == CM_HEADING
    assert proposal["duplicate_staging_count"] == 1
    assert proposal["duplicate_staging"][0]["staging_id"] == str(low_id)
    assert proposal["duplicate_staging"][0]["proposed_name"] == SPEAKER_HEADING


@pytest.mark.asyncio
async def test_newer_missing_text_does_not_outrank_an_older_heading(db):
    statute = Statute(
        name="Balochistan Missing Text Beside Heading Act, 1975",
        jurisdiction="Balochistan",
        year_enacted=2014,
        source_name="BalochistanAssembly",
        source_url=CM_URL,
    )
    db.add(statute)
    await db.flush()
    heading_prov = await _provenance(db, url=CM_URL, content_hash="e" * 64)
    missing_prov = await _provenance(db, url=CM_URL + "?empty=1", content_hash="f" * 64)
    heading_id = UUID("00000000-0000-4000-8000-000000000011")
    missing_id = UUID("00000000-0000-4000-8000-000000000012")
    heading = _staging(
        heading_id,
        url=CM_URL,
        provenance_id=heading_prov.id,
        raw_text=CM_PAGE.read_text(encoding="utf-8"),
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        content_hash="9" * 64,
    )
    missing = _staging(
        missing_id,
        url=CM_URL + "?empty=1",
        provenance_id=missing_prov.id,
        raw_text="   ",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        content_hash="0" * 64,
    )
    heading.promoted_to_id = statute.id
    missing.promoted_to_id = statute.id
    db.add_all([heading, missing])
    await db.flush()

    report = await consolidated_reprint_report(db)

    assert report["consolidated_count"] == 1
    proposal = report["consolidated"][0]
    assert proposal["staging_id"] == str(heading_id)
    assert proposal["proposed_name"] == CM_HEADING
    assert proposal["reason"] is None
    assert proposal["duplicate_staging"][0]["staging_id"] == str(missing_id)
    assert proposal["duplicate_staging"][0]["reason"] == "no_staging_text"


@pytest.mark.asyncio
async def test_report_refuses_a_role_without_staging_raw_text(db):
    probe = f"pab_report_probe_{uuid4().hex[:10]}"
    quoted = _quote_ident(probe)
    await db.execute(text(f"CREATE ROLE {quoted} NOLOGIN"))
    await db.execute(text(f"GRANT {quoted} TO CURRENT_USER"))
    await db.execute(text(f"GRANT USAGE ON SCHEMA public TO {quoted}"))
    await db.execute(text(f"SET LOCAL ROLE {quoted}"))
    needed = make_url(settings.DATABASE_URL).username
    assert needed

    try:
        with pytest.raises(Exception) as exc:
            await consolidated_reprint_report(db)
        message = str(exc.value)
        assert probe in message
        assert "statutes_staging.raw_text" in message
        assert "sikander_reader" in message
        assert f"Run it as {needed}." in message
    finally:
        await db.rollback()


@pytest.mark.asyncio
async def test_report_refuses_sikander_reader_by_name(db):
    role = settings.SIKANDER_READER_ROLE
    quoted = _quote_ident(role)
    await db.execute(text(f"GRANT {quoted} TO CURRENT_USER"))
    await db.execute(text(f"SET LOCAL ROLE {quoted}"))
    needed = make_url(settings.DATABASE_URL).username

    try:
        with pytest.raises(Exception) as exc:
            await consolidated_reprint_report(db)
        message = str(exc.value)
        assert role in message
        assert "statutes_staging.raw_text" in message
        assert f"Run it as {needed}." in message
    finally:
        await db.rollback()


async def _pool_default_read_only(factory) -> str:
    async with factory() as db:
        return (await db.execute(text("SHOW default_transaction_read_only"))).scalar()


@pytest.mark.asyncio
async def test_run_report_does_not_return_read_only_state_to_the_pool(monkeypatch):
    pooled = create_async_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(pooled, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(report_mod, "SessionLocal", factory, raising=False)
    seen: dict[str, str] = {}

    async def _capture(db):
        seen["default"] = (await db.execute(text("SHOW default_transaction_read_only"))).scalar()
        seen["transaction"] = (await db.execute(text("SHOW transaction_read_only"))).scalar()
        # Commit so a session-level SET survives. Rollback would hide the leak.
        await db.commit()
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await db.execute(text("CREATE TEMP TABLE pab_pool_probe (id int)"))
        raise RuntimeError("stop-after-read-only")

    monkeypatch.setattr(report_mod, "consolidated_reprint_report", _capture)
    try:
        with pytest.raises(RuntimeError, match="stop-after-read-only"):
            await run_report()
        assert seen["transaction"] == "on"
        assert seen["default"] == "on"
        assert await _pool_default_read_only(factory) == "off"
        async with factory() as db:
            await db.execute(text("CREATE TEMP TABLE pab_pool_probe_ok (id int)"))
            await db.rollback()
        monkeypatch.setattr(report_mod, "consolidated_reprint_report", consolidated_reprint_report)
        finished = await run_report()
        assert finished["dry_run"] is True
        assert finished["writes"] == 0
        assert await _pool_default_read_only(factory) == "off"
    finally:
        await pooled.dispose()


def test_conflict_rejects_federal_speaker_amendments_and_a_one_token_title():
    assert reprint_names_conflict(CM_HEADING, "Balochistan", STUB_NAME, "Balochistan")
    assert not reprint_names_conflict(SPEAKER_HEADING, "Balochistan", FEDERAL_SPEAKER, "Federal")
    assert not reprint_names_conflict(SPEAKER_HEADING, "Balochistan", FEDERAL_CHAIRMAN, "Balochistan")
    assert not reprint_names_conflict(MEMBERS_BASE, "Balochistan", MEMBERS_ACT_II, "Balochistan")
    assert not reprint_names_conflict(MEMBERS_BASE, "Balochistan", MEMBERS_ACT_VIII, "Balochistan")
    assert not reprint_names_conflict(CM_HEADING, "Balochistan", TRUNCATED_TITLE, "Balochistan")
    assert not reprint_names_conflict(CM_HEADING, "Balochistan", WILDLIFE_NAME, "Balochistan")
    assert not reprint_names_conflict(CM_HEADING, "", STUB_NAME, "Balochistan")
    amendment_ii = "The Balochistan Assembly Members (Salaries and Allowances) (Amendment) Act No. II of 1975"
    amendment_viii = "The Balochistan Assembly Members (Salaries and Allowances) (Amendment) Act No. VIII of 1975"
    assert not reprint_names_conflict(amendment_ii, "Balochistan", amendment_viii, "Balochistan")
    assert reprint_names_conflict(
        "The Balochistan Widgets (Amendment) Act No. II of 1975",
        "Balochistan",
        "Widgets Act No. II of 1975",
        "balochistan",
    )


@pytest.mark.asyncio
async def test_report_conflict_list_drops_the_auditor_false_positives(db):
    consolidated = Statute(
        name=CM_LISTING,
        jurisdiction="Balochistan",
        year_enacted=2014,
        source_name="BalochistanAssembly",
        source_url=CM_URL,
    )
    stub = Statute(
        name=STUB_NAME,
        jurisdiction="Balochistan",
        year_enacted=1975,
        source_name="BalochistanAssembly",
        source_url="https://pabalochistan.gov.pk/storage/1/act-iii-1975.pdf",
    )
    federal_speaker = Statute(
        name=FEDERAL_SPEAKER,
        jurisdiction="Federal",
        year_enacted=1975,
        source_name="NationalAssembly",
        source_url="https://na.gov.pk/uploads/documents/speaker-1975.pdf",
    )
    members_ii = Statute(
        name=MEMBERS_ACT_II,
        jurisdiction="Balochistan",
        year_enacted=1975,
        source_name="BalochistanAssembly",
        source_url="https://pabalochistan.gov.pk/storage/2/members-ii-1975.pdf",
    )
    members_viii = Statute(
        name=MEMBERS_ACT_VIII,
        jurisdiction="Balochistan",
        year_enacted=1975,
        source_name="BalochistanAssembly",
        source_url="https://pabalochistan.gov.pk/storage/3/members-viii-1975.pdf",
    )
    truncated = Statute(
        name=TRUNCATED_TITLE,
        jurisdiction="Balochistan",
        year_enacted=1975,
        source_name="BalochistanAssembly",
        source_url="https://pabalochistan.gov.pk/storage/4/privileges-fragment.pdf",
    )
    db.add_all([consolidated, stub, federal_speaker, members_ii, members_viii, truncated])
    await db.flush()
    provenance = await _provenance(db, url=CM_URL, content_hash="ab" * 32)
    staging = _staging(
        UUID("00000000-0000-4000-8000-0000000000c1"),
        url=CM_URL,
        provenance_id=provenance.id,
        raw_text=CM_PAGE.read_text(encoding="utf-8"),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        content_hash="cd" * 32,
    )
    staging.promoted_to_id = consolidated.id
    db.add(staging)
    await db.flush()

    report = await consolidated_reprint_report(db)

    assert report["writes"] == 0
    names = {item["stub_name"] for item in report["same_year_name_conflicts"]}
    assert names == {STUB_NAME}
    assert report["consolidated"][0]["stored_jurisdiction"] == "Balochistan"
