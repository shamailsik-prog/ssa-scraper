"""Locked section text for the four auditor-approved bundled-gazette pairs.

Three pairs have a text-layer fixture. The Withdrawal pair is pinned, and its
section numbers are the preview record. Its PDF text layer is not in this repo,
so the body is not locked.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.tasks.bundled_gazette_acts import CONFIRMED_BLEED_STATUTE_IDS, CONFIRMED_PDF_SHA256, PIN_UNTRUSTED

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bundled_gazette"
_GOLDENS = _FIXTURES / "goldens"


def _load(name: str) -> dict:
    return json.loads((_GOLDENS / name).read_text(encoding="utf-8"))


def _extract(golden: dict) -> list[dict]:
    text = (_FIXTURES / golden["fixture"]).read_text(encoding="utf-8")
    extracted = extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "Senate",
            "url": golden["source_url"],
            "act_title": golden["act_title"],
            "jurisdiction": "Federal",
        },
    )
    return [
        {"section_number": str(section["section_number"]), "section_text": section["section_text"]}
        for section in extracted["sections"]
    ]


def test_approved_pair_goldens_match_the_splitter():
    locked = [
        "legal_advisers_s710.json",
        "travel_agencies_s441.json",
        "pat_feeder_s478.json",
    ]
    expected_numbers = {
        "legal_advisers_s710.json": ["1", "2", "3"],
        "travel_agencies_s441.json": [str(number) for number in range(1, 17)],
        "pat_feeder_s478.json": ["1", "2"],
    }
    for name in locked:
        golden = _load(name)
        assert golden["body_locked"] is True
        assert [section["section_number"] for section in golden["sections"]] == expected_numbers[name]
        assert _extract(golden) == golden["sections"]
        for statute_id in golden["statute_ids"]:
            parsed = UUID(statute_id)
            assert parsed in CONFIRMED_BLEED_STATUTE_IDS
            assert CONFIRMED_PDF_SHA256[parsed] not in {None, "", PIN_UNTRUSTED}
            assert len(CONFIRMED_PDF_SHA256[parsed]) == 64


def test_pat_feeder_golden_keeps_the_trailing_year_and_not_the_next_act():
    golden = _load("pat_feeder_s478.json")
    section_two = golden["sections"][1]["section_text"]
    assert section_two.rstrip().endswith("1976.")
    assert "Motor Vehicles" not in section_two
    assert "Foreign Instrument" not in section_two


def test_withdrawal_pair_is_pinned_and_its_body_is_not_invented():
    golden = _load("withdrawal_s171.json")
    assert golden["body_locked"] is False
    assert golden["section_numbers"] == ["1", "2", "3"]
    assert "sections" not in golden
    assert not (_FIXTURES / "s171.txt").exists()
    for statute_id in golden["statute_ids"]:
        parsed = UUID(statute_id)
        assert parsed in CONFIRMED_BLEED_STATUTE_IDS
        assert CONFIRMED_PDF_SHA256[parsed] == golden["pinned_sha256"]
