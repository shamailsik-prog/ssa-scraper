"""Case title from the order-sheet header block only (petitioner/respondent lines)."""

from __future__ import annotations

import re
from typing import Optional

_ORDER_SHEET_END = re.compile(r"(?i)\b(?:decided|heard)\s+on\b|\bJUDGMENT\b|\bJUDGEMENT\b")
_BEFORE_LINE = re.compile(r"(?i)^\s*(?:before|coram)\b")
_PET_ROLES = r"Petitioners?|Appellants?|Applicants?|Plaintiffs?"
_RES_ROLES = r"Respondents?|Defendants?"
_ROLE_PET = re.compile(rf"(?:\.{{2,}}|-{{2,}})\s*{_PET_ROLES}\s*$", re.I)
_ROLE_RES = re.compile(rf"(?:\.{{2,}}|-{{2,}})\s*{_RES_ROLES}\s*$", re.I)
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


def _is_order_sheet_header_line(ln: str) -> bool:
    if _BEFORE_LINE.match(ln) or _VERSUS_LINE.match(ln):
        return True
    if re.match(r"^[\[(]", ln):
        return True
    if re.search(r"\b(?:19|20)\d{2}\s+[A-Z]", ln) and "versus" not in ln.lower():
        return True
    return False


def _party_name(lines: list[str], role_idx: int, role_pat: re.Pattern[str]) -> str:
    """Join lines from the order-sheet heading down to the role line (party name may span lines)."""
    i = role_idx - 1
    prefix_parts: list[str] = []
    while i >= 0:
        if _VERSUS_LINE.match(lines[i]) or _ROLE_PET.search(lines[i]) or _ROLE_RES.search(lines[i]):
            break
        if _is_order_sheet_header_line(lines[i]):
            break
        if re.search(r"\bversus\b", lines[i], re.I) and not _VERSUS_LINE.match(lines[i]):
            break
        prefix_parts.insert(0, lines[i])
        i -= 1
    last = _strip_role_suffix(lines[role_idx], role_pat)
    if prefix_parts:
        return f"{' '.join(prefix_parts)} {last}".strip()
    return last


def case_title_from_order_sheet(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    lines = _order_sheet_lines(text)
    if not lines:
        return None
    pet_idx = next((i for i, ln in enumerate(lines[:24]) if _ROLE_PET.search(ln)), None)
    if pet_idx is None:
        return None
    versus_idx = next((i for i in range(pet_idx + 1, min(len(lines), 28)) if _VERSUS_LINE.match(lines[i])), None)
    if versus_idx is None:
        return None
    res_idx = next((i for i in range(versus_idx + 1, min(len(lines), 32)) if _ROLE_RES.search(lines[i])), None)
    if res_idx is None:
        return None
    pet = _party_name(lines, pet_idx, _ROLE_PET)
    res = _party_name(lines, res_idx, _ROLE_RES)
    if not pet or not res:
        return None
    return f"{pet} versus {res}"


def is_valid_order_sheet_case_title(title: Optional[str]) -> bool:
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
