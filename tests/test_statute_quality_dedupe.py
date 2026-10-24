"""Statute quality DEDUPE layer: grouping keys and keeper ranking (auditor spot-check)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.models import Statute
from scraper.statute_quality.dedupe import (
    KEEPER_MIN_CHAR_FRACTION,
    count_duplicate_groups_from_statute_rows,
    count_fake_sections,
    count_missing_from_section_run,
    dedupe_canonical_name_key,
    dedupe_keeper_rank,
    group_substantive_run_end,
    is_fake_section_number,
    keeper_below_char_fraction,
    max_real_section_run_end,
    substantive_max_section_run_end,
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


def _rank_winner(a: list[dict[str, str]], b: list[dict[str, str]], *, has_url_a: bool = True, has_url_b: bool = True) -> str:
    nums_a = [str(s["section_number"]) for s in a]
    nums_b = [str(s["section_number"]) for s in b]
    group_end = group_substantive_run_end([a, b])
    ra = dedupe_keeper_rank(
        sections=a,
        text_chars=sum(len(s["section_text"]) for s in a),
        has_source_url=has_url_a,
        group_run_end=group_end or None,
    )
    rb = dedupe_keeper_rank(
        sections=b,
        text_chars=sum(len(s["section_text"]) for s in b),
        has_source_url=has_url_b,
        group_run_end=group_end or None,
    )
    return "a" if ra > rb else "b"


def _char_budget_sections(n: int, total_chars: int, *, may_be_called: bool = False) -> list[dict[str, str]]:
    base = max(40, total_chars // max(n, 1))
    out: list[dict[str, str]] = []
    for i in range(1, n + 1):
        text = f"Section {i}. " + ("p" * base)
        if i == 1 and may_be_called:
            text = "Section 1. This Act may be called the Sample Act, 1963."
        out.append({"section_number": str(i), "section_text": text})
    return out


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


def test_missing_sections_use_group_max_not_copy_max():
    short = _sections_range(3, may_be_called=False)
    full = _sections_range(16, may_be_called=True)
    group_end = max_real_section_run_end(
        [str(s["section_number"]) for s in short],
        [str(s["section_number"]) for s in full],
    )
    assert group_end == 16
    assert count_missing_from_section_run([str(s["section_number"]) for s in short], run_end=group_end) == 13
    assert count_missing_from_section_run([str(s["section_number"]) for s in short], run_end=None) == 0


def test_keeper_presidents_gratuity_act_1963_prefers_full_copy():
    """Truncated 3-section copy must not beat 16-section full text (~741 vs ~7076 chars)."""
    trunc = _char_budget_sections(3, 741, may_be_called=False)
    full = _char_budget_sections(16, 7076, may_be_called=True)
    assert _rank_winner(full, trunc) == "a"


def test_fake_section_typical_max_excludes_candidate():
    nums = [str(i) for i in range(1, 30)] + ["252", "661"]
    assert is_fake_section_number("252", all_numbers=nums)
    assert is_fake_section_number("661", all_numbers=nums)
    assert not is_fake_section_number("16", all_numbers=nums)


def test_year_shaped_section_1963_is_fake_not_missing():
    """R5: enactment year as a section number is fake, not a gap in the section run."""
    nums = [str(i) for i in range(1, 17)] + ["1963"]
    assert is_fake_section_number("1963", all_numbers=nums)
    assert count_fake_sections(nums) >= 1
    assert count_missing_from_section_run(nums, run_end=16) == 0


def test_missing_vs_fake_conflict_prefers_penalizing_fake_and_missing():
    """R1/R6: high missing from group run and fake year noise — full copy still wins."""
    group_end = 16
    trunc = _char_budget_sections(3, 741, may_be_called=False) + [
        {"section_number": "1963", "section_text": "Section 1963. Gazette year noise."}
    ]
    full = _char_budget_sections(16, 7076, may_be_called=True)
    assert dedupe_keeper_rank(
        sections=full,
        text_chars=7076,
        has_source_url=True,
        group_run_end=group_end,
    ) > dedupe_keeper_rank(
        sections=trunc,
        text_chars=741,
        has_source_url=True,
        group_run_end=group_end,
    )


def test_keeper_kp_aasan_karobar_2025_shape():
    clean = _sections_range(24, may_be_called=True)
    noisy = _sections_range(23, may_be_called=False, fake_years=frozenset({661}))
    assert _rank_winner(clean, noisy) == "a"


def test_keeper_kp_judicial_officers_welfare_fund_shape():
    clean = _sections_range(12, may_be_called=True)
    noisy = _sections_range(11, may_be_called=False, omit=frozenset({8}), fake_years=frozenset({252}))
    assert _rank_winner(clean, noisy) == "a"


def test_keeper_kp_provincial_assembly_powers_2026_shape():
    clean = _sections_range(8, may_be_called=True)
    noisy = _sections_range(7, may_be_called=False, fake_years=frozenset({2026}))
    assert _rank_winner(clean, noisy) == "a"


def _stub_tail_sections(start: int, count: int) -> list[dict[str, str]]:
    return [{"section_number": str(start + i), "section_text": f"Section {start + i}. Tr."} for i in range(count)]


def test_keeper_ajk_government_act_1964_prefers_fuller_copy():
    """Free-sites: 19-section stub tail must not beat 17-section fuller text (~6204 vs ~7887 chars)."""
    thinner = _char_budget_sections(17, 6204, may_be_called=True) + _stub_tail_sections(18, 2)
    fuller = _char_budget_sections(17, 7887, may_be_called=True)
    assert substantive_max_section_run_end(thinner) == substantive_max_section_run_end(fuller) == 17
    assert _rank_winner(fuller, thinner) == "a"


def test_keeper_ajk_finance_act_2017_prefers_more_sections():
    thinner = _char_budget_sections(3, 3411, may_be_called=True)
    fuller = _char_budget_sections(4, 3428, may_be_called=False)
    assert _rank_winner(fuller, thinner) == "a"


def test_keeper_ajk_finance_act_2014_prefers_more_sections():
    thinner = _char_budget_sections(3, 3241, may_be_called=True)
    fuller = _char_budget_sections(4, 3257, may_be_called=False)
    assert _rank_winner(fuller, thinner) == "a"


def test_keeper_ajk_speakers_salary_act_1971_prefers_fuller_copy():
    thinner = _char_budget_sections(3, 1498, may_be_called=True)
    fuller = _char_budget_sections(4, 2024, may_be_called=False)
    assert _rank_winner(fuller, thinner) == "a"


def test_keeper_ajk_speakers_salary_1971_stray_section_21_not_preferred():
    thinner = _char_budget_sections(3, 1498, may_be_called=True) + [
        {"section_number": "21", "section_text": "Section 21. " + ("q" * 80)}
    ]
    fuller = _char_budget_sections(4, 2024, may_be_called=False)
    assert _rank_winner(fuller, thinner) == "a"


def test_r5_r6_substantive_count_ignores_year_shaped_section_numbers():
    nums = [str(i) for i in range(1, 5)] + ["1990"]
    sections = _char_budget_sections(4, 3000, may_be_called=True) + [
        {"section_number": "1990", "section_text": "Section 1990. " + ("y" * 80)}
    ]
    assert dedupe_keeper_rank(
        sections=sections,
        text_chars=4000,
        has_source_url=True,
        group_run_end=4,
    ) < dedupe_keeper_rank(
        sections=_char_budget_sections(4, 4000, may_be_called=True),
        text_chars=4000,
        has_source_url=True,
        group_run_end=4,
    )


def test_r5_r6_substantive_run_end_ignores_fake_year_section_1122():
    """AJK Rescue 1122 / Industrial Relations 2017 shape: long s.1122 must not set group run end."""
    clean = _sections_range(12, may_be_called=True)
    noisy = _sections_range(12, may_be_called=True) + [
        {
            "section_number": "1122",
            "section_text": "Section 1122. " + ("x" * 80),
        }
    ]
    assert substantive_max_section_run_end(noisy) == substantive_max_section_run_end(clean) == 12
    assert _rank_winner(clean, noisy) == "a"


def test_rank_step_r1_missing_sections():
    a = _sections_range(10, may_be_called=True, omit=frozenset({5}))
    b = _sections_range(10, may_be_called=True)
    end = 10
    assert dedupe_keeper_rank(sections=a, text_chars=5000, has_source_url=True, group_run_end=end) < dedupe_keeper_rank(
        sections=b, text_chars=5000, has_source_url=True, group_run_end=end
    )


def test_rank_step_r2_fake_sections():
    clean = _sections_range(5, may_be_called=True)
    noisy = _sections_range(5, may_be_called=True, fake_years=frozenset({1860}))
    end = 5
    assert dedupe_keeper_rank(sections=clean, text_chars=1000, has_source_url=True, group_run_end=end) > dedupe_keeper_rank(
        sections=noisy, text_chars=1000, has_source_url=True, group_run_end=end
    )


def test_rank_step_r3_may_be_called():
    a = _sections_range(4, may_be_called=False)
    b = _sections_range(4, may_be_called=True)
    end = 4
    assert dedupe_keeper_rank(sections=b, text_chars=1000, has_source_url=True, group_run_end=end) > dedupe_keeper_rank(
        sections=a, text_chars=1000, has_source_url=True, group_run_end=end
    )


def test_rank_step_r4_substantive_section_count():
    a = _sections_range(4, may_be_called=True, truncate_section=4)
    b = _sections_range(4, may_be_called=True)
    end = 4
    assert dedupe_keeper_rank(sections=b, text_chars=1000, has_source_url=True, group_run_end=end) > dedupe_keeper_rank(
        sections=a, text_chars=1000, has_source_url=True, group_run_end=end
    )


def test_rank_step_r5_text_chars():
    a = _sections_range(4, may_be_called=True)
    b = _sections_range(4, may_be_called=True)
    end = 4
    assert dedupe_keeper_rank(sections=b, text_chars=9000, has_source_url=True, group_run_end=end) > dedupe_keeper_rank(
        sections=a, text_chars=1000, has_source_url=True, group_run_end=end
    )


def test_rank_step_r6_source_url():
    a = _sections_range(4, may_be_called=True)
    end = 4
    assert dedupe_keeper_rank(sections=a, text_chars=1000, has_source_url=True, group_run_end=end) > dedupe_keeper_rank(
        sections=a, text_chars=1000, has_source_url=False, group_run_end=end
    )


def test_rank_step_r7_char_fraction_guard():
    assert keeper_below_char_fraction(600, group_best_chars=1000, min_fraction=KEEPER_MIN_CHAR_FRACTION)
    assert not keeper_below_char_fraction(800, group_best_chars=1000, min_fraction=KEEPER_MIN_CHAR_FRACTION)


@pytest.mark.asyncio
async def test_n6_find_duplicate_groups_equal_substantive_sections_group_run_end_decides(db):
    """Equal substantive section counts: fuller run still wins via group_run_end missing gaps."""
    trunc_id, full_id = uuid4(), uuid4()
    for sid, name in (
        (trunc_id, "The Equal Substantive Act, 2018"),
        (full_id, "The Equal Substantive Act,2018"),
    ):
        db.add(
            Statute(
                id=sid,
                name=name,
                jurisdiction="AJ&K",
                year_enacted=2018,
                source_name="PakistanCode",
                source_url="https://example.test/full" if sid == full_id else None,
            )
        )
    await db.commit()
    trunc_secs = _char_budget_sections(8, 4000, may_be_called=False)
    full_secs = _char_budget_sections(12, 8000, may_be_called=True)
    for s in trunc_secs:
        await _add_section(db, trunc_id, s["section_number"], s["section_text"])
    for s in full_secs:
        await _add_section(db, full_id, s["section_number"], s["section_text"])
    await db.commit()
    dry = await find_duplicate_groups(db)
    assert dry["winners"] == [full_id]


@pytest.mark.asyncio
async def test_n6_find_duplicate_groups_group_run_end_when_substantive_count_tied(db):
    """Truncated copy with equal substantive sections still loses when group run end exposes gaps."""
    trunc_id, full_id = uuid4(), uuid4()
    for sid, name in (
        (trunc_id, "The Group Run End Act, 2017"),
        (full_id, "The Group Run End Act,2017"),
    ):
        db.add(
            Statute(
                id=sid,
                name=name,
                jurisdiction="AJ&K",
                year_enacted=2017,
                source_name="PakistanCode",
                source_url="https://example.test/full" if sid == full_id else None,
            )
        )
    await db.commit()
    trunc_secs = _char_budget_sections(8, 4000, may_be_called=False)
    full_secs = _char_budget_sections(12, 8000, may_be_called=True)
    for s in trunc_secs:
        await _add_section(db, trunc_id, s["section_number"], s["section_text"])
    for s in full_secs:
        await _add_section(db, full_id, s["section_number"], s["section_text"])
    await db.commit()
    dry = await find_duplicate_groups(db)
    assert dry["duplicate_groups"] == 1
    assert dry["winners"] == [full_id]
    assert trunc_id in dry["losers"]


@pytest.mark.asyncio
async def test_find_duplicate_groups_manual_review_when_keeper_too_short(db):
    """R6: may-be-called can win rank but keeper must not quarantine when <70% of group's best chars."""
    keeper_id, fuller_id = uuid4(), uuid4()
    db.add(
        Statute(
            id=keeper_id,
            name="The Manual Review Act, 1999",
            jurisdiction="Federal",
            year_enacted=1999,
            source_name="PakistanCode",
            source_url="https://example.test/keeper",
        )
    )
    db.add(
        Statute(
            id=fuller_id,
            name="The Manual Review Act,1999",
            jurisdiction="Federal",
            year_enacted=1999,
            source_name="PakistanCode",
            source_url="https://example.test/full",
        )
    )
    await db.commit()
    for i in range(1, 11):
        text = "Section 1. This Act may be called the Manual Review Act, 1999." if i == 1 else f"Section {i}. brief"
        await _add_section(db, keeper_id, str(i), text)
    for i in range(1, 11):
        if i == 5:
            continue
        await _add_section(db, fuller_id, str(i), f"Section {i}. " + ("q" * 2500))
    await db.commit()
    dry = await find_duplicate_groups(db)
    assert dry["duplicate_groups"] == 1
    assert dry["winners"] == []
    assert dry["losers"] == []
    assert len(dry["manual_review_groups"]) == 1


