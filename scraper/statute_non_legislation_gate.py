"""Keep department pages and bare government titles out of the statute corpus.

Two rows on main were accepted, then quarantined by hand:

* ``c6c40abd`` — ``Team.aspx`` on sindhlaws.gov.pk, a staff and phone page.
* ``5e575644`` — PDF ``PUB-26-000048.pdf`` titled only ``GOVERNMENT OF SINDH``.

Gazette listings and PDF publications stay. A short title that names an Act,
Ordinance, Rules, Regulation, Order, Statute, Code, or the Constitution stays.
A department masthead stays out even when it says ``Law and Order``.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlsplit

HTML_TEAM_CONTACT_REASON = "non_statute_department_page;html_team_contact"
BARE_NAME_REASON = "bare_name_no_act_word"

_SINDHLAWS_HOST = "sindhlaws.gov.pk"
# HTML the Sindh Laws crawler must still follow. Anything else on that host
# that is an HTML page is site chrome, not a statute.
_ALLOWED_HTML_PATHS = frozenset(
    {
        "/",
        "/index.aspx",
        "/gazette.aspx",
        "/gazettedetail.aspx",
        "/library.aspx",
    }
)
_HTML_SUFFIXES = (".aspx", ".html", ".htm", ".php", ".asp")
_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx")

_WS_RE = re.compile(r"\s+")
_STRONG_LEGISLATIVE_RE = re.compile(
    r"(?i)\b(?:acts?|ordinances?|rules?|regulations?|statutes?|codes?|constitution)\b"
)
_ORDER_RE = re.compile(r"(?i)\borders?\b")
_DEPARTMENT_RE = re.compile(r"(?i)\bdepartments?\b")
_BARE_GOVERNMENT_RE = re.compile(
    r"(?i)^(?:the\s+)?government\s+of\s+(?:the\s+)?[a-z][a-z .'-]*$"
)


def _sindhlaws_path(url: str) -> Optional[str]:
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != _SINDHLAWS_HOST:
        return None
    path = unquote(parts.path or "/").lower()
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def sindhlaws_non_statute_html_url(url: str) -> bool:
    """True for a sindhlaws.gov.pk HTML page that is not a gazette listing."""
    path = _sindhlaws_path(url)
    if path is None or path in _ALLOWED_HTML_PATHS:
        return False
    if path.endswith(_DOCUMENT_SUFFIXES):
        return False
    return path.endswith(_HTML_SUFFIXES)


def sindhlaws_html_statute_reject_reason(*, kind: str, url: str) -> Optional[str]:
    if kind not in ("statute", "instrument"):
        return None
    if not sindhlaws_non_statute_html_url(url):
        return None
    return HTML_TEAM_CONTACT_REASON


def _flat_title(name: str) -> str:
    flat = _WS_RE.sub(" ", (name or "").replace("\u00a0", " ")).strip()
    return flat.strip(" \t.,:;|-")


def _arabic_letter_ratio(name: str) -> float:
    letters = [ch for ch in name if ch.isalpha()]
    if not letters:
        return 0.0
    arabic = 0
    for ch in letters:
        code = ord(ch)
        if (
            0x0600 <= code <= 0x06FF
            or 0x0750 <= code <= 0x077F
            or 0xFB50 <= code <= 0xFDFF
            or 0xFE70 <= code <= 0xFEFF
        ):
            arabic += 1
    return arabic / len(letters)


def title_lacks_legislative_word(name: str) -> bool:
    """True when ``name`` is not a legislative short title.

    Empty names are a different gate. An Arabic-script title is left alone so
    an Urdu short title is not dropped for lacking an English word. ``Order``
    counts, except inside a department masthead (``Law and Order Department``).
    """
    flat = _flat_title(name)
    if not flat:
        return False
    if _arabic_letter_ratio(flat) >= 0.5:
        return False
    if _STRONG_LEGISLATIVE_RE.search(flat):
        return False
    if _DEPARTMENT_RE.search(flat) or _BARE_GOVERNMENT_RE.fullmatch(flat):
        return True
    if _ORDER_RE.search(flat):
        return False
    return True
