"""Read-only pabalochistan consolidated-reprint report.

The report proposes the base-Act name and year and flags a same-year stub
whose name differs. It does not write, and it has no --apply switch.
"""

from __future__ import annotations

import pytest

from scraper.models import SourceProvenance, Statute, StatutesStaging
from scraper.tasks.pab_consolidated_report import build_parser, consolidated_reprint_report

from tests.test_pab_consolidated_heading import (
    AMENDMENT_LISTING,
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


def test_report_cli_has_no_apply_switch():
    parser = build_parser()
    assert all(getattr(action, "dest", None) != "apply" for action in parser._actions)
    with pytest.raises(SystemExit):
        parser.parse_args(["--apply"])


@pytest.mark.asyncio
async def test_report_lists_proposed_base_act_and_flags_a_differing_stub(db):
    text = CM_PAGE.read_text(encoding="utf-8")
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
    wildlife = Statute(
        name=WILDLIFE_NAME,
        jurisdiction="Balochistan",
        year_enacted=1975,
        source_name="PakistanCode",
        source_url="https://pakistancode.gov.pk/english/UY2FqaJw1-apaUY2Fqa-ap%3D%3D-sg-jjjjjjjjjjjjj",
    )
    db.add_all([consolidated, stub, wildlife])
    await db.flush()

    provenance = SourceProvenance(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=CM_URL,
        content_hash="a" * 64,
        content_kind="text",
        byte_size=len(text),
    )
    other_host = SourceProvenance(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url="https://balochistancode.gob.pk/Document.aspx?docid=1",
        content_hash="b" * 64,
        content_kind="text",
        byte_size=len(text),
    )
    amendment_provenance = SourceProvenance(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=AMENDMENT_URL,
        content_hash="c" * 64,
        content_kind="text",
        byte_size=10,
    )
    db.add_all([provenance, other_host, amendment_provenance])
    await db.flush()

    staging = StatutesStaging(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=CM_URL,
        provenance_id=provenance.id,
        content_hash="d" * 64,
        raw_text=text,
        promoted_to_id=consolidated.id,
        status="promoted",
    )
    code_host = StatutesStaging(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url="https://balochistancode.gob.pk/Document.aspx?docid=1",
        provenance_id=other_host.id,
        content_hash="e" * 64,
        raw_text=text,
        status="extracted",
    )
    amendment = StatutesStaging(
        source_name="BalochistanAssembly",
        access_method="public",
        source_url=AMENDMENT_URL,
        provenance_id=amendment_provenance.id,
        content_hash="f" * 64,
        raw_text=AMENDMENT_PAGE.read_text(encoding="utf-8"),
        reconciled_json={"statute_name": AMENDMENT_LISTING, "year_enacted": 2025},
        status="extracted",
    )
    db.add_all([staging, code_host, amendment])
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
