"""Duplicate statute keeper ranking and canonical grouping keys."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from scraper.parsers.statute_parser import _parse_numeric_section_number

_MAY_BE_CALLED_RE = re.compile(r"\bmay be called\b", re.IGNORECASE)
_YEAR_LIKE_MIN = 1800
_YEAR_LIKE_MAX = 2100


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


def is_fake_section_number(section_number: str, *, all_numbers: Sequence[str]) -> bool:
    n = _section_int(section_number)
    if n is None:
        return False
    if _YEAR_LIKE_MIN <= n <= _YEAR_LIKE_MAX:
        return True
    real = _real_section_ints(all_numbers)
    if not real:
        return n > 5000
    typical_max = max(real)
    if n > 5000:
        return True
    if n > typical_max + 5 and n >= 100:
        return True
    return False


def count_fake_sections(section_numbers: Sequence[str]) -> int:
    nums = list(section_numbers)
    return sum(1 for n in nums if is_fake_section_number(n, all_numbers=nums))


def count_missing_from_section_run(section_numbers: Sequence[str]) -> int:
    real = sorted(set(_real_section_ints(section_numbers)))
    if not real:
        return 10_000
    end = real[-1]
    present = set(real)
    return sum(1 for i in range(1, end + 1) if i not in present)


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
) -> Tuple[int, int, int, int, int]:
    """Sort key tuple: higher is better (use with ``reverse=True``)."""
    nums = [str(s.get("section_number") or "") for s in sections]
    missing = count_missing_from_section_run(nums)
    fake = count_fake_sections(nums)
    may_called = 1 if section_1_has_may_be_called(sections) else 0
    prov = 1 if has_source_url else 0
    return (
        -missing,
        -fake,
        may_called,
        int(text_chars),
        prov,
    )


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
