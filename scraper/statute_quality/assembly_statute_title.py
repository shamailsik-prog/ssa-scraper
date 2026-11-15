"""Title and year for National Assembly and Senate statute PDFs.

The ingest used to keep an OCR fragment of the body (``Itr this Act``,
``The follorving Act``, ``REGTSTTRED No.``) or the listing chrome that was
glued on after the PDF path. A name that is already a real instrument title
stays. Otherwise the title comes from the short-title clause, then from an
``AN ACT to establish …`` heading read with the ``Act No. … of YYYY`` line,
then from a listing title the text supports. OCR repair is a fixed map.
A candidate that is still chrome, a body clause, or too thin is ``name_unknown``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from scraper.parsers.statute_parser import (
    _is_plausible_act_title,
    listed_title_supported_by_text,
    looks_like_clause_fragment,
    looks_like_garbage_name,
)
from scraper.statute_quality.senate_integrity import is_gazette_non_act_title

# 2099 is not a statute year. 1800-2030 keeps the historical acts and the
# current gazette years, and drops 1066, 2602 and 2099.
_YEAR_MIN = 1800
_YEAR_MAX = 2030
_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_YEARISH_RE = re.compile(r"\b\d{4}\b")
_INSTRUMENT_RE = re.compile(
    r"(?i)\b(acts?|ordinances?|orders?|codes?|rules|regulations|constitution)\b"
)
_CALLED_START_RE = re.compile(
    r"(?is)\b(?:this|the)\s+act\s+(?:shall|may)\s+be\b.{0,180}?\bcalled\b"
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
_FILE_NUMBER_RE = re.compile(r"(?i)\bno\.\s*f\.\s*\d[\w()/.\-]{0,40}")
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
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_STOP = frozenset({"the", "and", "of", "for", "a", "an", "to", "or", "by"})
_INSTRUMENT_WORDS = frozenset(
    {
        "act",
        "acts",
        "acr",
        "acf",
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
_INSTRUMENT_KEYS = {
    "act": "Act",
    "acr": "Act",
    "acf": "Act",
    "ordinance": "Ordinance",
    "order": "Order",
    "code": "Code",
}
# OCR tokens only. A word that is already spelled correctly is not in this map,
# so Armed Forces, Services, Institutes and the other clean titles stay as printed.
_OCR_MAP = {
    "notionul": "National",
    "agri-trddc": "Agri-trade",
    "ad": "and",
    "nd": "and",
    "ot": "of",
    "rhe": "the",
    "teh": "the",
    "tne": "the",
    "aut[ority": "Authority",
    "4uthoriq": "Authority",
    "esltblish": "establish",
    "f-orce": "Force",
    "acr": "Act",
    "acf": "Act",
}
_PHRASES = (
    ("this act", (1, 1)),
    ("following act", (2, 1)),
    ("registered no", (2, 1)),
    ("further to amend", (2, 1, 1)),
    ("an act to", (1, 1, 1)),
    ("whereas it", (2, 1)),
)
_CLAUSE_PREFIX_RE = re.compile(
    r"(?i)^(?:"
    r"amendment\s+of|insertion|substitution|repeal|notwithstanding|"
    r"sections?\b|no\.|an\s+act\b|whereas\b|called\b"
    r")"
)
# ``Code, 1860, and the …`` is a cross-reference, not an instrument name.
# ``Pakistan Penal Code, 1860`` and ``Code of Criminal Procedure, 1898`` do not match.
_LEADING_INSTRUMENT_YEAR_RE = re.compile(
    r"(?i)^(?:act|ordinance|order|code|rules|regulations|constitution)\s*,\s*"
    r"(?:1[89]\d{2}|20\d{2})\b"
)
_RUN_ON_SHORT_TITLE_RE = re.compile(
    r"(?i)(?:(?:^|\s)\d{1,3}\.|\bamendment\s+of\b|\bin\s+the\b|\bshall\b)"
)
# A stored name that already ran past the short title. ``(No. 2)`` does not match:
# the digit is not followed by ``. Capital``.
_CLAUSE_RUN_ON_RE = re.compile(
    r"(?i)(?:\bshall\b|(?<![\w(])\d{1,3}\.\s+[A-Z]|\.\s*\(\d{1,2}\))"
)
_YEAR_TOKEN_RE = re.compile(r"(\d{4})(.*)")
# ``2024.It`` and ``2024.(Z)`` glue the next words onto the year.
# ``2024.(2)`` and ``2024.(12)`` are a sub-clause, not this tail.
_YEAR_GLUED_TAIL_RE = re.compile(
    r"\b(?:1[89]\d{2}|20\d{2})(?:\.(?!\(\d{1,2}\))(?=\S)|(?=[A-Za-z]))"
)
# ``2024, It`` / ``2024 It`` / ``2024 - It``. A glued ``2024.It``, a clean
# ``2024.(2)``, and a parenthetical citation after the year do not match.
_YEAR_THEN_WORDS_RE = re.compile(
    r"\b(?:1[89]\d{2}|20\d{2})(?:\s*,\s*\S|\s+-\s*\S|\s+[A-Za-z])"
)
# ``Finance Act, 2024, Act No. V of, 2024`` is a called-line run-on.
# A citation ``(Act No. V of 2024)`` has no comma before Act and does not match.
_ACT_NO_RUN_ON_RE = re.compile(r"(?i),\s*act\s+no\b")
# One trailing citation, so the title year is what remains.
# ``(Act XLV of 1860)``, ``(Ordinance No. XLIX of 2001)``, ``[XVII of 2024]``,
# ``(No.XVII of 2024)`` and a trailing full stop are the same bracket.
_TRAILING_CITATION_RE = re.compile(
    r"(?i)\s*[\[(]\s*"
    r"(?:(?:act|ordinance|president's\s+order|regulation|ord\.|p\.o\.)\s+)?"
    r"(?:no\.?\s*)?"
    r"(?:[ivxlcdm]+|\d+)\s+of\s+(?:1[89]\d{2}|20\d{2})"
    r"\s*[\])]\.?\s*$"
)
# ``2O24`` / ``2o24`` is a year the OCR printed with a letter.
_OCR_YEAR_RE = re.compile(r"\b[12lI][0-9OoIl]{3}\b")
_PAREN_CONTINUES_RE = re.compile(
    r"(?i)^\((?:"
    r"amendment\)|second\s+amendment\)|no\.\s*\d|"
    r"(?:act\s+no\.\s*)?(?:[ivxlcdm]+|\d+)\s+of\s+(?:1[89]\d{2}|20\d{2})"
    r")"
)
# A year, then a connector, is the next clause rather than a longer title.
# ``of the`` is separate: a combining amendment may say ``and the``.
_CLAUSE_CONNECTOR_RE = re.compile(
    r"(?i)\b(?:1[89]\d{2}|20\d{2})\b\s*[, \-–—]*\s*(?:"
    r"as\s+amended\s+by|and\s+amends|amends|made\s+under|in\s+place\s+of|"
    r"read\s+with|repealing|replacing|under"
    r")\b"
)
_OF_THE_AFTER_YEAR_RE = re.compile(
    r"(?i)\b(?:1[89]\d{2}|20\d{2})\b\s*[, \-–—]*\s*of\s+the\b"
)
# A combined title closes on an amending phrase immediately after an earlier
# instrument year and before the closing instrument. Ordinals go past the
# tenth (``Eleventh``, ``Twenty-sixth``, ``2nd``). A letter, a roman numeral,
# a status word, a place, or another Act's name is not a close.
# ``(Special Provisions)``, ``(Continuance)``, ``(Revival)`` and ``(Abolition)``
# are not combining closes. A good listing that already uses one is kept.
_COMBINING_SUFFIX_RE = re.compile(
    r"(?i)(?P<phrase>"
    r"\([^()]{1,80}\)(?:\s*\([^()]{1,80}\))*"
    r"|"
    r"[a-z]+(?:\s+and\s+[a-z]+)?"
    r")\s+(?:act|ordinance|order|code|regulation)"
    r"(?:\s+no\.?\s*\d+[a-z]?)?(?:\s+of)?"
    r"\s*,?\s*(?:1[89]\d{2}|20\d{2})"
)
# One amending atom. A list is several of these joined by comma or "and".
# Both sides of "and" have to be atoms. ``(Amendment and Z)`` is not.
_ORDINAL_WORD = (
    r"(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"-(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)"
    r"|twentieth|thirtieth|fortieth|fiftieth|sixtieth|seventieth|eightieth|ninetieth"
    r"|eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth"
    r"|eighteenth|nineteenth"
    r"|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"
    r"|\d+(?:st|nd|rd|th))"
)
_AMENDING_ATOM_RE = re.compile(
    rf"(?i)^(?:{_ORDINAL_WORD}\s+)?(?:"
    r"amendments?|amending"
    r"|validat\w*(?:\s+of\s+laws)?"
    r"|repeal"
    r"|savings?"
    r"|extensions?(?:\s+of\s+validity)?"
    r"|removal\s+of\s+difficult(?:y|ies)"
    r")$"
)
# ``(2 Amendment)`` is a citation that still looks like a close. The citation
# gate has to reject it. ``(2nd Amendment)`` is an ordinal and is not a citation.
_CITATION_SHAPED_AMENDMENT_RE = re.compile(
    r"(?i)^\d+\s+(?:amendments?|amending|repeal|validat\w*|extensions?)$"
)
# Same four shapes as ``_CITATION_PAREN_RE``. A wrapper such as
# ``(No. 2 Amendment)`` is not a close, and each shape has to stay here
# or that wrapper becomes one.
_CITATION_PREFIX_RES = (
    re.compile(r"(?i)^no\.?\s*\d+[a-z]?\b"),
    re.compile(
        r"(?i)^(?:act|ordinance|ord\.?|p\.o\.?|president(?:'s)?(?:\s+order)?|regulation)\b"
    ),
    re.compile(r"(?i)^(?:[ivxlcdm]+|\d+)\s+of\b"),
    re.compile(r"(?i)^\d{1,3}(?!(?:st|nd|rd|th)\b)[a-z]?\b"),
)
_CITATION_PAREN_RE = re.compile(
    r"(?i)^\(\s*(?:"
    r"no\.?\s*\d+[a-z]?"
    r"|(?:act|ordinance|ord\.?|p\.o\.?|president(?:'s)?(?:\s+order)?|regulation)\b"
    r"|(?:[ivxlcdm]+|\d+)\s+of\b"
    r"|\d{1,3}(?!(?:st|nd|rd|th)\b)[a-z]?"
    r")[^)]*\)$"
)
_YEAR_BEFORE_SUFFIX_RE = re.compile(
    r"(?i)(?:1[89]\d{2}|20\d{2})(?:\s*\(\s*no\.?\s*\d+[a-z]?\s*\))?\s*$"
)
# A cut-off citation at the end of a stored name, with no closing bracket.
_UNCLOSED_CITATION_RE = re.compile(
    r"(?i)\s*\((?:act|ord|cabinet|passed)\b[^)]*$"
)
_INNER_YEAR_STOP_WORDS = frozenset(
    {"it", "lt", "shall", "extends", "comes", "this", "whereas"}
)
# Words that can sit between an earlier year and the next instrument.
# ``of the`` and ``and the`` are connectors, not part of the name.
_TITLE_BRIDGE_WORDS = frozenset({"of", "and", "the", "for"})
_ROMAN_OF_RE = re.compile(r"(?i)^[IVXLC]{1,12}\s+o[fnr]\b")


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
    return _YEAR_MIN <= year <= _YEAR_MAX


def _year_in(text: str) -> Optional[int]:
    match = _YEAR_RE.search(text or "")
    if not match:
        return None
    year = int(match.group(1))
    return year if _valid_year(year) else None


def _impossible_year(name: str) -> bool:
    return any(not _valid_year(int(token)) for token in _YEARISH_RE.findall(name or ""))


def _substantive_words(name: str) -> list[str]:
    return [word for word in _real_words(name) if word.lower() not in _INSTRUMENT_WORDS]


def _mentions_legis(name: str) -> bool:
    for word in re.findall(r"[a-z]+", (name or "").lower()):
        if _within(word, "legis", 1):
            return True
    return False


def _has_following_or_further(name: str) -> bool:
    """OCR of following/further, including a split ``lb!lowing`` and glued ``Actfurther``."""
    low = (name or "").lower()
    compact = re.sub(r"[^a-z]", "", low)
    if re.search(r"lb\W*lowing", low) or "lblowing" in compact:
        return True
    if "actfurther" in compact or "actfurlher" in compact:
        return True
    if re.search(r"(?i)\bto\s+amend\b", low) or "toamend" in compact:
        return True
    if re.search(r"foll\w{0,3}w\w{0,3}n\w{0,3}g|follorv|followiag|furlh", low):
        return True
    return False


def _starts_with_amendment_of(name: str) -> bool:
    match = re.match(r"(?i)^([a-z]+)\s+of\b", name)
    if not match:
        return False
    word = match.group(1).lower()
    return _within(word, "amendment", 1) or _within(word, "amendments", 1)


def _has_called_word(name: str) -> bool:
    for word in re.findall(r"[a-z]+", name.lower()):
        if _within(word, "called", 1):
            return True
    return False


def _has_section_number(name: str) -> bool:
    for match in re.finditer(r"(?i)\b([a-z]{5,10})\s*(\d+)", name):
        word = match.group(1).lower()
        if _within(word, "section", 1) or _within(word, "sections", 1):
            return True
    return False


def _leading_word_is_lower(name: str) -> bool:
    """A lowercase clause start. A leading ``e-`` or a digit (``5G``) is a title.

    ``the following Act`` and ``the Federal Government may … Act`` both start
    with a lowercase word. A listing strips that ``the`` before this check,
    so ``the Constitution (Twenty-sixth Amendment) Act`` can still be kept.
    """
    text = name.lstrip()
    if re.match(r"^e-", text, re.IGNORECASE):
        text = text[2:].lstrip()
    for char in text:
        if char.isalpha():
            return char.islower()
        if char.isdigit():
            return False
    return False


def _connector_has_clause_marker(name: str) -> bool:
    """``and the`` / ``or the`` / ``under the`` reject a name only beside a real clause."""
    if re.search(r"(?i)\bshall\b", name):
        return True
    if _has_section_number(name):
        return True
    if re.search(r"(?i)\bin\s+the\b.{0,80}\b(?:act|ordinance|code)\b", name):
        return True
    if ").-" in name or ")-" in name:
        return True
    return False


def _is_clause_like_name(name: str) -> bool:
    """Body clauses, amending long titles, and gazette notices are not instrument names."""
    flat = _collapse(name)
    if not flat:
        return True
    if _leading_word_is_lower(flat):
        return True
    if _LEADING_INSTRUMENT_YEAR_RE.match(flat):
        return True
    if _CLAUSE_RUN_ON_RE.search(flat):
        return True
    if _YEAR_GLUED_TAIL_RE.search(flat):
        return True
    if _has_ocr_year(flat):
        return True
    if _connector_between_years(flat):
        return True
    if _junk_close_at_end(flat):
        return True
    if _ACT_NO_RUN_ON_RE.search(flat):
        return True
    if _text_after_last_year(flat):
        return True
    if _CLAUSE_PREFIX_RE.match(flat) or _ROMAN_OF_RE.match(flat) or _starts_with_amendment_of(flat):
        return True
    if re.match(r"(?i)^short\s+title\b", flat):
        return True
    if _has_section_number(flat):
        return True
    if ")-" in flat or ").-" in flat or ")," in flat:
        return True
    if " / " in flat:
        return True
    if re.search(r"(?i)\bin\s+the\b.{0,80}\b(?:act|ordinance|code)\b", flat):
        return True
    if re.search(r"(?i)\bofthe\b", flat):
        return True
    words = re.findall(r"[a-z]+", flat.lower())
    if any(_within(word, "mentioned", 1) or _within(word, "ofthe", 1) for word in words):
        return True
    low = flat.lower()
    if "the meaning of" in low or "provisions of" in low:
        return True
    if re.search(r"(?i)\b(?:shall\s+be|have\s+been|mentioned\s+in|may\s+constitute)\b", flat):
        return True
    if re.search(r"(?i)\b(?:and|or|under)\s+the\b", flat) and _connector_has_clause_marker(flat):
        return True
    if re.search(r"(?i)no\.\s*f\.", flat) or _mentions_legis(flat):
        return True
    if re.search(r"(?i)\bassent\b", flat) or re.search(r"(?i)majlis(?:\s*-?\s*e\s*-?\s*shoor)", flat):
        return True
    if _has_called_word(flat) or _has_following_or_further(flat):
        return True
    return False


def is_well_formed_instrument_title(name: str) -> bool:
    """A short title such as ``Finance Act, 2026`` stays even with two words."""
    flat = _collapse(name)
    if not flat or not _INSTRUMENT_RE.search(flat):
        return False
    if _impossible_year(flat) or _is_clause_like_name(flat):
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
    if looks_like_garbage_name(flat):
        return True
    if is_gazette_non_act_title(flat) or is_listing_chrome_text(flat):
        return True
    if looks_like_clause_fragment(flat) or _is_clause_like_name(flat):
        return True
    if any(_matches_phrase(flat, phrase, budgets) for phrase, budgets in _PHRASES):
        return True
    if _impossible_year(flat):
        return True
    if is_well_formed_instrument_title(flat):
        return False
    if len(_real_words(flat)) < 3:
        return True
    if not _INSTRUMENT_RE.search(flat):
        return True
    return False


def strip_listing_chrome_source_url(url: Optional[str]) -> str:
    """Drop listing chrome glued after a PDF path. A real ``|IV of 2026`` label stays.

    ``%7C`` is decoded only in the path. A query string is left as written, so
    ``?a=1%7cb`` is not turned into a pipe.
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    query = ""
    path = raw
    split_at = raw.find("?")
    if split_at != -1:
        path, query = raw[:split_at], raw[split_at:]
    path = path.replace("%7C", "|").replace("%7c", "|")
    if "|" not in path:
        return path + query
    fetch, label = path.split("|", 1)
    label_text = re.sub(r"(?i)%20", " ", label)
    if is_listing_chrome_text(label) or is_listing_chrome_text(label_text):
        return fetch.strip() + query
    return path + query


