"""Duplicate statute keeper ranking and canonical grouping keys."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from scraper.parsers.statute_parser import _parse_numeric_section_number

_MAY_BE_CALLED_RE = re.compile(r"\bmay be called\b", re.IGNORECASE)
_YEAR_LIKE_MIN = 1800
_YEAR_LIKE_MAX = 2100
KEEPER_MIN_CHAR_FRACTION = 0.7
SUBSTANTIVE_SECTION_MIN_CHARS = 40


def dedupe_canonical_name_key(name: str) -> str:
    """Lowercase and strip all non-alphanumerics (group ``Act,2026`` with ``Act, 2026``)."""
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


def _section_int(section_number: str) -> Optional[int]:
    return _parse_numeric_section_number((section_number or "").strip())


def _real_section_ints(section_numbers: Sequence[str]) -> List[int]:
    ints: List[int] = []
    for num in section_numbers:
        n = _section_int(num)
        if n is None:
            continue
        if _YEAR_LIKE_MIN <= n <= _YEAR_LIKE_MAX:
            continue
        if n > 5000:
            continue
        ints.append(n)
    return ints


def max_real_section_run_end(*section_number_lists: Sequence[str]) -> int:
    """Largest numeric section index seen in any duplicate-group member."""
    end = 0
    for nums in section_number_lists:
        real = _real_section_ints(nums)
        if real:
            end = max(end, real[-1])
    return end


def substantive_max_section_run_end(
    sections: Sequence[Dict[str, str]],
    *,
    min_chars: int = SUBSTANTIVE_SECTION_MIN_CHARS,
) -> int:
    """Last section number with a substantive body (ignores one-line stubs that inflate missing counts)."""
    nums = [str(s.get("section_number") or "") for s in sections]
    end = 0
    for s in sections:
        num = str(s.get("section_number") or "")
        n = _section_int(num)
        if n is None:
            continue
        if _YEAR_LIKE_MIN <= n <= _YEAR_LIKE_MAX:
            continue
        if is_fake_section_number(num, all_numbers=nums):
            continue
        if len((s.get("section_text") or "").strip()) < min_chars:
            continue
        end = max(end, n)
    if end:
        return end
    real = _real_section_ints(nums)
    return real[-1] if real else 0


def group_substantive_run_end(section_lists: Sequence[Sequence[Dict[str, str]]]) -> int:
    """Run end for missing-section math: max substantive tail across duplicate-group members."""
    end = 0
    for sections in section_lists:
        end = max(end, substantive_max_section_run_end(sections))
    if end:
        return end
    flat = [[str(s.get("section_number") or "") for s in sections] for sections in section_lists]
    return max_real_section_run_end(*flat)


def _typical_max_excluding_candidate(all_numbers: Sequence[str], candidate: str) -> int:
    real = sorted(set(_real_section_ints(all_numbers)))
    n = _section_int(candidate)
    if n is not None:
        others = [x for x in real if x != n]
    else:
        others = real
    if not others:
        return 0
    median = others[len(others) // 2]
    if len(others) >= 2:
        return max(median, others[-2])
    return others[-1]


def is_fake_section_number(section_number: str, *, all_numbers: Sequence[str]) -> bool:
    n = _section_int(section_number)
    if n is None:
        return False
    if _YEAR_LIKE_MIN <= n <= _YEAR_LIKE_MAX:
        return True
    real = _real_section_ints(all_numbers)
    if not real:
        return n > 5000
    typical_max = _typical_max_excluding_candidate(all_numbers, section_number)
    if n > 5000:
        return True
    if n > typical_max + 5:
        return True
    return False


def count_fake_sections(section_numbers: Sequence[str]) -> int:
    nums = list(section_numbers)
    return sum(1 for n in nums if is_fake_section_number(n, all_numbers=nums))


def count_missing_from_section_run(section_numbers: Sequence[str], *, run_end: Optional[int] = None) -> int:
    real = sorted(set(_real_section_ints(section_numbers)))
    if not real:
        return 10_000
    end = int(run_end) if run_end is not None else real[-1]
    if end <= 0:
        return 10_000
    present = set(real)
    return sum(1 for i in range(1, end + 1) if i not in present)


def _is_substantive_section(section: Dict[str, str], all_numbers: Sequence[str]) -> bool:
    num = str(section.get("section_number") or "")
    n = _section_int(num)
    if n is None:
        return False
    if _YEAR_LIKE_MIN <= n <= _YEAR_LIKE_MAX:
        return False
    if is_fake_section_number(num, all_numbers=all_numbers):
        return False
    return len((section.get("section_text") or "").strip()) >= SUBSTANTIVE_SECTION_MIN_CHARS


def section_1_has_may_be_called(sections: Sequence[Dict[str, str]]) -> bool:
    for s in sections:
        if _section_int(str(s.get("section_number") or "")) != 1:
            continue
        if _MAY_BE_CALLED_RE.search(s.get("section_text") or ""):
            return True
    return False


def dedupe_keeper_rank(
    *,
    sections: Sequence[Dict[str, str]],
    text_chars: int,
    has_source_url: bool,
    group_run_end: Optional[int] = None,
) -> Tuple[int, int, int, int, int, int]:
    """Sort key tuple: higher is better (use with ``reverse=True``)."""
    nums = [str(s.get("section_number") or "") for s in sections]
    missing = count_missing_from_section_run(nums, run_end=group_run_end)
    fake = count_fake_sections(nums)
    may_called = 1 if section_1_has_may_be_called(sections) else 0
    substantive_sections = sum(1 for s in sections if _is_substantive_section(s, nums))
    prov = 1 if has_source_url else 0
    return (
        -missing,
        -fake,
        may_called,
        substantive_sections,
        int(text_chars),
        prov,
    )


def keeper_below_char_fraction(
    keeper_chars: int,
    *,
    group_best_chars: int,
    min_fraction: float = KEEPER_MIN_CHAR_FRACTION,
) -> bool:
    if group_best_chars <= 0:
        return False
    return keeper_chars < int(group_best_chars * min_fraction)


def count_duplicate_groups_from_statute_rows(rows: Sequence[Dict[str, object]]) -> int:
    """Dry-run duplicate group count for visible-row fixtures (name/year/jurisdiction)."""
    groups: Dict[Tuple[str, int, str], int] = {}
    for row in rows:
        year = row.get("year_enacted")
        if year is None:
            continue
        name = str(row.get("name") or "")
        jurisdiction = str(row.get("jurisdiction") or "").strip()
        key = dedupe_canonical_name_key(name)
        if not key:
            continue
        gk = (key, int(year), jurisdiction)
        groups[gk] = groups.get(gk, 0) + 1
    return sum(1 for c in groups.values() if c > 1)
