"""Foreign PLS court in heading must not resolve to Supreme Court of Pakistan."""

import pytest

from scraper.config import Settings, settings
from scraper.extractors.deterministic import extract_judgment_deterministic
from scraper.parsers.pls_court_resolution import (
    court_from_pls_header_block,
    foreign_court_from_first_300_chars,
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

SC_APPEAL_FROM_LHC = (
    "PLD 2020 SC 100\n"
    "[Supreme Court of Pakistan]\n"
    "IN THE SUPREME COURT OF PAKISTAN\n"
    "(On appeal from the judgment of the Lahore High Court, Lahore dated 1st January, 2019)\n"
    "Before X, J\n"
    "A versus B\n"
)

MLD_BODY_SC_MENTION = (
    "2021 MLD 1954\n"
    "[Lahore High Court]\n"
    "IN THE HIGH COURT OF LAHORE\n"
    "Before X, J\n"
    "A versus B\n"
    "In the Supreme Court of Pakistan it was held that this appeal fails.\n"
)


def test_header_block_reads_foreign_supreme_court():
    assert court_from_pls_header_block(FOREIGN_MLD) == "Supreme Court of India"


def test_resolve_overrides_grid_sc_pakistan_default():
    resolved = resolve_pls_court("Supreme Court of Pakistan", FOREIGN_MLD, citation_end=len("1990 MLD 947"))
    assert resolved == "Supreme Court of India"


def test_extract_with_flag_on_prefers_foreign_bracket(monkeypatch):
    monkeypatch.setattr(settings, "SSA_PLS_COURT_RESOLUTION", True)
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


def test_h1_pld_sc_appeal_from_lhc_not_relabeled():
    assert proposed_court_name("Supreme Court of Pakistan", SC_APPEAL_FROM_LHC, citation="PLD 2020 SC 100") is None
    assert court_from_pls_header_block(SC_APPEAL_FROM_LHC) is None


def test_w1_hc_stored_body_sc_mention_does_not_swap_to_sc():
    assert sc_hc_mislabel_fix("Lahore High Court", MLD_BODY_SC_MENTION, citation="2021 MLD 1954") is None
    assert proposed_court_name("Lahore High Court", MLD_BODY_SC_MENTION, citation="2021 MLD 1954") is None


@pytest.mark.parametrize(
    "bracket, expected",
    [
        ("[Supreme Court of India (India)]", "Supreme Court of India"),
        ("[Supreme Court of U.K.]", "Supreme Court of UK"),
        ("[Supreme Court (AJ&K)]", "Azad Jammu and Kashmir Supreme Court"),
    ],
)
def test_f2_foreign_canonical_display_names(bracket, expected):
    text = f"1990 MLD 1\n{bracket}\nBefore X, J\nA versus B\n"
    assert foreign_court_from_first_300_chars(text) == expected


def test_sc_hc_swap_proposal_non_pld_sc_reporter():
    text = (
        "2021 MLD 1\n"
        "[Lahore]\n"
        "IN THE HIGH COURT OF LAHORE\n"
        "Before X, J\n"
        "A versus B\n"
    )
    prop = proposed_court_name("Supreme Court of Pakistan", text, citation="2021 MLD 1")
    assert prop and "Lahore" in prop


@pytest.mark.asyncio
async def test_backfill_dry_run_and_apply_undo(db, monkeypatch):
    from uuid import uuid4

    from scraper.models import Judgment
    from scraper.tasks.judgment_court_backfill import _record_run, apply_court_fixes, undo_run

    monkeypatch.setattr(settings, "SSA_PLS_COURT_RESOLUTION", True)
    monkeypatch.setattr(settings, "SSA_JUDGMENT_COURT_ID_BACKFILL", True)
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
    fixes = [f for f in dry["fixes"] if f["id"] == str(jid)]
    await apply_court_fixes(db, run_id, fixes)
    await db.commit()
    j = await db.get(Judgment, jid)
    assert j.court_name == "Supreme Court of India"
    j.court_name = "Manually Edited Court"
    await db.commit()
    await undo_run(db, run_id)
    await db.refresh(j)
    assert j.court_name == "Manually Edited Court"


@pytest.mark.asyncio
async def test_u2_undo_restores_only_when_apply_value_unchanged(db, monkeypatch):
    from uuid import uuid4

    from scraper.models import Judgment
    from scraper.tasks.judgment_court_backfill import _record_run, apply_court_fixes, undo_run

    monkeypatch.setattr(settings, "SSA_PLS_COURT_RESOLUTION", True)
    monkeypatch.setattr(settings, "SSA_JUDGMENT_COURT_ID_BACKFILL", True)
    jid = uuid4()
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name="Supreme Court of Pakistan",
            canonical_citation="1991 MLD 948",
            full_text=FOREIGN_MLD.replace("947", "948") + ("y" * 600),
            full_text_hash="abc123foreign2",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    dry = await find_court_fixes(db)
    fixes = [f for f in dry["fixes"] if f["id"] == str(jid)]
    run_id = "testrun002"
    await _record_run(db, run_id)
    await apply_court_fixes(db, run_id, fixes)
    await db.commit()
    await undo_run(db, run_id)
    j = await db.get(Judgment, jid)
    assert j.court_name == "Supreme Court of Pakistan"


def test_mutant_b4_config_pls_court_resolution_default_off():
    assert Settings.model_fields["SSA_PLS_COURT_RESOLUTION"].default is False