def _lookup_key(token: str) -> str:
    text = token.strip().strip(".,;:")
    text = text.strip("'\"`*")
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    text = re.sub(r"[)'\"]+$", "", text)
    return text.lower()


def _apply_ocr_token(token: str) -> str:
    """Apply the fixed OCR map. A token that is not in the map is returned as printed."""
    key = _lookup_key(token)
    if key in _OCR_MAP:
        return _OCR_MAP[key]
    if key in _INSTRUMENT_KEYS:
        return _INSTRUMENT_KEYS[key]
    # Keep a printed comma ("Salary,"). An OCR comma inside a word is removed earlier.
    return token


def _is_caps_acronym(token: str) -> bool:
    """``PTCL`` and ``NTDC`` have no vowels. Mixed-case OCR junk does not."""
    letters = re.sub(r"[^A-Za-z]", "", token or "")
    return bool(letters) and letters.isupper() and not re.search(r"[AEIOUY]", letters)


def _symbol_mixed(token: str) -> bool:
    """True for ``'*qZOZA`` and other tokens that are not a word."""
    if _lookup_key(token) in _OCR_MAP or _is_caps_acronym(token):
        return False
    if re.search(r"[^A-Za-z0-9'’\-(),.\s]", token):
        return True
    letters = re.sub(r"[^A-Za-z]", "", token)
    if len(letters) >= 4 and not re.search(r"[AEIOUYaeiouy]", letters):
        return True
    return False


