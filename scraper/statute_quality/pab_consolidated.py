"""Title and year for a pabalochistan consolidated reprint.

A listing row, or a filename such as ``act282014``, can name the amending
instrument. When the PDF's first page carries an ``Amended upto`` or
``(as amended)`` marker, the statute title and year come from the base
Act's own heading. The listing or filename label is returned separately
so the caller can keep it as metadata. The cutoff stays on the heading,
so this edition does not share the bare Act's name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import unquote, urlsplit

_FIRST_PAGE_LINES = 80

_PAGE_NO_RE = re.compile(r"^\d{1,3}$")
_ORDINAL_ONLY_RE = re.compile(r"(?i)^(st|nd|rd|th)\.?$")
_CHROME_LINE_RE = re.compile(
    r"(?i)^(?:"
    r"extraordinary\b.*|"
    r"registered\s+no\b.*|"
    r"to be published\b.*|"
    r"balochistan provincial assembly\b.*|"
    r"secretariat\b.*|"
    r"notification\b.*|"
    r"dated\s*:.*"
    r")$"
)
_ACT_YEAR_LINE_RE = re.compile(
    r"(?i)^(?P<head>.*\b(?:act|ordinance|order|rules|regulations)\b)"
    r"\s*,?\s*(?P<year>(?:1[89]|20)\d{2})\s*\.?\s*$"
)
_INLINE_MARKER_RE = re.compile(
    r"(?i)^(?P<head>.*\b(?:act|ordinance|order|rules|regulations)\b)"
    r"\s*,?\s*(?P<year>(?:1[89]|20)\d{2})\s*\.?\s*"
    r"\(\s*(?P<marker>amended\s+up\s*to\b[^)]*|as\s+amended(?:\s+up\s*to\b[^)]*)?)\s*\)"
    r"\s*\.?\s*$"
)
_UPTO_LINE_RE = re.compile(
    r"(?i)^[ \t]*(?:\(\s*)?(?P<kw>amended\s+up\s*to)\b(?P<rest>[^)\n]*?)\)?\s*\.?\s*$"
)
_AS_AMENDED_LINE_RE = re.compile(
    r"(?i)^[ \t]*(?:\(\s*)?(?P<kw>as\s+amended)"
    r"(?P<rest>(?:\s+up\s*to\b[^)\n]*)?)\)?\s*\.?\s*$"
)
_AMENDMENT_LABEL_RE = re.compile(r"(?i)\bamendment\b|\bact\s+no\b")
_FILENAME_ACT_RE = re.compile(r"(?i)act(\d{1,3})((?:19|20)\d{2})(?!\d)")
_HAS_ORDINAL_RE = re.compile(r"(?i)\d(?:st|nd|rd|th)\b")


@dataclass(frozen=True)
class ConsolidatedHeading:
    title: str
    year: int
    marker: str


def is_pabalochistan_source(url: str) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "pabalochistan.gov.pk"


def amendment_label_from_source(source_meta: Mapping[str, object]) -> str:
    """Listing or filename amendment label, kept beside the base-Act heading."""
    for key in ("act_title", "detail_title", "statute_name"):
        value = re.sub(r"\s+", " ", str(source_meta.get(key) or "")).strip()
        if value and _AMENDMENT_LABEL_RE.search(value):
            return value[:500]
    return _filename_act_label(str(source_meta.get("url") or ""))


def consolidated_base_heading(text: str) -> Optional[ConsolidatedHeading]:
    """Base-Act title, enactment year, and cutoff marker from the first page."""
    lines = (text or "").splitlines()[:_FIRST_PAGE_LINES]
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        inline = _INLINE_MARKER_RE.match(stripped)
        if inline:
            marker = _collapse_marker(inline.group("marker"), ordinal=None)
            return _build(lines, index, inline.group("head"), int(inline.group("year")), marker)
        upto = _UPTO_LINE_RE.match(stripped)
        if upto:
            found = _act_line_above(lines, index)
            if found is None:
                continue
            act_index, head, year, ordinal = found
            marker = _collapse_marker(f"{upto.group('kw')} {upto.group('rest')}", ordinal)
            return _build(lines, act_index, head, year, marker)
        as_amended = _AS_AMENDED_LINE_RE.match(stripped)
        if as_amended:
            found = _act_line_above(lines, index)
            if found is None:
                continue
            act_index, head, year, ordinal = found
            marker = _collapse_marker(
                f"{as_amended.group('kw')} {as_amended.group('rest')}",
                ordinal,
            )
            return _build(lines, act_index, head, year, marker)
    return None


def _filename_act_label(url: str) -> str:
    match = _FILENAME_ACT_RE.search(unquote(urlsplit(url or "").path))
    if not match:
        return ""
    return f"Act No. {int(match.group(1))} of {match.group(2)}"


def _collapse_marker(raw: str, ordinal: Optional[str]) -> str:
    body = re.sub(r"\s+", " ", raw or "").strip(" .")
    if ordinal and not _HAS_ORDINAL_RE.search(body):
        body = re.sub(
            r"(\d{1,2})",
            lambda match: match.group(1) + ordinal.lower(),
            body,
            count=1,
        )
    return re.sub(r"\s+", " ", body).strip()


def _act_line_above(lines: list[str], marker_index: int):
    ordinal = None
    skipped_ordinal = False
    index = marker_index - 1
    while index >= 0:
        stripped = lines[index].strip()
        if not stripped or _PAGE_NO_RE.match(stripped):
            index -= 1
            continue
        if _ORDINAL_ONLY_RE.match(stripped) and not skipped_ordinal:
            ordinal = stripped.strip(".")
            skipped_ordinal = True
            index -= 1
            continue
        parsed = _split_act_year(stripped)
        if parsed is None:
            return None
        head, year = parsed
        return index, head, year, ordinal
    return None


def _split_act_year(line: str) -> Optional[tuple[str, int]]:
    match = _ACT_YEAR_LINE_RE.match(line.strip())
    if not match:
        return None
    head = re.sub(r"\s+", " ", match.group("head")).strip(" .,")
    if not head:
        return None
    return head, int(match.group("year"))


def _is_title_continuation(line: str) -> bool:
    if _CHROME_LINE_RE.match(line) or _PAGE_NO_RE.match(line) or _ORDINAL_ONLY_RE.match(line):
        return False
    if _ACT_YEAR_LINE_RE.match(line):
        return False
    if line.endswith((".", "?", "!", ":")):
        return False
    letters = [char for char in line if char.isalpha()]
    return len(letters) >= 3


def _continuations_above(lines: list[str], act_index: int) -> list[str]:
    collected: list[str] = []
    index = act_index - 1
    while index >= 0:
        stripped = lines[index].strip()
        index -= 1
        if not stripped:
            continue
        if not _is_title_continuation(stripped):
            break
        collected.append(re.sub(r"\s+", " ", stripped))
    collected.reverse()
    return collected


def _build(lines: list[str], act_index: int, head: str, year: int, marker: str) -> ConsolidatedHeading:
    parts = _continuations_above(lines, act_index)
    parts.append(re.sub(r"\s+", " ", head).strip(" .,"))
    heading = re.sub(r"\s+", " ", " ".join(parts)).strip()
    marker = re.sub(r"\s+", " ", marker).strip()
    title = f"{heading}, {year} ({marker})"
    return ConsolidatedHeading(title=title, year=year, marker=marker)
