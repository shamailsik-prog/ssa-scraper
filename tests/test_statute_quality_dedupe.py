"""Statute quality DEDUPE layer: grouping keys and keeper ranking (auditor spot-check)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.models import Statute
from scraper.statute_quality.dedupe import (
    count_duplicate_groups_from_statute_rows,
    dedupe_canonical_name_key,
    dedupe_keeper_rank,
)
from scraper.tasks.statute_quality import _dedupe_group_key, find_duplicate_groups
from tests.test_statute_quality_cli import _add_section

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statute_quality"


def _sections_range(
    end: int,
    *,
    may_be_called: bool = False,
    omit: frozenset[int] = frozenset(),
    fake_years: frozenset[int] = frozenset(),
    truncate_section: int | None = None,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for i in range(1, end + 1):
        if i in omit:
            continue
        text = f"Section {i}. Substantive provision with adequate body " + ("x" * 48)
        if i == 1 and may_be_called:
            text = "Section 1. This Act may be called the Sample Act, 1971."
        if truncate_section is not None and i == truncate_section:
            text = f"Section {i}. Trunc"
        out.append({"section_number": str(i), "section_text": text})
    for y in fake_years:
        out.append({"section_number": str(y), "section_text": f"Section {y}. Gazette year noise."})
    return out


def _rank_winner(a: list[dict[str, str]], b: list[dict[str, str]]) -> str:
    ra = dedupe_keeper_rank(sections=a, text_chars=sum(len(s["section_text"]) for s in a), has_source_url=True)
    rb = dedupe_keeper_rank(sections=b, text_chars=sum(len(s["section_text"]) for s in b), has_source_url=True)
    return "a" if ra > rb else "b"


def test_dedupe_canonical_key_strips_punctuation_and_case():
    assert dedupe_canonical_name_key("Foo Act,2026") == dedupe_canonical_name_key("Foo Act, 2026")
    assert dedupe_canonical_name_key("1967(Act V") == dedupe_canonical_name_key("1967 (Act V")
    assert dedupe_canonical_name_key("THE SAMPLE ACT") == "thesampleact"


def test_visible_row_baseline_duplicate_group_counts():
    main_rows = json.loads((FIXTURES / "dedupe_visible_main.json").read_text(encoding="utf-8"))["statutes"]
    free_rows = json.loads((FIXTURES / "dedupe_visible_free_sites.json").read_text(encoding="utf-8"))["statutes"]
    assert count_duplicate_groups_from_statute_rows(main_rows) == 113
    assert count_duplicate_groups_from_statute_rows(free_rows) == 110


def test_keeper_ajk_prevention_of_cruelty_animals_2014():
    clean = _sections_range(31, may_be_called=True)
    noisy = _sections_range(30, may_be_called=False, fake_years=frozenset({1860}))
    assert _rank_winner(clean, noisy) == "a"


def test_keeper_ajk_mineral_industrial_dev_corp_1971():
    clean = _sections_range(29, may_be_called=True)
    noisy = _sections_range(29, may_be_called=False, omit=frozenset({16}), fake_years=frozenset({252}), truncate_section=11)
    assert _rank_winner(clean, noisy) == "a"


def test_keeper_incomplete_projects_recovery_payments_1985():
    clean = _sections_range(18, may_be_called=True)
    noisy = _sections_range(17, may_be_called=False, fake_years=frozenset({1985}))
    assert _rank_winner(clean, noisy) == "a"


@pytest.mark.asyncio
async def test_find_duplicate_groups_punctuation_merge_and_keeper(db):
    good_id, bad_id = uuid4(), uuid4()
    db.add(
        Statute(
            id=good_id,
            name="The Incomplete Projects (Recovery of Payments) Act, 1985",
            jurisdiction="Federal",
            year_enacted=1985,
            source_name="PakistanCode",
            source_url="https://example.test/good",
        )
    )
    db.add(
        Statute(
            id=bad_id,
            name="The Incomplete Projects (Recovery of Payments) Act,1985",
            jurisdiction="Federal",
            year_enacted=1985,
            source_name="PakistanCode",
        )
    )
    await db.commit()
    clean = _sections_range(18, may_be_called=True)
    noisy = _sections_range(17, may_be_called=False, fake_years=frozenset({1985}))
    for s in clean:
        await _add_section(db, good_id, s["section_number"], s["section_text"])
    for s in noisy:
        await _add_section(db, bad_id, s["section_number"], s["section_text"])
    await db.commit()

    assert _dedupe_group_key(await db.get(Statute, good_id))[0] == dedupe_canonical_name_key(
        "The Incomplete Projects (Recovery of Payments) Act,1985"
    )
    dry = await find_duplicate_groups(db)
    assert dry["duplicate_groups"] == 1
    assert dry["winners"] == [good_id]
    assert dry["losers"] == [], "differing statutory wording must not be auto-quarantined"
    assert dry["review_required_ids"] == [bad_id]
