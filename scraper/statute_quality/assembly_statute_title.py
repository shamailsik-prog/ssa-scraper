"""Title and year for National Assembly and Senate statute PDFs.

The ingest used to keep an OCR fragment of the body (``Itr this Act``,
``The follorving Act``, ``REGTSTTRED No.``) or the listing chrome that was
glued on after the PDF path. The title now comes from the short-title
clause, then from an ``AN ACT to …`` heading read with the ``Act No. … of
YYYY`` line, then from a real listing title. A candidate that is still
chrome, a body clause, or too thin to be a name is ``name_unknown``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from scraper.parsers.statute_parser import (
    _is_plausible_act_title,
    looks_like_clause_fragment,
    looks_like_garbage_name,
)
from scraper.statute_quality.senate_integrity import is_gazette_non_act_title

_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_INSTRUMENT_RE = re.compile(
    r"(?i)\b(acts?|ordinances?|orders?|codes?|rules|regulations|constitution)\b"
)
_CALLED_RE = re.compile(
    r"(?is)\b(?:this|the)\s+act\s+(?:shall|may)\s+be\b"
    r".{0,180}?\bcalled\s+(?:the\s+)?"
    r"(?P<title>.{4,240}?)"
    r"(?=\s*(?:\(\s*[0-9]+\s*\)|\bit\s+(?:extends|shall|ex)\b|\n\s*\n|\Z))"
)
_AN_ACT_RE = re.compile(
    r"(?is)\ban\s+act\b.{0,80}?\bto\s+(?P<purpose>[^\n]{8,240})"
)
_ESTABLISH_RE = re.compile(
    r"(?i)^(?:further\s+to\s+)?(?:"
    r"establish|provide\s+for(?:\s+the)?\s+establishment\s+of"
    r")\s+(?:the\s+)?(?P<name>.+?)\s*$"
)
_ACT_NO_YEAR_RE = re.compile(
    r"(?i)\b(?:act|acr)\s+no\.?\s+[A-Za-z0-9]{1,16}\s+(?:of|o[fnr])\s+"
    r"((?:1[89]|20)\d{2})\b"
)
_GAZETTE_DATE_RE = re.compile(
    r"(?i)\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b"
    r"[^\n]{0,30}\b((?:1[89]|20)\d{2})\b"
)
_LISTING_CHROME_RE = re.compile(
    r"(?i)(?:"
    r"\bshare\s+print\b"
    r"|\bfilter\s+by\b"
    r"|\bgovernment\s+bills\s+laid\b"
    r"|\bprivate\s+member'?s?\s+bills\s+laid\b"
    r"|\bacts\s+of\s+parliament\b"
    r")"
)
_WELL_FORMED_TITLE_RE = re.compile(
    r"(?i)\b(?:act|ordinance|order|code|rules|regulations|constitution)\b"
    r",?\s+(?:1[89]\d{2}|20\d{2})\b"
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_STOP = frozenset({"the", "and", "of", "for", "a", "an", "to", "or", "by"})
_INSTRUMENT_WORDS = frozenset(
    {
        "act",
        "acts",
        "ordinance",
        "ordinances",
        "order",
        "orders",
        "code",
        "codes",
        "rules",
        "regulations",
        "constitution",
    }
)
_FUNCTION_WORDS = frozenset({"the", "and", "of", "to", "for", "an", "a", "or"})
_SHORT_OCR = {
    "rhe": "the",
    "teh": "the",
    "tne": "the",
    "ad": "and",
    "nd": "and",
    "ot": "of",
}
# Words a gazette title is allowed to snap to. Proper names that are not here
# stay as printed, so a two-edit slip cannot rename an instrument.
_TITLE_WORDS = frozenset(
    {
        "national",
        "pakistan",
        "authority",
        "food",
        "safety",
        "trade",
        "infrastructure",
        "development",
        "assets",
        "management",
        "library",
        "federal",
        "islamic",
        "republic",
        "amendment",
        "establishment",
        "establish",
        "provide",
        "further",
        "amend",
        "commission",
        "corporation",
        "university",
        "institute",
        "finance",
        "force",
        "shipping",
        "service",
        "tribunal",
        "tribunals",
    }
)
_PHRASES = (
    ("this act", (1, 0)),
    ("following act", (2, 0)),
    ("registered no", (2, 1)),
    ("further to amend", (2, 0, 1)),
    ("an act to", (1, 0, 0)),
    ("whereas it", (2, 0)),
)
_LEADING_DIGIT = {"4": "a", "8": "b", "0": "o", "1": "i", "5": "s"}


@dataclass(frozen=True)
class AssemblyTitleDecision:
    name: str
    year: Optional[int]
    source: str
    detail: str = ""


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if abs(len(left) - len(right)) > 3:
        return abs(len(left) - len(right))
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, 1):
        current = [i]
        for j, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _within(left: str, right: str, budget: int) -> bool:
    if abs(len(left) - len(right)) > budget:
        return False
    return _edit_distance(left, right) <= budget


def _letters(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", token or "")


def _phrase_words(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _collapse(name).lower())


def _matches_phrase(name: str, phrase: str, budgets: tuple[int, ...]) -> bool:
    words = _phrase_words(name)
    target = phrase.split()
    width = len(target)
    if len(words) < width:
        return False
    for start in range(len(words) - width + 1):
        window = words[start : start + width]
        if all(_within(got, want, budget) for got, want, budget in zip(window, target, budgets)):
            return True
    return False


def is_listing_chrome_text(text: str) -> bool:
    """True for NA listing chrome (``Share Print …``, ``Filter by``, bills-laid)."""
    flat = _collapse(text)
    if not flat or not _LISTING_CHROME_RE.search(flat):
        return False
    if re.search(r"(?i)\bshare\s+print\b|\bfilter\s+by\b", flat):
        return True
    if re.search(r"(?i)\b(?:government|private)\b.+\bbills\s+laid\b", flat):
        return True
    # ``Acts of Parliament`` on its own is the listing column, not an instrument.
    return _WELL_FORMED_TITLE_RE.search(flat) is None


def _real_words(name: str) -> list[str]:
    words = []
    for token in _WORD_RE.findall(name or ""):
        if len(token) < 3 or token.lower() in _STOP:
            continue
        words.append(token)
    return words


def _valid_year(year: int) -> bool:
    return 1800 <= year <= 2099


def _year_in(text: str) -> Optional[int]:
    match = _YEAR_RE.search(text or "")
    if not match:
        return None
    year = int(match.group(1))
    return year if _valid_year(year) else None


def _substantive_words(name: str) -> list[str]:
    return [word for word in _real_words(name) if word.lower() not in _INSTRUMENT_WORDS]


def is_well_formed_instrument_title(name: str) -> bool:
    """A short title such as ``Finance Act, 2026`` stays even with two words."""
    flat = _collapse(name)
    if not flat or not _INSTRUMENT_RE.search(flat):
        return False
    if not _substantive_words(flat):
        return False
    if is_listing_chrome_text(flat) or is_gazette_non_act_title(flat):
        return False
    if any(_matches_phrase(flat, phrase, budgets) for phrase, budgets in _PHRASES):
        return False
    if looks_like_clause_fragment(flat):
        return False
    year = _year_in(flat)
    if year is not None:
        return True
    return len(_real_words(flat)) >= 3


def is_rejected_assembly_statute_name(name: str) -> bool:
    """True when this string must not be stored as the instrument name."""
    flat = _collapse(name)
    if not flat:
        return True
    lowered = flat.lower()
    if lowered in {"unknown statute", "name_unknown"}:
        return True
    if looks_like_garbage_name(flat) and not _substantive_words(flat):
        return True
    if is_gazette_non_act_title(flat) or is_listing_chrome_text(flat):
        return True
    if looks_like_clause_fragment(flat):
        return True
    if any(_matches_phrase(flat, phrase, budgets) for phrase, budgets in _PHRASES):
        return True
    if is_well_formed_instrument_title(flat):
        return False
    if len(_real_words(flat)) < 3:
        return True
    if not _INSTRUMENT_RE.search(flat):
        return True
    return False


def strip_listing_chrome_source_url(url: Optional[str]) -> str:
    """Drop listing chrome glued after a PDF path. A real ``|IV of 2026`` label stays."""
    raw = (url or "").strip().replace("%7C", "|").replace("%7c", "|")
    if not raw or "|" not in raw:
        return raw
    fetch, label = raw.split("|", 1)
    if is_listing_chrome_text(label):
        return fetch.strip()
    return raw


def _fold_piece(piece: str) -> str:
    chars = []
    for index, char in enumerate(piece):
        if index == 0 and char in _LEADING_DIGIT and len(piece) > 1:
            chars.append(_LEADING_DIGIT[char])
        else:
            chars.append(char)
    return "".join(chars)


def _closest_title_word(token: str) -> Optional[str]:
    if len(token) < 4:
        return None
    budget = 1 if len(token) <= 4 else 2
    best: list[tuple[int, str]] = []
    for word in _TITLE_WORDS:
        if token[0] != word[0]:
            continue
        if abs(len(token) - len(word)) > budget:
            continue
        distance = _edit_distance(token, word)
        if distance == 0:
            return word
        if distance <= budget:
            best.append((distance, word))
    if not best:
        return None
    best.sort()
    if len(best) > 1 and best[0][0] == best[1][0]:
        return None
    return best[0][1]


def _repair_piece(piece: str) -> str:
    cleaned = piece.strip(".,;:*'\"()[]{}")
    cleaned = cleaned.replace(",", "").replace("[", "").replace("]", "").replace("*", "")
    cleaned = _fold_piece(cleaned)
    if not cleaned:
        return piece
    low = cleaned.lower()
    if low in _SHORT_OCR:
        return _SHORT_OCR[low]
    if low in _FUNCTION_WORDS:
        return low
    if low in _TITLE_WORDS or low in _INSTRUMENT_WORDS:
        return cleaned
    snapped = _closest_title_word(low)
    if not snapped:
        return cleaned
    if cleaned[:1].isupper() or piece[:1] in _LEADING_DIGIT:
        return snapped[:1].upper() + snapped[1:]
    return snapped


def _repair_token(token: str) -> str:
    if "-" in token:
        return "-".join(_repair_piece(part) for part in token.split("-"))
    return _repair_piece(token)


def _prepare_span(span: str) -> str:
    """Join OCR splits that landed inside a word, and leave real punctuation alone."""
    span = re.sub(r"(?<=[A-Za-z]),(?=[A-Za-z])", "", span or "")
    span = re.sub(r"(?<=[A-Za-z])[\[\]](?=[A-Za-z])", "", span)
    return span


def _repair_span(span: str) -> str:
    span = _prepare_span(span)
    parts = []
    last = 0
    for match in _TOKEN_RE.finditer(span or ""):
        parts.append(span[last : match.start()])
        parts.append(_repair_token(match.group(0)))
        last = match.end()
    parts.append((span or "")[last:])
    repaired = _collapse("".join(parts))
    repaired = re.sub(r"(?<![A-Za-z])['’\"]+(?=[A-Za-z])", "", repaired)
    return _strip_ocr_closers(repaired)


def _instrument_word(text: str) -> str:
    head = (text or "")[:5000]
    if re.search(r"(?i)\ban\s+ordinance\b|\bordinance\s+no\b", head):
        return "Ordinance"
    return "Act"


def _act_number_year(text: str) -> Optional[int]:
    match = _ACT_NO_YEAR_RE.search((text or "")[:8000])
    if not match:
        return None
    year = int(match.group(1))
    return year if _valid_year(year) else None


def _gazette_year(text: str) -> Optional[int]:
    match = _GAZETTE_DATE_RE.search((text or "")[:2500])
    if not match:
        return None
    year = int(match.group(1))
    return year if _valid_year(year) else None


def _strip_ocr_closers(title: str) -> str:
    """Drop a stray quote or an unmatched ')' left by an OCR heading."""
    cleaned = _collapse(title).strip(" .,:;")
    cleaned = re.sub(r"['’\"]+$", "", cleaned).rstrip()
    while cleaned.endswith(")") and cleaned.count("(") < cleaned.count(")"):
        cleaned = cleaned[:-1].rstrip()
        cleaned = re.sub(r"['’\"]+$", "", cleaned).rstrip()
    return cleaned.strip(" .,:;")


def _with_instrument_and_year(title: str, text: str, year: Optional[int]) -> tuple[str, Optional[int]]:
    cleaned = _strip_ocr_closers(title)
    cleaned = re.sub(r"(?i)^the\s+", "", cleaned).strip()
    title_year = _year_in(cleaned)
    chosen = title_year or year or _act_number_year(text) or _gazette_year(text)
    has_instrument = _INSTRUMENT_RE.search(cleaned) is not None
    if not has_instrument and _substantive_words(cleaned):
        word = _instrument_word(text)
        if chosen is not None:
            cleaned = f"{cleaned} {word}, {chosen}"
        else:
            cleaned = f"{cleaned} {word}"
    elif chosen is not None and title_year is None and _INSTRUMENT_RE.search(cleaned):
        cleaned = f"{cleaned.rstrip(' .,:;')}, {chosen}"
    return cleaned, chosen if chosen is not None else title_year


def _accept(title: str, text: str, year: Optional[int]) -> Optional[tuple[str, Optional[int]]]:
    cleaned, chosen = _with_instrument_and_year(title, text, year)
    if not cleaned or is_rejected_assembly_statute_name(cleaned):
        return None
    if not _is_plausible_act_title(cleaned):
        return None
    return cleaned, chosen or _year_in(cleaned)


def _short_title(text: str) -> Optional[str]:
    match = _CALLED_RE.search(text or "")
    if not match:
        return None
    return _repair_span(match.group("title"))


def _established_name(text: str) -> Optional[str]:
    match = _AN_ACT_RE.search((text or "")[:8000])
    if not match:
        return None
    purpose = _repair_span(match.group("purpose"))
    established = _ESTABLISH_RE.match(purpose)
    if not established:
        return None
    return _collapse(established.group("name")).strip(" .,:;")


def _listing_title(listing_title: Optional[str], text: str) -> Optional[tuple[str, Optional[int]]]:
    flat = _collapse(listing_title or "")
    if not flat or is_rejected_assembly_statute_name(flat):
        return None
    if not _is_plausible_act_title(flat):
        return None
    year = _year_in(flat) or _act_number_year(text) or _gazette_year(text)
    return flat, year


def resolve_assembly_statute_title(
    text: str,
    *,
    listing_title: Optional[str] = None,
    source_url: Optional[str] = None,
) -> AssemblyTitleDecision:
    """Pick a name and year. ``source_url`` is accepted so callers can pass the row."""
    del source_url  # the URL is cleaned separately; the title comes from the text
    body = text or ""
    act_year = _act_number_year(body)
    gazette_year = _gazette_year(body)
    fallback_year = act_year or gazette_year

    short = _short_title(body)
    if short:
        accepted = _accept(short, body, fallback_year)
        if accepted:
            name, year = accepted
            return AssemblyTitleDecision(name, year, "short_title")

    established = _established_name(body)
    if established:
        accepted = _accept(established, body, fallback_year)
        if accepted:
            name, year = accepted
            return AssemblyTitleDecision(name, year, "act_heading")

    listed = _listing_title(listing_title, body)
    if listed:
        name, year = listed
        return AssemblyTitleDecision(name, year, "listing")

    return AssemblyTitleDecision(
        "name_unknown",
        fallback_year,
        "name_unknown",
        "no short title, act heading, or listing title",
    )


def name_needs_assembly_title(name: Optional[str]) -> bool:
    flat = _collapse(name or "")
    if not flat or flat.lower() in {"unknown statute", "name_unknown"}:
        return True
    return is_rejected_assembly_statute_name(flat)


def _is_object_of_amendment_heading(name: str, text: str) -> bool:
    """True when this name is the Act an amending instrument says it amends.

    ``AN ACT further to amend the Pakistan Air Force Act, 1953`` is not the
    short title of the 2025 amending Act. The check looks at the opening of
    the PDF, so a later cross-reference inside a real Act does not rename it.
    """
    flat_name = re.sub(r"(?i)^the\s+", "", _collapse(name)).lower()
    if len(flat_name) < 8:
        return False
    head = _collapse((text or "")[:1800]).lower()
    for match in re.finditer(r"\b([a-z]{6,12})\s+to\s+amend\b(.{0,160})", head):
        if not _within(match.group(1), "further", 2):
            continue
        if flat_name in _collapse(match.group(2)):
            return True
    return False


def apply_rejected_assembly_title(
    *,
    name: Optional[str],
    year: Optional[int],
    text: str,
    listing_title: Optional[str] = None,
    source_url: Optional[str] = None,
) -> tuple[str, Optional[int], str]:
    """Replace a junk name. A title that is already a real instrument name stays."""
    current = _collapse(name or "")
    amended_principal = _is_object_of_amendment_heading(current, text)
    if not name_needs_assembly_title(current) and not amended_principal:
        return current, year, ""
    decision = resolve_assembly_statute_title(
        text,
        listing_title=listing_title,
        source_url=source_url,
    )
    chosen_year = decision.year if decision.year is not None else year
    detail = decision.detail if decision.source == "name_unknown" else ""
    return decision.name, chosen_year, detail


def statute_name_unknown_reason(name: Optional[str], evidence: Optional[dict]) -> Optional[str]:
    """Promotion quarantine text when the extractor refused to store a junk title."""
    evidence = evidence or {}
    label = _collapse(name or "")
    detail = str(evidence.get("name_unknown") or "").strip()
    if label == "name_unknown" or (detail and label in {"", "Unknown Statute", "name_unknown"}):
        why = detail or "statute title was listing chrome, OCR, or a body clause"
        return f"name_unknown: {why}"[:1000]
    return None
