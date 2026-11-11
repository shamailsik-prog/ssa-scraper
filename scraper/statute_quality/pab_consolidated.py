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
# A consolidated marker is a parenthetical in the title block. Preamble and
# short-title lines are not that block, even when they say "(as amended)".
_PREAMBLE_OR_CLAUSE_RE = re.compile(
    r"(?i)(?:"
    r"^(?:an\s+act|a\s+bill|whereas|it\s+is\s+hereby|short\s+title)\b"
    r"|"
    r"\b(?:further\s+to\s+amend|to\s+further\s+amend|to\s+amend|may\s+be\s+called|whereas)\b"
    r")"
)
_MARKER_START_RE = re.compile(r"(?i)(?:amended\s+up\s*to|as\s+amended)\b")
_MARKER_BODY_RE = re.compile(
    r"(?i)\(\s*(?P<body>(?:amended\s+up\s*to|as\s+amended)\b[^)]*)\)"
)
_MARKER_PAREN_RE = re.compile(
    r"(?i)\s*\(\s*(?:amended\s+up\s*to|as\s+amended)\b[^)]*\)"
)
_CUTOFF_YEAR_RE = re.compile(r"\b(?:1[89]|20)\d{2}\b")
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
    """Base-Act title, enactment year, and cutoff marker from the first page.

    The marker has to be a finished parenthetical in the title block, before
    ``An Act`` / ``further to amend`` / ``may be called``. An ``Amended upto``
    cutoff wrapped onto the next line is joined first. An open cutoff that
    never reaches its year is not a heading, so two reprints cannot share a
    title that dropped the year.
    """
    lines = (text or "").splitlines()[:_FIRST_PAGE_LINES]
    limit = _title_block_end(lines)
    index = 0
    while index < limit:
        assembled = _assemble_marker(lines, index, limit)
        if assembled is None:
            index += 1
            continue
        body, last = assembled
        if not _marker_is_complete(body):
            index = last + 1
            continue
        inline = _act_on_marker_line(lines[index])
        if inline is not None:
            act_index = index
            head, year = inline
            ordinal = None
        else:
            found = _act_line_above(lines, index)
            if found is None:
                index = last + 1
                continue
            act_index, head, year, ordinal = found
        marker = _collapse_marker(body, ordinal)
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


def _is_preamble_or_clause(line: str) -> bool:
    return bool(_PREAMBLE_OR_CLAUSE_RE.search(line or ""))


def _title_block_end(lines: list[str]) -> int:
    """Index of the first preamble or short-title line. The heading sits above it."""
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped and _is_preamble_or_clause(stripped):
            return index
    return len(lines)


def _marker_is_complete(body: str) -> bool:
    """``Amended upto`` needs its cutoff year. Bare ``(as amended)`` does not."""
    if re.search(r"(?i)\bup\s*to\b", body or ""):
        return _CUTOFF_YEAR_RE.search(body or "") is not None
    return bool(re.match(r"(?i)as\s+amended\b", (body or "").strip()))


def _assemble_marker(lines: list[str], index: int, limit: int) -> Optional[tuple[str, int]]:
    """Finished parenthetical marker starting at ``index``, and the last line it uses.

    A line that opens ``(Amended upto 9th October,`` is not finished until the
    closing paren, which may hold the cutoff year on the next line. Preamble
    text is not consumed, and an unclosed paren is rejected.
    """
    first = lines[index].strip()
    if not first or not _MARKER_START_RE.search(first) or _is_preamble_or_clause(first):
        return None
    chunk = first
    last = index
    cursor = index
    while chunk.count("(") > chunk.count(")") and cursor + 1 < limit:
        cursor += 1
        if cursor - index > 3:
            break
        nxt = lines[cursor].strip()
        if not nxt or _ORDINAL_ONLY_RE.match(nxt) or _PAGE_NO_RE.match(nxt):
            continue
        if _is_preamble_or_clause(nxt):
            break
        chunk = f"{chunk} {nxt}"
        last = cursor
    if chunk.count("(") > chunk.count(")"):
        return None
    match = _MARKER_BODY_RE.search(chunk)
    if not match:
        return None
    return match.group("body").strip(), last


def _act_on_marker_line(line: str) -> Optional[tuple[str, int]]:
    """Act year on the same line as a finished marker, when that line is the heading."""
    if _is_preamble_or_clause(line):
        return None
    without = _MARKER_PAREN_RE.sub("", line).strip()
    parsed = _split_act_year(without)
    if parsed is None:
        return None
    head, year = parsed
    if _is_preamble_or_clause(head):
        return None
    return head, year


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
        if _is_preamble_or_clause(stripped):
            return None
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
    if _is_preamble_or_clause(line):
        return False
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