def _is_price_chrome(token: str) -> bool:
    """Price and gazette running heads. A digit-led word such as ``5G`` is not one."""
    bare = token.strip(".,;:()[]").lower()
    if bare in {"price", "rs", "gaz", "gazette", "ex"}:
        return True
    return re.search(r"(?i)/ex\b|\bgaz\b", token) is not None


def _join_inside_words(text: str) -> str:
    return re.sub(r"(?<=[A-Za-z]),(?=[A-Za-z])", "", text or "")


def _repair_span(span: str) -> str:
    span = _join_inside_words(span)
    parts: list[str] = []
    last = 0
    for match in re.finditer(r"\S+", span or ""):
        parts.append(span[last : match.start()])
        token = match.group(0)
        if _symbol_mixed(token):
            last = match.end()
            continue
        parts.append(_apply_ocr_token(token))
        last = match.end()
    parts.append((span or "")[last:])
    repaired = _collapse("".join(parts))
    repaired = re.sub(r"(?<![A-Za-z])['’\"]+(?=[A-Za-z])", "", repaired)
    return _strip_ocr_closers(repaired)


def _strip_ocr_closers(title: str) -> str:
    """Drop a stray quote or an unmatched ')' left by an OCR heading."""
    cleaned = _collapse(title).strip(" .,:;")
    cleaned = re.sub(r"['’\"]+$", "", cleaned).rstrip()
    while cleaned.endswith(")") and cleaned.count("(") < cleaned.count(")"):
        cleaned = cleaned[:-1].rstrip()
        cleaned = re.sub(r"['’\"]+$", "", cleaned).rstrip()
    return cleaned.strip(" .,:;")


def _without_file_numbers(text: str) -> str:
    return _FILE_NUMBER_RE.sub(" ", text or "")


def _instrument_word(text: str) -> str:
    head = (text or "")[:5000]
    if re.search(r"(?i)\ban\s+ordinance\b|\bordinance\s+no\b", head):
        return "Ordinance"
    return "Act"


def _act_number_year(text: str) -> Optional[int]:
    match = _ACT_NO_YEAR_RE.search(_without_file_numbers(text)[:8000])
    if not match:
        return None
    year = int(match.group(1))
    return year if _valid_year(year) else None


def _gazette_year(text: str) -> Optional[int]:
    match = _GAZETTE_DATE_RE.search(_without_file_numbers(text)[:2500])
    if not match:
        return None
    year = int(match.group(1))
    return year if _valid_year(year) else None


def _with_instrument_and_year(title: str, text: str, year: Optional[int]) -> tuple[str, Optional[int]]:
    cleaned = _strip_ocr_closers(title)
    # Drop the gazette formula "the", and keep a capitalised "The" that is part of the name.
    cleaned = re.sub(r"^the\s+", "", cleaned).strip()
    already_dated = _YEARISH_RE.search(cleaned) is not None or _has_ocr_year(cleaned)
    title_year = _year_in(cleaned)
    act_year = _act_number_year(text)
    if title_year is not None and act_year is not None and title_year != act_year:
        if _last_year_in(cleaned) != act_year:
            return "", None
        title_year = act_year
    if already_dated:
        chosen = title_year
    else:
        chosen = title_year or year or act_year or _gazette_year(text)
    has_instrument = _INSTRUMENT_RE.search(cleaned) is not None
    if not has_instrument and _substantive_words(cleaned):
        word = _instrument_word(text)
        if chosen is not None:
            cleaned = f"{cleaned} {word}, {chosen}"
        else:
            cleaned = f"{cleaned} {word}"
    elif chosen is not None and title_year is None and not already_dated and _INSTRUMENT_RE.search(cleaned):
        cleaned = f"{cleaned.rstrip(' .,:;')}, {chosen}"
    return cleaned, chosen if chosen is not None else title_year


def _last_year_in(text: str) -> Optional[int]:
    years = re.findall(r"\b(?:1[89]\d{2}|20\d{2})\b", text or "")
    if not years:
        return None
    year = int(years[-1])
    return year if _valid_year(year) else None


def _years_disagree(title: str, text: str) -> bool:
    printed = _year_in(title or "")
    act_year = _act_number_year(text)
    if not printed or not act_year or printed == act_year:
        return False
    # An amending title may name an older Act and still close on the Act-number year.
    return _last_year_in(title or "") != act_year


def _accept(title: str, text: str, year: Optional[int]) -> Optional[tuple[str, Optional[int]]]:
    if _years_disagree(title, text):
        return None
    cleaned, chosen = _with_instrument_and_year(title, text, year)
    if not cleaned or is_rejected_assembly_statute_name(cleaned):
        return None
    if not _is_plausible_act_title(cleaned):
        return None
    return cleaned, chosen or _year_in(cleaned)


def _subsection_end(token: str) -> bool:
    return re.fullmatch(r"[\(\[]?\d{1,3}[\)\]]?", token or "") is not None


def _continues_numbered_act(kept: list[tuple[str, str]], token: str) -> bool:
    """``(No. 2)`` and ``Act No. 2`` belong to the title. ``(2)`` is the next subsection."""
    prev = kept[-1][1].rstrip()
    if not re.search(r"(?i)(?:\bno\.?|\(no\.)$", prev):
        return False
    return re.fullmatch(r"\d+[a-z]?\)?", token or "") is not None


