"""Case title from the order-sheet header block only (petitioner/respondent lines).

Parties are taken only from lines ending in ---Petitioner(s) / ---Respondent(s) inside the
order-sheet block (after the citation / Before caption, before \"decided on\" or JUDGMENT).
Never scans headnotes for a bare \"versus\" line.
"""

from __future__ import annotations

import re
from typing import Optional

_ORDER_SHEET_END = re.compile(r"(?i)\b(?:decided|heard)\s+on\b|\bJUDGMENT\b|\bJUDGEMENT\b")
_BEFORE_LINE = re.compile(r"(?i)^\s*(?:before|coram)\b")
_ROLE_PET = re.compile(r"(?:\.{2,}|-{2,})\s*Petitioners?\s*$", re.I)
_ROLE_RES = re.compile(r"(?:\.{2,}|-{2,})\s*Respondents?\s*$", re.I)
_VERSUS_LINE = re.compile(r"(?i)^\s*(?:versus|vs)\.?\s*$")
_CITATION_IN_TITLE = re.compile(
    r"\b(?:PLD|SCMR|CLC|YLR|CLD|PLC|MLD|PCrLJ|PTD|GBLR)\b|\b(?:19|20)\d{2}\s+[A-Z]",
    re.I,
)


def _strip_role_suffix(line: str, role: re.Pattern[str]) -> str:
    return re.sub(r"\s+", " ", role.sub("", line).strip(" .,-"))


def _order_sheet_lines(text: str) -> list[str]:
    end = len(text)
    m = _ORDER_SHEET_END.search(text)
    if m:
        end = m.start()
    window = text[:end]
    lines = [ln.strip() for ln in window.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return []
    start = 0
    for i, ln in enumerate(lines[:8]):
        if _BEFORE_LINE.match(ln) or (i <= 2 and re.match(r"^[\[(][A-Za-z].{1,70}[\])]$", ln)):
            start = i + 1
    return lines[start:]


def case_title_from_order_sheet(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    lines = _order_sheet_lines(text)
    if not lines:
        return None
    pet_line: Optional[str] = None
    res_line: Optional[str] = None
    versus_seen = False
    for ln in lines[:20]:
        if _ROLE_PET.search(ln):
            pet_line = _strip_role_suffix(ln, _ROLE_PET)
            continue
        if _VERSUS_LINE.match(ln):
            versus_seen = bool(pet_line)
            continue
        if versus_seen and _ROLE_RES.search(ln):
            res_line = _strip_role_suffix(ln, _ROLE_RES)
            break
    if not pet_line or not res_line or not versus_seen:
        return None
    return f"{pet_line} versus {res_line}"


def is_valid_order_sheet_case_title(title: Optional[str]) -> bool:
    """Title safe to persist from order-sheet backfill / reconcile."""
    if not title:
        return False
    t = title.strip()
    if len(t) > 300 or len(t) < 8:
        return False
    if t.lower().startswith("before"):
        return False
    if _CITATION_IN_TITLE.search(t):
        return False
    low = t.lower()
    if " versus " not in low and not re.search(r"\bvs\.?\s", low):
        return False
    parts = re.split(r"\s+versus\s+|\bvs\.?\s+", t, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return False
    return bool(parts[0].strip()) and bool(parts[1].strip())
