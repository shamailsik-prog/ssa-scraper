"""Header citation re-key detection and save-time guard."""

from __future__ import annotations

import pytest

from scraper.config import settings
from scraper.parsers.header_citation import (
    citation_key,
    leading_header_citation_judgment,
    leading_header_citation_staging,
    mismatch_kind,
    rekey_actionable,
)
from scraper.tasks.citation_header_rekey import scan_header_rekeys


def _header_block(citation_line: str, *, court: str = "Lahore") -> str:
    return (
        f"{citation_line}\n[{court}]\n"
        "Before Muhammad Bashir Khan Jehangiri, J\n"
        "Mst. KAUSAR---Petitioner\nversus\nMUHAMMAD MUSHTAQ---Respondent\n"
        "Civil Revision No.329 of 1989, decided on 22nd November, 1989.\n"
        + "The court held that the appeal lacks merit. " * 40
    )


@pytest.mark.parametrize(
    "stored_line,header_line,expect_kind,actionable",
    [
        ("1986 CLC 1008", "1986 C L C 1011", "page_only", True),
        ("1986 CLC 1011", "1986 CLC 1016", "page_only", True),
        ("1986 CLC 2355", "1986 CLC 2255", "page_only", True),
        ("2000 PCrLJ 1052", "2000 PCrLJ 1069", "page_only", True),
        ("1984 CLC 1413", "1984 CLC 1113", "page_only", True),
    ],
)
def test_monitor_examples_page_only_mismatch(stored_line, header_line, expect_kind, actionable):
    text = _header_block(header_line)
    header = leading_header_citation_judgment(text)
    assert header
    kind = mismatch_kind(stored_line, header)
    assert kind == expect_kind
    assert rekey_actionable(stored_line, header or "", text) is actionable


def test_1994_pcrj_reporter_year_requires_anchor_for_judgment_rekey():
    stored = "1994 PCrLJ 505"
    header_line = "1991 PCrLJ 505"
    text = _header_block(header_line)
    header = leading_header_citation_judgment(text)
    assert header == "1991 PCrLJ 505"
    assert mismatch_kind(stored, header) == "reporter_year"
    assert rekey_actionable(stored, header, text) is True


def test_reporter_year_without_anchor_is_candidate_only():
    stored = "1994 PCrLJ 505"
    header_line = "1991 PCrLJ 505"
    # No [court] / Before / Present after the citation line
    text = f"{header_line}\nParty versus Other\n" + "x " * 200
    header = leading_header_citation_judgment(text)
    assert header == "1991 PCrLJ 505"
    assert mismatch_kind(stored, header) == "reporter_year"
    assert rekey_actionable(stored, header, text, staging=False) is False
    assert rekey_actionable(stored, header, text, staging=True) is True


def test_staging_uses_first_citation_before_court_bracket():
    text = "2000 PCrLJ 1069\n[KARACHI]\nBefore J, J\nA versus B\n" + "y " * 30
    assert leading_header_citation_staging(text) == "2000 PCrLJ 1069"


@pytest.mark.asyncio
async def test_scan_dry_run_counts_mismatch(db):
    from scraper.models import Judgment

    text = _header_block("1986 CLC 1011")
    j = Judgment(
        canonical_citation="1986 CLC 1008",
        full_text=text,
        source_name="PakistanLawSite",
        access_method="login_session",
        confidence_score=0.9,
    )
    db.add(j)
    await db.flush()
    rows, counts = await scan_header_rekeys(db)
    assert counts["would_rekey"] >= 1
    assert any(r.get("action") == "rekey" for r in rows)
    await db.rollback()


def test_header_key_guard_flag_default_off():
    assert settings.SSA_HEADER_CITATION_KEY_GUARD is False


def test_header_key_mismatch_detected_for_guard():
    text = _header_block("1986 CLC 1011")
    from scraper.parsers.header_citation import citation_key, leading_header_citation_judgment

    header = leading_header_citation_judgment(text)
    assert header and citation_key("1986 CLC 1008") != citation_key(header)