def _numbered_act_year_index(tokens: list[tuple[str, str]], index: int) -> Optional[int]:
    """The year token of ``Act No. N, YEAR`` / ``Act No. N of YEAR``, if this word is that Act."""
    nxt = index + 1
    if nxt >= len(tokens) or not re.match(r"(?i)^no\.?$", tokens[nxt][1].strip(".,")):
        return None
    nxt += 1
    if nxt < len(tokens) and re.fullmatch(r"\d+[a-z]?,?", tokens[nxt][1]):
        nxt += 1
    else:
        return None
    if nxt < len(tokens) and tokens[nxt][1].strip() == ",":
        nxt += 1
    if nxt < len(tokens) and re.match(r"(?i)^of$", tokens[nxt][1].strip(".,")):
        nxt += 1
    if nxt < len(tokens) and _year_token(tokens[nxt][1]):
        return nxt
    return None


def _year_token(token: str) -> Optional[re.Match]:
    """A year token, including ``2024.``, ``2024.(2)``, and ``2024.It`` / ``2024.(Z)``."""
    text = (token or "").strip().strip(",")
    match = _YEAR_TOKEN_RE.fullmatch(text)
    if not match or not _valid_year(int(match.group(1))):
        return None
    return match


def _year_token_closes(token: str) -> bool:
    """Anything glued after the year ends the title. A bare ``2024`` does not."""
    match = _year_token(token)
    return bool(match and match.group(2))


def _combining_match_is_citation(match: re.Match) -> bool:
    """A bracket such as ``(No. 2)`` or ``(Act V of 2024)`` is a citation, not a close.

    ``(2nd Amendment)`` is an ordinal close. The bare-number shape stops before
    ``st`` / ``nd`` / ``rd`` / ``th``.
    """
    paren = re.match(r"\([^()]*\)", match.group(0))
    if not paren:
        return False
    if re.match(r"(?i)^\(\s*\d+(?:st|nd|rd|th)\b", paren.group(0)):
        return False
    return _CITATION_PAREN_RE.match(paren.group(0)) is not None


def _close_phrase(match: re.Match) -> str:
    phrase = _collapse(match.group("phrase"))
    if phrase.startswith("(") and phrase.endswith(")") and phrase.count("(") == 1:
        phrase = phrase[1:-1].strip()
    return phrase


def _split_close_parts(phrase: str) -> list[str]:
    flat = _collapse(phrase).lower().strip(" .")
    if not flat:
        return []
    return [part.strip() for part in re.split(r"\s*,\s*|\s+and\s+", flat) if part.strip()]


def _part_is_amending(part: str) -> bool:
    return _AMENDING_ATOM_RE.match(part) is not None


def _parts_are_amending(phrase: str) -> bool:
    parts = _split_close_parts(phrase)
    return bool(parts) and all(_part_is_amending(part) for part in parts)


def _close_phrase_is_amending(phrase: str) -> bool:
    """``(Third Amendment)`` and ``Repeal`` close a combined title. ``(Z)`` does not.

    ``(2 Amendment)`` is amending-shaped so the citation gate still has a case
    to reject. An ordinal (``2nd``, ``Twenty-sixth``) is a real close.
    """
    raw = _collapse(phrase).strip()
    if re.fullmatch(r"(?:\([^()]{1,80}\)\s*)+", raw):
        # ``(Amendment) (No. 2)`` is one close. A second amending parenthesis
        # or a second number is stacked junk.
        amending = 0
        numbers = 0
        for inner in re.findall(r"\(([^()]*)\)", raw):
            text = _collapse(inner)
            if re.fullmatch(r"(?i)no\.?\s*\d+[a-z]?", text):
                numbers += 1
                continue
            if not _parts_are_amending(text):
                return False
            amending += 1
        return amending == 1 and numbers <= 1
    if _parts_are_amending(raw):
        return True
    return _CITATION_SHAPED_AMENDMENT_RE.match(raw) is not None


def _phrase_contains_amending_word(phrase: str) -> bool:
    return (
        re.search(
            r"\b(?:amend(?:ment|ments|ing)?|validat\w*|\brepeal\b|extensions?|"
            r"removal\s+of\s+difficult(?:y|ies))\b",
            _collapse(phrase).lower(),
        )
        is not None
    )


def _citation_prefix(phrase: str) -> bool:
    flat = _collapse(phrase)
    return any(pattern.match(flat) for pattern in _CITATION_PREFIX_RES)


def _combining_match_counts(match: re.Match) -> bool:
    """An amending close counts. A citation wrapper around an amending word does not.

    Both returns keep the citation test. ``(2nd Amendment)`` is not a citation.
    ``(2 Amendment)`` and ``(No. 2 Amendment)`` are, so they do not count.
    A phrase that merely contains an amending word counts only when it also
    has a citation prefix, and then the citation test still rejects it.
    """
    phrase = _close_phrase(match)
    if _close_phrase_is_amending(phrase):
        return not _combining_match_is_citation(match)
    if _phrase_contains_amending_word(phrase) and _citation_prefix(phrase):
        return not _combining_match_is_citation(match)
    return False


def _amending_spans(flat: str) -> list[re.Match]:
    """Amending closes that sit after an earlier instrument year."""
    found: list[re.Match] = []
    for match in _COMBINING_SUFFIX_RE.finditer(flat):
        if not _combining_match_counts(match):
            continue
        if _YEAR_BEFORE_SUFFIX_RE.search(flat[: match.start()]) is None:
            continue
        found.append(match)
    return found


def _phrase_is_junk_close(phrase: str) -> bool:
    """A close after an earlier year is junk unless every word of it amends.

    Shape, not a word list: ``(Punjab)``, ``(2nd)``, ``(Third Schedule)``,
    ``(Draft)`` and ``(Amendment and Z)`` are junk. ``(Repeal and Saving)``
    is not. A citation prefix (``No. 2 Amendment``, ``2 Amendment``) is junk
    even when the words look like a close. A good listing that is the called
    title is kept before this check runs.
    """
    raw = _collapse(phrase)
    if not raw:
        return False
    if _citation_prefix(raw):
        return True
    if _close_phrase_is_amending(raw):
        return False
    return True


def _numbered_act_paren(match: re.Match) -> bool:
    """``(No. 2) Act, 2024`` is a numbered Act. ``(No. 2 Amendment)`` is not."""
    paren = re.match(r"\([^()]*\)", match.group(0))
    if not paren:
        return False
    return re.fullmatch(r"(?i)\(\s*no\.?\s*\d+[a-z]?\s*\)", paren.group(0)) is not None


def _junk_close_at_end(name: str) -> bool:
    """A non-amending tail after an earlier year, or two amending instruments.

    ``Finance Act, 2024`` is a principal. ``2005 (Z) Act, 2025``,
    ``(Punjab) Act, 2025`` and ``(No. 2) Act, 2025`` are not.
    ``(Amendment) Act, 2024 Repeal Act, 2025`` is two instruments.
    A good listing that is the called title is kept before this runs.
    """
    flat = _collapse(name or "").strip()
    if len(_amending_spans(flat)) >= 2:
        return True
    end = None
    for match in _COMBINING_SUFFIX_RE.finditer(flat):
        if flat[match.end() :].strip(" .") == "":
            end = match
    if end is None:
        return False
    if _YEAR_BEFORE_SUFFIX_RE.search(flat[: end.start()]) is None:
        return False
    if _combining_match_counts(end):
        return False
    # ``(No. N) Act, YYYY`` after a title that already closed is a second
    # instrument. ``Finance (No. 2) Act`` has no year before the parenthesis,
    # so it never reaches this line.
    if _numbered_act_paren(end):
        return True
    return _phrase_is_junk_close(_close_phrase(end))


def _combining_suffix_starts(text: str) -> bool:
    """True when this text, already past an instrument year, opens on the closing instrument."""
    match = _COMBINING_SUFFIX_RE.match(_collapse(text))
    return match is not None and _combining_match_counts(match)


def _combining_suffix_at_end(name: str) -> bool:
    """True when the name closes on an amending instrument after an earlier year.

    ``Sales Tax Act, 1990`` is a principal. ``1990 (Validation) Act, 2024`` and
    ``2001 Repeal Act, 2024`` are one amending instrument. ``(Z) Act, 2025`` is not.
    """
    flat = _collapse(name or "").strip()
    # One close, and it has to be the end of the name. A second instrument
    # (``Repeal Act, 2025`` after a title that already closed) does not count,
    # and a close with words after it is not at the end.
    spans = _amending_spans(flat)
    if len(spans) != 1:
        return False
    return flat[spans[0].end() :].strip(" .") == ""


def _text_has_combining_suffix(text: str) -> bool:
    """True when an amending close sits in this remainder, on a year or at its start."""
    flat = _collapse(text)
    for match in _COMBINING_SUFFIX_RE.finditer(flat):
        if not _combining_match_counts(match):
            continue
        before = flat[: match.start()]
        if before.strip() == "" or _YEAR_BEFORE_SUFFIX_RE.search(before):
            return True
    return False


