"""Dry-run section-0 report writes backup files and does not touch the database.

``--apply`` merges one visible section into the preceding section only when
the id and the text md5 still match the dry-run backup.
"""

from __future__ import annotations

import json
import subprocess
import sys
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
    classify_section_zero_csv,
    classify_section_zero_text,
    main,
    plan_section_zero_merges,
    precheck_section_zero,
    render_rollback_sql,
    run_section_zero_report,
    section_text_md5,
    write_report_files,
)

EVIDENCE = Path(__file__).resolve().parent / "fixtures" / "statute_quality"
from sqlalchemy import select

CLAUSE = "0) provide guidelines to the Registration Board for grant, renewal, suspension or cancellation of registrations."


def test_cli_defaults_to_dry_run_and_apply_requires_id_and_md5():
    args = build_parser().parse_args([])
    assert args.apply is False
    assert args.out == "section_zero_reports"
    assert build_parser().parse_args(["--apply", "--id", "x", "--md5", "ab"]).apply is True
    assert main(["--apply"]) == 2
    assert main(["--csv", str(EVIDENCE / "section_zero_fs.csv"), "--apply"]) == 2


def test_plan_merges_section_zero_into_the_preceding_section():
    sections = [
        {"id": "a", "section_number": "4", "text": "4. Powers and functions of the Authority.", "sort_key": 1},
        {"id": "b", "section_number": "0", "text": CLAUSE, "sort_key": 2},
        {"id": "c", "section_number": "5", "text": "5. Meetings of the Board.", "sort_key": 3},
        {"id": "d", "section_number": "0", "text": "0) leading clause with no previous section.", "sort_key": 0},
    ]
    plan = plan_section_zero_merges(sections)
    merged_text = "4. Powers and functions of the Authority.\n" + CLAUSE
    leading = "0) leading clause with no previous section."
    assert plan["merged"] == [
        {
            "zero_section_id": "b",
            "zero_section_number": "0",
            "section_zero_class": "A",
            "zero_md5": section_text_md5(CLAUSE),
            "into_section_id": "a",
            "into_section_number": "4",
            "before_md5": section_text_md5("4. Powers and functions of the Authority."),
            "after_md5": section_text_md5(merged_text),
            "merged_text": merged_text,
            "proposal": "merge",
            "skip_reason": None,
        }
    ]
    assert plan["skipped_no_previous"] == [
        {
            "zero_section_id": "d",
            "zero_section_number": "0",
            "section_zero_class": "A",
            "zero_md5": section_text_md5(leading),
            "into_section_id": None,
            "into_section_number": None,
            "before_md5": None,
            "after_md5": None,
            "merged_text": "",
            "proposal": "skip",
            "skip_reason": "no_preceding_section",
        }
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
            "versions": [
                {
                    "id": "66666666-6666-6666-6666-666666666666",
                    "version_no": 0,
                    "section_text": "older clause text",
                    "text_hash": "olderhash",
                    "effective_from": "2017-01-01",
                    "effective_to": "2018-06-12",
                    "amending_instrument_id": None,
                    "amending_instrument_text": None,
                    "version_confidence": 0.4,
                    "source_provenance_id": None,
                },
                {
                    "id": "33333333-3333-3333-3333-333333333333",
                    "version_no": 1,
                    "section_text": text,
                    "text_hash": "def",
                    "effective_from": "2018-06-13",
                    "effective_to": None,
                    "amending_instrument_id": "77777777-7777-7777-7777-777777777777",
                    "amending_instrument_text": "Act XXIII of 2018",
                    "version_confidence": 0.5,
                    "source_provenance_id": None,
                },
            ],
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
    assert "DATE '2018-06-13'" in sql
    assert "DATE '2017-01-01'" in sql
    assert "older clause text" in sql
    assert "Act XXIII of 2018" in sql
    assert "77777777-7777-7777-7777-777777777777" in sql
    assert "DELETE FROM statute_section_version" in sql
    assert "embedding_queue" in sql
    kept = dict(row)
    kept["merged_version_id"] = "88888888-8888-8888-8888-888888888888"
    kept_sql = render_rollback_sql([kept])
    assert "DELETE FROM statute_section_version" not in kept_sql
    assert "embedding_queue" in kept_sql
    paths = write_report_files(tmp_path, [row])
    backup = Path(paths["backup_jsonl"]).read_text(encoding="utf-8")
    assert json.loads(backup)["proposal"] == "merge"
    assert "DELETE FROM statute_section" not in Path(paths["rollback_sql"]).read_text(encoding="utf-8").split("BEGIN;", 1)[0]


def test_precheck_refuses_a_changed_md5_or_a_backup_without_version_history():
    version_id = "33333333-3333-3333-3333-333333333333"
    into_version = "55555555-5555-5555-5555-555555555555"
    zero = {
        "id": "22222222-2222-2222-2222-222222222222",
        "section_number": "0",
        "text": CLAUSE,
        "version_id": version_id,
    }
    into = {
        "id": "44444444-4444-4444-4444-444444444444",
        "text": "4. Powers.",
        "version_id": into_version,
    }
    backup = {
        "proposal": "merge",
        "merged_version_id": None,
        "zero_section": {
            "id": zero["id"],
            "md5": section_text_md5(CLAUSE),
            "version_id": version_id,
            "versions": [
                {
                    "id": version_id,
                    "version_no": 1,
                    "section_text": CLAUSE,
                    "text_hash": "abc",
                    "effective_from": "2018-06-13",
                }
            ],
        },
        "preceding_section": {
            "id": into["id"],
            "md5": section_text_md5(into["text"]),
            "version_id": into_version,
        },
        "relation_ids": [],
    }
    precheck_section_zero(
        claimed_id=zero["id"],
        claimed_md5=section_text_md5(CLAUSE),
        backup_row=backup,
        live_zero=zero,
        live_into=into,
        live_relation_ids=[],
        live_version_ids=[version_id],
    )
    with pytest.raises(SectionZeroPrecheckError, match="md5"):
        precheck_section_zero(
            claimed_id=zero["id"],
            claimed_md5="ab" * 16,
            backup_row=backup,
            live_zero=zero,
            live_into=into,
            live_relation_ids=[],
            live_version_ids=[version_id],
        )
    thin = {
        **backup,
        "zero_section": {"id": zero["id"], "md5": section_text_md5(CLAUSE), "version_id": version_id},
    }
    with pytest.raises(SectionZeroPrecheckError, match="version history"):
        precheck_section_zero(
            claimed_id=zero["id"],
            claimed_md5=section_text_md5(CLAUSE),
            backup_row=thin,
            live_zero=zero,
            live_into=into,
            live_relation_ids=[],
            live_version_ids=[version_id],
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
    visible_row = next(row for row in report["rows"] if row["zero_section_id"] == str(seeded["zero"].id))
    previous_text = "4. Powers and functions of the Authority shall be as follows."
    merged_text = previous_text + "\n" + CLAUSE
    assert visible_row["section_zero_class"] == "A"
    assert visible_row["into_section_number"] == "4"
    assert visible_row["before_md5"] == section_text_md5(previous_text)
    assert visible_row["after_md5"] == section_text_md5(merged_text)
    assert visible_row["before_md5"] != visible_row["after_md5"]
    leading_row = next(row for row in report["rows"] if row["skip_reason"] == "no_preceding_section")
    assert leading_row["section_zero_class"] == "A"
    assert leading_row["before_md5"] is None
    assert leading_row["after_md5"] is None
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
    sql = Path(report["rollback_sql"]).read_text(encoding="utf-8")
    assert "embedding_queue" in sql
    with pytest.raises(SectionZeroPrecheckError, match="recovery files"):
        await run_section_zero_report(apply=False, out_dir=tmp_path)
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


def test_script_invocation_imports_scraper_from_the_repository_root():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/report_section_zero.py",
            "--csv",
            str(EVIDENCE / "section_zero_fs.csv"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["writes"] is False
    assert payload["counts"]["A"] == 77
    assert payload["counts"]["unknown"] == 0


def test_classifier_sorts_the_three_shapes_and_leaves_anything_else_unknown():
    assert classify_section_zero_text(
        "0) provide guidelines to the Registration Board for grant, renewal, suspension or cancellation"
    ) == "A"
    assert classify_section_zero_text('0) " prescribed " means prcscribed by regulations; (k) "prjncipal "') == "A"
    assert classify_section_zero_text("0) the Treasurer; (m) such other persons") == "A"
    assert classify_section_zero_text('0)) "member" means a member of the Policy Board') == "A"
    assert classify_section_zero_text("0.2 per cent of morphine") == "B"
    assert classify_section_zero_text("0.5% of the value of the property") == "B"
    assert classify_section_zero_text("0.1% of the gross amount of sales") == "B"
    assert classify_section_zero_text("0.50%will be deducted from the basic pay") == "B"
    assert classify_section_zero_text("0.1 million fioe") == "B"
    assert classify_section_zero_text("0.09, 90. l0") == "B"
    assert classify_section_zero_text("0. [)isputc [orscparate possession") == "C"
    assert classify_section_zero_text("0. No. 7 of 1972)") == "C"
    assert classify_section_zero_text("0. 1993 (Act XIV of 1993)") == "C"
    assert classify_section_zero_text("0 . iby the power to issue a search Warrant") == "C"
    assert classify_section_zero_text("0..1033 REGISTERED No.") == "C"
    assert classify_section_zero_text("section 4 continues without a zero marker") == "unknown"


def test_evidence_csvs_sort_every_section_zero_row(capsys):
    fs = EVIDENCE / "section_zero_fs.csv"
    main_csv = EVIDENCE / "section_zero_main.csv"
    assert main(["--csv", str(fs), "--csv", str(main_csv)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["writes"] is False
    rows = payload["rows"]
    assert [row["source_csv"] for row in rows].count("section_zero_fs.csv") == 90
    assert [row["source_csv"] for row in rows].count("section_zero_main.csv") == 41
    assert payload["counts"]["unknown"] == 0
    assert payload["counts"]["A"] > 0
    assert payload["counts"]["B"] > 0
    assert payload["counts"]["C"] > 0
    by_id = {row["statute_id"]: row for row in rows}
    assert len(by_id) == 131
    for statute_id in (
        "aa8a983e-379d-4772-b1e5-83ffcc7edc0a",
        "97b7dbea-1931-4c8e-a617-3a0c40726ac2",
        "a1dd5b62-ab68-480a-b71e-841a2e05a668",
        "4363583c-061d-4854-86d6-9ea4acec1459",
    ):
        assert by_id[statute_id]["section_zero_class"] == "A"
        assert by_id[statute_id]["proposed_merge_target"] == "preceding section"
        assert by_id[statute_id]["before_md5"] is None
        assert by_id[statute_id]["after_md5"] is None
    assert by_id["aa8a983e-379d-4772-b1e5-83ffcc7edc0a"]["zero_md5"] == "fe7e4a585fff84d477b95a42fab2b6fd"
    for statute_id in (
        "cedfb485-5e3b-47aa-abd4-41c6dd3a3616",
        "8d11f55d-3168-4e94-be2b-b5c62b9be8de",
        "a2f068d4-6dc8-4683-bc6e-761ef9311ebf",
        "06ea4193-99e3-463b-9175-8704fe42fb04",
        "4e73e42b-8728-48de-8d4c-01161cbbb6b3",
        "fa78deb6-9ea2-49dd-bbc7-6a63ad7dc186",
    ):
        assert by_id[statute_id]["section_zero_class"] == "B"
    assert by_id["cedfb485-5e3b-47aa-abd4-41c6dd3a3616"]["proposal"] == "skip"
    assert by_id["cedfb485-5e3b-47aa-abd4-41c6dd3a3616"]["skip_reason"] == "no_preceding_section"
    assert by_id["a2f068d4-6dc8-4683-bc6e-761ef9311ebf"]["proposed_merge_target"] == "preceding section"
    assert by_id["ad06f94d-2b63-48f2-ab33-3afe06de41f3"]["section_zero_class"] == "C"
    assert by_id["ad06f94d-2b63-48f2-ab33-3afe06de41f3"]["zero_md5"] == "94a8654928de4d67e767b13cc5411960"
    direct = classify_section_zero_csv(fs) + classify_section_zero_csv(main_csv)
    assert [row["section_zero_class"] for row in direct] == [row["section_zero_class"] for row in rows]
