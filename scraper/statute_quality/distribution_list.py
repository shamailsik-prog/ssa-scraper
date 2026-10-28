"""Strip Balochistan-style distribution-list footers before section splitting.

Assembly PDFs end with a "copy forwarded" / "No. …" block whose lines are
numbered like sections:

    1. The Principal Secretary to Governor Balochistan, Quetta.
    6. The System Analyst, Balochistan Provincial Assembly.

Those lines were stored as sections 1–6. Defect class:
``distribution_list_footer_as_sections``.

The block is a suffix of the extracted text. A secretary named inside an
operative provision stays. Re-extract from the source PDF after this change;
do not rewrite the stored footer rows in place.
"""

from __future__ import annotations

import re

_MAY_PLEASE_RE = re.compile(r"(?i)\bmay\s+please\b")
_OPERATIVE_RE = re.compile(
    r"(?i)\b(?:shall|must|means|includes|notwithstanding|hereby|deemed|"
    r"provided\s+that|subject\s+to|may|is|are|was|were|be|been|"
    r"consist|comprise|appoint|notify|publish|hold|exercise|perform)\b"
)
_ADDRESSEE_RE = re.compile(
    r"(?i)^(?:\d+\s*[\.\)\]\:\-]+\s*)?(?:the\s+)?"
    r"(?:"
    r"principal\s+secretary\b"
    r"|chief\s+secretary\b"
    r"|secretary\s*[,.]?\s+government\b"
    r"|secretary\s+to\s+(?:the\s+)?(?:government|governor|chief\s+minister)\b"
    r"|director[\s-]*general\b[^.\n]{0,120}?\b(?:public\s+relations|press\s+information)\b"
    r"|system\s+analyst\b"
    r"|(?:p\.?\s*s\.?|private\s+secretary)\s+to\s+(?:the\s+)?secretary\b"
    r")"
)
_HEADER_RE = re.compile(
    r"(?i)(?:\ba\s+)?cop(?:y|ies)\s+(?:is\s+|are\s+)?forwarded\b|\bcopy\s+for\s+information\b"
)
_REFERENCE_RE = re.compile(r"(?i)^no\.?\s*\S*(?:/|legis)")
_DATED_RE = re.compile(r"(?i)^dated\b")
_SIGNATURE_RE = re.compile(
    r"(?i)^(?:"
    r"[\(\{][^)\n]{2,80}[\)\}]"
    r"|special\s+secretary\b.*$"
    r"|secretary(?:\s*\([^)]{0,40}\))?\s*\.?"
    r")$"
)
_CONTINUATION_RE = re.compile(
    r"(?i)^(?:and\s+)?(?:department|quetta|karachi|islamabad|lahore|peshawar|muzaffarabad|gilgit)\b.{0,80}$"
)
_PRINTING_RE = re.compile(
    r"(?i)(?:printing\s+press|proof\s+read|fifty\s+copies|chief\s+controller|"
    r"for\s+favou?r\s+of\s+publication|extra[\s\-–]*ordinary|ordinary\s+issue|"
    r"final\s+printing|may\s+please|secretariat\s+for\s+record)"
)
_PAGE_RE = re.compile(r"^\d{1,3}$")
_BLOCK_LINE_KINDS = frozenset(
    {"addressee", "header", "reference", "dated", "signature", "page", "continuation", "printing", "blank"}
)
_TRAILING_FURNITURE = frozenset({"blank", "signature", "page"})


def _operative(text: str) -> bool:
    return bool(_OPERATIVE_RE.search(_MAY_PLEASE_RE.sub(" ", text or "")))


def _line_kind(stripped: str) -> str:
    if not stripped:
        return "blank"
    # Forwarding letters say "a copy is forwarded" and "may please be supplied".
    # Those are furniture even though they contain "is" or "be". A line that
    # still says "shall" is operative text and stops the walk.
    shall = bool(re.search(r"(?i)\bshall\b", stripped))
    if not shall and _HEADER_RE.search(stripped):
        return "header"
    if not shall and _REFERENCE_RE.match(stripped):
        return "reference"
    if _DATED_RE.match(stripped):
        return "dated"
    if _SIGNATURE_RE.match(stripped):
        return "signature"
    if _PAGE_RE.fullmatch(stripped):
        return "page"
    if not shall and _PRINTING_RE.search(stripped):
        return "printing"
    if _operative(stripped):
        return "act"
    if _ADDRESSEE_RE.match(stripped):
        return "addressee"
    if _CONTINUATION_RE.match(stripped):
        return "continuation"
    return "act"


def _has_distribution_cue(block: list[str]) -> bool:
    for line in block:
        kind = _line_kind(line.strip())
        if kind in {"header", "reference"}:
            return True
        if re.search(r"(?i)system\s+analyst|public\s+relations|p\.?\s*s\.?\s+to\s+(?:the\s+)?secretary", line):
            return True
    return False


def _distribution_block_start(lines: list[str]) -> int | None:
    """Line index where a trailing distribution block begins, or None."""
    i = len(lines) - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    skipped = 0
    while i >= 0 and skipped < 8 and _line_kind(lines[i].strip()) in _TRAILING_FURNITURE:
        i -= 1
        skipped += 1
    if i < 0 or _line_kind(lines[i].strip()) != "addressee":
        return None
    addressees = 0
    while i >= 0:
        kind = _line_kind(lines[i].strip())
        if kind not in _BLOCK_LINE_KINDS:
            break
        if kind == "addressee":
            addressees += 1
        i -= 1
    start = i + 1
    if addressees < 2:
        return None
    block = lines[start:]
    if not _has_distribution_cue(block):
        return None
    return start


def strip_distribution_list_footer(text: str) -> str:
    """Remove a trailing copy-forwarded addressee block. Leave other text unchanged."""
    if not text:
        return text or ""
    lines = text.splitlines()
    start = _distribution_block_start(lines)
    if start is None:
        return text
    return "\n".join(lines[:start]).rstrip()


def is_distribution_addressee_section(text: str) -> bool:
    """True when a candidate section is only an official's designation line."""
    raw = (text or "").strip()
    if not raw or _operative(raw):
        return False
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return False
    addressees = 0
    for line in lines:
        kind = _line_kind(line)
        if kind == "addressee":
            addressees += 1
            continue
        if kind in {"continuation", "signature", "page", "header", "reference", "dated", "printing"}:
            continue
        return False
    return addressees >= 1
