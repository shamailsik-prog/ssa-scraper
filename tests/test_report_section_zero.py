"""Dry-run section-0 report writes backup files and does not touch the database.

``--apply`` merges one visible section into the preceding section only when
the id and the text md5 still match the dry-run backup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scraper.models import (
    DocumentQuarantine,
    Instrument,
    InstrumentSectionRelation,
    Statute,
    StatuteSection,
    StatuteSectionVersion,
)
from scripts.report_section_zero import (
    SectionZeroPrecheckError,
    build_parser,
    main,
    plan_section_zero_merges,
    precheck_section_zero,
    render_rollback_sql,
    run_section_zero_report,
    section_text_md5,
    write_report_files,
)
from sqlalchemy import select

CLAUSE = "0) provide guidelines to the Registration Board for grant, renewal, suspension or cancellation of registrations."


def test_cli_defaults_to_dry_run_and_apply_requires_id_and_md5():
    args = build_parser().parse_args([])
    assert args.apply is False
    assert args.out == "section_zero_reports"
    assert build_parser().parse_args(["--apply", "--id", "x", "--md5", "ab"]).apply is True
    assert main(["--apply"]) == 2


def test_plan_merges_section_zero_into_the_preceding_section():
    sections = [
        {"id": "a", "section_number": "4", "text": "4. Powers and functions of the Authority.", "sort_key": 1},
        {"id": "b", "section_number": "0", "text": CLAUSE, "sort_key": 2},
        {"id": "c", "section_number": "5", "text": "5. Meetings of the Board.", "sort_key": 3},
        {"id": "d", "section_number": "0", "text": "0) leading clause with no previous section.", "sort_key": 0},
    ]
    plan = plan_section_zero_merges(sections)
    assert plan["merged"] == [
        {
            "zero_section_id": "b",
            "zero_section_number": "0",
            "into_section_id": "a",
            "into_section_number": "4",
            "merged_text": "4. Powers and functions of the Authority.\n" + CLAUSE,
        }
    ]
    assert plan["skipped_no_previous"] == [
        {"zero_section_id": "d", "zero_section_number": "0"}
    ]
    assert "provide guidelines" in plan["merged_text_by_target"]["a"]


def test_rollback_sql_is_not_executed_and_quotes_section_text(tmp_path):
    text = "0) board's guidelines $sz$ stay quoted"
    row = {
        "proposal": "merge",
        "statute_id": "11111111-1111-1111-1111-111111111111",
        "relation_ids": [],
        "merged_text": "4. Powers.\n" + text,
        "merged_text_hash": "abc",
        "zero_section": {
            "id": "22222222-2222-2222-2222-222222222222",
            "section_title": "Functions",
            "chapter": None,
            "part": None,
            "sort_key": 2,
            "version_id": "33333333-3333-3333-3333-333333333333",
            "version_no": 1,
            "section_text": text,
            "text_hash": "def",
            "version_confidence": 0.5,
        },
        "preceding_section": {
            "id": "44444444-4444-4444-4444-444444444444",
            "version_id": "55555555-5555-5555-5555-555555555555",
            "version_no": 1,
        },
    }
    sql = render_rollback_sql([row])
    assert "report_section_zero.py does not execute this file" in sql
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "22222222-2222-2222-2222-222222222222" in sql
    assert "board's guidelines" in sql
    assert "$szx$" in sql
    paths = write_report_files(tmp_path, [row])
    backup = Path(paths["backup_jsonl"]).read_text(encoding="utf-8")
    assert json.loads(backup)["proposal"] == "merge"
    assert "DELETE FROM statute_section" not in Path(paths["rollback_sql"]).read_text(encoding="utf-8").split("BEGIN;", 1)[0]


def test_precheck_refuses_a_changed_md5():
    zero = {"id": "22222222-2222-2222-2222-222222222222", "section_number": "0", "text": CLAUSE}
    into = {"id": "44444444-4444-4444-4444-444444444444", "text": "4. Powers."}
    backup = {
        "proposal": "merge",
        "zero_section": {"id": zero["id"], "md5": section_text_md5(CLAUSE)},
        "preceding_section": {"id": into["id"], "md5": section_text_md5(into["text"])},
        "relation_ids": [],
    }
    precheck_section_zero(
        claimed_id=zero["id"],
        claimed_md5=section_text_md5(CLAUSE),
        backup_row=backup,
        live_zero=zero,
        live_into=into,
        live_relation_ids=[],
    )
    with pytest.raises(SectionZeroPrecheckError, match="md5"):
        precheck_section_zero(
            claimed_id=zero["id"],
            claimed_md5="ab" * 16,
            backup_row=backup,
            live_zero=zero,
            live_into=into,
            live_relation_ids=[],
        )


async def _add_section(db, statute_id, number: str, text: str, sort_key: int) -> StatuteSection:
    sec = StatuteSection(statute_id=statute_id, section_number=number, sort_key=sort_key, section_title=f"s.{number}")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(
        section_id=sec.id,
        version_no=1,
        section_text=text,
        text_hash=f"{number}-{sort_key}".ljust(64, "b"),
    )
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    await db.flush()
    return sec


async def _seed(db):
    visible = Statute(
        name="The Islamabad Healthcare Regulation Act, 2018",
        jurisdiction="Federal",
        source_url="https://senate.gov.pk/uploads/documents/1528868742_798.pdf",
        source_name="Senate",
    )
    hidden = Statute(
        name="Hidden Healthcare Regulation Act, 2018",
        jurisdiction="Federal",
        source_url="https://senate.gov.pk/uploads/documents/hidden.pdf",
        source_name="Senate",
    )
    leading = Statute(
        name="Leading Zero Section Act, 2018",
        jurisdiction="Federal",
        source_url="https://senate.gov.pk/uploads/documents/leading.pdf",
        source_name="Senate",
    )
    other = Statute(
        name="Ordinary Numbered Act, 2018",
        jurisdiction="Federal",
        source_url="https://senate.gov.pk/uploads/documents/ordinary.pdf",
        source_name="Senate",
    )
    db.add_all([visible, hidden, leading, other])
    await db.flush()
    previous = await _add_section(db, visible.id, "4", "4. Powers and functions of the Authority shall be as follows.", 1)
    zero = await _add_section(db, visible.id, "0", CLAUSE, 2)
    await _add_section(db, visible.id, "5", "5. Meetings of the Board shall be held each quarter.", 3)
    hidden_zero = await _add_section(db, hidden.id, "0", CLAUSE, 2)
    await _add_section(db, hidden.id, "4", "4. Hidden powers of the Authority under this Act.", 1)
    await _add_section(db, leading.id, "0", "0) leading clause with no previous section in this Act.", 1)
    await _add_section(db, leading.id, "1", "1. Short title. This Act may be called the Leading Zero Section Act.", 2)
    await _add_section(db, other.id, "0A", "0A. Not a stored section number zero and must stay listed as 0A.", 1)
    db.add(DocumentQuarantine(doc_type="statute", doc_id=hidden.id, reason="operator:hold", run_id="hold"))
    instrument = Instrument(type="amendment", full_text_hash="d" * 64, title="Note on section 0")
    db.add(instrument)
    await db.flush()
    db.add(
        InstrumentSectionRelation(
            source_instrument_id=instrument.id,
            target_statute_id=visible.id,
            target_statute_section_id=zero.id,
            target_section_key="0",
            amendment_operation="insert",
            relation_phrase="inserted in",
            target_mention_raw="section 0",
            span_start=0,
            span_end=9,
            evidence_snippet="section 0 of the Act",
        )
    )
    await db.commit()
    return {"visible": visible, "hidden": hidden, "zero": zero, "previous": previous, "hidden_zero": hidden_zero}


async def test_dry_run_lists_visible_section_zero_and_writes_files_without_deleting(db, tmp_path):
    seeded = await _seed(db)
    report = await run_section_zero_report(apply=False, out_dir=tmp_path)
    assert report["dry_run"] is True
    assert report["apply"] is False
    assert report["merged"] == 1
    assert report["skipped_no_previous"] == 1
    ids = {row["zero_section_id"] for row in report["rows"]}
    assert str(seeded["zero"].id) in ids
    assert str(seeded["hidden_zero"].id) not in ids
    assert all(row["statute_name"] != "Ordinary Numbered Act, 2018" for row in report["rows"])
    backup_rows = [json.loads(line) for line in Path(report["backup_jsonl"]).read_text(encoding="utf-8").splitlines()]
    assert {row["proposal"] for row in backup_rows} == {"merge", "skip"}
    sql = Path(report["rollback_sql"]).read_text(encoding="utf-8")
    assert "does not execute this file" in sql
    assert str(seeded["zero"].id) in sql
    still = (
        await db.execute(select(StatuteSection).where(StatuteSection.id == seeded["zero"].id))
    ).scalars().one()
    assert still.section_number == "0"
    quarantine = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == seeded["hidden"].id))
    ).scalars().one()
    assert quarantine.reason == "operator:hold"


async def test_apply_refuses_a_bad_md5_and_merges_when_the_precheck_matches(db, tmp_path):
    seeded = await _seed(db)
    zero_uuid = seeded["zero"].id
    visible_id = seeded["visible"].id
    hidden_id = seeded["hidden"].id
    hidden_zero_id = seeded["hidden_zero"].id
    leading_id = next(
        statute_id
        for statute_id in (
            await db.execute(select(Statute.id).where(Statute.name == "Leading Zero Section Act, 2018"))
        ).scalars().all()
    )
    dry = await run_section_zero_report(apply=False, out_dir=tmp_path)
    zero_id = str(zero_uuid)
    with pytest.raises(SectionZeroPrecheckError):
        await run_section_zero_report(
            apply=True,
            section_id=zero_id,
            section_md5="ab" * 16,
            out_dir=tmp_path,
        )
    db.expire_all()
    still = (
        await db.execute(select(StatuteSection).where(StatuteSection.id == zero_uuid))
    ).scalars().one()
    assert still.section_number == "0"

    digest = next(row["zero_md5"] for row in dry["rows"] if row["zero_section_id"] == zero_id)
    applied = await run_section_zero_report(
        apply=True,
        section_id=zero_id,
        section_md5=digest,
        out_dir=tmp_path,
    )
    assert applied["applied_section_id"] == zero_id
    db.expire_all()
    numbers = (
        await db.execute(
            select(StatuteSection.section_number).where(StatuteSection.statute_id == visible_id)
        )
    ).scalars().all()
    assert "0" not in numbers
    assert set(numbers) >= {"4", "5"}
    target = (
        await db.execute(
            select(StatuteSection).where(
                StatuteSection.statute_id == visible_id,
                StatuteSection.section_number == "4",
            )
        )
    ).scalars().one()
    current = (
        await db.execute(
            select(StatuteSectionVersion).where(StatuteSectionVersion.id == target.current_version_id)
        )
    ).scalars().one()
    assert "Powers and functions" in current.section_text
    assert "provide guidelines" in current.section_text
    relation = (
        await db.execute(
            select(InstrumentSectionRelation).where(
                InstrumentSectionRelation.target_statute_id == visible_id
            )
        )
    ).scalars().one()
    assert relation.target_statute_section_id == target.id
    hidden = (
        await db.execute(select(StatuteSection).where(StatuteSection.id == hidden_zero_id))
    ).scalars().one()
    assert hidden.section_number == "0"
    quarantine = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == hidden_id))
    ).scalars().one()
    assert quarantine.reason == "operator:hold"
    leading_numbers = (
        await db.execute(select(StatuteSection.section_number).where(StatuteSection.statute_id == leading_id))
    ).scalars().all()
    assert leading_numbers.count("0") == 1