def _called_clause(text: str) -> str:
    match = _CALLED_START_RE.search(text or "")
    if not match:
        return ""
    return _collapse((text or "")[match.end() : match.end() + 700])


def _listing_is_another_principal(listing: str, resolved: str, text: str) -> bool:
    """The listing names a principal in the called line that this resolution does not replace."""
    listed = _collapse(listing)
    if not listed:
        return False
    # A leading ``the`` is clause-like on its own. The listing is still a
    # principal when the words after ``the`` are a full title.
    if is_rejected_assembly_statute_name(listed) and not _listing_body_is_well_formed(listing):
        return False
    if not resolved or resolved == "name_unknown" or is_rejected_assembly_statute_name(resolved):
        return False
    if _title_core(listed) == _title_core(resolved):
        return False
    if _short_title_supersedes(listed, resolved):
        return False
    core = _title_core(listed)
    if len(core) < 8:
        return False
    return re.search(r"(?<![a-z])" + re.escape(core) + r"(?![a-z])", _called_clause(text).lower()) is not None


def _second_principal_joined(name: str) -> bool:
    """The first two instrument years are joined by and, &, and the, or , and the.

    A comma, a space, or a newline between two Acts is not that list.
    """
    flat = _collapse(name or "")
    years = list(re.finditer(r"\b(?:1[89]\d{2}|20\d{2})\b", flat))
    if len(years) < 2:
        return False
    between = flat[years[0].end() : years[1].start()]
    if "&" in between:
        return True
    return re.search(r"(?i)(?<![a-z])and(?![a-z])", between) is not None


def _connector_between_years(name: str) -> bool:
    """True when a later year is only a connector (``as amended by``, ``and the X Act``).

    ``X, YYYY and the Y, YYYY (Amendment) Act, YYYY`` is one title. ``of the``
    is a connector unless that combining suffix follows.
    """
    text = name or ""
    if _CLAUSE_CONNECTOR_RE.search(text):
        return True
    if _combining_suffix_at_end(text):
        return False
    if _OF_THE_AFTER_YEAR_RE.search(text):
        return True
    years = list(re.finditer(r"\b(?:1[89]\d{2}|20\d{2})\b", text))
    if len(years) < 2:
        return False
    between = text[years[0].end() : years[1].start()]
    words = re.findall(r"[A-Za-z]+", between)
    lowered = [word.lower() for word in words]
    joined = " ".join(lowered)
    if "of the" in joined or "and the" in joined:
        return True
    if "and" not in lowered or "(" in between or "[" in between:
        return False
    # ``and Code of Criminal Procedure`` continues. ``and Cantonments Order``
    # names a different instrument, so the first capital word is not Code/Act.
    capitals = [
        word
        for word in words
        if word[:1].isupper() and word.lower() not in _TITLE_BRIDGE_WORDS
    ]
    return bool(capitals) and capitals[0].lower() not in _INSTRUMENT_KEYS


def _has_ocr_year(name: str) -> bool:
    """True when a year token contains an OCR letter, as in ``2O24``."""
    for match in _OCR_YEAR_RE.finditer(name or ""):
        if re.search(r"[OoIl]", match.group(0)):
            return True
    return False


def _last_year_match(name: str) -> tuple[str, Optional[re.Match]]:
    """The last year, after peeling one trailing citation or a cut-off fragment."""
    peeled = _UNCLOSED_CITATION_RE.sub("", _collapse(name))
    peeled = _TRAILING_CITATION_RE.sub("", peeled)
    years = list(re.finditer(r"\b(?:1[89]\d{2}|20\d{2})\b", peeled))
    if not years:
        return peeled, None
    return peeled, years[-1]


def _text_after_last_year(name: str) -> bool:
    """True when more than punctuation follows the final year.

    An earlier year inside a real title does not count. ``1860 and Code … Act,
    2024`` ends at 2024. ``2024 (Z) It`` and ``2024, It`` do not.
    """
    peeled, year = _last_year_match(name)
    if year is None:
        return False
    return re.search(r"[^.,\s]", peeled[year.end() :]) is not None


def _bad_bracket_after_last_year(name: str) -> bool:
    """A space and a bracket after the final year that is not part of the title."""
    peeled, year = _last_year_match(name)
    if year is None:
        return False
    tail = peeled[year.end() :]
    if not re.match(r"\s+\(", tail):
        return False
    return _PAREN_CONTINUES_RE.match(tail.lstrip()) is None


def _paren_after_year_continues(tokens: list[tuple[str, str]], start: int) -> bool:
    """``(Amendment)``, ``(No. 2)``, and a citation continue. ``(Z)`` does not."""
    window = " ".join(token for _gap, token in tokens[start : start + 8])
    if _PAREN_CONTINUES_RE.match(window):
        return True
    # ``(Third Amendment) Act, 2024`` and ``(Removal of Difficulties) Act, 2024``.
    return _combining_suffix_starts(window)


def _text_from(tokens: list[tuple[str, str]], start: int) -> str:
    return _collapse(" ".join(token for _gap, token in tokens[start:]))


def _joined_statute_continues(tokens: list[tuple[str, str]], start: int) -> bool:
    """A newline still joins ``and the Y, YYYY (Amendment) Act, YYYY``.

    One year after the break, as in ``Code`` then ``(Amendment) Act``, does not.
    """
    text = _text_from(tokens, start)
    if not text or _CLAUSE_CONNECTOR_RE.search(text):
        return False
    if not _text_has_combining_suffix(text):
        return False
    return len(re.findall(r"\b(?:1[89]\d{2}|20\d{2})\b", text)) >= 2


def _suffix_is_combining(tokens: list[tuple[str, str]], instrument_index: int) -> bool:
    """True when this instrument's year is followed by an amending or validating instrument."""
    nxt = instrument_index + 1
    if nxt < len(tokens) and tokens[nxt][1].strip() == ",":
        nxt += 1
    if nxt >= len(tokens) or not _year_token(tokens[nxt][1]):
        return False
    text = _text_from(tokens, nxt + 1).strip()
    if not text or _CLAUSE_CONNECTOR_RE.search(text):
        return False
    return _combining_suffix_starts(text)


def _gap_is_title_shaped(tokens: list[tuple[str, str]], start: int, instrument_index: int) -> bool:
    """True when every word between an earlier year and the next instrument is part of the name."""
    words: list[str] = []
    saw_paren = False
    saw_capital = False
    for index in range(start, instrument_index):
        _gap, token = tokens[index]
        if re.fullmatch(r"[-–—]+", token):
            return False
        if token[:1] in "([":
            if not (_paren_continues_title(token) or _paren_after_year_continues(tokens, index)):
                return False
            saw_paren = True
            continue
        word = re.sub(r"[^A-Za-z]", "", token).lower()
        if not word:
            continue
        if word in _INNER_YEAR_STOP_WORDS:
            return False
        if token.rstrip(".,;:").endswith(".") and word != "no":
            return False
        if word in _TITLE_BRIDGE_WORDS:
            words.append(word)
            continue
        if token.strip(".,;:()[]")[:1].isupper():
            saw_capital = True
            words.append(word)
            continue
        return False
    joined = " ".join(words)
    # The joined Act may sit after another instrument word (``and the Code of …, 1898``).
    combining = _suffix_is_combining(tokens, instrument_index) or _joined_statute_continues(tokens, start)
    if ("of the" in joined or "and the" in joined) and not combining:
        return False
    # ``and Code`` continues. ``and Cantonments Order`` names a different instrument.
    # ``and Sales Tax Act, YYYY (Amendment) Act`` is one amending title.
    if "and" in words and saw_capital and not saw_paren and not combining:
        return False
    return True


def _later_instrument_year(tokens: list[tuple[str, str]], start: int) -> bool:
    """A later instrument and its year means the year already seen is inner.

    The words between the two years have to be part of the name. A connector
    (``as amended by``, ``read with``, ``of the``), a dash, or a new line
    ends the title at the earlier year.
    """
    for index in range(start, len(tokens)):
        gap, token = tokens[index]
        if index > start and "\n" in gap and not _joined_statute_continues(tokens, start):
            return False
        if re.fullmatch(r"[-–—]+", token):
            return False
        word = re.sub(r"[^A-Za-z]", "", token).lower()
        if word in _INNER_YEAR_STOP_WORDS:
            return False
        if token.endswith(".") and not _year_token(token) and word != "no":
            return False
        if not _INSTRUMENT_KEYS.get(_lookup_key(token)):
            continue
        if not _gap_is_title_shaped(tokens, start, index):
            return False
        nxt = index + 1
        if nxt < len(tokens) and re.match(r"(?i)no\.?$", tokens[nxt][1].strip(".,")):
            continue
        if nxt < len(tokens) and tokens[nxt][1].strip() == ",":
            nxt += 1
        if (
            nxt < len(tokens)
            and "\n" not in tokens[nxt][0]
            and _year_token(tokens[nxt][1])
        ):
            return True
    return False


