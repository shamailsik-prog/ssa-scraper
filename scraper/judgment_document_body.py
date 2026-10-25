"""Classify bodies fetched from PDF-shaped judgment URLs (view-file, .pdf links, etc.)."""

from __future__ import annotations

import re
from typing import Literal

from scraper.fetchers import FetchResult, has_pdf_signature
from scraper.parsers.text_cleaner import clean_html

MIN_HTML_JUDGMENT_WORDS = 200
PDF_MISS_DEFER_ERROR = "missing %PDF signature; body is not an accepted HTML judgment"
PDF_MISS_RETIRE_AFTER_ATTEMPTS = 10
TRANSIENT_HTTP_STATUSES = frozenset({406, 408, 425, 429, 502, 503, 504})
_HTML_JUDGMENT_MARKER_RE = re.compile(
    r"(?i)\b(judgment|judgement|decided on|coram|before|versus|vs\.?|v\.)\b"
)


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


def html_body_has_judgment_marker(html: str) -> bool:
    text = clean_html(html or "")
    return bool(_HTML_JUDGMENT_MARKER_RE.search(text))


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
    if html_judgment_word_count(res.text) >= MIN_HTML_JUDGMENT_WORDS and html_body_has_judgment_marker(res.text):
        return "html_judgment"
    return "defer"


def pdf_miss_backoff_seconds(attempts: int) -> int:
    """Exponential backoff for temporary non-PDF bodies (cap 24h)."""
    return min(86400, 300 * (2 ** min(max(int(attempts) - 1, 0), 6)))
