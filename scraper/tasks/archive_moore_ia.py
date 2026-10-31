"""Moore's Indian Appeals (reporter MIA) from public-domain archive.org OCR.

One volume at a time. Nothing here is scheduled, and nothing writes the database.
Volume 13 is not on archive.org (follow-up). BAILII is not fetched.

    python -m scraper.tasks.archive_moore_ia --volume 6 --dry-run
    python -m scraper.tasks.archive_moore_ia --volume 6 --ocr-path ./reportsofcaseshe06grea_djvu.txt --dry-run

The first command downloads that volume's ``_djvu.txt`` only (about one request, then a
second only if the catalog filename 404s). Requests are at least one second apart.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote, urlparse

import httpx

from scraper.config import settings

logger = logging.getLogger(__name__)

SOURCE_NAME = "archive_moore_ia"
REPORTER = "MIA"
COURT = "Privy Council"
ARCHIVE_MIN_INTERVAL_SECONDS = 1.0

# Moore's Privy Council and Moore's Privy Council New Series. Not Indian Appeals.
DECOY_ITEM_IDS = frozenset(
    {
        "reportscaseshea00commgoog",
        "reportscaseshea00moorgoog",
        "reportscaseshea01commgoog",
    }
)

# PDF-only on BAILII, behind Anubis. Recorded so a later ingest can attach the URL.
# This module never requests bailii.org.
BAILII_SECONDARY_URLS = {
    (8, 379): "https://www.bailii.org/uk/cases/UKPC/1861/1861_19.html",
    (9, 307): "https://www.bailii.org/uk/cases/UKPC/1862/1862_21.html",
    (10, 203): "https://www.bailii.org/uk/cases/UKPC/1865/1865_33.html",
}


@dataclass(frozen=True)
class VolumeCatalogEntry:
    volume: int
    item_id: Optional[str]
    ocr_filename: Optional[str]
    year_from: int
    year_to: int
    alternates: tuple[str, ...] = ()
    note: str = ""


def _grea(volume: int, year_from: int, year_to: int, alternates: tuple[str, ...] = (), note: str = "") -> VolumeCatalogEntry:
    item_id = f"reportsofcaseshe{volume:02d}grea"
    return VolumeCatalogEntry(
        volume=volume,
        item_id=item_id,
        ocr_filename=f"{item_id}_djvu.txt",
        year_from=year_from,
        year_to=year_to,
        alternates=alternates,
        note=note,
    )


# Primary item is the grea series where it exists. DLI / Google fill volumes the grea
# scan does not cover. Alternates are recorded and not fetched.
MOORE_IA_VOLUMES: Dict[int, VolumeCatalogEntry] = {
    1: _grea(1, 1836, 1837, ("wbsl.20954", "dli.ernet.285813")),
    2: _grea(2, 1837, 1841, ("wbsl.21183",)),
    3: _grea(3, 1841, 1846, ("wbsl.20857",)),
    4: _grea(4, 1846, 1850, ("wbsl.21207", "in.ernet.dli.2015.213464")),
    5: _grea(5, 1849, 1854, ("wbsl.20824", "dli.ernet.469687")),
    6: _grea(6, 1854, 1857, ("dli.ernet.469685",), note="Alternate dli.ernet.469685 is PDF only."),
    7: _grea(7, 1857, 1860, ("dli.ernet.469860",)),
    8: _grea(8, 1859, 1861, ("in.ernet.dli.2015.531362",)),
    9: VolumeCatalogEntry(
        volume=9,
        item_id="in.ernet.dli.2015.531521",
        ocr_filename="2015.531521.Reports-Of_djvu.txt",
        year_from=1861,
        year_to=1864,
        alternates=("wbsl.21273",),
        note="wbsl.21273 is an uncertain vol 9 or 10 copy; not fetched.",
    ),
    10: _grea(10, 1864, 1866),
    11: VolumeCatalogEntry(
        volume=11,
        item_id="reportscaseshea01moorgoog",
        ocr_filename="reportscaseshea01moorgoog_djvu.txt",
        year_from=1866,
        year_to=1867,
        alternates=("dli.ernet.286028",),
        note="Google scan. reportscaseshea00moorgoog and reportscaseshea01commgoog are Moore PC NS, not this volume.",
    ),
    12: _grea(12, 1867, 1869, ("in.ernet.dli.2015.286029",)),
    13: VolumeCatalogEntry(
        volume=13,
        item_id=None,
        ocr_filename=None,
        year_from=1869,
        year_to=1871,
        note="Not on archive.org (13 IA 160 and 13 IA 467 have no scan). Follow-up, not a blocker for volumes 1-12 and 14.",
    ),
    14: VolumeCatalogEntry(
        volume=14,
        item_id="in.ernet.dli.2015.469863",
        ocr_filename="2015.469863.Reports-Of_djvu.txt",
        year_from=1871,
        year_to=1872,
    ),
}


class VolumeNotOnArchive(LookupError):
    """The volume has no archive.org item. Volume 13 is the known gap."""


class ArchiveFetchError(RuntimeError):
    """archive.org did not return OCR text for one volume."""


class ArchiveRateLimiter:
    """At least `interval` seconds between archive.org requests."""

    def __init__(self, interval: float = ARCHIVE_MIN_INTERVAL_SECONDS, *, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.interval = interval
        self._sleep = sleep
        self._clock = clock
        self._next = 0.0

    def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._clock()
        self._next = now + self.interval


_limiter = ArchiveRateLimiter()

_CAPTION_RE = re.compile(r"^On [Aa]ppeal [FfJj]rom\b")
_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*&\s*\d{1,2}(?:st|nd|rd|th)?)?"
    r"(?:\s+of)?"
    r"\s+"
    r"(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
    r",?\s+"
    r"(?P<year>18\d{2})\b",
    re.IGNORECASE,
)
_MONTHS = (
    ("september", 9),
    ("sept", 9),
    ("january", 1),
    ("february", 2),
    ("march", 3),
    ("april", 4),
    ("june", 6),
    ("july", 7),
    ("august", 8),
    ("october", 10),
    ("november", 11),
    ("december", 12),
    ("jan", 1),
    ("feb", 2),
    ("mar", 3),
    ("apr", 4),
    ("jun", 6),
    ("jul", 7),
    ("aug", 8),
    ("sep", 9),
    ("oct", 10),
    ("nov", 11),
    ("dec", 12),
    ("may", 5),
)
_VERSUS_RE = re.compile(r"(?:\bv\b|\bt\b|\^)", re.IGNORECASE)
_PAGE_END_RE = re.compile(r"(\d{1,4})\s*$")
_SECTION_RE = re.compile(r"^[A-Z0]\.?$")
_AND_RE = re.compile(r"AND\.?", re.IGNORECASE)
_STOPWORDS = frozenset(
    {
        "appellant",
        "appellants",
        "respondent",
        "respondents",
        "others",
        "appeal",
        "appeals",
        "court",
        "supreme",
        "sudder",
        "dewanny",
        "adawlut",
        "bombay",
        "calcutta",
        "bengal",
        "government",
        "company",
        "india",
        "indian",
        "privy",
        "council",
        "present",
        "members",
        "judicial",
        "committee",
        "bahadoor",
    }
)


def download_url(item_id: str, filename: str) -> str:
    return f"https://archive.org/download/{item_id}/{quote(filename, safe='.-_')}"


def details_url(item_id: str) -> str:
    return f"https://archive.org/details/{item_id}"


def ocr_filename_from_metadata(payload: Dict[str, Any]) -> Optional[str]:
    """The item's DjVu text filename, when archive.org metadata lists one."""
    files = payload.get("files") or []
    for item in files:
        if str(item.get("format") or "").lower() == "djvutxt" and item.get("name"):
            return str(item["name"])
    for item in files:
        name = str(item.get("name") or "")
        if name.endswith("_djvu.txt"):
            return name
    return None


