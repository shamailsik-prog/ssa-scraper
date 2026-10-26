"""Classify bodies fetched from PDF-shaped judgment URLs (view-file, .pdf links, etc.)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional

from scraper.fetchers import FetchResult, has_pdf_signature
from scraper.parsers.text_cleaner import clean_html

MIN_HTML_JUDGMENT_WORDS = 200
# Short Word-HTML order sheets / hearing lists lack "judgment" vocabulary but are real documents.
MIN_HTML_STRONG_COURT_DOCUMENT_WORDS = 50
# Real SHC bail / Katcha Peshi order sheets are often under 50 words after Word-HTML cleanup.
MIN_HTML_SHORT_ORDER_SHEET_WORDS = 12
PDF_MISS_DEFER_ERROR = "missing %PDF signature; body is not an accepted HTML judgment"
PDF_MISS_RETIRE_AFTER_ATTEMPTS = 10
# Whitespace-only HTTP 200 may be a short SHC burst; retire only after repeated empties over 24h.
EMPTY_BODY_RETIRE_AFTER_ATTEMPTS = 3
EMPTY_BODY_RETIRE_MIN_AGE_SECONDS = 86400
EMPTY_BODY_FIRST_SEEN_AT_KEY = "empty_body_first_seen_at"
TRANSIENT_HTTP_STATUSES = frozenset({406, 408, 425, 429, 502, 503, 504})
_HTML_JUDGMENT_MARKER_RE = re.compile(
    r"(?i)\b(judgment|judgement|decided on|coram|before|versus|vs\.?|v/s|v\.)\b"
)
# Headers and formats common on Pakistani high-court sites (SHC view-file Word-HTML, etc.).
_HTML_STRONG_COURT_DOCUMENT_RE = re.compile(
    r"(?i)(?:"
    r"\border\s*sheet\b|"
    r"\bordersheet\b|"
    r"\bhigh court of sindh\b|"
    r"\bin the high court of\b|"
    r"\bin the supreme court of\b|"
    r"\bfor katcha peshi\b|"
    r"\bpresent:\s*(?:mr\.?|ms\.?|mrs\.?|hon|the\s+registrar|court)"
    r")"
)
# Spaced "O R D E R" / short Larkano bench bail sheets (see FREE-SITES SHC view-file rows).
_HTML_SHORT_ORDER_SHEET_RE = re.compile(
    r"(?i)(?:"
    r"\border\s*sheet\b|"
    r"\bordersheet\b|"
    r"\bfor katcha peshi\b|"
    r"(?:\bhigh court of sindh\b|\bin the high court of sindh\b)"
    r".{0,200}?\b(?:criminal\s+bail|\bcr\.?\s*misc|c\.?\s*p\.?|\bbail\b)"
    r")"
)

# Word-HTML hard-wraps phrases across <br/> / newlines; collapse before phrase matching.
_WS_COLLAPSE_RE = re.compile(r"\s+")
# Letter-spaced headings in Word exports: "O R D E R", "O R D E R S H E E T".
_LETTER_SPACED_RUN_RE = re.compile(r"\b(?:[A-Za-z] )+[A-Za-z]\b")


def _collapse_letter_spaced_words(text: str) -> str:
    def _join_run(match: re.Match[str]) -> str:
        letters = match.group(0).split()
        if len(letters) >= 3 and all(len(part) == 1 for part in letters):
            return "".join(letters)
        return match.group(0)

    return _LETTER_SPACED_RUN_RE.sub(_join_run, text)


def _html_text_collapsed_whitespace(html: str) -> str:
    text = clean_html(html or "")
    return _WS_COLLAPSE_RE.sub(" ", text).strip()


def _html_text_for_markers(html: str) -> str:
    return _collapse_letter_spaced_words(_html_text_collapsed_whitespace(html))


def frontier_http_status_action(status_code: int) -> str:
    """How to treat a document fetch HTTP status before body classification."""
    if status_code < 400:
        return "ok"
    if status_code in (404, 410):
        return "retire"
    if status_code in (400, 401, 403):
        return "retire"
    if status_code in TRANSIENT_HTTP_STATUSES or status_code >= 500:
        return "defer"
    return "defer"


def pdf_miss_error_message(status_code: int, detail: str) -> str:
    return f"HTTP {status_code}; {detail}"[:1000]


def html_judgment_word_count(html: str) -> int:
    text = clean_html(html or "")
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


def html_body_has_short_order_sheet_marker(html: str) -> bool:
    text = _html_text_for_markers(html)
    return bool(_HTML_SHORT_ORDER_SHEET_RE.search(text))


def html_body_has_strong_court_document_marker(html: str) -> bool:
    text = _html_text_for_markers(html)
    return bool(_HTML_STRONG_COURT_DOCUMENT_RE.search(text))


def html_body_has_judgment_marker(html: str) -> bool:
    text = _html_text_for_markers(html)
    return bool(
        _HTML_JUDGMENT_MARKER_RE.search(text)
        or _HTML_STRONG_COURT_DOCUMENT_RE.search(text)
        or _HTML_SHORT_ORDER_SHEET_RE.search(text)
    )


def min_html_judgment_words(html: str) -> int:
    if html_body_has_short_order_sheet_marker(html):
        return MIN_HTML_SHORT_ORDER_SHEET_WORDS
    if html_body_has_strong_court_document_marker(html):
        return MIN_HTML_STRONG_COURT_DOCUMENT_WORDS
    return MIN_HTML_JUDGMENT_WORDS


def body_is_whitespace_only(content: bytes) -> bool:
    if not content:
        return True
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")
    return not clean_html(text).strip()


PdfEndpointBodyKind = Literal["pdf", "html_judgment", "empty", "defer"]


def classify_pdf_endpoint_body(res: FetchResult) -> PdfEndpointBodyKind:
    if has_pdf_signature(res.content):
        return "pdf"
    if body_is_whitespace_only(res.content):
        return "empty"
    threshold = min_html_judgment_words(res.text)
    if html_judgment_word_count(res.text) >= threshold and html_body_has_judgment_marker(res.text):
        return "html_judgment"
    return "defer"


def pdf_miss_backoff_seconds(attempts: int) -> int:
    """Exponential backoff for temporary non-PDF bodies (cap 24h)."""
    return min(86400, 300 * (2 ** min(max(int(attempts) - 1, 0), 6)))


def note_empty_body_first_seen(
    cursor_json: Optional[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Record the first time we saw a whitespace-only judgment document body."""
    out: dict[str, Any] = dict(cursor_json or {})
    if EMPTY_BODY_FIRST_SEEN_AT_KEY not in out:
        out[EMPTY_BODY_FIRST_SEEN_AT_KEY] = now.astimezone(timezone.utc).isoformat()
    return out


def _parse_cursor_timestamp(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def empty_body_retire_allowed(
    attempts: int,
    cursor_json: Optional[Mapping[str, Any]],
    now: datetime,
) -> bool:
    """True when empty-body failures may be retired (not merely deferred)."""
    if int(attempts) < EMPTY_BODY_RETIRE_AFTER_ATTEMPTS:
        return False
    raw = (cursor_json or {}).get(EMPTY_BODY_FIRST_SEEN_AT_KEY)
    if not raw:
        return False
    first_seen = _parse_cursor_timestamp(str(raw))
    age_seconds = (now.astimezone(timezone.utc) - first_seen).total_seconds()
    return age_seconds >= EMPTY_BODY_RETIRE_MIN_AGE_SECONDS
