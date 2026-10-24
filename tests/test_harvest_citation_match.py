"""Search-harvest treats Aj&K / AJ&K / AJK PLD and paren variants as the same known citation."""

from __future__ import annotations

from scraper.parsers.citation_extractor import normalise_citation
from scraper.parsers.harvest_citation_match import citation_harvest_equivalent_keys
from scraper.pls_search_harvest_core import citation_keys_for_row, partition_rows_by_known


def test_normalise_pld_ajk_high_court_uses_stored_ajk_casing():
    assert normalise_citation("PLD 2024 AJ&K 1") == "PLD 2024 Aj&K 1"
    assert normalise_citation("PLD 2024 AJK 1") == "PLD 2024 Aj&K 1"


def test_harvest_equivalents_pld_ajk_high_court():
    stored = "PLD 2024 Aj&K 1"
    site = "PLD 2024 AJ&K 1"
    equiv_site = citation_harvest_equivalent_keys(site)
    equiv_stored = citation_harvest_equivalent_keys(stored)
    assert stored in equiv_site
    assert site in equiv_stored


def test_partition_rows_known_when_site_spelling_differs_from_corpus():
    rows = [{"citation": "PLD 2024 AJ&K 5", "detail_url": "/x"}]
    known, new_rows = partition_rows_by_known(rows, {"PLD 2024 Aj&K 5"})
    assert known == 1
    assert new_rows == []


def test_citation_keys_for_row_includes_ajk_variants():
    keys = citation_keys_for_row({"citation": "PLD 2024 Azad Jammu and Kashmir 3"})
    assert "PLD 2024 Aj&K 3" in keys
    assert "PLD 2024 AJ&K 3" in keys


def test_year_first_clc_ajk_paren_variants():
    stored = "2023 CLC (Aj&K) 12"
    site = "2023 CLC (AJ&K) 12"
    assert stored in citation_harvest_equivalent_keys(site)


def test_pld_sc_ajk_supreme_court_variants():
    a = "PLD 2024 SC (AJ&K) 50"
    b = "PLD 2024 SC (Aj&K) 50"
    assert a in citation_harvest_equivalent_keys(b)
