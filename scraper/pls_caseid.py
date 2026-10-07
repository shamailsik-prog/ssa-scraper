"""PakistanLawSite case-ID walk: which judgment page to ask for next.

The site opens every judgment at /Login/ReferenceCaseLawSearch?CaseName=<year><court><serial>, for
example 2026S701 (2026 SCMR 1, Supreme Court), 2026L2501 (2026 MLD 1, Lahore), 2026P4001 (2026 YLR 1,
Peshawar), 1980L238 and 1980L307 (1980 CLC 367 and 857). The serial counts up within one court and
year, and each journal starts its own block at a round number (701, 2501, 4001). So, as the
operator put it on 7 October 2026, the next judgment follows the last one: counting the serial up
from 1 reaches every judgment the site holds, not only the 20,568 rows of the citation table.

This module is the pure planner. It learns the court letters, years and serials already in the
corpus, orders the (year, court) groups, and walks a group one serial at a time: a serial already
held is never asked for, a run of misses jumps to the next block start (x01), and a group ends
past its ceiling. scraper/tasks/pls_caseid_walk.py does the fetching."""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

from scraper.extractors.judgment_guards import _has_case_content
from scraper.parsers.citation_extractor import extract_citations

CASE_ID_RE = re.compile(r"CaseName=(\d{4})([A-Za-z]{1,3})(\d{1,6})(?![0-9A-Za-z])")
BLOCK = 100
MIN_CASE_PAGE_CHARS = 300

CaseId = Tuple[int, str, int]


def parse_case_id(url: Optional[str]) -> Optional[CaseId]:
    """(year, court letter(s), serial) from a ReferenceCaseLawSearch link, or None."""
    m = CASE_ID_RE.search(url or "")
    if not m:
        return None
    return int(m.group(1)), m.group(2).upper(), int(m.group(3))


def group_key(year: int, court: str) -> str:
    return f"{year}{court}"


def split_group_key(key: str) -> Tuple[int, str]:
    return int(key[:4]), key[4:]


def case_name(key: str, serial: int) -> str:
    return f"{key}{serial}"


def case_url(base_url: str, key: str, serial: int) -> str:
    return f"{base_url.rstrip('/')}/Login/ReferenceCaseLawSearch?CaseName={case_name(key, serial)}&court=&Row=0&bookName=undefined"


def known_by_group(urls: Iterable[Optional[str]]) -> Dict[str, Set[int]]:
    out: Dict[str, Set[int]] = {}
    for url in urls:
        cid = parse_case_id(url)
        if cid:
            out.setdefault(group_key(cid[0], cid[1]), set()).add(cid[2])
    return out


def group_order(known: Dict[str, Set[int]], *, first_year: int, last_year: int) -> List[str]:
    """Groups the corpus already touches, densest first (their gaps hold the surest finds); then every
    other year for the court letters seen, newest year first."""
    held = sorted(known, key=lambda k: (-len(known[k]), -split_group_key(k)[0], k))
    courts = sorted({split_group_key(k)[1] for k in known})
    empty = [group_key(y, c) for y in range(last_year, first_year - 1, -1) for c in courts if group_key(y, c) not in known]
    return held + empty


def new_state() -> Dict[str, int | bool]:
    return {"next": 1, "streak": 0, "last_hit": 0, "hits": 0, "misses": 0, "done": False}


def ceiling(state: Dict, known: Set[int], *, floor: int, pad: int) -> int:
    top = max(max(known) if known else 0, int(state.get("last_hit") or 0))
    return max(floor, top + pad)


def next_serial(state: Dict, known: Set[int], *, floor: int, pad: int) -> Optional[int]:
    """The next serial to ask for, skipping serials already held; None (and done) past the ceiling.
    Passing a held serial is evidence of a populated stretch, so the miss streak starts again."""
    n = max(1, int(state.get("next") or 1))
    while n in known:
        n += 1
        state["streak"] = 0
    state["next"] = n
    if n > ceiling(state, known, floor=floor, pad=pad):
        state["done"] = True
        return None
    return n


def record(state: Dict, serial: int, hit: bool, known: Set[int], *, miss_streak: int) -> None:
    """Fold one answer into the walk. After `miss_streak` misses in a row each further miss jumps to
    the next block start (x01), but never past a serial already held."""
    if hit:
        state["hits"] = int(state.get("hits") or 0) + 1
        state["last_hit"] = max(int(state.get("last_hit") or 0), serial)
        state["streak"] = 0
        state["next"] = serial + 1
        known.add(serial)
        return
    state["misses"] = int(state.get("misses") or 0) + 1
    state["streak"] = int(state.get("streak") or 0) + 1
    nxt = serial + 1
    if state["streak"] >= miss_streak:
        block_start = ((serial - 1) // BLOCK + 1) * BLOCK + 1
        held_above = [k for k in known if k > serial]
        nxt = min([block_start] + ([min(held_above)] if held_above else []))
    state["next"] = nxt


def looks_like_case_page(text: str, year: int, *, raw_html: Optional[str] = None) -> bool:
    """A judgment page carries real text and a citation of its own year near the top; the site's
    answer for a serial it does not hold has neither."""
    body = (text or "").strip()
    if len(body) < MIN_CASE_PAGE_CHARS:
        return False
    if not _has_case_content(raw_text=body, raw_html=raw_html):
        return False
    return any(c.get("year") == year for c in extract_citations(body[:4000]))