def collapse_ocr_lines(text: str) -> List[str]:
    """One logical line per OCR line, with the wide word-spacing of djvu.txt collapsed."""
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.append(re.sub(r"[ \t]+", " ", raw).strip())
    return lines


def is_running_head(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 110:
        return False
    if re.match(r"^ON APPEAL FROM\b", text):
        return True
    if re.match(r"^\d{1,4}\s+CASES\b", text, re.IGNORECASE):
        return True
    if re.search(r"\bCASES\b", text, re.IGNORECASE) and re.search(
        r"PRIVY|PKIYY|PEIYY|COUNCIL|COUKCIL|COUXCIL", text, re.IGNORECASE
    ):
        return True
    if re.match(r"^\d{1,4}\s+MOORE", text, re.IGNORECASE):
        return True
    if re.search(r"INDIAN APPEALS", text, re.IGNORECASE):
        return True
    return False


def is_bare_page(line: str) -> bool:
    match = re.fullmatch(r"\d{1,4}", line.strip())
    return bool(match) and _plausible_page(int(match.group(0)))


def is_boilerplate(line: str) -> bool:
    """Digitization credits and a line whose host is archive.org itself.

    The host comes from urlparse, compared in full. A prefix check would treat
    ``https://archive.org.evil.com/...`` as this site.
    """
    if "digitized by" in line.lower():
        return True
    parsed = urlparse(line.strip())
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    host = host.lower().rstrip(".")
    return host == "archive.org" or host == "www.archive.org"


def is_caption(line: str) -> bool:
    text = line.strip()
    if text.startswith("ON APPEAL"):
        return False
    return bool(_CAPTION_RE.match(text))


def _plausible_page(number: int) -> bool:
    return 1 <= number <= 1200 and not 1800 <= number <= 1899


def _toc_header(lines: Sequence[str]) -> bool:
    compact = re.sub(r"[^A-Za-z]", "", "".join(lines)).upper()
    return "TABLE" in compact and "CASE" in compact


def _tokens(text: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z]{8,}", text) if word.lower() not in _STOPWORDS}


def parse_table_of_cases(lines: Sequence[str], end: int) -> List[tuple[str, int]]:
    start = next((i for i in range(end) if _toc_header(lines[i : i + 5])), None)
    if start is None:
        return []
    entries: List[tuple[str, int]] = []
    buf: List[str] = []
    for line in lines[start:end]:
        if not line or _SECTION_RE.match(line) or line.upper() == "PAGE":
            continue
        if _toc_header([line]) and not buf:
            continue
        buf.append(line)
        match = _PAGE_END_RE.search(line)
        if match is None:
            if len(buf) > 5:
                buf = buf[-1:]
            continue
        page = int(match.group(1))
        text = " ".join(buf)
        buf = []
        if not _plausible_page(page) or not _VERSUS_RE.search(text):
            continue
        entries.append((_PAGE_END_RE.sub("", text).strip(" .,"), page))
    return entries


def _running_page(lines: Sequence[str], party_start: int) -> Optional[int]:
    seen = 0
    index = party_start - 1
    while index >= 0 and seen < 12:
        line = lines[index]
        index -= 1
        if not line:
            continue
        seen += 1
        leading = re.match(r"^(\d{1,4})\s+CASES\b", line, re.IGNORECASE)
        if leading and _plausible_page(int(leading.group(1))):
            return int(leading.group(1))
        if is_running_head(line):
            trailing = _PAGE_END_RE.search(line)
            if trailing and _plausible_page(int(trailing.group(1))):
                return int(trailing.group(1))
        if is_bare_page(line):
            return int(line)
    return None


def _page_for_parties(parties: str, entries: Sequence[tuple[str, int]], running: Optional[int]) -> Optional[int]:
    want = _tokens(parties)
    scored = [(len(want & _tokens(text)), page) for text, page in entries if want]
    scored = [(score, page) for score, page in scored if score]
    if not scored:
        return running
    best = max(score for score, _page in scored)
    candidates = [page for score, page in scored if score == best]
    if len(set(candidates)) == 1:
        return candidates[0]
    if running is not None:
        return min(candidates, key=lambda page: (abs(page - running), page))
    return candidates[0]


def _party_start(lines: Sequence[str], caption_at: int) -> int:
    start = caption_at
    blanks = 0
    index = caption_at - 1
    while index >= 0 and caption_at - index <= 24:
        line = lines[index]
        if not line:
            blanks += 1
            if blanks >= 3:
                break
            index -= 1
            continue
        blanks = 0
        if is_running_head(line) or is_bare_page(line) or is_caption(line) or is_boilerplate(line):
            break
        if len(line) > 120 and not re.search(r"Appellant", line, re.IGNORECASE):
            break
        start = index
        index -= 1
    return start


def _join_hyphenated(lines: Iterable[str]) -> str:
    joined: List[str] = []
    for line in lines:
        if joined and joined[-1].endswith("-"):
            joined[-1] = joined[-1][:-1] + line.lstrip()
        else:
            joined.append(line)
    return " ".join(joined)


def _clean_party(text: str) -> str:
    text = re.sub(r"\bAppellants?\b\.?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bRespondents?\b\.?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[-–—._^)\]\[(,;:'\"`]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ,;")


def parties_from_block(block: Sequence[str]) -> str:
    kept = [line for line in block if line and not is_running_head(line) and not is_bare_page(line) and not is_boilerplate(line)]
    and_at = next((i for i, line in enumerate(kept) if _AND_RE.fullmatch(line.strip())), None)
    if and_at is None:
        return _clean_party(_join_hyphenated(kept))
    left = _clean_party(_join_hyphenated(kept[:and_at]))
    right = _clean_party(_join_hyphenated(kept[and_at + 1 :]))
    if left and right:
        return f"{left} v. {right}"
    return left or right


def lower_court_from_caption(lines: Sequence[str], caption_at: int, end: int) -> str:
    bits = [lines[caption_at]]
    if not lines[caption_at].rstrip().endswith("."):
        index = caption_at + 1
        while index < end and index < caption_at + 4:
            nxt = lines[index]
            index += 1
            if not nxt or is_running_head(nxt) or is_caption(nxt) or is_boilerplate(nxt):
                continue
            if len(nxt) > 80:
                break
            bits.append(nxt)
            if nxt.endswith("."):
                break
    text = re.sub(r"^On [Aa]ppeal [FfJj]rom\s+", "", " ".join(bits)).strip()
    return text.rstrip("., ")


def _month_number(token: str) -> Optional[int]:
    key = token.lower().rstrip(".")
    for name, number in _MONTHS:
        if key.startswith(name):
            return number
    return None


def decision_date_from_case(lines: Sequence[str], caption_at: int, end: int, year_from: int, year_to: int) -> Optional[str]:
    window = lines[caption_at : min(end, caption_at + 50)]
    blobs: List[str] = []
    buf: List[str] = []
    for line in window:
        if line and len(line) <= 40 and not is_running_head(line) and not is_bare_page(line):
            buf.append(line)
            continue
        if buf:
            blobs.append(" ".join(buf))
            buf = []
    if buf:
        blobs.append(" ".join(buf))
    found: List[str] = []
    for blob in blobs:
        for match in _DATE_RE.finditer(blob):
            day = int(match.group("day"))
            month = _month_number(match.group("month"))
            year = int(match.group("year"))
            if month is None or not 1 <= day <= 31:
                continue
            if year_from <= year <= year_to:
                found.append(f"{year:04d}-{month:02d}-{day:02d}")
    if found:
        return found[0]
    return None


def _case_text(lines: Sequence[str], start: int, end: int) -> str:
    kept: List[str] = []
    for line in lines[start:end]:
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if is_boilerplate(line) or is_running_head(line) or is_bare_page(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def dedup_key(volume: int, page: Optional[int]) -> Optional[str]:
    if page is None:
        return None
    return f"{REPORTER}:{int(volume)}:{int(page)}"


def _dropped_duplicate(row: Dict[str, Any]) -> Dict[str, Any]:
    record = {"dedup_key": row.get("dedup_key"), "parties": row.get("parties")}
    logger.warning("MIA dedupe dropped duplicate %s parties=%s", record["dedup_key"], record["parties"])
    return record


def dedupe_judgments(rows: Sequence[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """One record per (MIA, volume, page). The longer text wins. Unpaged rows are kept.

    The second list is every paged row that lost a collision, with its key and parties.
    """
    chosen: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    unpaged: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for row in rows:
        key = row.get("dedup_key")
        if not key:
            unpaged.append(row)
            continue
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = row
            order.append(key)
            continue
        if len(row.get("full_text") or "") > len(previous.get("full_text") or ""):
            dropped.append(_dropped_duplicate(previous))
            chosen[key] = row
        else:
            dropped.append(_dropped_duplicate(row))
    return [chosen[key] for key in order] + unpaged, dropped


def split_moore_ia_report(
    text: str,
    *,
    volume: int,
    item_id: Optional[str],
    year_from: int,
    year_to: int,
    ocr_source: str,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split one volume's OCR. Returns kept judgments and the duplicates dedupe dropped."""
    lines = collapse_ocr_lines(text)
    captions = [index for index, line in enumerate(lines) if is_caption(line)]
    toc_end = captions[0] if captions else len(lines)
    entries = parse_table_of_cases(lines, toc_end)
    raw_rows: List[Dict[str, Any]] = []
    for nth, caption_at in enumerate(captions):
        start = _party_start(lines, caption_at)
        block = lines[start:caption_at]
        if not any(re.search(r"Appellant", line, re.IGNORECASE) for line in block):
            continue
        next_start = len(lines)
        if nth + 1 < len(captions):
            next_start = _party_start(lines, captions[nth + 1])
        parties = parties_from_block(block)
        running = _running_page(lines, start)
        page = _page_for_parties(parties, entries, running)
        raw_rows.append(
            {
                "reporter": REPORTER,
                "volume": volume,
                "page": page,
                "parties": parties,
                "decision_date": decision_date_from_case(lines, caption_at, next_start, year_from, year_to),
                "court": COURT,
                "lower_court": lower_court_from_caption(lines, caption_at, next_start),
                "full_text": _case_text(lines, start, next_start),
                "source_url": details_url(item_id) if item_id else None,
                "ocr_source": ocr_source,
                "alt_source_url": BAILII_SECONDARY_URLS.get((volume, page)) if page else None,
                "dedup_key": dedup_key(volume, page),
            }
        )
    return dedupe_judgments(raw_rows)


def split_moore_ia_volume(
    text: str,
    *,
    volume: int,
    item_id: Optional[str],
    year_from: int,
    year_to: int,
    ocr_source: str,
) -> List[Dict[str, Any]]:
    """Kept judgments from one volume. Dropped duplicates are logged and omitted here."""
    kept, _dropped = split_moore_ia_report(
        text,
        volume=volume,
        item_id=item_id,
        year_from=year_from,
        year_to=year_to,
        ocr_source=ocr_source,
    )
    return kept


def _http_client() -> httpx.Client:
    return httpx.Client(
        timeout=60.0,
        follow_redirects=True,
        headers={"User-Agent": settings.SCRAPER_USER_AGENT, "Accept": "text/plain,application/json,*/*"},
    )


def _get(client: httpx.Client, limiter: ArchiveRateLimiter, url: str) -> httpx.Response:
    limiter.wait()
    logger.info("MIA archive.org GET %s", url)
    return client.get(url)


def _download_ocr(entry: VolumeCatalogEntry, client: httpx.Client, limiter: ArchiveRateLimiter) -> str:
    assert entry.item_id and entry.ocr_filename
    response = _get(client, limiter, download_url(entry.item_id, entry.ocr_filename))
    if response.status_code == 404:
        meta = _get(client, limiter, f"https://archive.org/metadata/{entry.item_id}")
        if meta.status_code != 200:
            raise ArchiveFetchError(f"archive.org metadata HTTP {meta.status_code} for {entry.item_id}")
        resolved = ocr_filename_from_metadata(meta.json())
        if not resolved or resolved == entry.ocr_filename:
            raise ArchiveFetchError(f"no DjVu text listed for {entry.item_id}")
        logger.info("MIA vol %s OCR filename from metadata: %s", entry.volume, resolved)
        response = _get(client, limiter, download_url(entry.item_id, resolved))
    if response.status_code != 200:
        raise ArchiveFetchError(f"archive.org HTTP {response.status_code} for MIA volume {entry.volume}")
    return response.text


def fetch_volume_ocr(
    volume: int,
    *,
    ocr_path: Optional[str] = None,
    client: Optional[httpx.Client] = None,
    limiter: Optional[ArchiveRateLimiter] = None,
) -> str:
    """OCR text for one volume, from a local file or from that volume's archive.org item."""
    if ocr_path:
        return Path(ocr_path).read_text(encoding="utf-8", errors="replace")
    entry = MOORE_IA_VOLUMES.get(volume)
    if entry is None or not entry.item_id or not entry.ocr_filename:
        raise VolumeNotOnArchive(f"MIA volume {volume} is not on archive.org; tracked as a follow-up.")
    limiter = limiter or _limiter
    if client is not None:
        return _download_ocr(entry, client, limiter)
    with _http_client() as owned:
        return _download_ocr(entry, owned, limiter)


def dry_run_volume(volume: int, *, ocr_path: Optional[str] = None, text: Optional[str] = None) -> Dict[str, Any]:
    entry = MOORE_IA_VOLUMES.get(volume)
    if entry is None:
        raise VolumeNotOnArchive(f"MIA volume {volume} is not on archive.org; tracked as a follow-up.")
    fetched = False
    if text is None:
        if ocr_path:
            text = fetch_volume_ocr(volume, ocr_path=ocr_path)
        else:
            text = fetch_volume_ocr(volume)
            fetched = True
    if entry.item_id and entry.ocr_filename:
        ocr_source = f"local:{ocr_path}" if ocr_path else f"archive.org:{entry.ocr_filename}"
    else:
        ocr_source = f"local:{ocr_path}" if ocr_path else ""
    rows, dropped = split_moore_ia_report(
        text,
        volume=volume,
        item_id=entry.item_id,
        year_from=entry.year_from,
        year_to=entry.year_to,
        ocr_source=ocr_source,
    )
    unpaged = sum(1 for row in rows if not row.get("page"))
    logger.info(
        "MIA volume %s dry-run: %s cases, %s unpaged, %s duplicates dropped (database not written)",
        volume,
        len(rows),
        unpaged,
        len(dropped),
    )
    return {
        "reporter": REPORTER,
        "volume": volume,
        "item_id": entry.item_id,
        "fetched": fetched,
        "cases": len(rows),
        "unpaged_count": unpaged,
        "dropped_duplicate_count": len(dropped),
        "dropped_duplicates": dropped,
        "wrote_database": False,
        "missing_volume": entry.item_id is None,
        "note": entry.note,
        "judgments": rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run Moore's Indian Appeals (MIA) from one archive.org volume. Does not write the database."
    )
    parser.add_argument("--volume", type=int, help="Volume 1–14. Volume 13 has no archive.org scan.")
    parser.add_argument("--ocr-path", help="Local _djvu.txt. Skips the network.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print records. This is the only mode.")
    parser.add_argument("--list-volumes", action="store_true", help="Print the item-id catalog, including the vol 13 gap.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.list_volumes:
        catalog = [
            {
                "volume": entry.volume,
                "item_id": entry.item_id,
                "ocr_filename": entry.ocr_filename,
                "years": [entry.year_from, entry.year_to],
                "alternates": list(entry.alternates),
                "note": entry.note,
            }
            for entry in MOORE_IA_VOLUMES.values()
        ]
        json.dump({"reporter": REPORTER, "volumes": catalog, "decoys_excluded": sorted(DECOY_ITEM_IDS)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.volume is None:
        _parser().error("--volume is required")
    try:
        payload = dry_run_volume(args.volume, ocr_path=args.ocr_path)
    except VolumeNotOnArchive as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ArchiveFetchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
