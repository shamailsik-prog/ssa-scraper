"""Indian courts on PakistanLawSite, from the CaseName token and the court label.

PLS CaseNames for Indian decisions look like ``1998A-INDIA6012`` (year, court
code, country, serial) or a CourtName slug such as ``MADRAS-HIGH-COURT-INDIA``.
The court directory's substring aliases then mis-read those names: ``lah`` sits
inside Allahabad, ``kar`` inside Karnataka, and ``supreme court`` inside
Supreme Court of India, so the row was stored as Lahore, Sindh, or the Supreme
Court of Pakistan.

Short codes below are the ones confirmed on live MAIN (2026-10-28):

- ``S-INDIA`` — Supreme Court of India (was Supreme Court of Pakistan)
- ``A-INDIA`` — Allahabad High Court (was Lahore High Court)
- ``KR-INDIA`` — Karnataka High Court (was High Court of Sindh)

Other Indian courts are read from the PLS CourtName slug (the same list the
site publishes: Madras, Madhya Pradesh, Andhra Pradesh, and the rest). An
unverified short code is not guessed. A CaseName that contains INDIA never
falls through to a Pakistani court.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote

# Confirmed CaseName codes. Do not invent the rest: an unknown code fails closed.
_INDIA_CASE_CODES = {
    "S": "Supreme Court of India",
    "A": "Allahabad High Court, India",
    "KR": "Karnataka High Court, India",
}

# PLS CourtName slugs (plsbeta courtwisesearch "Indian Courts"), longest first.
_HIGH_COURT_PLACES = (
    (("andhra", "pradesh"), "Andhra Pradesh"),
    (("assam", "nagaland"), "Assam and Nagaland"),
    (("himachal", "pradesh"), "Himachal Pradesh"),
    (("madhya", "pradesh"), "Madhya Pradesh"),
    (("punjab", "and", "haryana"), "Punjab and Haryana"),
    (("punjab", "haryana"), "Punjab and Haryana"),
    (("jammu", "and", "kashmir"), "Jammu and Kashmir"),
    (("jammu", "kashmir"), "Jammu and Kashmir"),
    (("allahabad",), "Allahabad"),
    (("bombay",), "Bombay"),
    (("chandigarh",), "Chandigarh"),
    (("calcutta",), "Calcutta"),
    (("gauhati",), "Gauhati"),
    (("delhi",), "Delhi"),
    (("gujarat",), "Gujarat"),
    (("kerala",), "Kerala"),
    (("karnataka",), "Karnataka"),
    (("madras",), "Madras"),
    (("orissa",), "Orissa"),
    (("mysore",), "Mysore"),
    (("patna",), "Patna"),
    (("rajasthan",), "Rajasthan"),
)

# Places whose letters collide with a Pakistani alias even with no "India" word.
_COLLIDING_PLACES = {"allahabad", "karnataka"}

_CODED_CASE = re.compile(
    r"(?i)(?:CaseName=|casetypeid=)?(\d{4})([A-Za-z]{1,8})-INDIA(\d+)"
)
_BARE_CODE = re.compile(r"(?i)^([A-Za-z]{1,8})-INDIA$")
_CASE_NAME_PARAM = re.compile(r"(?i)(?:CaseName|CourtName)=([^&]+)")
_HEADER_LINE = re.compile(
    r"(?im)^\s*(?:\[(?P<bracket>[^\]]{3,160})\]|IN\s+THE\s+(?P<inthe>[^\n]{3,160}))"
)


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _norm_court_key(value: str) -> str:
    text = value.lower().replace(".", "")
    text = re.sub(r"[\s_\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _high_court_name(place: str) -> str:
    return f"{place} High Court, India"


def _known_place(words: list[str]) -> Optional[str]:
    for phrase, place in _HIGH_COURT_PLACES:
        span = len(phrase)
        for i in range(len(words) - span + 1):
            if tuple(words[i : i + span]) == phrase:
                return place
    return None


def _generic_india_place(words: list[str]) -> Optional[str]:
    """Place tokens before HIGH only when the label itself says INDIA.

    Without that country word, "Lahore High Court" would be read as Indian.
    """
    if "india" not in words or "high" not in words:
        return None
    head = words[: words.index("high")]
    head = [w for w in head if w not in {"the", "of", "at", "in"} and not w.isdigit()]
    if not head:
        return None
    return " ".join(part.capitalize() for part in head)


def court_from_india_case_code(value: Optional[str]) -> Optional[str]:
    """Court for a ``YYYY{CODE}-INDIA####`` CaseName, or None when the code is unknown."""
    if not value:
        return None
    decoded = unquote(value)
    match = _CODED_CASE.search(decoded)
    code = match.group(2).upper() if match else None
    if code is None:
        bare = _BARE_CODE.match(decoded.strip())
        code = bare.group(1).upper() if bare else None
    if not code:
        return None
    return _INDIA_CASE_CODES.get(code)