def _same_line_words_after_year(tokens: list[tuple[str, str]], index: int) -> bool:
    """``2024, It``, ``2024 It``, ``2024 - It``, or ``2024 (Z)`` on the called line.

    A glued year, a clean subsection, ``(Amendment)``, ``(No. 2)``, and a
    citation stay with the title. An earlier year does too when a later
    instrument year is still on the line. A year with nothing after it, or
    only a following line, stays a whole title.
    """
    nxt = index + 1
    if nxt < len(tokens) and tokens[nxt][1].strip() == ",":
        nxt += 1
    if nxt >= len(tokens) or not _year_token(tokens[nxt][1]):
        return False
    if _year_token_closes(tokens[nxt][1]):
        return False
    nxt += 1
    if nxt < len(tokens) and tokens[nxt][1].strip() == ",":
        nxt += 1
    if nxt < len(tokens) and re.fullmatch(r"[-–—]+", tokens[nxt][1]):
        return True
    if nxt >= len(tokens):
        return False
    gap, token = tokens[nxt]
    if "\n" in gap or _subsection_end(token):
        return False
    if token.startswith("("):
        return not _paren_after_year_continues(tokens, nxt)
    if not re.search(r"[A-Za-z]", token):
        return False
    if _later_instrument_year(tokens, nxt):
        return False
    return True


def _paren_continues_title(token: str) -> bool:
    """``(Amendment)`` continues a wrapped title. ``(2)`` does not."""
    text = token or ""
    return text.startswith("(") and not _subsection_end(text)


def _instrument_ends_title(tokens: list[tuple[str, str]], index: int) -> bool:
    """An instrument word ends the title only when the clause ends after it.

    ``Code of Ethics Act, 2024`` keeps reading past Code. ``(Amendment) Act``
    is not cut off. A following year, period, ``(2)``, newline, price line, or
    the end of the text does end it.
    """
    if _same_line_words_after_year(tokens, index):
        return True
    nxt = index + 1
    if nxt < len(tokens) and tokens[nxt][1].strip() == ",":
        nxt += 1
    if nxt < len(tokens) and _year_token(tokens[nxt][1]):
        if _year_token_closes(tokens[nxt][1]):
            return True
        nxt += 1
    if nxt >= len(tokens):
        return True
    gap, token = tokens[nxt]
    if "\n" in gap or _subsection_end(token) or _is_price_chrome(token):
        return True
    if re.fullmatch(r"\d{1,3}\.", token):
        return True
    return token[:1] == "."


def _year_token_index(tokens: list[tuple[str, str]], index: int) -> Optional[int]:
    """The year token sitting immediately after an instrument word, if there is one."""
    nxt = index + 1
    if nxt < len(tokens) and tokens[nxt][1].strip() == ",":
        nxt += 1
    if nxt < len(tokens) and _year_token(tokens[nxt][1]):
        return nxt
    return None


def _commencement_end(tokens: list[tuple[str, str]], index: int) -> bool:
    """``, and shall`` / ``and it shall`` / ``which shall`` after the year ends the clause."""
    nxt = index + 1
    if nxt < len(tokens) and tokens[nxt][1].strip(".,") == "":
        nxt += 1
    if nxt < len(tokens) and _year_token(tokens[nxt][1]):
        nxt += 1
    if nxt < len(tokens) and tokens[nxt][1].strip(".,") == "":
        nxt += 1
    words = [re.sub(r"[^A-Za-z]", "", token).lower() for _gap, token in tokens[nxt : nxt + 3]]
    if words[:2] in (["and", "shall"], ["which", "shall"]):
        return True
    return words[:3] == ["and", "it", "shall"]


def _sentence_closed(tokens: list[tuple[str, str]], index: int) -> bool:
    """The clause has ended: ``Act.``, ``Act, 2024.``, ``2024.``, or a commencement tail.

    A newline after ``Code`` in ``Pakistan Penal Code`` / ``(Amendment) Act`` is not
    a sentence end, so the read continues to the later instrument word.
    """
    if _commencement_end(tokens, index):
        return True
    # A year at the end of the line stays the title. A wrapped ``(Amendment)``
    # on the next line is still the same name.
    year_at = _year_token_index(tokens, index)
    if year_at is not None:
        after = year_at + 1
        if (
            after < len(tokens)
            and "\n" in tokens[after][0]
            and not _paren_continues_title(tokens[after][1])
            and not _joined_statute_continues(tokens, after)
        ):
            return True
    # ``Code.\n(Amendment) Act`` is one title. ``Act.\n(2)`` is the end of it.
    if tokens[index][1].rstrip().endswith("."):
        nxt = index + 1
        if nxt < len(tokens) and _paren_continues_title(tokens[nxt][1]):
            return False
        return True
    if _same_line_words_after_year(tokens, index):
        return True
    year_at = _year_token_index(tokens, index)
    return year_at is not None and _year_token_closes(tokens[year_at][1])


def _keep_following_year(kept: list[tuple[str, str]], tokens: list[tuple[str, str]], index: int) -> None:
    """Leave the year in the token list so rendering can append it."""
    nxt = index + 1
    if nxt < len(tokens) and tokens[nxt][1].strip() == ",":
        kept.append(tokens[nxt])
        nxt += 1
    if nxt < len(tokens) and _year_token(tokens[nxt][1]):
        kept.append(tokens[nxt])


def _render_short_title(tokens: list[tuple[str, str]], end: int) -> str:
    parts: list[str] = []
    kept = tokens[: end + 1]
    for index, (_gap, token) in enumerate(kept):
        instrument = _INSTRUMENT_KEYS.get(_lookup_key(token))
        if instrument:
            piece = instrument
            nxt = kept[index + 1][1] if index + 1 < len(kept) else ""
            if "," in token and nxt and (_year_token(nxt) or nxt[:1].isdigit()):
                piece = f"{instrument},"
            parts.append(piece)
        else:
            parts.append(_apply_ocr_token(token))
    title = _collapse(" ".join(parts))
    year = _year_after(tokens, end + 1)
    if year and not re.search(rf"\b{year}\b", title) and not _has_ocr_year(title):
        title = f"{title}, {year}"
    return title.strip(" .")


def _short_title(text: str) -> Optional[str]:
    """Read the called-clause through the first instrument word that ends the sentence."""
    match = _CALLED_START_RE.search(text or "")
    if not match:
        return None
    rest = (text or "")[match.end() :]
    rest = re.sub(
        r"^\s*(?:the\s+)?(?:[-–]?\s*[\(\{]\s*[0-9lI]+\s*[\)\}]\s*)?",
        "",
        rest,
        count=1,
    )
    rest = _join_inside_words(rest[:700])
    tokens = [(gap, token) for gap, token in _token_gaps(rest)]
    kept: list[tuple[str, str]] = []
    ends: list[int] = []
    seen_instrument = False
    for index, (gap, token) in enumerate(tokens):
        if kept and re.search(r"\n\s*\n", gap):
            break
        if kept and _subsection_end(token) and not _continues_numbered_act(kept, token):
            break
        # A bare ``2.`` or ``2.-`` after the title has already ended is the
        # next section, including ``(V of 2024). 2.-``.
        if seen_instrument and re.fullmatch(r"\d{1,3}\.-?", token or ""):
            break
        # ``&`` joins two principals inside one amending title. Other symbols still stop.
        if _symbol_mixed(token) and not (
            token == "&" and _text_has_combining_suffix(_text_from(tokens, index))
        ):
            break
        if _is_price_chrome(token) and seen_instrument:
            break
        kept.append((gap, token))
        if _INSTRUMENT_KEYS.get(_lookup_key(token)):
            seen_instrument = True
            numbered_year = _numbered_act_year_index(tokens, index)
            if numbered_year is not None:
                for extra in range(index + 1, numbered_year + 1):
                    kept.append(tokens[extra])
                ends.append(len(kept) - 1)
                break
            if _instrument_ends_title(tokens, index) or _commencement_end(tokens, index):
                ends.append(len(kept) - 1)
                if _sentence_closed(tokens, index):
                    _keep_following_year(kept, tokens, index)
                    break
    if ends:
        return _render_short_title(kept, ends[-1])
    if not kept:
        return None
    return _render_short_title(kept, len(kept) - 1)


