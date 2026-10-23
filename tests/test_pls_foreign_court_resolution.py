"""Foreign PLS court in heading must not resolve to Supreme Court of Pakistan."""

import pytest

from scraper.config import settings
from scraper.extractors.deterministic import extract_judgment_deterministic
from scraper.parsers.pls_court_resolution import (
    court_from_pls_header_block,
    proposed_court_name,
    resolve_pls_court,
    sc_hc_mislabel_fix,
)
from scraper.tasks.judgment_court_backfill import find_court_fixes


FOREIGN_MLD = (
    "1990 MLD 947\n"
    "[Supreme Court of India]\n"
    "Before X, J\n"
    "A---Petitioner\n"
    "versus\n"
    "B---Respondent\n"
    "Civil Appeal No. 1 of 1989, decided on 1st January, 1990.\n"
)


def test_header_block_reads_foreign_supreme_court():
    assert court_from_pls_header_block(FOREIGN_MLD) == "Supreme Court of India"


def test_resolve_overrides_grid_sc_pakistan_default():
    resolved = resolve_pls_court("Supreme Court of Pakistan", FOREIGN_MLD, citation_end=len("1990 MLD 947"))
    assert resolved == "Supreme Court of India"


def test_extract_with_flag_on_prefers_foreign_bracket():
    out = extract_judgment_deterministic(
        html=None,
        text=FOREIGN_MLD,
        source_meta={"court": "Supreme Court of Pakistan", "citation": "1990 MLD 947"},
    )
    assert out["court"] == "Supreme Court of India"


def test_mutant_resolution_flag_off_keeps_row_default(monkeypatch):
    monkeypatch.setattr(settings, "SSA_PLS_COURT_RESOLUTION", False)
    out = extract_judgment_deterministic(
        html=None,
        text=FOREIGN_MLD,
        source_meta={"court": "Supreme Court of Pakistan", "citation": "1990 MLD 947"},
    )
    assert out["court"] == "Supreme Court of Pakistan"


def test_sc_hc_swap_proposal():
    text = (
        "2021 SCMR 1\n"
        "[Lahore]\n"
        "IN THE HIGH COURT OF LAHORE\n"
        "Before X, J\n"
        "A versus B\n"
    )
    assert sc_hc_mislabel_fix("Supreme Court of Pakistan", text) is not None
    prop = proposed_court_name("Supreme Court of Pakistan", text)
    assert prop and prop != "Supreme Court of Pakistan"


@pytest.mark.asyncio
async def test_backfill_dry_run_and_apply_undo(db, monkeypatch):
    from uuid import uuid4

    from scraper.models import Judgment
    from scraper.tasks.judgment_court_backfill import _record_run, apply_court_fixes, undo_run

    monkeypatch.setattr(settings, "SSA_PLS_COURT_RESOLUTION", True)
    jid = uuid4()
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name="Supreme Court of Pakistan",
            canonical_citation="1990 MLD 947",
            full_text=FOREIGN_MLD + ("x" * 600),
            full_text_hash="abc123foreign",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    dry = await find_court_fixes(db)
    assert dry["count"] >= 1
    assert any(f["id"] == str(jid) for f in dry["fixes"])
    run_id = "testrun001"
    await _record_run(db, run_id)
    await apply_court_fixes(db, run_id, dry["fixes"])
    await db.commit()
    j = await db.get(Judgment, jid)
    assert j.court_name == "Supreme Court of India"
    await undo_run(db, run_id)
    await db.refresh(j)
    assert j.court_name == "Supreme Court of Pakistan"
