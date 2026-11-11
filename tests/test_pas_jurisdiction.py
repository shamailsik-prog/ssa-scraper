"""pas.gov.pk statutes are Sindh, and a provincial Act No. marker is not stored as Federal.

The source map already said SindhAssembly → Sindh. Promotion applied that map
only for KPAssembly, then stored an empty jurisdiction as Federal. A confident
extract skips AI, so nothing else filled the province. These tests keep that
path on Sindh, including the two 2026 pas.gov.pk acts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from scraper.extractors.deterministic import _extract_statute_by_contents, extract_statute_deterministic
from scraper.fetchers import record_provenance, stage_statute
from scraper.models import ScraperSource, Statute
from scraper.statute_quality_jurisdiction import (
    ACT_NO_MARKER_HEAD_CHARS,
    resolve_statute_jurisdiction,
)
from scraper.tasks.promotion import promote_statute_staging

FIXTURE = Path(__file__).parent / "fixtures" / "pas_sindh_act_no_xxxvi_2026.txt"
FIU_URL = "https://pas.gov.pk/index.php/acts/details/33/664"
IQRA_URL = "https://www.pas.gov.pk/index.php/acts/details/33/662"
FIU_NAME = "The Fatimiyah International University (FIU) at Karachi Act, 2026"
FS_IDS = (
    "966d98fc-9a20-4e0a-aeb1-6e01a1f792a7",
    "aa951b26-60c1-49de-a63f-d13a35523889",
)

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "backfill_pas_jurisdiction.py"
_SPEC = importlib.util.spec_from_file_location("backfill_pas_jurisdiction", _SCRIPT_PATH)
backfill = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(backfill)


def _act_body(*markers: str) -> str:
    head = "\n".join(markers)
    return (
        f"{head}\n"
        "1. Short title.- This Act may be called the Example Provincial Act, 2020 and it extends to the Province.\n"
        "2. Definitions.- In this Act the words in this section have the meanings assigned for the Province.\n"
        "3. Extent.- This Act extends to the whole of the Province and comes into force at once on publication.\n"
    )


def test_pas_host_and_sindh_assembly_map_beat_the_federal_default():
    text = FIXTURE.read_text(encoding="utf-8")
    assert "Sindh Act No. XXXVI of 2026" in text
    for explicit in (None, "", "Federal", "federal"):
        resolved = resolve_statute_jurisdiction(
            source_name="SindhAssembly",
            source_url=FIU_URL,
            explicit=explicit,
            text=text,
            act_no="Sindh Act No. XXXVI of 2026",
        )
        assert resolved == "Sindh"
    assert resolve_statute_jurisdiction(
        source_name="SindhAssembly",
        source_url="http://127.0.0.1/uploads/acts/fiu.pdf",
        explicit="Federal",
        text="1. Short title.- This Act extends to the Province of Sindh and comes into force at once.",
    ) == "Sindh"
    assert resolve_statute_jurisdiction(
        source_name="PakistanCode",
        source_url="https://pakistancode.gov.pk/english/example",
        detail_url=IQRA_URL,
        explicit="Federal",
    ) == "Sindh"
    assert resolve_statute_jurisdiction(
        source_name="SindhAssembly",
        source_url="https://pas.gov.pk/uploads/acts/fiu.pdf|XXXVI of 2026",
        explicit="Federal",
    ) == "Sindh"


def test_provincial_act_no_marker_is_not_stored_as_federal():
    assert resolve_statute_jurisdiction(
        source_name="NationalAssembly",
        source_url="https://na.gov.pk/uploads/documents/example.pdf",
        explicit="Federal",
        text=_act_body("Punjab Act No. IV of 2024"),
    ) == "Punjab"
    assert resolve_statute_jurisdiction(
        source_name="Senate",
        source_url="https://senate.gov.pk/uploads/documents/example.pdf",
        explicit="",
        act_no="Khyber Pakhtunkhwa Act No. XIII of 2026",
        text="1. Short title.- This Act may be called the Example Act, 2026.",
    ) == "Khyber Pakhtunkhwa"
    assert resolve_statute_jurisdiction(
        source_name="GazetteOfPakistan",
        source_url="http://pcp.gov.pk/siteimage/downloads/example.pdf",
        explicit="Federal",
        text=_act_body("Balochistan Act No. II of 2019"),
    ) == "Balochistan"
    assert resolve_statute_jurisdiction(
        source_name="PakistanCode",
        source_url="https://pakistancode.gov.pk/english/example",
        explicit="Federal",
        text=_act_body("sindh   act   no. XXXVI of 2026"),
    ) == "Sindh"
    buried = ("word " * 40) * ((ACT_NO_MARKER_HEAD_CHARS // 200) + 2)
    buried += "\nSindh Act No. I of 2020\n"
    assert len(buried) > ACT_NO_MARKER_HEAD_CHARS
    assert resolve_statute_jurisdiction(
        source_name="NationalAssembly",
        source_url="https://na.gov.pk/uploads/documents/example.pdf",
        explicit="Federal",
        text=buried,
    ) == "Federal"
    assert resolve_statute_jurisdiction(
        source_name="PakistanCode",
        source_url="https://pakistancode.gov.pk/english/example",
        explicit="Federal",
        text=_act_body("Act No. V of 1908"),
    ) == "Federal"
    assert resolve_statute_jurisdiction(
        source_name=None,
        source_url="https://example.test/act",
        explicit=None,
        text=_act_body(),
    ) is None


def test_lookalike_hosts_are_not_pas():
    for url in (
        "https://notpas.gov.pk/index.php/acts/details/33/664",
        "https://pas.gov.pk.example.com/index.php/acts/details/33/664",
        "https://example.com/?next=https://pas.gov.pk/index.php/acts/details/33/664",
        "https://sindhlaws.gov.pk/Gazette.aspx?pg=ACT",
    ):
        assert resolve_statute_jurisdiction(
            source_name="PakistanCode",
            source_url=url,
            explicit="Federal",
        ) == "Federal"


def test_extract_fiu_fixture_from_pas_is_sindh_even_when_labeled_federal():
    text = FIXTURE.read_text(encoding="utf-8")
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "SindhAssembly",
            "url": FIU_URL,
            "detail_url": FIU_URL,
            "act_title": FIU_NAME,
            "act_no": "Sindh Act No. XXXVI of 2026",
            "jurisdiction": "Federal",
        },
    )
    assert extracted["jurisdiction"] == "Sindh"
    assert extracted["extractor_confidence"] >= 0.9
    assert extracted["sections"]
    assert all(section["jurisdiction"] == "Sindh" for section in extracted["sections"])

    unlabeled = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "SindhAssembly",
            "url": "https://PAS.GOV.PK/index.php/acts/details/33/664",
            "act_title": FIU_NAME,
        },
    )
    assert unlabeled["jurisdiction"] == "Sindh"


def test_extract_punjab_marker_on_a_federal_source_is_not_federal():
    extracted = extract_statute_deterministic(
        html=None,
        text=_act_body("Punjab Act No. IV of 2024"),
        source_meta={
            "source_name": "NationalAssembly",
            "url": "https://na.gov.pk/uploads/documents/punjab-act.pdf",
            "jurisdiction": "Federal",
            "act_title": "Example Provincial Act, 2020",
        },
    )
    assert extracted["jurisdiction"] == "Punjab"
    assert all(section["jurisdiction"] == "Punjab" for section in extracted["sections"])


def test_pakistan_code_contents_path_keeps_a_sindh_act_number():
    text = FIXTURE.read_text(encoding="utf-8")
    sindh = _extract_statute_by_contents(
        text,
        FIU_NAME,
        {
            "source_name": "PakistanCode",
            "url": "https://pakistancode.gov.pk/english/example",
            "jurisdiction": "Federal",
        },
    )
    assert sindh["jurisdiction"] == "Sindh"
    federal = _extract_statute_by_contents(
        "Act No. V of 1908\n1. Short title.- This Act may be called the Code of Civil Procedure, 1908.",
        "Code of Civil Procedure, 1908",
        {
            "source_name": "PakistanCode",
            "url": "https://pakistancode.gov.pk/english/example",
            "jurisdiction": "Federal",
        },
    )
    assert federal["jurisdiction"] == "Federal"


def _section(text: str) -> dict:
    return {
        "section_number": "1",
        "section_title": "Short title",
        "section_text": text,
    }


async def _stage(db, source_name: str, url: str, raw_text: str, payload: dict):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == source_name))
    ).scalars().first()
    assert source is not None
    prov = await record_provenance(
        db,
        source=source,
        url=url,
        content=raw_text.encode("utf-8"),
        content_kind="text",
    )
    staging = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=raw_text,
        url=url,
        kind="statute",
    )
    staging.status = "extracted"
    staging.reconciled_json = payload
    return staging


async def test_promotion_stores_sindh_for_a_federal_labeled_pas_act(db):
    text = FIXTURE.read_text(encoding="utf-8")
    section = (
        "1. Short title.- This Act may be called the Fatimiyah International University "
        "(FIU) at Karachi Act, 2026 and it extends to the Province of Sindh."
    )
    staging = await _stage(
        db,
        "SindhAssembly",
        FIU_URL,
        text,
        {
            "statute_name": FIU_NAME,
            "year_enacted": 2026,
            "jurisdiction": "Federal",
            "statute_type": "act",
            "sections": [_section(section)],
        },
    )
    assert await promote_statute_staging(db, staging) == "promoted"
    statute = (await db.execute(select(Statute).where(Statute.id == staging.promoted_to_id))).scalars().one()
    assert statute.jurisdiction == "Sindh"
    assert statute.source_url == FIU_URL


async def test_promotion_uses_the_sindh_assembly_map_without_a_pas_host_or_marker(db):
    """The root cause: SindhAssembly with an empty jurisdiction was stored as Federal."""
    section = (
        "1. Short title and extent.- This Act may be called the Example Assembly Act, 2024 "
        "and it extends to the Province and comes into force at once."
    )
    staging = await _stage(
        db,
        "SindhAssembly",
        "http://127.0.0.1/uploads/acts/example-assembly-act-2024.pdf",
        section,
        {
            "statute_name": "The Example Assembly Act, 2024",
            "year_enacted": 2024,
            "statute_type": "act",
            "sections": [_section(section)],
        },
    )
    assert await promote_statute_staging(db, staging) == "promoted"
    statute = (await db.execute(select(Statute).where(Statute.id == staging.promoted_to_id))).scalars().one()
    assert statute.jurisdiction == "Sindh"


async def test_promotion_keeps_kp_and_federal_sources(db):
    kp_section = (
        "1. Short title.- This Act may be called the Example Khyber Act, 2020 "
        "and it extends to the Province and comes into force at once."
    )
    kp = await _stage(
        db,
        "KPAssembly",
        "https://www.pakp.gov.pk/wp-content/uploads/example-khyber-act.pdf",
        kp_section,
        {
            "statute_name": "The Example Khyber Act, 2020",
            "year_enacted": 2020,
            "jurisdiction": "Federal",
            "statute_type": "act",
            "sections": [_section(kp_section)],
        },
    )
    assert await promote_statute_staging(db, kp) == "promoted"
    kp_statute = (await db.execute(select(Statute).where(Statute.id == kp.promoted_to_id))).scalars().one()
    assert kp_statute.jurisdiction == "Khyber Pakhtunkhwa"

    federal_section = (
        "1. Title and extent of operation of the Code.- This Act shall be called the Sample "
        "Federal Code, and shall take effect throughout Pakistan."
    )
    federal = await _stage(
        db,
        "NationalAssembly",
        "https://na.gov.pk/uploads/documents/sample-federal-code.pdf",
        federal_section,
        {
            "statute_name": "The Sample Federal Code, 2020",
            "year_enacted": 2020,
            "jurisdiction": "Federal",
            "statute_type": "act",
            "sections": [_section(federal_section)],
        },
    )
    assert await promote_statute_staging(db, federal) == "promoted"
    federal_statute = (
        await db.execute(select(Statute).where(Statute.id == federal.promoted_to_id))
    ).scalars().one()
    assert federal_statute.jurisdiction == "Federal"


def test_backfill_precheck_matches_fs_two_rows_and_main_zero():
    assert backfill.evaluate_precheck(FS_IDS, expect_count=2, expect_ids=FS_IDS) is None
    with pytest.raises(backfill.PrecheckError):
        backfill.evaluate_precheck(FS_IDS, expect_count=0, expect_ids=[])
    with pytest.raises(backfill.PrecheckError):
        backfill.evaluate_precheck([], expect_count=2, expect_ids=list(FS_IDS))
    with pytest.raises(backfill.PrecheckError):
        backfill.evaluate_precheck(FS_IDS, expect_count=2, expect_ids=[FS_IDS[0], FS_IDS[0]])
    backfill.evaluate_precheck([], expect_count=0, expect_ids=[])
    epilog = backfill._OPS_EPILOG
    assert "--expect-count 2" in epilog
    assert "--expect-count 0" in epilog
    assert FS_IDS[0] in epilog
    assert FS_IDS[1] in epilog
    sql = backfill.render_rollback_sql(
        [
            {
                "id": FS_IDS[1],
                "jurisdiction": "Federal",
                "updated_at": "2026-11-11T08:00:00+00:00",
            }
        ]
    )
    assert f"WHERE id = '{FS_IDS[1]}'" in sql
    assert "jurisdiction = 'Federal'" in sql
    assert "AND jurisdiction = 'Sindh'" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql
    quoted = backfill.render_rollback_sql(
        [{"id": FS_IDS[0], "jurisdiction": "O'Brien", "updated_at": None}]
    )
    assert "jurisdiction = 'O''Brien'" in quoted


async def test_backfill_dry_run_and_apply_touch_only_non_sindh_pas_rows(db, tmp_path):
    pas_federal = Statute(
        id=UUID(FS_IDS[1]),
        name=FIU_NAME,
        jurisdiction="Federal",
        year_enacted=2026,
        source_name="SindhAssembly",
        source_url=FIU_URL,
    )
    pas_sindh = Statute(
        id=UUID(FS_IDS[0]),
        name="The Iqra University (Amendment) Act, 2026",
        jurisdiction="Sindh",
        year_enacted=2026,
        source_name="SindhAssembly",
        source_url=IQRA_URL,
    )
    www_punjab = Statute(
        name="The Example Punjab Labeled Act, 2021",
        jurisdiction="Punjab",
        year_enacted=2021,
        source_name="SindhAssembly",
        source_url="https://www.pas.gov.pk/index.php/acts/details/33/100",
    )
    lookalike = Statute(
        name="The Not Pas Act, 2021",
        jurisdiction="Federal",
        year_enacted=2021,
        source_name="SindhAssembly",
        source_url="https://notpas.gov.pk/index.php/acts/details/33/664",
    )
    sindhlaws = Statute(
        name="The Sindh Laws Portal Act, 2021",
        jurisdiction="Federal",
        year_enacted=2021,
        source_name="SindhAssembly",
        source_url="https://sindhlaws.gov.pk/setup/publications/example.pdf",
    )
    db.add_all([pas_federal, pas_sindh, www_punjab, lookalike, sindhlaws])
    await db.commit()

    backup = tmp_path / "pas_jurisdiction_backup.jsonl"
    rollback = tmp_path / "pas_jurisdiction_backup.jsonl.rollback.sql"
    www_id = str(www_punjab.id)
    dry = await backfill.run_backfill(
        db,
        apply=False,
        backup=backup,
        rollback_sql=rollback,
    )
    assert dry["dry_run"] is True
    assert dry["updated"] == 0
    assert dry["count"] == 2
    assert FS_IDS[1] in dry["ids"]
    assert FS_IDS[0] not in dry["ids"]
    refreshed = (await db.get(Statute, FS_IDS[1]))
    assert refreshed.jurisdiction == "Federal"
    assert "Federal" in backup.read_text(encoding="utf-8")
    assert "Sindh" in rollback.read_text(encoding="utf-8")

    with pytest.raises(backfill.PrecheckError):
        await backfill.run_backfill(
            db,
            apply=True,
            backup=backup,
            rollback_sql=rollback,
            expect_count=0,
            expect_ids=[],
        )
    assert (await db.get(Statute, FS_IDS[1])).jurisdiction == "Federal"

    www_id = str(www_punjab.id)
    applied = await backfill.run_backfill(
        db,
        apply=True,
        backup=backup,
        rollback_sql=rollback,
        expect_count=2,
        expect_ids=[FS_IDS[1], www_id],
    )
    assert applied["updated"] == 2

    async def _jurisdiction(statute_id) -> str:
        row = (
            await db.execute(
                select(Statute)
                .where(Statute.id == statute_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return row.jurisdiction

    assert await _jurisdiction(UUID(FS_IDS[1])) == "Sindh"
    assert await _jurisdiction(www_punjab.id) == "Sindh"
    assert await _jurisdiction(UUID(FS_IDS[0])) == "Sindh"
    assert await _jurisdiction(lookalike.id) == "Federal"
    assert await _jurisdiction(sindhlaws.id) == "Federal"
    rollback_text = rollback.read_text(encoding="utf-8")
    assert "jurisdiction = 'Federal'" in rollback_text
    assert "jurisdiction = 'Punjab'" in rollback_text
    assert f"WHERE id = '{FS_IDS[1]}'" in rollback_text

    again = await backfill.select_targets(db)
    assert again == []
    second = await backfill.run_backfill(
        db,
        apply=True,
        backup=backup,
        rollback_sql=rollback,
        expect_count=0,
        expect_ids=[],
    )
    assert second["updated"] == 0
    assert second["count"] == 0
