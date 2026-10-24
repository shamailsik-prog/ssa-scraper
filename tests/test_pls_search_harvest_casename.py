"""CaseName URL parsing for search-harvest known rows (including AJ&K court codes)."""

from __future__ import annotations

import hashlib
import re

import pytest

from scraper.models import Judgment, ScraperStaging, SourceProvenance
from scraper.pls_search_harvest_core import CASE_KEY_PREFIX, case_name_from_url, citation_keys_for_row
from scraper.tasks import pls_search_harvest as harvest

_PRE_HYPHEN_CASE_NAME_RE = re.compile(r"CaseName=(\d{4}[A-Za-z]{1,8}\d{1,6})(?![0-9A-Za-z])")
_CASE_NAME_FIXTURE_URLS = (
    "https://x/Login/ReferenceCaseLawSearch?CaseName=2022S472&Row=0",
    "https://x/?CaseName=2025L8&bookName=undefined",
    "https://x/?CaseName=2023HCAJK1&Row=3",
    "https://x/?CaseName=2023SCAJK1&Row=0",
    "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2026S701&court=&Row=0",
    "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=1980L238&&court= &&Row=0",
)


def test_case_name_from_url_legacy_short_codes():
    assert case_name_from_url("https://x/Login/ReferenceCaseLawSearch?CaseName=2022S472&Row=0") == "2022S472"
    assert case_name_from_url("https://x/?CaseName=2025L8&bookName=undefined") == "2025L8"


def test_case_name_from_url_ajk_high_and_supreme_court_codes():
    assert case_name_from_url("https://x/?CaseName=2023HCAJK1&Row=3") == "2023HCAJK1"
    assert case_name_from_url("https://x/?CaseName=2023SCAJK1&Row=0") == "2023SCAJK1"


def test_case_name_from_url_hyphenated_court_codes():
    assert case_name_from_url("https://x/?CaseName=2024LAT-SINDH12&Row=0") == "2024LAT-SINDH12"
    assert case_name_from_url("https://x/?CaseName=2025HC-A3&court=") == "2025HC-A3"
    assert case_name_from_url("https://x/?CaseName=2026SC-A701&Row=0") == "2026SC-A701"


def test_hyphen_pattern_does_not_change_keys_on_existing_fixtures():
    changed = []
    for url in _CASE_NAME_FIXTURE_URLS:
        old = _PRE_HYPHEN_CASE_NAME_RE.search(url)
        new = case_name_from_url(url)
        assert old is not None and new is not None
        if new != old.group(1).upper():
            changed.append((url, old.group(1).upper(), new))
    assert changed == []


@pytest.mark.asyncio
async def test_load_known_citation_keys_marks_ajk_case_names_known(db):
    held_url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=2023HCAJK1&court=&Row=0&bookName=undefined"
    )
    prov = SourceProvenance(
        source_name="PakistanLawSite",
        access_method="login_session",
        source_url=held_url,
        content_hash=hashlib.sha256(held_url.encode()).hexdigest(),
        content_kind="html",
    )
    db.add(prov)
    await db.flush()
    db.add(
        Judgment(
            source_name="PakistanLawSite",
            source_url=held_url,
            canonical_citation="PLD 2023 Aj&K 1",
            full_text="x",
        )
    )
    await db.flush()

    grid_row = {"citation": "", "detail_url": held_url.replace("Row=0", "Row=7")}
    keys = citation_keys_for_row(grid_row)
    assert keys == {CASE_KEY_PREFIX + "2023HCAJK1"}

    known = await harvest.load_known_citation_keys(db, keys)
    assert known == {CASE_KEY_PREFIX + "2023HCAJK1"}

    await db.rollback()


@pytest.mark.asyncio
async def test_load_known_citation_keys_scajk_via_staging(db):
    held_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2023SCAJK1&Row=0"
    prov = SourceProvenance(
        source_name="PakistanLawSite",
        access_method="login_session",
        source_url=held_url,
        content_hash=hashlib.sha256(held_url.encode()).hexdigest(),
        content_kind="html",
    )
    db.add(prov)
    await db.flush()
    db.add(
        ScraperStaging(
            source_name="PakistanLawSite",
            access_method="login_session",
            source_url=held_url,
            provenance_id=prov.id,
            content_hash="y" * 64,
            status="promoted",
        )
    )
    await db.flush()

    keys = citation_keys_for_row({"detail_url": held_url.replace("Row=0", "Row=2")})
    assert CASE_KEY_PREFIX + "2023SCAJK1" in keys
    assert await harvest.load_known_citation_keys(db, keys) == {CASE_KEY_PREFIX + "2023SCAJK1"}
    await db.rollback()