def _token_gaps(text: str):
    last = 0
    for match in re.finditer(r"\S+", text or ""):
        yield text[last : match.start()], match.group(0)
        last = match.end()


def _year_after(tokens: list[tuple[str, str]], index: int) -> Optional[str]:
    if index < len(tokens) and tokens[index][1] == ",":
        index += 1
    if index >= len(tokens):
        return None
    match = _year_token(tokens[index][1])
    if not match:
        return None
    return match.group(1)


def _established_name(text: str) -> Optional[str]:
    match = _AN_ACT_RE.search((text or "")[:8000])
    if not match:
        return None
    purpose = _repair_span(match.group("purpose"))
    established = _ESTABLISH_RE.match(purpose)
    if not established:
        return None
    return _collapse(established.group("name")).strip(" .,:;")


def _listing_supported(listing_title: Optional[str], text: str) -> bool:
    flat = _collapse(listing_title or "")
    if not flat or is_rejected_assembly_statute_name(flat):
        return False
    if not _is_plausible_act_title(flat):
        return False
    return listed_title_supported_by_text(flat, text or "")


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
    accepted = _accept(short, body, fallback_year) if short else None
    if accepted:
        name, year = accepted
        return AssemblyTitleDecision(name, year, "short_title")

    # A garbage or year-disagreeing short title must not beat a listing title
    # that the text actually contains.
    if short and _listing_supported(listing_title, body):
        listed = _listing_title(listing_title, body)
        if listed:
            name, year = listed
            return AssemblyTitleDecision(name, year, "listing")

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
    # The purpose ends where the short-title clause starts. The called name is
    # the instrument, not the Act that ``further to amend`` points at.
    head_raw = (text or "")[:1800]
    called = _CALLED_START_RE.search(head_raw)
    if called:
        head_raw = head_raw[: called.start()]
    head = _collapse(head_raw).lower()
    for match in re.finditer(r"\b([a-z]{6,12})\s+to\s+amend\b(.{0,160})", head):
        if not _within(match.group(1), "further", 2):
            continue
        if flat_name in _collapse(match.group(2)):
            return True
    return False


def _title_core(name: str) -> str:
    core = re.sub(r"(?i)^the\s+", "", _collapse(name))
    core = re.sub(r",?\s*(?:1[89]\d{2}|20\d{2})\s*$", "", core)
    return core.strip(" .,;").lower()


def _closes_on_instrument_year(name: str) -> bool:
    """True when the last year is the year of a closing Act, Ordinance, Order, or Code."""
    years = list(re.finditer(r"\b(?:1[89]\d{2}|20\d{2})\b", name or ""))
    if not years:
        return False
    last = years[-1]
    if re.search(r"[A-Za-z]", (name or "")[last.end() :]):
        return False
    before = (name or "")[: last.start()]
    # ``Act No. 2, 2024`` and ``Act No. 2 of 2024`` close on that year.
    # A parenthesised citation ``(Act No. 2 of 1860)`` does not.
    return re.search(
        r"(?i)(?<!\()"
        r"(?:act|ordinance|order|code)"
        r"(?:\s+no\.?\s*\d+[a-z]?)?"
        r"(?:\s+of)?"
        r"\s*,?\s*$",
        before,
    ) is not None


def _truncated_principal(current: str, short_name: str) -> bool:
    """``Tax Act, 1990`` is the unfinished tail of ``Sales Tax Act, 1990 (Amendment) Act``.

    The words in front must be more of the same name. A different Act joined
    with ``and`` still has its own instrument word in that prefix.
    """
    if not _combining_suffix_at_end(short_name):
        return False
    current_flat = _collapse(current).lower()
    short_flat = _collapse(short_name).lower()
    if len(current_flat) < 8 or current_flat not in short_flat:
        return False
    at = short_flat.find(current_flat)
    if at <= 0:
        return False
    before = short_flat[at - 1]
    after = short_flat[at + len(current_flat) : at + len(current_flat) + 1]
    if before.isalnum() or (after and after.isalnum()):
        return False
    prefix = short_flat[:at]
    if re.search(r"\d|\(", prefix):
        return False
    if re.search(r"\b(?:act|ordinance|order|code|rules|regulations)\b", prefix):
        return False
    return bool(prefix.strip())


def _strip_leading_the(name: str) -> str:
    return re.sub(r"(?i)^the\s+", "", _collapse(name))


def _listing_body_is_well_formed(listing: str) -> bool:
    """A full instrument title. Clause junk stays out. The close is not re-listed here."""
    flat = _strip_leading_the(listing)
    if not flat or is_listing_chrome_text(flat) or is_gazette_non_act_title(flat):
        return False
    if looks_like_garbage_name(flat) or looks_like_clause_fragment(flat):
        return False
    if _leading_word_is_lower(flat):
        return False
    if re.search(r"(?i)\b(?:shall|may)\b", flat):
        return False
    if re.search(r"(?i)\b(?:or|under)\s+the\b", flat):
        return False
    if _starts_with_amendment_of(flat) or _has_section_number(flat) or _has_called_word(flat):
        return False
    # ``as amended by`` between years is a clause. ``and the Y (Special
    # Provisions) Act`` is one title, so a parenthetical close keeps it.
    if _connector_between_years(flat) and not re.search(
        r"(?i)\([^)]{1,80}\)\s*(?:act|ordinance|order|code|regulation)\b",
        flat,
    ):
        return False
    if _CLAUSE_RUN_ON_RE.search(flat) or _YEAR_GLUED_TAIL_RE.search(flat):
        return False
    if _ACT_NO_RUN_ON_RE.search(flat) or _has_ocr_year(flat):
        return False
    if not _INSTRUMENT_RE.search(flat):
        return False
    return _closes_on_instrument_year(flat)


def _called_listing_matches(listing: str, text: str) -> bool:
    """The called clause is ``may/shall be called the <listing>``, and nothing further."""
    listed = _collapse(listing)
    clause = _called_clause(text)
    if not listed or not clause:
        return False
    low_clause = clause.lower()
    low_listed = listed.lower()
    if low_clause.startswith(low_listed):
        rest = clause[len(listed) :]
    elif low_clause.startswith("the " + low_listed):
        rest = clause[len("the ") + len(listed) :]
    else:
        return False
    # The called title may be followed by ``(2)`` or ``2.`` The next instrument
    # (`` (Amendment) Act``) is a different name.
    return re.match(r"(?i)^(?:\s*[.)\]])+(?:\s*(?:\(\s*\d|\d{1,3}\.).*)?$", rest) is not None or rest.strip() == ""


def _verbatim_good_listing(listing: Optional[str], text: str) -> bool:
    """A well-formed listing that is the called title, whatever its close or join.

    Repair runs only for chrome, a missing listing, or a listing that is not
    this called title.
    """
    if not listing or is_listing_chrome_text(listing):
        return False
    if not _listing_body_is_well_formed(listing):
        return False
    return _called_listing_matches(listing, text)


def _own_instrument_listing(listing: Optional[str], text: str) -> bool:
    """The listing is a full title inside the called clause, not a prefix of a longer one.

    ``the Constitution (Twenty-sixth Amendment) Act, 2024`` beside the Finance
    Act stays. ``Sales Tax Act, 1990`` in front of ``(Amendment) Act, 2024``
    does not: that called line is the amending instrument.
    """
    if not listing or not _listing_body_is_well_formed(listing):
        return False
    clause = _strip_leading_the(_called_clause(text))
    listed = _strip_leading_the(listing)
    if not clause or not listed:
        return False
    low_clause = clause.lower()
    low_listed = listed.lower()
    at = low_clause.find(low_listed)
    if at < 0:
        return False
    before = low_clause[at - 1] if at else " "
    after = low_clause[at + len(low_listed) : at + len(low_listed) + 1]
    if (at and before.isalnum()) or (after and after.isalnum()):
        return False
    rest = clause[at + len(listed) :].lstrip()
    continues = rest[:1].isalpha() or (rest.startswith("(") and not re.match(r"^\(\s*\d", rest))
    if not continues:
        return True
    # The called line keeps going, so this listing is a prefix or a truncated tail
    # of one amending instrument, not a side-by-side Act.
    if at == 0 or low_clause[:at].rstrip().lower().endswith("the"):
        return False
    prefix = clause[:at]
    if not re.search(r"(?i)\b(?:act|ordinance|order|code|rules|regulations)\b", prefix):
        return False
    head = re.split(r"[.]", clause, maxsplit=1)[0]
    if _combining_suffix_at_end(head) and (_second_principal_joined(head) or at > 0):
        return False
    return True