def case_name_indicates_india(value: Optional[str]) -> bool:
    """True when a CaseName / CourtName / court label is an Indian PLS identifier."""
    if not value:
        return False
    decoded = unquote(value)
    if _CODED_CASE.search(decoded):
        return True
    param = _CASE_NAME_PARAM.search(decoded)
    token = param.group(1) if param else decoded.strip()
    if _BARE_CODE.match(token.strip()):
        return True
    words = _words(token)
    if "india" in words and (
        "court" in words or "high" in words or _CODED_CASE.search(token) or _BARE_CODE.match(token.strip())
    ):
        return True
    # A court field that is only the Indian place (Allahabad, Karnataka) still collides.
    if any(place in words for place in _COLLIDING_PLACES) and ("high" in words or "court" in words or len(words) <= 3):
        return True
    return False


def indian_court_from_label(value: Optional[str]) -> Optional[str]:
    """Canonical Indian court for a slug, header, or CaseName. None when it is not one."""
    if not value:
        return None
    decoded = unquote(value).strip()
    coded = court_from_india_case_code(decoded)
    if coded:
        return coded
    param = _CASE_NAME_PARAM.search(decoded)
    token = param.group(1) if param else decoded
    words = _words(token)
    if "india" in words and "supreme" in words and "court" in words:
        return "Supreme Court of India"
    place = _known_place(words)
    if place:
        has_india = "india" in words
        has_high_court = "high" in words and "court" in words
        colliding = any(part in _COLLIDING_PLACES for part in place.lower().split())
        if has_india or has_high_court or colliding:
            return _high_court_name(place)
        return None
    generic = _generic_india_place(words)
    if generic:
        return _high_court_name(generic)
    return None


def _indian_header_court(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    head = text[:800]
    for match in _HEADER_LINE.finditer(head):
        label = match.group("bracket") or match.group("inthe") or ""
        named = indian_court_from_label(label)
        if named:
            return named
    return None


def is_pakistani_court_name(name: Optional[str]) -> bool:
    """True for a seeded Pakistani court name or alias, never for an Indian court."""
    if not name or not str(name).strip():
        return False
    if indian_court_from_label(name):
        return False
    key = _norm_court_key(str(name))
    if not key or "india" in key.split():
        return False
    from scraper.database import COURT_SEED

    for row in COURT_SEED:
        labels = (row[0], row[1], *(row[5] or []))
        if any(_norm_court_key(label) == key for label in labels if label):
            return True
    return False


def resolve_displayed_court(
    *,
    grid_court: Optional[str],
    source_url: Optional[str] = None,
    text: Optional[str] = None,
) -> tuple[Optional[str], bool]:
    """Return ``(court, india_locked)``.

    ``india_locked`` means the CaseName or court label is Indian, so a Pakistani
    court must not be filled in later. The court is the Indian name, or None
    when the Indian court cannot be named.
    """
    sources = [source_url or "", grid_court or ""]
    india = any(case_name_indicates_india(item) for item in sources)
    for item in sources:
        coded = court_from_india_case_code(item)
        if coded:
            return coded, True
    for item in sources:
        named = indian_court_from_label(item)
        if named:
            return named, True
    if not india:
        return grid_court, False
    header = _indian_header_court(text)
    if header:
        return header, True
    if is_pakistani_court_name(grid_court):
        return None, True
    return grid_court, True


def india_case_rejects_pakistani_court(source_url: Optional[str], court_name: Optional[str]) -> bool:
    """Hard guard: a CaseName that says INDIA must not be stored under a Pakistani court."""
    if not case_name_indicates_india(source_url):
        return False
    return is_pakistani_court_name(court_name)