@pytest.mark.asyncio
async def test_find_duplicate_groups_gratuity_truncated_loses_to_full_copy(db):
    """N6: DB path must pick the 16-section Gratuity-shaped keeper, not the 3-section trunc."""
    full_id, trunc_id = uuid4(), uuid4()
    act_name = "The President's Gratuity Act, 1963"
    db.add(
        Statute(
            id=full_id,
            name=act_name,
            jurisdiction="Federal",
            year_enacted=1963,
            source_name="PakistanCode",
            source_url="https://example.test/gratuity-full",
        )
    )
    db.add(
        Statute(
            id=trunc_id,
            name="The President's Gratuity Act,1963",
            jurisdiction="Federal",
            year_enacted=1963,
            source_name="PakistanCode",
        )
    )
    await db.commit()
    for s in _char_budget_sections(16, 7076, may_be_called=True):
        await _add_section(db, full_id, s["section_number"], s["section_text"])
    for s in _char_budget_sections(3, 741, may_be_called=False):
        await _add_section(db, trunc_id, s["section_number"], s["section_text"])
    await db.commit()

    dry = await find_duplicate_groups(db)
    assert dry["duplicate_groups"] == 1
    assert dry["winners"] == [full_id]
    assert trunc_id in dry["losers"]


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
    assert bad_id in dry["losers"]