def _short_title_supersedes(current: str, short_name: str) -> bool:
    """A truncated instrument (``Pakistan Code, 2024``) loses to the full short title.

    A short title that has already run into the next section (``2.``, ``Amendment of``,
    ``In the``, ``shall``, or a second year) must not replace a stored name.
    A year glued to the next words (``2024.It``, ``2024.(Z)``) must not either.
    Nor words or a non-title bracket after the final year.
    """
    if _has_ocr_year(short_name or ""):
        return False
    if _RUN_ON_SHORT_TITLE_RE.search(short_name or ""):
        return False
    if _YEAR_GLUED_TAIL_RE.search(short_name or ""):
        return False
    _peeled, _last_year = _last_year_match(short_name or "")
    if _last_year is not None and _YEAR_THEN_WORDS_RE.match(_peeled, _last_year.start()):
        return False
    if _bad_bracket_after_last_year(short_name or ""):
        return False
    if len(re.findall(r"\b(?:1[89]\d{2}|20\d{2})\b", short_name or "")) >= 2:
        # A title that closes on its own instrument year may name an older Act.
        # ``and 1860`` and a citation year with more words after it still block.
        if not _closes_on_instrument_year(short_name or ""):
            return False
    current_core = _title_core(current)
    short_core = _title_core(short_name)
    if not current_core or not short_core or current_core == short_core:
        return False
    if (
        short_core.startswith(current_core + " ")
        or short_core.startswith(current_core + "(")
        or short_core.startswith(current_core + ",")
    ):
        return True
    if _truncated_principal(current, short_name or ""):
        return True
    # The known principal can be the second Act only when the list is joined by
    # and / & / and the / , and the and the close is an amending phrase.
    # ``Finance Act, 2024, Federal Excise Act, 2005 (Z) Act, 2025`` stays the
    # Excise Act. ``X and Y (Amendment) Act`` is still one title.
    return bool(
        _combining_suffix_at_end(short_name or "")
        and _second_principal_joined(short_name or "")
        and len(current_core) >= 8
        and re.search(r"(?<![a-z])" + re.escape(current_core) + r"(?![a-z])", short_core)
    )


def _peel_trailing_citation(name: str) -> str:
    return _collapse(_TRAILING_CITATION_RE.sub("", _collapse(name))).strip(" .")


def _peel_captured_citation(name: str) -> str:
    """Drop a trailing citation or a cut-off citation fragment from a stored name."""
    flat = _collapse(name)
    peeled = _peel_trailing_citation(flat)
    peeled = _collapse(_UNCLOSED_CITATION_RE.sub("", peeled)).strip(" .")
    return peeled or flat


def _without_captured_citation(name: str, listing: Optional[str]) -> str:
    """A listing that is itself the cited name stays. Any other capture stores the core."""
    flat = _collapse(name)
    listed = _collapse(listing or "")
    if not flat or (listed and listed == flat):
        return flat
    peeled = _peel_captured_citation(flat)
    if not peeled or peeled == flat or is_rejected_assembly_statute_name(peeled):
        return flat
    return peeled


def _enacted_year(name: str, year: Optional[int]) -> Optional[int]:
    """The closing instrument's own year, when the kept name ends on one.

    A trailing citation is not that year. ``Act, 2024 (Act V of 1860)`` is 2024.
    """
    core = _peel_captured_citation(name)
    if _closes_on_instrument_year(core) or _combining_suffix_at_end(core):
        last = _last_year_in(core)
        if last is not None:
            return last
    return year


def _cited_upstream_name(current: str, short: str, listing: Optional[str]) -> bool:
    """True when a called-line capture adds only a trailing citation to the short title.

    A listing that is itself the cited name stays. Chrome and a missing listing
    store the name without that citation.
    """
    current_flat = _collapse(current)
    listed = _collapse(listing or "")
    if listed and listed == current_flat:
        return False
    peeled = _peel_trailing_citation(current_flat)
    return bool(peeled) and peeled.lower() == _collapse(short).lower() and len(current_flat) > len(peeled)


def _dated_listing_is_complete(current: str, short: str, listing: Optional[str]) -> bool:
    """True when the stored name is already the dated listing and the called line only runs on.

    ``Sales Tax Act, 1990 (Amendment) Act, 2024`` is a longer instrument, not a
    run-on. ``Finance Act, 2024 and Code (Amendment) Act, 2025`` still is.
    """
    listed = _collapse(listing or "")
    if not listed or listed != _collapse(current) or _year_in(listed) is None:
        return False
    longer = _collapse(short or "")
    if not (longer.lower().startswith(listed.lower()) and len(longer) > len(listed)):
        return False
    rest = longer[len(listed) :].lstrip()
    if rest.startswith("(") and _combining_suffix_at_end(longer):
        return False
    return True


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
    # One guard. A well-formed listing that the short-title clause names
    # verbatim stays, close and join included. Chrome, no listing, and junk
    # listings are the only names repair is allowed to change.
    listed_flat = _collapse(listing_title or "")
    keep_listing = _verbatim_good_listing(listing_title, text or "") or (
        listed_flat
        and _own_instrument_listing(listing_title, text or "")
        and (
            current == listed_flat
            or name_needs_assembly_title(current)
        )
    )
    if keep_listing:
        listed_year = _last_year_in(listed_flat) if _closes_on_instrument_year(listed_flat) else _year_in(listed_flat)
        return listed_flat, listed_year if listed_year is not None else year, ""

    def finish(stored: str, stored_year: Optional[int], detail: str = "") -> tuple[str, Optional[int], str]:
        if stored and stored != "name_unknown":
            stored = _without_captured_citation(stored, listing_title)
            stored_year = _enacted_year(stored, stored_year)
        return stored, stored_year, detail

    amended_principal = _is_object_of_amendment_heading(current, text)
    needs = name_needs_assembly_title(current)
    # ``Pakistan Penal Code, 1860`` detected under ``ACT NO. … OF 2024`` is not
    # the instrument. A called title whose own last year is 2024 is.
    if not needs and not amended_principal and _years_disagree(current, text or ""):
        needs = True
    if not needs and not amended_principal:
        short = _short_title(text or "")
        accepted = _accept(short, text or "", year) if short else None
        if accepted and _short_title_supersedes(current, accepted[0]):
            # A dated listing is already the instrument. A longer called line
            # that only adds a later year must not replace it.
            if _dated_listing_is_complete(current, accepted[0], listing_title):
                return finish(current, year)
            chosen = accepted[1] if accepted[1] is not None else year
            return finish(accepted[0], chosen)
        if accepted and _cited_upstream_name(current, accepted[0], listing_title):
            chosen = accepted[1] if accepted[1] is not None else year
            return finish(accepted[0], chosen)
        return finish(current, year)
    decision = resolve_assembly_statute_title(
        text,
        listing_title=listing_title,
        source_url=source_url,
    )
    # The short-title or Act-number year wins over a year carried in from an
    # earlier bad name. An incoming year is only a fallback.
    chosen_year = decision.year if decision.year is not None else year
    # A rejected gazette capture is not a reason to rename the other Act in
    # the same called line. ``Federal Excise Act, 2005`` stays that Act when
    # the line also names the Finance Act and then a junk close.
    if _listing_is_another_principal(listing_title or "", decision.name, text or ""):
        listed = _collapse(listing_title or "")
        listed_year = _year_in(listed)
        # The listing's own year. The Act-number year belongs to the other instrument.
        kept_year = listed_year if listed_year is not None else chosen_year
        return _without_captured_citation(listed, listing_title), kept_year, ""
    detail = decision.detail if decision.source == "name_unknown" else ""
    return finish(decision.name, chosen_year, detail)


def statute_name_unknown_reason(name: Optional[str], evidence: Optional[dict]) -> Optional[str]:
    """Promotion quarantine text when the extractor refused to store a junk title."""
    evidence = evidence or {}
    label = _collapse(name or "")
    detail = str(evidence.get("name_unknown") or "").strip()
    if label == "name_unknown" or (detail and label in {"", "Unknown Statute", "name_unknown"}):
        why = detail or "statute title was listing chrome, OCR, or a body clause"
        return f"name_unknown: {why}"[:1000]
    return None
