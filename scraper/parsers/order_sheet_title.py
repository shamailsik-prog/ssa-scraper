"""Case title from the order-sheet header block only (petitioner/respondent lines).

Does not scan the full judgment for the first 'versus' line, so cited-case titles in headnotes are
not mistaken for the reported judgment's parties.
"""

from __future__ import annotations

import re
from typing import Optional

from scraper.parsers.case_metadata import parse_case_metadata

_PETITIONERS_SUFFIX = re.compile(r"\.{2,}\s*Petitioners?\s*$", re.I)


def case_title_from_order_sheet(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    meta = parse_case_metadata(text)
    pet = (meta.petitioner or "").strip()
    res = (meta.respondent or "").strip()
    if not pet and not res:
        return None
    pet = _PETITIONERS_SUFFIX.sub("", pet).strip()
    if pet and res:
        return f"{pet} versus {res}"[:2000]
    return (pet or res)[:2000] or None
