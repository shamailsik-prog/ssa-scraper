"""
statute_parser.py - Production Pakistani Legal Statute Parser
Spec: FINAL_SCRAPER_PROMPT_1.md Section 15
Dual strategies: HTML-aware + regex fallback, 5 strategies progressive.

Functions:
    split_into_sections(text, statute_name) -> list[dict]
    detect_statute_name(text, url) -> str

Context: PostgreSQL 16 + pgvector, FastAPI, Celery, httpx, BeautifulSoup,
         playwright, pdfplumber, pytesseract, Fernet, OpenAI embeddings.
"""

import re
import logging
from typing import List, Dict, Match, Optional, Sequence, Tuple
from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known Pakistani Statutes - canonical mapping
# ---------------------------------------------------------------------------
KNOWN_STATUTES_MAP = {
    "pakistan penal code": "Pakistan Penal Code, 1860",
    "ppc": "Pakistan Penal Code, 1860",
    "penal code": "Pakistan Penal Code, 1860",
    "code of criminal procedure": "Code of Criminal Procedure, 1898",
    "crpc": "Code of Criminal Procedure, 1898",
    "cr.p.c": "Code of Criminal Procedure, 1898",
    "criminal procedure code": "Code of Criminal Procedure, 1898",
    "code of civil procedure": "Code of Civil Procedure, 1908",
    "cpc": "Code of Civil Procedure, 1908",
    "c.p.c": "Code of Civil Procedure, 1908",
    "civil procedure code": "Code of Civil Procedure, 1908",
    "constitution of pakistan": "Constitution of the Islamic Republic of Pakistan, 1973",
    "constitution of islamic republic of pakistan": "Constitution of the Islamic Republic of Pakistan, 1973",
    "constitution": "Constitution of the Islamic Republic of Pakistan, 1973",
    "qanun-e-shahadat": "Qanun-e-Shahadat Order, 1984",
    "qanun e shahadat": "Qanun-e-Shahadat Order, 1984",
    "qso": "Qanun-e-Shahadat Order, 1984",
    "contract act": "Contract Act, 1872",
    "companies act": "Companies Act, 2017",
    "companies act 2017": "Companies Act, 2017",
    "transfer of property act": "Transfer of Property Act, 1882",
    "negotiable instruments act": "Negotiable Instruments Act, 1881",
    "limitation act": "Limitation Act, 1908",
    "specific relief act": "Specific Relief Act, 1877",
    "arbitration act": "Arbitration Act, 1940",
    "family courts act": "Family Courts Act, 1964",
    "narcotics": "Control of Narcotic Substances Act, 1997",
    "anti terrorism act": "Anti-Terrorism Act, 1997",
    "ata": "Anti-Terrorism Act, 1997",
    "prevention of electronic crimes act": "Prevention of Electronic Crimes Act, 2016",
    "peca": "Prevention of Electronic Crimes Act, 2016",
}

# For URL slug detection - maps slug fragment to canonical
URL_SLUG_MAP = {
    "ppc": "Pakistan Penal Code, 1860",
    "pakistan-penal-code": "Pakistan Penal Code, 1860",
    "crpc": "Code of Criminal Procedure, 1898",
    "cr-p-c": "Code of Criminal Procedure, 1898",
    "criminal-procedure": "Code of Criminal Procedure, 1898",
    "cpc": "Code of Civil Procedure, 1908",
    "civil-procedure": "Code of Civil Procedure, 1908",
    "constitution": "Constitution of the Islamic Republic of Pakistan, 1973",
    "qanun-e-shahadat": "Qanun-e-Shahadat Order, 1984",
    "contract": "Contract Act, 1872",
    "companies-act": "Companies Act, 2017",
    "limitation": "Limitation Act, 1908",
}

GENERIC_ACT_PATTERN = re.compile(
    r'\b((?:The\s+)?[A-Z][A-Za-z0-9\s\-\&\']+?\s+(?:Act|Ordinance|Order|Code|Rules|Regulations)(?:,?\s*\d{4})?)\b'
)
ACT_WITH_YEAR_PATTERN = re.compile(
    r'\b([A-Z][A-Za-z0-9\s\-\&\']+\s+(?:Act|Ordinance|Order),?\s+\d{4})\b'
)
SECTION_DIGIT_START_RE = re.compile(r'^\s*(\d+[A-Z]?)\s*[\.\-\)\:\]]+\s*(.*)', re.DOTALL)
SECTION_KEYWORD_RE = re.compile(r'^\s*(?:Section|Sec\.|S\.)\s+(\d+[A-Z]?)\s*[:\.\-\)]*\s*(.*)', re.IGNORECASE | re.DOTALL)
# Balochistan gazette PDFs put the marginal note in the left column and the
# section number in the right. pdfplumber joins them onto one line
# ("Short title, 1. (1) This Act may be called…"). A line-start split then
# sees only the heading whose note wrapped onto the previous line, and the
# rest of the act is glued into that one row.
_MARGINAL_SECTION_HEAD_RE = re.compile(
    r"(?m)^[ \t]*"
    r"[A-Z][A-Za-z][A-Za-z,'’\-]{0,40}"
    r"(?:[ \t]+[A-Za-z][A-Za-z,'’\-]{0,40}){0,6}"
    r"\.?[ \t]+"
    r"(\d{1,3}[A-Z]?)"
    # "1. (1)" / "7. The" on a text layer. OCR also drops the period ("2 (1)")
    # or reads the subsection mark as "@)".
    r"(?:"
    r"\s*\.(?:\s*[-–—]\s*|\s+)"
    r"|"
    r"\s+(?=\()"
    r")"
    r"(?=(?:[\(\[\{]\s*\d|@\)|[A-Z]))"
)
# The word immediately before a marginal number. "Section 5. The" and
# "under section 12. (1)" are cross-references, not headings.
_CROSS_REF_PRIOR_RE = re.compile(
    r"(?i)(?:sub-?sections?|sections?|clauses?|rules?|articles?|schedules?)\s*$"
)
# Scanned amendment gazettes put "Amendment of section N of W.P. Ordinance…"
# in the margin. OCR lifts that note onto its own line ("section 9 of W.P.
# Ordinance", "S. 11 of W. P. Ordinance", "Sec. 4 of the Land Revenue Act",
# "section 16O ofthe said Act"). The number is the principal instrument's,
# not the amending act's. A real keyword heading is "Section 2. Definitions".
_PRINCIPAL_MARGIN_NOTE_LEAD_RE = re.compile(
    r"(?i)^[ \t]*(?:amendment\s+of\s+)?"
    r"(?:sections?|sec\.|s\.)\s+"
    r"\d+[A-Za-z]{0,2}"
    r"(?:[ \t]*(?:,|and)[ \t]+\d+[A-Za-z]{0,2})*"
    r"[ \t]*[:.\-)]*[ \t]+"
    r"of(?:[ \t]*the)?"
    r"(?:[ \t]+(?P<tail>\S.*)|[ \t]*)$"
)
_PRINCIPAL_MARGIN_NOTE_INSTRUMENT_RE = re.compile(
    r"(?i)^(?:the[ \t]+)?(?:"
    r"said|principal|west[ \t]+pakistan|w[ \t]*\.[ \t]*p\b\.?|wp"
    r")\b"
    r"|^(?:the[ \t]+)?"
    r"(?:[A-Za-z][A-Za-z'’.\-]*[ \t]+){0,4}"
    r"(?:act|ordinance|order|code|rules|regulations)\b"
)
_LINE_START_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(\d+[A-Z]?)\s*[\.\-\)\:\]]\s*",
    re.MULTILINE,
)
# Used only when line-start and marginal headings together find fewer than two
# sections. Same cross-reference and sequence rules as a marginal heading.
_LOOSE_SECTION_RE = re.compile(r"(\d+[A-Z]?)\s*[\.\-\)]\s+(?=[A-Z][a-z])")
SHORT_TITLE_CLAUSE_RE = re.compile(
    r'^\s*(?:section\s+\d+[A-Z]?\s*[\.\-\)\:]\s*)?(?:short\s+title[^.:\n]*[:\-\.\)]\s*)?(?:this|the)\s+'
    r'(?:act|ordinance|order|code|rules?|regulations?)\s+(?:may|shall)\s+be\s+called\b',
    re.IGNORECASE,
)
SHALL_BE_CALLED_ACT_RE = re.compile(
    r"(?is)\b(?:this|the)\s+act\s+shall\s+be\s+called\s+the\s+"
    r"(?P<title>.{6,400}?\b(?:act|ordinance|order|rules|regulations)\b.{0,40})"
)
FOOTNOTE_ANNOTATION_RE = re.compile(
    r'(?i)\b(?:substituted|inserted|added|omitted|amended|renumbered|repealed)\s+by\b'
)
GAZETTE_FOOTNOTE_RE = re.compile(
    r'(?i)\bgazette\b.*\b(extraordinary|dated|notification|part)\b'
)
SECTION_RANGE_FOOTNOTE_RE = re.compile(
    r'(?i)\b(?:for|in)\s+sections?\s+\d+[A-Z]?(?:\s*,\s*\d+[A-Z]?)*(?:\s+and\s+\d+[A-Z]?)\b'
)
TRAILING_AND_SECTION_RE = re.compile(r'(?i)^\s*(?:and|or)\s+\d+[A-Z]?\.?\s*$')
# Auditor 2026-10-26: section_number values that are years, not headings.
EXACT_YEAR_SECTION_RE = re.compile(r"^(1[89]|20)\d{2}$")
# Auditor 2026-10-27: a footnote/amendment date at the start of a line is not a
# heading. The day is a small number (11.10.2018) and collides with a real
# section, so the 1800–2099 year rule does not catch it. Separate from that class.
AMENDMENT_DATE_LINE_RE = re.compile(r"^\s*\d{1,2}\.\d{1,2}\.(?:19|20)\d{2}\b")
# assembly_pdf_sections treats a jump of more than 120 as a bogus heading.
YEAR_SECTION_SEQUENCE_GAP = 120
_YEAR_CITATION_BEFORE_RE = re.compile(r"(?i)(?:\(|\bof)\s*$")
_YEAR_CITATION_DT_RE = re.compile(r"(?i)^\s*dt\.")
ACT_SECTION_SANITY_CAPS = {
    "canal and drainage act 1873": 75,
    "canal and drainage act": 75,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_whitespace(text: str) -> str:
    """Collapse whitespace but preserve paragraph breaks."""
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\r", "\n")
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def _is_html(text: str) -> bool:
    if not text or len(text) < 20:
        return False
    lowered = text[:2000].lower()
    return "<html" in lowered or "<div" in lowered or "<p" in lowered or "<body" in lowered

def _clean_section_text(raw: str) -> str:
    raw = _normalize_whitespace(raw)
    raw = raw.replace("—", "-").replace("–", "-")
    return raw.strip()

# ``section 4 of`` / ``of the`` / ``of W.P.`` with the instrument on a later line.
# ``of the following shall`` is a sentence, not this fragment.
_BARE_MARGIN_OF_TAIL_RE = re.compile(
    r"(?i)^(?:the|said|principal|w[ \t]*\.[ \t]*p\b\.?|wp)?[ \t]*\.?$"
)
_MARGIN_OPERATIVE_TAIL_RE = re.compile(
    r"(?i)\b(?:shall|means|provided|whereas|substituted|inserted|omitted)\b"
)


def _line_is_principal_section_margin_note(line: str, following: str = "") -> bool:
    """True for ``section 9 of W.P. Ordinance`` and a note whose instrument wraps.

    ``Section 2. Definitions`` is a heading and is not this note. A scanned
    margin wraps the instrument across the next three lines (``section 4 of`` /
    ``Ordinsace XI of`` / ``1960``). A line that ends at ``of``, ``of the`` or
    ``of W.P.`` is the same note. ``of the following shall`` is not.
    """
    lead = _PRINCIPAL_MARGIN_NOTE_LEAD_RE.match(line or "")
    if not lead:
        return False
    tail = (lead.group("tail") or "").strip()
    if tail and _PRINCIPAL_MARGIN_NOTE_INSTRUMENT_RE.match(tail):
        return True
    if tail and _MARGIN_OPERATIVE_TAIL_RE.search(tail):
        return False
    if tail and len(tail.split()) > 6:
        return False
    if not tail or _BARE_MARGIN_OF_TAIL_RE.match(tail):
        return True
    next_lines = [item.strip() for item in (following or "").splitlines() if item.strip()][:3]
    if any(_PRINCIPAL_MARGIN_NOTE_INSTRUMENT_RE.match(item) for item in next_lines):
        return True
    joined = " ".join([tail, *next_lines]).strip()
    return bool(joined and _PRINCIPAL_MARGIN_NOTE_INSTRUMENT_RE.match(joined))


def _match_is_principal_section_margin_note(text: str, pos: int) -> bool:
    # The keyword match includes the newline that opens the line.
    while pos < len(text) and text[pos] in "\r\n":
        pos += 1
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", start)
    line = text[start: end if end != -1 else None]
    parts: List[str] = []
    cursor = end
    for _ in range(3):
        if cursor == -1:
            break
        next_end = text.find("\n", cursor + 1)
        parts.append(text[cursor + 1: next_end if next_end != -1 else None])
        cursor = next_end
    return _line_is_principal_section_margin_note(line, "\n".join(parts))


def _chunk_is_principal_margin_note(chunk: str) -> bool:
    """A paragraph that is only the margin note (and the clause under it).

    A line that already starts with the amending act's own number is a heading.
    ``Amendment of`` wrapped above ``section N of`` is the same note.
    """
    lines = [line for line in (chunk or "").splitlines() if line.strip()]
    if not lines or re.match(r"^[ \t]*\d", lines[0]):
        return False
    following = "\n".join(lines[1:4])
    if _line_is_principal_section_margin_note(lines[0], following):
        return True
    if re.match(r"(?i)^[ \t]*amendment\s+of\b", lines[0]) and len(lines) > 1:
        return _line_is_principal_section_margin_note(lines[1], "\n".join(lines[2:5]))
    return False


def _extract_number_title_from_chunk(chunk: str) -> Tuple[Optional[str], str, str]:
    """
    Returns (section_number, section_title, section_text_body)
    section_text_body is full chunk cleaned.
    """
    chunk_clean = _clean_section_text(chunk)
    if not chunk_clean:
        return None, "", ""

    # Try keyword form first: Section 3 ...
    # "section 9 of W.P. Ordinance" is a margin note, not "Section 3. Title".
    m = SECTION_KEYWORD_RE.match(chunk_clean)
    first_line, _, rest_lines = chunk_clean.partition("\n")
    if m and not _line_is_principal_section_margin_note(first_line, rest_lines):
        num = m.group(1).strip()
        rest = m.group(2).strip() if m.group(2) else ""
        title = _derive_title(rest)
        return num, title, chunk_clean

    # Try digit start form: 3. Title ...
    m2 = SECTION_DIGIT_START_RE.match(chunk_clean)
    if m2:
        num = m2.group(1).strip()
        rest = m2.group(2).strip() if m2.group(2) else ""
        title = _derive_title(rest)
        return num, title, chunk_clean

    # Marginal note, then the number: "Short title, 1. (1) This Act…"
    marginal = _MARGINAL_SECTION_HEAD_RE.match(chunk_clean)
    if marginal:
        num = marginal.group(1).strip()
        note = _usable_marginal_title(chunk_clean[: marginal.start(1)])
        rest = chunk_clean[marginal.end() :].strip()
        title = note or _derive_title(rest)
        return num, title, chunk_clean

    return None, "", chunk_clean

def _derive_title(rest: str) -> str:
    if not rest:
        return ""
    # Title is first line or first sentence, capped
    first_line = rest.split("\n")[0].strip()
    if len(first_line) <= 5 and "\n" in rest:
        # try second line
        parts = [p.strip() for p in rest.split("\n") if p.strip()]
        first_line = parts[0] if parts else first_line
    # If still long, take up to first period or 200 chars
    if len(first_line) > 200:
        # try sentence split
        sentence_match = re.split(r'[\.\:\;] ', first_line, maxsplit=1)
        if sentence_match and len(sentence_match[0]) > 10 and len(sentence_match[0]) < 200:
            first_line = sentence_match[0]
        else:
            first_line = first_line[:200]
    # Clean trailing dash or colon
    first_line = first_line.strip(" -:;.")
    return first_line[:300]

def _make_section_dict(number: str, title: str, text: str, statute_name: str) -> Dict[str, str]:
    # Ensure non-empty
    if not title:
        # fallback title from statute + section
        title = f"{statute_name} - Section {number}" if statute_name else f"Section {number}"
        # try derive again from text first 120 chars
        snippet = text[:200].split("\n")[0]
        if snippet and len(snippet) > 10:
            # remove number prefix for title snippet
            snippet = re.sub(r'^\s*(?:Section\s+)?\d+[A-Z]?\s*[\.\-\)\:]*\s*', '', snippet, flags=re.I)
            if snippet:
                title = snippet[:200]
    return {
        "section_number": str(number).strip(),
        "section_title": _normalize_whitespace(title)[:500],
        "section_text": _clean_section_text(text),
    }

def _normalize_statute_key(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', (name or "").lower()).strip()

def _parse_numeric_section_number(raw_section_number: str) -> Optional[int]:
    match = re.search(r'\d+', raw_section_number or "")
    return int(match.group(0)) if match else None


def is_exact_year_section_number(raw_section_number: str) -> bool:
    """True for a 4-digit token 1800–2099 (``^(1[89]|20)\\d{2}$``)."""
    return bool(EXACT_YEAR_SECTION_RE.fullmatch((raw_section_number or "").strip()))


def section_text_starts_with_amendment_date(text: str) -> bool:
    """True when the first line is a footnote date ``DD.MM.YYYY``, not a heading.

    ``11.10.2018. 278 Volume XI`` is an amendment note. It must not occupy
    section 11. A wrapped year on the next line (``2018.``) is the separate
    1800–2099 class and is not decided here.
    """
    first = (text or "").split("\n", 1)[0]
    return bool(AMENDMENT_DATE_LINE_RE.match(first))


# HS / PCT and Balochistan-AJK services rows: 1–2 digits, a dot, exactly two
# digits, then a commodity or services word (``08.05 Nuts``, ``98.01 hotels``).
# OCR reads chapter 08 as ``0E`` and chapter 01 as ``0I``. A clock (``7.30 p.m.``)
# and a footnote date (``11.10.2018``) use the same NN.NN shape and are not rows.
_TARIFF_CLOCK_RE = re.compile(r"(?i)^[ap](?:\.?\s*m\.?)(?!\w)")
_TARIFF_CODE_HEAD_RE = re.compile(
    r"(?i)^(?P<head>(?:\d{1,2}|0[EI])\.\d{2})(?!\.\d)\.?\s+(?P<rest>\S.*)$"
)
_TARIFF_OCR_TOKEN_RE = re.compile(
    r"(?i)^0[EI]\.?\s+(?P<rest>(?:\d{1,2}|0[EI])\.\d{2}(?!\.\d)\.?\s+\S.*)$"
)
_TARIFF_CODE_ONLY_RE = re.compile(r"(?i)^(?:\d{1,2}|0[EI])\.\d{2}\.?$")
_COMMODITY_WORD_RE = re.compile(r"[A-Za-z]{2,}")
# ``78. 38BO5 Chromite``: the dot of HS ``38.05`` was read as B and the 0 as O.
# The token keeps two chapter digits, then 2–4 OCR digits, and at least one
# mangled letter. A plain ``3805`` is not this class.
_TARIFF_OCR_MANGLED_HS_RE = re.compile(
    r"(?i)^(?:\d{1,3}[A-Z]?\s*[\.\)\]]\s+)?"
    r"(?P<code>\d{2}[0-9OIB]{2,4})"
    r"\s+(?P<rest>[A-Za-z].*)$"
)
# ``03) Cuprammonium``: a zero-padded schedule row with no decimal. ``1) Short
# title`` and ``12) Penalties`` are not zero-padded and stay headings.
_TARIFF_ZERO_PAD_PAREN_RE = re.compile(
    r"(?i)^0\d{1,2}\)\s+(?P<word>[A-Za-z]{4,})\b"
)
# A column header glued on the same line (``Rate 1. Where…``) is still the
# rate row. ``_join_wrapped_marginal_notes`` used to do that glue.
_TARIFF_INCOME_RATE_RE = re.compile(
    r"(?i)^(?:(?:rates?\s+of\s+tax|rates?|rs\.?|per\s+cent)\s+)?"
    r"\d{1,3}[A-Z]?\s*[\.\)\]]\s+Where the taxable income\b"
)
_COLUMN_HEADER_LINE_RE = re.compile(
    r"(?i)^(?:rates?\s+of\s+tax|rates?|rs\.?|per\s+cent)\.?$"
)
# ``4. Rolled but not further worked`` is a tariff sub-item with no HS code.
# ``4. Other provisions.—`` keeps the marginal dash and is a heading.
_TARIFF_SUBROW_RE = re.compile(
    r"(?i)^\d{1,2}\.\s+(?P<opener>Rolled|Hot|Cold|Other|Containing|Of)\b"
)
_TARIFF_SUBROW_CUE_RE = re.compile(
    r"(?i)\b(?:thickness|millimetres?|millimeters?|metric\s+tons?|per\s+kg|"
    r"per\s+cent|percent|worked|by\s+weight)\b"
)
_HEADING_TERMINATOR_RE = re.compile(r"(?:\.—|\.--|—|–|--|\.-)")
_ZERO_PAD_STATUTORY_OPENER = frozenset({
    "short",
    "definitions",
    "definition",
    "this",
    "amendment",
    "income",
    "subject",
    "provided",
    "notwithstanding",
    "powers",
    "penalties",
    "penalty",
    "hours",
    "weekly",
    "overtime",
    "commencement",
    "extent",
    "preliminary",
    "title",
    "interpretation",
    "application",
    "repeal",
    "savings",
    "saving",
    "where",
    "every",
    "whoever",
})


def _tariff_commodity_rest(rest: str) -> str:
    """Words after the heading code. A second code (``0E. 08.05 Nuts``) is peeled once."""
    nested = _TARIFF_CODE_HEAD_RE.match((rest or "").strip())
    if nested:
        return nested.group("rest")
    return (rest or "").strip()


def _rest_is_commodity_line(rest: str) -> bool:
    body = _tariff_commodity_rest(rest)
    if not body or _TARIFF_CLOCK_RE.match(body):
        return False
    return bool(_COMMODITY_WORD_RE.match(body))


def _ocr_mangled_hs_code(code: str) -> bool:
    token = code or ""
    return bool(re.search(r"[OIBoib]", token))


def _line_is_zero_padded_chemical_row(line: str) -> bool:
    match = _TARIFF_ZERO_PAD_PAREN_RE.match(line)
    if not match:
        return False
    return match.group("word").lower() not in _ZERO_PAD_STATUTORY_OPENER


def _line_is_tariff_schedule_heading(line: str) -> bool:
    raw = (line or "").strip()
    if not raw:
        return False
    if _TARIFF_INCOME_RATE_RE.match(raw):
        return True
    ocr = _TARIFF_OCR_TOKEN_RE.match(raw)
    if ocr:
        return _rest_is_commodity_line(ocr.group("rest"))
    match = _TARIFF_CODE_HEAD_RE.match(raw)
    if match and _rest_is_commodity_line(match.group("rest")):
        return True
    mangled = _TARIFF_OCR_MANGLED_HS_RE.match(raw)
    if mangled and _ocr_mangled_hs_code(mangled.group("code")) and _rest_is_commodity_line(mangled.group("rest")):
        return True
    return _line_is_zero_padded_chemical_row(raw)


def _line_is_tariff_subrow(line: str) -> bool:
    """True for ``4. Rolled…`` / ``4. Of … thickness`` with no marginal dash.

    ``Rolled``, ``Hot`` and ``Cold`` are commodity openers. ``Other``,
    ``Containing`` and ``Of`` need a schedule word such as ``thickness`` so
    ``4. Other provisions.—`` is not a sub-row. The caller still requires the
    previous chunk to be tariff-like before folding the line.
    """
    raw = (line or "").strip()
    match = _TARIFF_SUBROW_RE.match(raw)
    if not match or _HEADING_TERMINATOR_RE.search(raw):
        return False
    if match.group("opener").lower() in {"rolled", "hot", "cold"}:
        return True
    return bool(_TARIFF_SUBROW_CUE_RE.search(raw))


def section_text_opens_unheaded_tariff_subrow(text: str) -> bool:
    """True when the chunk's first line is an unheaded tariff sub-row."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return bool(lines) and _line_is_tariff_subrow(lines[0])


def section_text_starts_with_tariff_schedule_row(text: str) -> bool:
    """True when the chunk opens on a schedule row, not an Act heading.

    ``08.05 Nuts``, ``32.03 Synthetic organic…``, and ``98.01 hotels`` are
    schedule lines, including OCR chapter tokens ``0E`` / ``0I`` and a mangled
    code such as ``78. 38BO5 Chromite``. ``03) Cuprammonium`` is a zero-padded
    chemical row. ``1. Where the taxable income…`` is an income-tax rate-table
    row. ``7.``, ``32.``, ``1) Short title``, and ``12) Penalties`` stay
    headings. ``7.30 p.m.`` and ``11.10.2018`` are not schedule rows.
    A leading column header (``Rate``, ``Rs.``, ``Per cent``) does not hide
    the row under it.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    if _line_is_tariff_schedule_heading(lines[0]):
        return True
    if _COLUMN_HEADER_LINE_RE.match(lines[0]) and len(lines) >= 2 and (
        _line_is_tariff_schedule_heading(lines[1]) or _line_is_tariff_subrow(lines[1])
    ):
        return True
    if len(lines) >= 2 and _TARIFF_CODE_ONLY_RE.match(lines[0]):
        return _rest_is_commodity_line(lines[1])
    return False


_HS_SECTION_NUMBER_RE = re.compile(r"(?i)^\d{1,2}\.\d{2}$")
_OCR_HS_SECTION_NUMBER_RE = re.compile(r"(?i)^0[EI]$")


def is_tariff_schedule_section(number: str, text: str) -> bool:
    """True when this id/text pair is a schedule row rather than an Act section.

    The splitter stores the chapter (``32``) and leaves ``32.03`` at the start
    of the text. A later pass can store the heading token itself as the id
    and the commodity words as the text. ``7.30`` with ``p.m.`` is a clock.
    """
    if section_text_starts_with_tariff_schedule_row(text):
        return True
    num = (number or "").strip()
    body = (text or "").strip()
    if _HS_SECTION_NUMBER_RE.fullmatch(num):
        if _rest_is_commodity_line(body):
            return True
        return section_text_starts_with_tariff_schedule_row(f"{num} {body}".strip())
    if _OCR_HS_SECTION_NUMBER_RE.fullmatch(num):
        return section_text_starts_with_tariff_schedule_row(body)
    return False


def year_section_fits_sequence(number: int, siblings: Sequence[Tuple[str, int]]) -> bool:
    """A year-shaped heading is real only when it continues this act's section numbers.

    Non-year section numbers are the anchors. The year fits when one of them is within
    ``YEAR_SECTION_SEQUENCE_GAP`` (the same jump ``assembly_pdf_sections`` already treats
    as bogus). With no anchor, only a single tight run of year-shaped headings fits
    (sections 1798–1801). An isolated 1860 next to s.25 does not.
    """
    anchors = [n for token, n in siblings if not is_exact_year_section_number(token)]
    if anchors:
        return min(abs(number - anchor) for anchor in anchors) <= YEAR_SECTION_SEQUENCE_GAP
    year_vals = sorted({n for token, n in siblings if is_exact_year_section_number(token)})
    if len(year_vals) < 2:
        return False
    return all(b - a <= YEAR_SECTION_SEQUENCE_GAP for a, b in zip(year_vals, year_vals[1:]))


def _year_citation_context(text: str, match: Match[str]) -> bool:
    """Year token follows '(' or 'of ', ends with ')', or is followed by 'dt.'."""
    token = (match.group(1) or "").strip()
    if not is_exact_year_section_number(token):
        return False
    before = text[: match.start(1)]
    heading = text[match.start(1) : match.end()]
    after = text[match.end() : match.end() + 16]
    if _YEAR_CITATION_BEFORE_RE.search(before):
        return True
    if re.search(r"\)\s*$", heading):
        return True
    if _YEAR_CITATION_DT_RE.match(after):
        return True
    return False


def _previous_tail_marks_year_citation(previous_text: str) -> bool:
    """The prior section was cut on a line break after '(' or 'of '."""
    return bool(_YEAR_CITATION_BEFORE_RE.search(previous_text or ""))


def _section_text_is_year_citation(token: str, text: str) -> bool:
    if not is_exact_year_section_number(token):
        return False
    raw = text or ""
    if re.search(rf"(?i)(?:\(|\bof)\s*{re.escape(token)}\b", raw):
        return True
    if re.match(rf"(?is)^\s*(?:(?:section|sec\.|s\.)\s+)?{re.escape(token)}\s*\)", raw):
        return True
    if re.match(
        rf"(?is)^\s*(?:(?:section|sec\.|s\.)\s+)?{re.escape(token)}\b[^\n]{{0,12}}?\bdt\.",
        raw,
    ):
        return True
    return False


def _sibling_number_pairs(tokens: Sequence[str]) -> List[Tuple[str, int]]:
    pairs: List[Tuple[str, int]] = []
    for token in tokens:
        cleaned = (token or "").strip()
        number = _parse_numeric_section_number(cleaned)
        if number is None:
            continue
        pairs.append((cleaned, number))
    return pairs


def reject_year_section_heading(token: str, siblings: Sequence[Tuple[str, int]], *, citation: bool) -> bool:
    """Do not accept 1800–2099 as a heading when it is a citation or off the act's sequence."""
    if not is_exact_year_section_number(token):
        return False
    if citation:
        return True
    return not year_section_fits_sequence(int(token), siblings)


def year_section_is_bogus_heading(
    token: str,
    text: str,
    previous_text: str,
    sibling_tokens: Sequence[str],
) -> bool:
    """Same citation and sequence rule the splitter uses, for one stored section."""
    citation = _section_text_is_year_citation(token, text) or _previous_tail_marks_year_citation(previous_text or "")
    return reject_year_section_heading(token, _sibling_number_pairs(sibling_tokens), citation=citation)


_WRAPPED_MARGINAL_NOTE_RE = re.compile(
    r"^[A-Z][A-Za-z]+(?:[ \t]+[A-Za-z][A-Za-z]*){0,5}\.?$"
)
_FOLLOWING_SECTION_HEAD_RE = re.compile(
    r"^\d{1,3}[A-Z]?\.\s+(?:\(\d+\)|[A-Z])"
)


def _join_wrapped_marginal_notes(text: str) -> str:
    """Join a marginal label that landed on its own line onto the heading below.

    Two-column extracts sometimes wrap the note above the number::

        Definitions.
        2. In this Act, unless the context otherwise requires

    That label belongs to section 2. A line with a comma, a digit, or no
    following ``N.`` heading is left where it is (footer continuations such as
    ``Department, Quetta.`` and the wrapped year ``2025.``).
    """
    lines = text.splitlines()
    joined: List[str] = []
    index = 0
    while index < len(lines):
        current = lines[index]
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        note = current.strip()
        following = nxt.strip()
        # ``Rate`` over ``1. Where the taxable income`` is a column header,
        # not a marginal note. Joining it hides the rate row from the
        # anchored schedule check.
        if (
            _WRAPPED_MARGINAL_NOTE_RE.match(note)
            and _FOLLOWING_SECTION_HEAD_RE.match(following)
            and not _COLUMN_HEADER_LINE_RE.match(note)
            and not _line_is_tariff_schedule_heading(following)
            and not _line_is_tariff_subrow(following)
        ):
            joined.append(f"{note} {following}")
            index += 2
            continue
        joined.append(current)
        index += 1
    return "\n".join(joined)


def _merge_heading_matches(*groups: Sequence[Match[str]]) -> List[Match[str]]:
    """Keep the earliest heading when a line-start match and a marginal match overlap."""
    ordered = sorted((match for group in groups for match in group), key=lambda match: match.start(1))
    kept: List[Match[str]] = []
    covered_until = -1
    for match in ordered:
        if match.start(1) < covered_until:
            continue
        kept.append(match)
        covered_until = match.end()
    return kept


def _usable_marginal_title(note: str) -> str:
    """Drop an OCR scrap such as ``ONS ine`` and keep ``Short title`` or ``Definitions``.

    A real marginal note is title case. A word with no lowercase letter is scanner junk.
    """
    words = re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*", note or "")
    if not words or any(word == word.upper() for word in words):
        return ""
    return (note or "").strip(" .,:;-–—")


def _heading_parts(token: str) -> Tuple[Optional[int], str]:
    match = re.fullmatch(r"(\d+)([A-Za-z]*)", (token or "").strip())
    if not match:
        return None, ""
    return int(match.group(1)), match.group(2).upper()


def _heading_continues(token: str, previous: Optional[str]) -> bool:
    """A marginal heading may be the next integer, or 2A after 2 / 2B after 2A."""
    number, letter = _heading_parts(token)
    if number is None or is_exact_year_section_number(token):
        return False
    if previous is None:
        return True
    prev_number, prev_letter = _heading_parts(previous)
    if prev_number is None:
        return True
    if letter == "" and number == prev_number + 1:
        return True
    return bool(letter) and number == prev_number and letter != prev_letter


def _cross_ref_prior(text: str, match: Match[str]) -> bool:
    return bool(_CROSS_REF_PRIOR_RE.search(text[: match.start(1)]))


def _select_heading_matches(
    text: str,
    line_start: Sequence[Match[str]],
    extras: Sequence[Match[str]],
) -> List[Match[str]]:
    """Keep line-start headings. Keep a marginal or loose heading only when it is not a cross-reference and it continues the previous number."""
    line_pos = {match.start(1) for match in line_start}
    previous: Optional[str] = None
    kept: List[Match[str]] = []
    for match in _merge_heading_matches(line_start, extras):
        token = (match.group(1) or "").strip()
        if match.start(1) in line_pos:
            kept.append(match)
            if not is_exact_year_section_number(token):
                previous = token
            continue
        if _cross_ref_prior(text, match) or not _heading_continues(token, previous):
            continue
        kept.append(match)
        previous = token
    return kept


# ``(2)`` read with the opening parenthesis as a digit: ``12) "Board" means``.
# The ``1`` is the parenthesis, not a tens place. A period heading (``12.``)
# is a real section and is not this pattern.
_DEFINITIONS_CONTEXT_RE = re.compile(
    r"(?is)\b(?:defi\w{3,16}\b|in\s+this\s+act\s*,?\s*unless\b)"
)
_PAREN_DEF_CLAUSE_RE = re.compile(r"\(\s*(\d{1,2}|[lI])\s*\)")
_OCR_QUOTED_CLAUSE_RE = re.compile(
    r"(?m)(?<![\d(])1(\d{1,2})\)[ \t]*[\"'“”‘’:]"
)
_QUOTED_DEFINITION_START_RE = re.compile(r"""(?s)^\s*["'“”‘’:]""")


def _heading_closer(text: str, match: Match[str]) -> str:
    for char in text[match.end(1) : match.end()]:
        if char in ".-):]":
            return char
    return ""


def _next_definition_clause(text: str, start: int, end: int) -> int:
    """Clause number that should follow the markers already seen in this block."""
    last = 0
    for match in _PAREN_DEF_CLAUSE_RE.finditer(text, start, end):
        raw = match.group(1)
        number = 1 if raw in "lI" else int(raw)
        if 1 <= number <= 40:
            last = max(last, number)
    for match in _OCR_QUOTED_CLAUSE_RE.finditer(text, start, end):
        number = int(match.group(1))
        if 1 <= number <= 40:
            last = max(last, number)
    return last + 1


def _is_ocr_definition_clause(text: str, match: Match[str], block_start: int) -> bool:
    """True for ``12)`` when it continues ``(1)`` as clause 2 inside definitions."""
    if _heading_closer(text, match) != ")":
        return False
    expected = _next_definition_clause(text, block_start, match.start())
    if not 1 <= expected <= 40:
        return False
    token = (match.group(1) or "").strip()
    if token != f"1{expected}":
        return False
    return bool(_QUOTED_DEFINITION_START_RE.match(text[match.end() : match.end() + 40]))


def _is_shadowed_paren_subsection(text: str, matches: Sequence[Match[str]], index: int) -> bool:
    """``12)`` after a real ``12.`` when ``(1)`` precedes it and ``(3)`` follows.

    The digit is the parenthesis. Dropping the split keeps the subsection on
    the section that contains it. A ``12)`` with no earlier ``12.`` stays a heading.
    """
    match = matches[index]
    if _heading_closer(text, match) != ")":
        return False
    token = (match.group(1) or "").strip()
    earlier_period = any(
        _heading_closer(text, prior) == "." and (prior.group(1) or "").strip() == token
        for prior in matches[:index]
    )
    if not earlier_period:
        return False
    previous_end = matches[index - 1].end() if index else 0
    expected = _next_definition_clause(text, previous_end, match.start())
    if token != f"1{expected}":
        return False
    next_period = next(
        (later for later in matches[index + 1 :] if _heading_closer(text, later) == "."),
        None,
    )
    span_end = next_period.start() if next_period is not None else len(text)
    following = re.compile(rf"\(\s*{expected + 1}\s*\)")
    return bool(following.search(text, match.end(), span_end))


def _drop_shadowed_paren_ocr_subsections(text: str, matches: Sequence[Match[str]]) -> List[Match[str]]:
    return [
        match
        for index, match in enumerate(matches)
        if not _is_shadowed_paren_subsection(text, matches, index)
    ]


def _drop_ocr_definition_clause_headings(text: str, matches: Sequence[Match[str]]) -> List[Match[str]]:
    """Keep a parenthesis-as-1 definition clause inside the section that opened it.

    The block opens on a definitions heading and closes at the next heading
    that is not itself one of these clauses. ``12. Regulation`` stays a
    section. ``12) Penalties`` outside that block stays a section.
    """
    kept: List[Match[str]] = []
    in_definitions = False
    block_start = 0
    for match in matches:
        if in_definitions and _is_ocr_definition_clause(text, match, block_start):
            continue
        kept.append(match)
        window = text[match.start() : match.start() + 320]
        if _DEFINITIONS_CONTEXT_RE.search(window):
            in_definitions = True
            block_start = match.end()
        else:
            in_definitions = False
    return kept


def _filter_year_heading_matches(text: str, matches: Sequence[Match[str]]) -> List[Match[str]]:
    """Drop split points that are year citations ('of 1860)', '2001) dt.').

    Off-sequence year headings such as ``2000.`` stay as their own chunks so the
    previous real section can be accepted first; ``_deduplicate_sections`` then
    merges that tail back. Gluing the tail on here would make a definitions
    section look like a footnote when the tail says ``inserted by``.
    """
    kept: List[Match[str]] = []
    for match in matches:
        if _year_citation_context(text, match):
            continue
        kept.append(match)
    return kept


def _append_section_text(target: Dict[str, str], extra: str) -> None:
    addition = (extra or "").strip()
    if not addition:
        return
    current = target.get("section_text") or ""
    if addition in current:
        return
    target["section_text"] = current.rstrip() + "\n" + addition

def _section_cap_for_statute(statute_name: str) -> Optional[int]:
    key = _normalize_statute_key(statute_name)
    if not key:
        return None
    for act_key, cap in ACT_SECTION_SANITY_CAPS.items():
        if act_key in key:
            return cap
    return None

def _strip_section_prefix(text: str) -> str:
    return re.sub(
        r'^\s*(?:Section|Sec\.|S\.)?\s*\d+[A-Z]?\s*[\.\-\)\:\]]\s*',
        '',
        text or "",
        flags=re.IGNORECASE,
    ).strip()

def _is_false_section_candidate(section_text: str) -> bool:
    body = _strip_section_prefix(section_text)
    if not body:
        return True
    try:
        from scraper.parsers.assembly_pdf_sections import strip_gazette_page_break_lines

        body = strip_gazette_page_break_lines(body) or body
    except Exception:
        pass
    if re.fullmatch(r'\d{1,4}', body):
        return True
    if TRAILING_AND_SECTION_RE.match(body):
        return True
    if GAZETTE_FOOTNOTE_RE.search(body) and len(body) < 400:
        return True
    if FOOTNOTE_ANNOTATION_RE.search(body):
        return True
    if SECTION_RANGE_FOOTNOTE_RE.search(body) and len(body) < 260:
        return True
    from scraper.statute_quality.distribution_list import is_distribution_addressee_section
    from scraper.statute_quality.garbled_ocr import section_is_garbled_ocr

    if is_distribution_addressee_section(section_text or ""):
        return True
    if section_is_garbled_ocr(section_text or ""):
        return True
    return False

def is_short_title_clause(name: str) -> bool:
    return bool(SHORT_TITLE_CLAUSE_RE.match(_normalize_whitespace(name or "")))


LISTING_CHROME_TITLE_RE = re.compile(r"(?i)\b(share|print|filter\s+by|acts\s+of\s+parliament)\b")


def _collapse_whitespace_flat(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return la + lb
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[lb]


def _looks_like_gazette_act_text(text: str) -> bool:
    head = (text or "")[:12000]
    return bool(re.search(r"(?i)THE\s+(?:G[A-Z]{0,2}ZET{1,2}E|CAZETTE)\s+OF\s+PAKISTAN", head)) and bool(
        re.search(r"(?i)\bact\s+no\.?", head)
    )


def _is_plural_suffix_swap(a: str, b: str) -> bool:
    al, bl = a.lower(), b.lower()
    if al == bl:
        return False
    for shorter, longer in ((al, bl), (bl, al)):
        if longer == shorter + "s" or longer == shorter + "es":
            return True
    return False


def _repair_act_title_ocr(title: str, preamble: str) -> str:
    """Replace OCR-mangled title tokens using preamble dictionary + tight fuzzy match."""
    from difflib import SequenceMatcher

    preamble_head = re.split(r"(?i)\benacted\s+as\s+follows\b", preamble or "", maxsplit=1)[0]
    pre_words = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", preamble_head)
    preamble_lower = {
        w.lower()
        for w in pre_words
        if not (w.isupper() and len(w) <= 10)
    }

    repaired: List[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z'-]*|[^A-Za-z\s]+|\s+", title):
        if not token or token.isspace():
            if token:
                repaired.append(token)
            continue
        if not re.match(r"^[A-Za-z]", token):
            repaired.append(token)
            continue
        core = token.rstrip(",")
        suffix = token[len(core) :]
        low = core.lower()
        if low in preamble_lower:
            repaired.append(core + suffix)
            continue
        best: Optional[str] = None
        best_d = 3
        if len(core) >= 6:
            for pw in pre_words:
                if pw.isupper() and len(pw) <= 10:
                    continue
                pl = pw.lower()
                if pl == low:
                    continue
                dist = _edit_distance(low, pl)
                if dist < 1 or dist > 2:
                    continue
                if SequenceMatcher(None, low, pl).ratio() < 0.84:
                    continue
                if _is_plural_suffix_swap(low, pl):
                    continue
                if dist < best_d:
                    best_d = dist
                    best = pw
        repaired.append((best or core) + suffix)
    return _collapse_whitespace_flat("".join(repaired))


def _clean_called_act_title(raw: str, preamble: Optional[str] = None, *, repair_ocr: bool = False) -> str:
    title = _collapse_whitespace_flat(raw)
    title = re.split(r"\s+\(\d+\)", title, maxsplit=1)[0].strip(" .,:;")
    year_match = re.search(r"\b((?:19|20)\d{2})\s*\.?\s*$", title)
    if year_match:
        name_part = title[: year_match.start()].strip(" .,:;")
        name_part = re.sub(r",\s*$", "", name_part).strip()
        if name_part:
            title = f"{name_part}, {year_match.group(1)}"
    if preamble and repair_ocr:
        title = _repair_act_title_ocr(title, preamble)
    return title


def looks_like_clause_fragment(name: str) -> bool:
    """Auditor 2026-10-18: about a third of names are clause fragments ('means the ... Act')."""
    lowered = _normalize_whitespace(name or "").lower()
    if not lowered:
        return False
    if lowered.startswith("means the ") or lowered.startswith("further to amend"):
        return True
    if " further to amend " in f" {lowered} ":
        return True
    return False


def looks_like_garbage_name(name: str) -> bool:
    """Binary leftovers (JPEG/PDF bytes) used as a statute title."""
    raw = name or ""
    if not raw:
        return True
    if any(sig in raw for sig in ("JFIF", "%PDF", "\x00", "\xff\xd8")):
        return True
    printable = sum(1 for ch in raw if ch.isprintable())
    return printable / max(len(raw), 1) < 0.85


def _is_plausible_act_title(name: str) -> bool:
    candidate = _normalize_whitespace(name or "")
    if not candidate or is_short_title_clause(candidate):
        return False
    if LISTING_CHROME_TITLE_RE.search(candidate):
        return False
    if looks_like_fragment_name(candidate):
        return False
    if not (6 <= len(candidate) <= 180):
        return False
    return bool(re.search(r'\b(Act|Ordinance|Order|Code|Rules|Regulations|Constitution)\b', candidate, re.IGNORECASE))


def looks_like_fragment_name(name: str) -> bool:
    """True for URL slugs, ViewerJS fragments, encoded PakistanCode ids, or filename leftovers."""
    candidate = _normalize_whitespace(name or "")
    if not candidate:
        return False
    if looks_like_clause_fragment(candidate) or looks_like_garbage_name(candidate):
        return True
    lowered = candidate.lower()
    if any(token in lowered for token in ("pdffiles", "viewerjs", "uy2f", "lgu0x", "%3d", "%2f")):
        return True
    if re.search(r"\badministrator[\s-]+act\b", lowered):
        return True
    if re.search(r"\.(pdf|html?|php|aspx)\b", lowered):
        return True
    compact = re.sub(r"\s+", "", candidate)
    if re.fullmatch(r"[A-Za-z0-9+/=_-]{20,}", compact):
        return True
    if re.search(r"[%][0-9A-Fa-f]{2}", candidate):
        return True
    if candidate.count("=") >= 2 and " " not in candidate:
        return True
    return False


def listed_title_supported_by_text(title: str, text: str) -> bool:
    """Require the listing title to appear in the document so a shared junk PDF cannot rename an act."""
    official = _normalize_whitespace(title or "")
    body = text or ""
    if not official or not body:
        return False
    if official.lower() in body.lower():
        return True
    tokens = [t for t in re.findall(r"[A-Za-z]{5,}", official) if t.lower() not in {"pakistan", "federal", "islamic", "republic", "ordinance"}]
    if not tokens:
        return False
    hits = sum(1 for token in tokens if re.search(rf"\b{re.escape(token)}\b", body, re.IGNORECASE))
    return hits >= max(1, (len(tokens) + 1) // 2)

def prefer_official_statute_title(detected_name: Optional[str], official_title: Optional[str]) -> str:
    detected = _collapse_whitespace_flat(detected_name or "") or "Unknown Statute"
    official = _collapse_whitespace_flat(official_title or "")
    if official and _is_plausible_act_title(official):
        return official
    return detected

def _deduplicate_sections(sections: List[Dict[str, str]], statute_name: Optional[str] = None) -> List[Dict[str, str]]:
    sanity_cap = _section_cap_for_statute(statute_name or "")
    siblings = _sibling_number_pairs([s.get("section_number", "") for s in sections])
    seen = set()
    out = []
    pending_year_text = ""
    pending_schedule_text = ""
    # A tariff sub-row (``4. Rolled…``) folds only when the previous raw chunk
    # was itself a schedule row. A following ``4. Other provisions.—`` is not
    # a sub-row, so the flag never reaches it.
    previous_raw_was_tariff = False
    for s in sections:
        key = s.get("section_number", "").strip().lower()
        token = (s.get("section_number") or "").strip()
        text = s.get("section_text") or ""
        # Date lines are not headings. Park the note on the previous real section
        # before the duplicate-number and footnote filters. A leading note is
        # dropped rather than pasted onto the next section, which would make that
        # section open with DD.MM.YYYY. Gluing the note on at the match would make
        # the previous section look like a footnote when the note says "substituted
        # by", and the first copy of a colliding number would hide the real heading.
        if section_text_starts_with_amendment_date(text):
            previous_raw_was_tariff = False
            if text.strip() and out:
                _append_section_text(out[-1], text)
            continue
        # HS / services rows, OCR chemical rows (``78. 38BO5``, ``03) Cuprammonium``),
        # and income-tax rate lines (``1. Where the taxable income``) are schedule
        # text. Park them on the previous real section. A leading row waits and is
        # appended after the first real heading is accepted, so Short title stays
        # the opening line and the row cannot take its number. Checked before the
        # 20-character floor so a short ``98.01 hotels`` is kept, and before the
        # duplicate-number drop so the row cannot hide a later ``32. Definitions``.
        # ``4. Rolled…`` has no HS code. It folds only after a schedule row.
        opens_tariff = section_text_starts_with_tariff_schedule_row(text)
        opens_subrow = section_text_opens_unheaded_tariff_subrow(text)
        if opens_tariff or (opens_subrow and previous_raw_was_tariff):
            previous_raw_was_tariff = True
            if text.strip() and out:
                _append_section_text(out[-1], text)
            elif text.strip():
                pending_schedule_text = (pending_schedule_text + "\n" + text).strip()
            continue
        previous_raw_was_tariff = False
        # allow duplicate numbers if text differs substantially (>50 chars diff) - keep first only for safety
        if key in seen:
            continue
        num_int = _parse_numeric_section_number(token)
        # Year citations and off-sequence years are not headings. Keep their text on the
        # previous real section instead of dropping the tail (Auditor 2026-10-26).
        # Do this before the footnote/tiny filters: the tail often says "inserted by" or
        # "dt." and must not be discarded, and it must not be glued on early enough to
        # make the previous real section look like a footnote.
        previous_text = out[-1].get("section_text") if out else ""
        if reject_year_section_heading(
            token,
            siblings,
            citation=_section_text_is_year_citation(token, text) or _previous_tail_marks_year_citation(previous_text),
        ):
            if text.strip():
                if out:
                    _append_section_text(out[-1], text)
                else:
                    pending_year_text = (pending_year_text + "\n" + text).strip()
            continue
        # filter tiny sections that are likely not real
        if len(text) < 20:
            continue
        if _is_false_section_candidate(text):
            continue
        if num_int == 0:
            continue
        # 1860A / 2100 still use the pre-existing numeric sanity drop. Exact years
        # 1800–2099 are decided above so an in-sequence heading can survive.
        if not is_exact_year_section_number(token) and num_int is not None and 1800 <= num_int <= 2100:
            continue
        if sanity_cap is not None and num_int is not None and num_int > sanity_cap:
            continue
        # filter bogus numbers like 99999
        if num_int is not None and num_int > 2000:
            # statutes rarely exceed 1000 sections except constitution
            # still keep constitution but skip crazy
            if not (num_int < 3000 and "constitution" in s.get("section_title", "").lower()):
                # but allow if number is e.g., 511
                if num_int > 5000:
                    continue
        if pending_year_text or pending_schedule_text:
            s = dict(s)
            body = s.get("section_text") or ""
            if pending_year_text:
                body = pending_year_text + "\n" + body
                pending_year_text = ""
            if pending_schedule_text:
                body = (body + "\n" + pending_schedule_text).strip()
                pending_schedule_text = ""
            s["section_text"] = body
        seen.add(key)
        out.append(s)
    return out

# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------

def _strategy_1_p_class_section(soup: BeautifulSoup, statute_name: str) -> List[Dict[str, str]]:
    """
    Strategy 1: soup.find_all p class section
    Finds <p class="section"> or any p where class contains 'section'
    """
    try:
        # Handle both string and list class attrs
        candidates = soup.find_all("p", class_=re.compile(r"section", re.I))
        if not candidates:
            # also try exact match search manually for robustness
            candidates = []
            for p in soup.find_all("p"):
                cls = p.get("class")
                if not cls:
                    continue
                cls_str = " ".join(cls) if isinstance(cls, list) else str(cls)
                if "section" in cls_str.lower():
                    candidates.append(p)
        sections = []
        for p in candidates:
            txt = p.get_text(separator="\n", strip=True)
            if not txt or len(txt) < 10:
                continue
            num, title, body = _extract_number_title_from_chunk(txt)
            if num:
                sections.append(_make_section_dict(num, title, body, statute_name))
            else:
                # if p class section but text doesn't start with digit, still consider if it contains digit elsewhere leading
                # Search inside text for Section 2 pattern
                inner_m = re.search(r'(?:Section\s+)?(\d+[A-Z]?)\s*[\.\-\)]', txt[:120], re.I)
                if inner_m:
                    sections.append(_make_section_dict(inner_m.group(1), _derive_title(txt), txt, statute_name))
        logger.debug(f"Strategy1 p.class=section found {len(sections)}")
        return _deduplicate_sections(sections, statute_name)
    except Exception as e:
        logger.warning(f"Strategy1 failed: {e}")
        return []

def _strategy_2_div_section_body(soup: BeautifulSoup, statute_name: str) -> List[Dict[str, str]]:
    """
    Strategy 2: div section-body
    Finds <div class="section-body">, <div class="statute-section"> etc.
    """
    try:
        divs = soup.find_all("div", class_=re.compile(r"section-body|section_body|statute-section|sec-body|law-section", re.I))
        if not divs:
            # fallback exact class name contains section-body
            divs = soup.find_all("div", attrs={"class": lambda x: x and "section-body" in str(x).lower() if x else False})
        sections = []
        for div in divs:
            txt = div.get_text(separator="\n", strip=True)
            if not txt or len(txt) < 15:
                continue
            # div may contain multiple sections internally, try to split inside
            # If div itself looks like single section
            num, title, body = _extract_number_title_from_chunk(txt.split("\n")[0] + "\n" + "\n".join(txt.split("\n")[1:3]) if "\n" in txt else txt)
            # Actually attempt to parse whole div as one section, using first 200 chars for number detection
            head = "\n".join(txt.split("\n")[:3])  # first few lines likely contain number
            num_head, _, _ = _extract_number_title_from_chunk(head)
            if num_head:
                # Use full div text as section_text
                sections.append(_make_section_dict(num_head, _derive_title(txt), txt, statute_name))
            else:
                # try to find all p inside div that are sections
                inner_ps = div.find_all("p")
                for p in inner_ps[:10]:  # limit
                    pt = p.get_text(strip=True)
                    n, t, b = _extract_number_title_from_chunk(pt)
                    if n:
                        sections.append(_make_section_dict(n, t, b, statute_name))
        logger.debug(f"Strategy2 div.section-body found {len(sections)}")
        return _deduplicate_sections(sections, statute_name)
    except Exception as e:
        logger.warning(f"Strategy2 failed: {e}")
        return []

def _strategy_3_p_filtered_digit_start(soup: BeautifulSoup, statute_name: str) -> List[Dict[str, str]]:
    """
    Strategy 3: p filtered digit start
    All <p> tags where text starts with digit pattern like "3. ..." or "10) ..."
    """
    try:
        all_p = soup.find_all("p")
        filtered = []
        for p in all_p:
            txt = p.get_text(separator=" ", strip=True)
            if not txt:
                continue
            if re.match(r'^\s*\d+[A-Z]?\s*[\.\-\)\:]\s+', txt):
                filtered.append(p)
        sections = []
        for p in filtered:
            txt = p.get_text(separator="\n", strip=True)
            # include next sibling p if it looks like continuation (doesn't start with digit)
            next_p = p.find_next_sibling("p")
            full_text = txt
            if next_p:
                next_text = next_p.get_text(strip=True)
                if next_text and not re.match(r'^\s*\d+[A-Z]?\s*[\.\-\)]', next_text) and len(next_text) > 30:
                    # append as continuation but not too long
                    if len(full_text) < 1000:
                        full_text = full_text + "\n\n" + next_text
            num, title, body = _extract_number_title_from_chunk(full_text)
            if num:
                sections.append(_make_section_dict(num, title, full_text, statute_name))
        logger.debug(f"Strategy3 p digit-start found {len(sections)}")
        return _deduplicate_sections(sections, statute_name)
    except Exception as e:
        logger.warning(f"Strategy3 failed: {e}")
        return []

# A period heading the line-start splitter already accepts. The lookahead
# keeps a gazette date ``12.1975`` from being read as section 12.
_PERIOD_LINE_HEADING_RE = re.compile(r"(?m)^[ \t]*(\d{1,3})[ \t]*\.(?!\d)")
_MARGINAL_DASH_RE = re.compile(r"(?:\.-|--|—|–)")
# ``Committees.--The Board`` and ``Finality of determination.-The``.
_MARGINAL_NOTE_LINE_RE = re.compile(
    r"^[ \t]*[A-Za-z][A-Za-z,'’\-]{0,40}"
    r"(?:[ \t]+[A-Za-z][A-Za-z,'’\-]{0,40}){0,14}"
    r"(?:\.-|--|—|–)"
)


def _apply_span_replacements(text: str, spans: Sequence[Tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(spans, key=lambda item: item[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _renumber_duplicate_ocr_heading(text: str) -> str:
    """A second ``35.`` whose next heading is ``37.`` is section 36.

    The 6 was read as 5, so the row collides with the real section 35 and
    ``_deduplicate_sections`` drops it. The following heading being exactly
    two ahead is the gap. A repeated number whose neighbour is the next
    integer stays a repeat.
    """
    matches = list(_PERIOD_LINE_HEADING_RE.finditer(text))
    spans: List[Tuple[int, int, str]] = []
    for index, match in enumerate(matches):
        if index == 0 or index + 1 >= len(matches):
            continue
        current = int(match.group(1))
        previous = int(matches[index - 1].group(1))
        following = int(matches[index + 1].group(1))
        if previous != current or following != current + 2 or current >= 400:
            continue
        window = text[match.end() : match.end() + 200]
        if not _MARGINAL_DASH_RE.search(window):
            continue
        spans.append((match.start(1), match.end(1), str(current + 1)))
    return _apply_span_replacements(text, spans)


def _next_content_line(text: str, after: int) -> str:
    for line in text[after:].splitlines():
        if line.strip():
            return line
    return ""


def _repair_one_heading_gap(slice_text: str, missing: int) -> str:
    """Rewrite one swallowed heading inside the text between ``N.`` and ``N+2.``.

    Readable forms win: ``22, Title`` and ``. 5.``. An unreadable stub
    (``t.`` or ``rt4.``) is used only when the next line is a marginal note.
    Junk glued to that note (``i-'Ft+. Other functions ….-``) is the same
    repair. An ordinary word, or more than one candidate, is left alone.
    """
    comma = re.compile(
        rf"(?m)^(?P<indent>[ \t]*){missing}(?P<gap>[ \t]*),(?P<rest>[ \t]+[A-Z][A-Za-z]{{3,}})"
    )
    found = comma.search(slice_text)
    if found:
        repaired = f"{found.group('indent')}{missing}.{found.group('rest')}"
        return slice_text[: found.start()] + repaired + slice_text[found.end() :]
    stray = re.compile(rf"(?m)^[ \t]*[^\w\n]{{0,6}}{missing}[ \t]*\.[ \t]*$")
    found = stray.search(slice_text)
    if found:
        return slice_text[: found.start()] + f"{missing}." + slice_text[found.end() :]

    stub_re = re.compile(r"(?m)^[ \t]*(?P<tok>\S{1,8})[ \t]*\.[ \t]*$")
    stubs: List[Match[str]] = []
    for match in stub_re.finditer(slice_text):
        token = match.group("tok")
        if token.isdigit():
            continue
        single_letter = bool(re.fullmatch(r"[A-Za-z]", token))
        has_digit = any(char.isdigit() for char in token)
        has_punct = any(not char.isalnum() for char in token)
        if not (single_letter or has_digit or has_punct):
            continue
        if not _MARGINAL_NOTE_LINE_RE.match(_next_content_line(slice_text, match.end())):
            continue
        stubs.append(match)
    if len(stubs) == 1:
        match = stubs[0]
        return slice_text[: match.start()] + f"{missing}." + slice_text[match.end() :]

    inline_re = re.compile(
        r"(?m)^(?P<prefix>[ \t]*[^\s\d][^\n]{0,16})\.[ \t]+"
        r"(?P<title>[A-Z][A-Za-z,'’\-]*(?:[ \t]+[A-Za-z][A-Za-z,'’\-]*){0,14})"
        r"(?:\.-|--|—|–)"
    )
    inlines: List[Match[str]] = []
    for match in inline_re.finditer(slice_text):
        prefix = match.group("prefix").strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z'’\-]*", prefix) or re.fullmatch(r"\d{1,3}", prefix):
            continue
        inlines.append(match)
    if len(inlines) == 1:
        match = inlines[0]
        return slice_text[: match.start()] + f"{missing}." + slice_text[match.end("prefix") + 1 :]
    return slice_text


# ``L, Short title and extent.—`` and ``3, Amendment of Act IV of 1969.—``.
# The dash (em dash, ``.--``, or ``.-``) is what keeps a prose comma out.
_OCR_COMMA_HEADING_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<n>\d{1,3}|[LIl|])(?P<sep>[ \t]*,[ \t]+)"
    r"(?P<title>[A-Z][^\n]{2,80}?)"
    r"(?=(?:[ \t]*\.[ \t]*(?:—|–|--|-){1,2}|[ \t]*(?:—|–|--)))"
)
_STRUCTURAL_COMMA_TITLE_RE = re.compile(r"(?i)^(?:amendment|short\s+title|repeal)\b")
_ENACTED_AS_FOLLOWS_RE = re.compile(r"(?i)\benacted\s+as\s+follows\b[ \t:.\-—–]*")


# ``2, In the said Ordinance`` / ``3, In the principal Act``. The comma is the
# OCR of the period. The dash form ``3, Amendment ….—`` is a different repair.
_AMENDING_CLAUSE_COMMA_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<n>\d{1,3})[ \t]*,[ \t]+"
    r"(?=In[ \t]+the[ \t]+(?:said|principal)[ \t]+(?:Act|Ordinance|Order|Code)\b)"
)


def _repair_amending_clause_comma_headings(text: str) -> str:
    """Turn ``2, In the said Ordinance`` into the period heading the splitter accepts."""
    return _AMENDING_CLAUSE_COMMA_RE.sub(r"\g<indent>\g<n>. ", text)


def _is_amendment_statute(name: str, text: str = "") -> bool:
    """An amending Act, not a principal Act that merely cites another instrument.

    The name carries ``Amendment``. A gazette whose opening lines say
    ``further to amend`` is the same class when the stored name dropped the word.
    ``Constitutional Amendment`` inside a principal wages preamble is not.
    """
    if re.search(r"(?i)\bamendments?\b", name or ""):
        return True
    head = (text or "")[:5000]
    return bool(re.search(r"(?i)\bfurther(?:\s+to)?\s+amend\b|\bto\s+further\s+amend\b", head))


# ``l.`` / ``I.`` for section 1, ``4..`` for a doubled period, ``3s`` for a
# period read as s, and ``3 In the said`` with the period missing.
_AMEND_OCR_ONE_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)[lI][ \t]*\.[ \t]+"
    r"(?=(?:\(\s*1\s*\)|This[ \t]+Act\b|In[ \t]+the\b))"
)
_AMEND_OCR_DOUBLE_DOT_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<n>\d{1,2})[ \t]*\.{2,}[ \t]*"
)
_AMEND_OCR_S_PERIOD_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<n>\d{1,2})[sS][ \t]+(?=In[ \t]+the\b)"
)
_AMEND_BARE_CLAUSE_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<n>\d{1,2})[ \t]+"
    r"(?=In[ \t]+the[ \t]+(?:said|principal|Balochistan)\b)"
)


# ``Short title and l. (1)`` — the letter is not at the start of the line.
_AMEND_OCR_ONE_INLINE_RE = re.compile(
    r"(?i)(^|[ \t])[lI][ \t]*\.[ \t]+(?=\(\s*1\s*\)|This[ \t]+Act\b)"
)
# ``15. Inthe said Ordinance`` — the space in ``In the`` was dropped.
_AMEND_INTHE_RE = re.compile(r"(?i)\bIn(?=the[ \t]+(?:said|principal|Balochistan)\b)")
# A clause number on its own line: ``l.`` or ``2``, optionally with a period.
_BARE_CLAUSE_NUMBER_LINE_RE = re.compile(
    r"^[ \t]*(?:(?P<letter>[lI])[ \t]*\.|(?P<digit>\d{1,2})[ \t]*\.?)[ \t]*$"
)
_UNNUMBERED_CLAUSE_LINE_RE = re.compile(
    r"(?ix)^(?P<pre>.{0,80}?)"
    r"(?:\(\s*\d+\s*\)[ \t]*)?"
    r"(?:this[ \t]+act,?[ \t]+may[ \t]+be[ \t]+called"
    r"|in[ \t]+the[ \t]+(?:said|principal|balochistan)\b)"
)
_OWN_CLAUSE_NUMBER_RE = re.compile(r"(?i)(?:^|\s)(?:\d{1,2}|[lI])[ \t]*\.(?!\d)")


def _line_is_unnumbered_amendment_clause(line: str) -> bool:
    """A clause formula that does not already carry ``N.`` or ``l.``."""
    match = _UNNUMBERED_CLAUSE_LINE_RE.match((line or "").strip())
    if not match:
        return False
    pre = match.group("pre") or ""
    if re.search(r"\d", pre) or _OWN_CLAUSE_NUMBER_RE.search(pre):
        return False
    return True


def _join_own_line_amendment_clause_numbers(text: str) -> str:
    """Pull ``l.`` / ``2`` down onto the clause line they were split from.

    A page number above a line that already has its clause number stays put.
    One blank line between the number and the clause is the page break.
    """
    lines = text.split("\n")
    joined: List[str] = []
    index = 0
    while index < len(lines):
        match = _BARE_CLAUSE_NUMBER_LINE_RE.match(lines[index])
        if match is None:
            joined.append(lines[index])
            index += 1
            continue
        number = "1" if match.group("letter") else match.group("digit")
        follow = index + 1
        if follow < len(lines) and not lines[follow].strip() and follow + 1 < len(lines):
            follow += 1
        if follow < len(lines) and _line_is_unnumbered_amendment_clause(lines[follow]):
            indent = re.match(r"^[ \t]*", lines[index]).group(0)
            joined.append(f"{indent}{number}. {lines[follow].strip()}")
            index = follow + 1
            continue
        joined.append(lines[index])
        index += 1
    return "\n".join(joined)


def _repair_amendment_ocr_clause_heads(text: str) -> str:
    """Normalise OCR clause numbers on an amendment Act only.

    ``l. (1) This Act`` and a lone ``l.`` above ``(1) This Act`` become ``1.``.
    ``2`` above ``In the said``, ``3 In the said`` and ``4.. In the said``
    become the period heading the clause splitter accepts. ``Inthe said``
    regains its space. A principal Act is not passed through here.
    """
    text = _AMEND_OCR_ONE_INLINE_RE.sub(lambda match: f"{match.group(1)}1. ", text)
    text = _AMEND_OCR_ONE_RE.sub(r"\g<indent>1. ", text)
    text = _AMEND_OCR_DOUBLE_DOT_RE.sub(r"\g<indent>\g<n>. ", text)
    text = _AMEND_OCR_S_PERIOD_RE.sub(r"\g<indent>\g<n>. ", text)
    text = _AMEND_BARE_CLAUSE_RE.sub(r"\g<indent>\g<n>. ", text)
    text = _AMEND_INTHE_RE.sub("In ", text)
    text = _repair_amending_clause_comma_headings(text)
    return _join_own_line_amendment_clause_numbers(text)


# A two-letter stub such as ``Sh`` above ``ort title`` is one word. ``No`` /
# ``In`` / ``Be`` above the next line are real words and stay separate.
_COLUMN_SPLIT_STOP_RE = re.compile(
    r"(?i)^(?:am|an|as|at|be|by|do|go|he|if|in|is|it|me|my|no|of|on|or|so|to|up|we|and|the|for|not|any|all|may|act|its|his|her|was|are|but)$"
)


def _rejoin_column_split_heading(text: str) -> str:
    """Join ``Sh i }?`` / ``ort title`` back into ``Short title``.

    The stub is two or three letters, not a word on its own, and the rest of
    its line is column junk. The next line continues in lowercase.
    """
    lines = text.split("\n")
    joined: List[str] = []
    index = 0
    while index < len(lines):
        match = re.match(r"^([A-Z][a-z]{1,2})\b(.*)$", lines[index])
        if match and index + 1 < len(lines):
            fragment, rest = match.group(1), match.group(2)
            continuation = re.match(r"^([a-z]{2,})(.*)$", lines[index + 1])
            if (
                continuation
                and not _COLUMN_SPLIT_STOP_RE.match(fragment)
                and not re.search(r"[A-Za-z]{3,}", rest)
            ):
                joined.append(fragment + continuation.group(1) + continuation.group(2))
                index += 2
                continue
        joined.append(lines[index])
        index += 1
    return "\n".join(joined)


def _strip_ocr_column_pipes(text: str) -> str:
    """Drop scanner column rules and join a word the columns split.

    Payment of Wages OCR prints ``|`` down the margin, breaks ``Pay-`` /
    ``ment``, and prints ``Sh`` above ``ort title``. A line-start
    ``|, Short title`` is the OCR of section 1 and stays so the comma-heading
    repair can read it.
    """
    if not text:
        return text
    if "|" in text or re.search(r"-\n[a-z]", text):
        kept: List[str] = []
        for line in text.split("\n"):
            if re.match(r"[ \t]*\|[ \t]*,", line):
                kept.append(line)
                continue
            cleaned = re.sub(r"^[ \t]*\|[ \t]*", "", line)
            cleaned = re.sub(r"[ \t]*\|[ \t]*$", "", cleaned)
            cleaned = cleaned.replace("|", "")
            kept.append(cleaned)
        text = re.sub(r"-\n(?=[a-z])", "", "\n".join(kept))
    return _rejoin_column_split_heading(text)


_AMENDMENT_CLAUSE_OPEN_RE = re.compile(
    r"(?im)^(?P<prefix>[^\n]{0,80}?)"
    r"[ \t_.:\-]*"
    r"(?P<op>this[ \t]+act,?[ \t]+may[ \t]+be[ \t]+called"
    r"|in[ \t]+the[ \t]+(?:said|principal)\b"
    r"|in[ \t]+the[ \t]+balochistan\b)"
)
_MARGIN_BODY_START_RE = re.compile(r"(?i)^\s*(?:section|sec\.|s\.)\s+\d")


_AMENDMENT_MARGINAL_LABEL_RE = re.compile(
    r"(?i)^(?:amendment|insertion|omission|substitution|addition|short\s+title|repeal)\b"
)


def _amendment_clause_prefix_ok(prefix: str) -> bool:
    """A clause number or a short marginal label may sit in front of the opener.

    ``In the said`` alone is the sentence under a margin note, not a new
    clause. ``section 9 of W.P. Ordinance`` on the same line is that note.
    ``Amendment of ve`` is the note whose number OCR lost. A sentence that
    already says ``shall`` or opens a quotation is the inserted text.
    """
    token = (prefix or "").strip()
    if not token:
        return False
    if len(token) > 80 or len(token.split()) > 12:
        return False
    if token[0] in "\"'“”‘’":
        return False
    if re.search(r"(?i)\b(?:shall|means|provided|whereas|substituted|inserted|omitted)\b", token):
        return False
    if _PRINCIPAL_MARGIN_NOTE_LEAD_RE.match(token):
        return False
    if re.search(r"\d", token):
        return True
    return bool(_AMENDMENT_MARGINAL_LABEL_RE.match(token))


def _split_amendment_clauses(text: str, statute_name: str) -> List[Dict[str, str]]:
    """Number an amendment Act 1..N from its own clauses.

    The short title (``This Act may be called``) is section 1, including
    ``It shall come into force``. Each later ``In the said`` / ``In the
    principal`` / ``In the Balochistan`` line is the next clause, even when
    OCR printed ``3 In``, ``4..``, ``ve`` or ``(2)``. The principal's section
    number inside the margin, and a substituted ``4. The Board shall``, are
    not clauses.
    """
    from scraper.statute_quality.distribution_list import strip_distribution_list_footer

    prepared = _repair_amendment_ocr_clause_heads(text)
    matches: List[Match[str]] = []
    for match in _AMENDMENT_CLAUSE_OPEN_RE.finditer(prepared):
        if not _amendment_clause_prefix_ok(match.group("prefix") or ""):
            continue
        op = match.group("op") or ""
        if re.match(r"(?i)this[ \t]+act\b", op) and any(
            re.match(r"(?i)this[ \t]+act\b", item.group("op") or "") for item in matches
        ):
            continue
        matches.append(match)
    if len(matches) < 2:
        return []
    sections: List[Dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prepared)
        chunk = prepared[match.start() : end].strip()
        if index + 1 == len(matches):
            chunk = strip_distribution_list_footer(chunk).strip()
        if len(chunk) < 15:
            continue
        sections.append(_make_section_dict(str(len(sections) + 1), "", chunk, statute_name))
    return sections


def _split_result_is_bad(sections: List[Dict[str, str]]) -> bool:
    """A collapse, a missing short title, a margin-note number, or a gap."""
    if len(sections) < 2:
        return True
    numbers = [str(section.get("section_number") or "").strip() for section in sections]
    if not numbers or numbers[0] != "1":
        return True
    if any(_MARGIN_BODY_START_RE.match(section.get("section_text") or "") for section in sections):
        return True
    if any(len(section.get("section_text") or "") >= 5000 for section in sections):
        return True
    try:
        ints = [int(number) for number in numbers]
    except ValueError:
        return True
    return ints != list(range(1, len(ints) + 1))


def _prefer_amendment_split(
    generic: List[Dict[str, str]],
    clauses: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Use the clause split when the ordinary split collapsed or mis-numbered.

    A clean ``1. / 2. Amendment / 3. Repeal`` result stays. The clause split
    does not know ``Repeal`` and must not replace that result with a shorter one.
    """
    if not clauses or _split_result_is_bad(clauses):
        return generic
    if _split_result_is_bad(generic) or len(clauses) > len(generic):
        return clauses
    return generic


def _repair_ocr_comma_section_headings(text: str) -> str:
    """Turn an OCR comma heading into a period heading the splitter already accepts.

    ``L``, ``I``, ``l`` and ``|`` map to 1 only when they are the first heading.
    A later letter stays put. When the printed number repeats, goes backwards,
    or jumps by exactly two, and the title starts with Amendment, Short title
    or Repeal, the heading takes the next integer. Finance Act 1985 reads
    section 2 as ``3,`` and then prints ``3,`` again; the jump of two is that
    misread. A wider gap keeps the printed number. ``12, Penalties.—`` stays 12.
    """
    events: List[Tuple[str, int, Match[str]]] = []
    for match in _OCR_COMMA_HEADING_RE.finditer(text):
        events.append(("comma", match.start(), match))
    for match in _PERIOD_LINE_HEADING_RE.finditer(text):
        events.append(("period", match.start(), match))
    events.sort(key=lambda item: (item[1], 0 if item[0] == "period" else 1))
    last: Optional[int] = None
    spans: List[Tuple[int, int, str]] = []
    for kind, _start, match in events:
        if kind == "period":
            line_end = text.find("\n", match.start())
            line = text[match.start() : line_end if line_end != -1 else None]
            stripped = line.strip()
            if (
                _line_is_tariff_schedule_heading(stripped)
                or _line_is_tariff_subrow(stripped)
                or section_text_starts_with_amendment_date(stripped)
            ):
                continue
            last = int(match.group(1))
            continue
        raw = match.group("n")
        title = (match.group("title") or "").strip()
        if raw.isdigit():
            number = int(raw)
        elif last is None:
            number = 1
        else:
            continue
        if last is not None and _STRUCTURAL_COMMA_TITLE_RE.match(title) and (
            number <= last or number == last + 2
        ):
            number = last + 1
        last = number
        spans.append((match.start("n"), match.end("sep"), f"{number}. "))
    return _apply_span_replacements(text, spans)


def _operative_text_before_first_heading(text: str, first_start: int) -> str:
    """Words after ``enacted as follows`` and before the first heading match."""
    head = text[:first_start]
    enacted = _ENACTED_AS_FOLLOWS_RE.search(head)
    if not enacted:
        return ""
    return head[enacted.end() :].strip()


_MAY_BE_CALLED_RE = re.compile(r"(?i)\bthis[ \t]+act,?[ \t]+may[ \t]+be[ \t]+called\b")


def _short_title_before_first_heading(text: str, first_start: int) -> str:
    """Short title that never became a heading, even without ``enacted as follows``.

    OCR leaves ``1. (1)`` in the middle of the marginal-note line, so the
    heading scan starts at section 2. The call formula and the commencement
    lines under it are still section 1. The recital above that formula stays out.
    """
    head = text[:first_start]
    match = _MAY_BE_CALLED_RE.search(head)
    if not match:
        return ""
    line_start = head.rfind("\n", 0, match.start()) + 1
    return head[line_start:].strip()


def _repair_ocr_swallowed_section_headings(text: str) -> str:
    """Restore headings that OCR hid between two period headings one number apart.

    Runs of ``N.`` with no gap are unchanged. ``5l . Power`` has no marginal
    dash of the ``.-`` / ``--`` form, so it is not promoted over a later
    ``51. Repeal``.
    """
    text = _renumber_duplicate_ocr_heading(text)
    matches = list(_PERIOD_LINE_HEADING_RE.finditer(text))
    spans: List[Tuple[int, int, str]] = []
    for index in range(len(matches) - 1):
        current = int(matches[index].group(1))
        following = int(matches[index + 1].group(1))
        if following != current + 2 or current >= 400:
            continue
        start = matches[index].end()
        end = matches[index + 1].start()
        repaired = _repair_one_heading_gap(text[start:end], current + 1)
        if repaired != text[start:end]:
            spans.append((start, end, repaired))
    return _apply_span_replacements(text, spans)


def _strategy_4_resplit_digit(text: str, statute_name: str) -> List[Dict[str, str]]:
    """
    Strategy 4: re.split digit
    Plain-text split using pattern for digit-start sections.
    Handles cases where input is raw text (not HTML) from pdfplumber/pytesseract.
    """
    try:
        plain = BeautifulSoup(text, "html.parser").get_text(separator="\n") if _is_html(text) else text
        plain = _join_wrapped_marginal_notes(_normalize_whitespace(plain))
        if _is_amendment_statute(statute_name, plain):
            plain = _repair_amendment_ocr_clause_heads(plain)
        plain = _repair_ocr_swallowed_section_headings(
            _repair_ocr_comma_section_headings(
                _repair_amending_clause_comma_headings(plain)
            )
        )
        # Line-start headings ("2. In this Act") plus marginal-note headings
        # ("Short title, 1. (1) This Act"). The second form is how a two-column
        # gazette extract places every section except the one whose note wrapped.
        line_start = list(_LINE_START_SECTION_RE.finditer(plain))
        marginal = list(_MARGINAL_SECTION_HEAD_RE.finditer(plain))
        # Marginal headings are not a Baloch-only path: the same matcher used to
        # turn "Section 5. The" into a section in any plain-text act. Cross-reference
        # words and the previous+1 rule apply here and to the loose fallback.
        matches = _select_heading_matches(plain, line_start, marginal)
        if len(matches) < 2:
            loose = list(_LOOSE_SECTION_RE.finditer(plain))
            matches = _select_heading_matches(plain, line_start, [*marginal, *loose])
        # A line opening ``11.10.2018`` matches the day as a heading. Leave the
        # match in place so the note stays its own chunk. ``_deduplicate_sections``
        # refuses that chunk as a heading and parks the text on the neighbouring
        # real section. Dropping the match here would fold "substituted by" into
        # the previous section and the footnote filter would discard that section.
        # A year token on its own line (``2018.``) is the separate 1800–2099 class.
        matches = _filter_year_heading_matches(plain, matches)
        matches = _drop_ocr_definition_clause_headings(plain, matches)
        matches = _drop_shadowed_paren_ocr_subsections(plain, matches)
        sections = []
        if len(matches) >= 2:
            first_end = matches[1].start() if len(matches) > 1 else len(plain)
            first_chunk = plain[matches[0].start() : first_end]
            first_token = (matches[0].group(1) or "").strip()
            first_num = int(first_token) if first_token.isdigit() else None
            prefix = _operative_text_before_first_heading(plain, matches[0].start())
            # The real short-title line was not a heading, or the first heading
            # is a schedule row. The words after ``enacted as follows`` are
            # section text. The recital before that formula stays dropped.
            if not prefix and first_num is not None and first_num > 1:
                prefix = _short_title_before_first_heading(plain, matches[0].start())
            if prefix and len(prefix) >= 20 and (
                (first_num is not None and first_num > 1)
                or section_text_starts_with_tariff_schedule_row(first_chunk)
                or section_text_opens_unheaded_tariff_subrow(first_chunk)
            ):
                sections.append(_make_section_dict("1", "", prefix, statute_name))
            for i, m in enumerate(matches):
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(plain)
                chunk = plain[start:end].strip()
                # A short HS row (``98.01 hotels``) is still schedule text. The
                # 15-character floor would drop it between two longer headings.
                if len(chunk) < 15 and not section_text_starts_with_tariff_schedule_row(chunk):
                    continue
                num, title, body = _extract_number_title_from_chunk(chunk)
                if num:
                    sections.append(_make_section_dict(num, title, chunk, statute_name))
            logger.debug(f"Strategy4 re.split digit found {len(sections)}")
            return _deduplicate_sections(sections, statute_name)
        else:
            # fallback naive split by double newline and filter
            chunks = re.split(r'\n{2,}', plain)
            pending_margin = ""
            for chunk in chunks:
                # "section 9 of W.P. Ordinance…" is the margin note. Keep its
                # words on the neighbouring real section. Do not number it 9.
                if _chunk_is_principal_margin_note(chunk):
                    if sections:
                        _append_section_text(sections[-1], chunk)
                    else:
                        pending_margin = (pending_margin + "\n" + chunk).strip()
                    continue
                if pending_margin:
                    chunk = pending_margin + "\n" + chunk
                    pending_margin = ""
                num, title, body = _extract_number_title_from_chunk(chunk)
                if num:
                    sections.append(_make_section_dict(num, title, chunk, statute_name))
            if pending_margin and sections:
                _append_section_text(sections[-1], pending_margin)
            logger.debug(f"Strategy4 fallback chunks found {len(sections)}")
            return _deduplicate_sections(sections, statute_name)
    except Exception as e:
        logger.warning(f"Strategy4 failed: {e}")
        return []

def _strategy_5_resplit_section_keyword(text: str, statute_name: str) -> List[Dict[str, str]]:
    """
    Strategy 5: re.split Section
    Split using keyword "Section X" - common in Pakistani bare acts.
    """
    try:
        plain = BeautifulSoup(text, "html.parser").get_text(separator="\n") if _is_html(text) else text
        plain = _normalize_whitespace(plain)
        # Pattern that matches Section 2, Sec. 2, S. 2 etc.
        pattern = re.compile(r'(?:\n|^)\s*(?:Section|Sec\.|S\.)\s+(\d+[A-Z]?)\s*[:\.\-\)]*\s*', re.IGNORECASE | re.MULTILINE)
        # "section 9 of W.P. Ordinance" matches this pattern and would store
        # the principal's number. "Section 2. Definitions" does not cite an Act.
        matches = [
            match
            for match in pattern.finditer(plain)
            if not _match_is_principal_section_margin_note(plain, match.start())
        ]
        sections = []
        if len(matches) >= 1:
            for i, m in enumerate(matches):
                sec_num = m.group(1)
                start_content = m.end()
                end_content = matches[i + 1].start() if i + 1 < len(matches) else len(plain)
                chunk = plain[m.start():end_content].strip()
                if len(chunk) < 15:
                    continue
                # Derive title from content after marker
                content_after = plain[start_content:end_content].strip()
                title = _derive_title(content_after)
                sections.append(_make_section_dict(sec_num, title, chunk, statute_name))
        if sections:
            logger.debug(f"Strategy5 re.split Section found {len(sections)}")
            return _deduplicate_sections(sections, statute_name)
        # If no "Section" keyword, try Article for Constitution
        article_pattern = re.compile(r'(?:\n|^)\s*(?:Article|Art\.)\s+(\d+[A-Z]?)\s*[:\.\-\)]*\s*', re.IGNORECASE | re.MULTILINE)
        matches = list(article_pattern.finditer(plain))
        if matches:
            for i, m in enumerate(matches):
                sec_num = m.group(1)
                start_content = m.end()
                end_content = matches[i + 1].start() if i + 1 < len(matches) else len(plain)
                chunk = plain[m.start():end_content].strip()
                if len(chunk) < 15:
                    continue
                content_after = plain[start_content:end_content].strip()
                title = _derive_title(content_after)
                # For constitution, keep Article prefix in number
                sections.append(_make_section_dict(f"Article {sec_num}" if not sec_num.lower().startswith("article") else sec_num, title, chunk, statute_name))
            logger.debug(f"Strategy5 Article fallback found {len(sections)}")
            return _deduplicate_sections(sections, statute_name)
        return []
    except Exception as e:
        logger.warning(f"Strategy5 failed: {e}")
        return []

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _split_into_sections_strategies(text: str, statute_name: Optional[str] = None) -> List[Dict[str, str]]:
    """
    Split statute text into sections using 5 progressive strategies.

    Args:
        text: Raw HTML or plain text of statute (from pdfplumber / playwright / httpx)
        statute_name: Statute short/long name for fallback title generation. Can be None.

    Returns:
        List of dicts: [{section_number, section_title, section_text}, ...]
        Ordered by appearance. Empty list if input empty.

    Strategy order:
        1) soup.find_all p class section
        2) div section-body
        3) p filtered digit start
        4) re.split digit pattern
        5) re.split Section keyword (and Article fallback)

    Each strategy tries to produce >=2 sections. First successful strategy wins.
    If all fail, fallback to single chunk as section 1.
    """
    if not text or not isinstance(text, str):
        logger.warning("split_into_sections received empty/non-str input")
        return []
    from scraper.statute_quality.distribution_list import strip_distribution_list_footer

    text = strip_distribution_list_footer(text)
    # Column rules in a scanned principal Act ("|") are not statute text.
    # Amendment clause repair stays inside the amendment path.
    if not _is_html(text):
        text = _strip_ocr_column_pipes(text)
    if len(text.strip()) < 10:
        return []

    statute_name = (statute_name or "Unidentified Statute").strip()
    soup = None
    if _is_html(text):
        try:
            soup = BeautifulSoup(text, "html.parser")
        except Exception as e:
            logger.warning(f"BeautifulSoup parse failed: {e}, treating as plain text")
            soup = None

    # Strategy 1
    if soup is not None:
        result = _strategy_1_p_class_section(soup, statute_name)
        if len(result) >= 2:
            logger.info(f"split_into_sections: Strategy1 succeeded with {len(result)} sections for {statute_name}")
            return result
        if len(result) == 1 and len(result[0].get("section_text", "")) > 500:
            # If only 1 but large body, still may be valid single-section statute (e.g., small ordinance)
            # Continue trying for more splits
            pass

    # Strategy 2
    if soup is not None:
        result = _strategy_2_div_section_body(soup, statute_name)
        if len(result) >= 2:
            logger.info(f"split_into_sections: Strategy2 succeeded with {len(result)} sections for {statute_name}")
            return result

    # Strategy 3
    if soup is not None:
        result = _strategy_3_p_filtered_digit_start(soup, statute_name)
        if len(result) >= 2:
            logger.info(f"split_into_sections: Strategy3 succeeded with {len(result)} sections for {statute_name}")
            return result

    # Strategy 4 - works on both HTML and plain
    result = _strategy_4_resplit_digit(text, statute_name)
    if len(result) >= 2:
        logger.info(f"split_into_sections: Strategy4 succeeded with {len(result)} sections for {statute_name}")
        return result

    # Strategy 5 - keyword based
    result = _strategy_5_resplit_section_keyword(text, statute_name)
    if len(result) >= 2:
        logger.info(f"split_into_sections: Strategy5 succeeded with {len(result)} sections for {statute_name}")
        return result
    if len(result) == 1:
        # If strategy 5 found single but with decent length, return it as better than fallback
        if len(result[0].get("section_text", "")) > 50:
            logger.info(f"split_into_sections: Strategy5 single section used for {statute_name}")
            return result

    # Fallback: if any previous strategy returned 1 section and that section is meaningful, use best effort largest
    # Collect all attempts and pick largest
    candidates = []
    if soup is not None:
        candidates.extend(_strategy_1_p_class_section(soup, statute_name))
        candidates.extend(_strategy_2_div_section_body(soup, statute_name))
        candidates.extend(_strategy_3_p_filtered_digit_start(soup, statute_name))
    candidates.extend(_strategy_4_resplit_digit(text, statute_name))
    candidates.extend(_strategy_5_resplit_section_keyword(text, statute_name))
    if candidates:
        deduped = _deduplicate_sections(candidates, statute_name)
        if deduped:
            # sort by numeric section number if possible
            def sort_key(s):
                m = re.search(r'\d+', s.get("section_number", "0"))
                return int(m.group()) if m else 9999
            try:
                deduped_sorted = sorted(deduped, key=sort_key)
            except Exception:
                deduped_sorted = deduped
            logger.info(f"split_into_sections: Fallback merged {len(deduped_sorted)} sections for {statute_name}")
            if len(deduped_sorted) >= 1:
                return deduped_sorted

    # Ultimate fallback: whole text as one section
    plain_text = BeautifulSoup(text, "html.parser").get_text(separator="\n") if _is_html(text) else text
    plain_text = _clean_section_text(plain_text)
    if plain_text:
        logger.warning(f"split_into_sections: All strategies failed for {statute_name}, returning single chunk fallback")
        return [
            {
                "section_number": "1",
                "section_title": statute_name[:500],
                "section_text": plain_text[:50000],  # cap to avoid massive embedding payload
            }
        ]
    return []


def split_into_sections(text: str, statute_name: Optional[str] = None) -> List[Dict[str, str]]:
    """Split a statute, preferring an amendment Act's own clauses when the
    ordinary heading scan collapsed or numbered the principal's sections.
    """
    sections = _split_into_sections_strategies(text, statute_name)
    if not isinstance(text, str) or not text or _is_html(text):
        return sections
    name = (statute_name or "Unidentified Statute").strip()
    if not _is_amendment_statute(name, text):
        return sections
    from scraper.statute_quality.distribution_list import strip_distribution_list_footer

    prepared = _strip_ocr_column_pipes(strip_distribution_list_footer(text))
    return _prefer_amendment_split(sections, _split_amendment_clauses(prepared, name))


def _detect_statute_name_from_text_or_url(text: str, url: Optional[str] = None) -> str:
    """
    Detect canonical statute name from text and URL.

    Args:
        text: Raw HTML or plain text (first few KB used)
        url: Source URL (may contain slug)

    Returns:
        Canonical statute name string, e.g. "Pakistan Penal Code, 1860"
        Never empty - returns "Unknown Statute" if undetectable.

    Logic:
        1) URL slug mapping
        2) HTML <title> / <h1> inspection
        3) Search known statutes list in first 6000 chars
        4) Generic Act/Ordinance/Order pattern with year
        5) First line heuristic containing Act/Code/Ordinance
        6) Fallback slug humanization
    """
    try:
        text_to_search = ""
        if text:
            if _is_html(text):
                try:
                    soup = BeautifulSoup(text[:20000], "html.parser")
                    # Title tag
                    if soup.title and soup.title.get_text(strip=True):
                        title_text = soup.title.get_text(strip=True)
                        # Check title against known map
                        for key, canonical in KNOWN_STATUTES_MAP.items():
                            if key.lower() in title_text.lower():
                                logger.debug(f"detect_statute_name: matched title '{key}' -> {canonical}")
                                return canonical
                        # If title looks like Act name, return cleaned title
                        if re.search(r'(Act|Ordinance|Code|Constitution)', title_text, re.I):
                            cleaned = re.sub(r'\s+\|\s+-\s+.*$', '', title_text).strip()
                            cleaned = re.sub(r'^.*?\b((?:The\s+)?[A-Z].*(?:Act|Ordinance|Code).*$)', r'\1', cleaned)
                            if len(cleaned) > 5 and len(cleaned) < 150:
                                return _normalize_whitespace(cleaned)
                    # H1 inspection
                    h1 = soup.find(["h1", "h2"])
                    if h1:
                        h1_text = h1.get_text(separator=" ", strip=True)[:300]
                        for key, canonical in KNOWN_STATUTES_MAP.items():
                            if key.lower() in h1_text.lower() and len(key) > 2:
                                logger.debug(f"detect_statute_name: matched h1 '{key}' -> {canonical}")
                                return canonical
                        if re.search(r'(Act|Ordinance|Code|Constitution)', h1_text, re.I):
                            # plausible statute name
                            if len(h1_text) < 150:
                                return _normalize_whitespace(h1_text)
                    text_to_search = soup.get_text(separator="\n", strip=True)[:6000]
                except Exception as e:
                    logger.warning(f"detect_statute_name HTML parse failed: {e}")
                    text_to_search = text[:6000]
            else:
                text_to_search = text[:6000]
        else:
            text_to_search = ""

        # 1) URL slug mapping - most reliable for punjablaws, pakistanlawsite etc.
        if url:
            try:
                url_lower = url.lower()
                parsed = urlparse(url)
                path = unquote(parsed.path.lower())
                query = unquote(parsed.query.lower())
                combined_url = path + " " + query + " " + url_lower

                # Exact slug map check
                for slug, canonical in URL_SLUG_MAP.items():
                    if slug in combined_url:
                        logger.debug(f"detect_statute_name: URL slug '{slug}' -> {canonical}")
                        return canonical

                # Known map in URL
                for key, canonical in KNOWN_STATUTES_MAP.items():
                    # avoid tiny keys like ppc matching random url part? allow but require word boundary
                    if len(key) <= 3:
                        # check with boundaries for short keys
                        if re.search(rf'\b{re.escape(key)}\b', combined_url):
                            # extra validation: avoid false positives like "ppc" in "https"
                            if key == "ppc" and ("ppc" in path or "penal" in path):
                                return canonical
                            if key not in ("ppc", "cpc", "crpc", "qso", "ata", "peca"):
                                continue
                            # for short codes, only return if path contains act identifier
                            if key in ("ppc", "crpc", "cpc"):
                                return canonical
                    else:
                        if key in combined_url:
                            return canonical
            except Exception as e:
                logger.warning(f"detect_statute_name URL parse failed for {url}: {e}")

        called_early = SHALL_BE_CALLED_ACT_RE.search(text_to_search)
        if called_early:
            title = _clean_called_act_title(
                called_early.group("title"),
                preamble=text_to_search,
                repair_ocr=_looks_like_gazette_act_text(text_to_search),
            )
            if title and not is_short_title_clause(title) and _is_plausible_act_title(title):
                return title

        # 2) Search known statutes in text_to_search
        text_lower = text_to_search.lower()
        for key, canonical in KNOWN_STATUTES_MAP.items():
            # prioritize longer keys to avoid false "act" match
            if len(key) < 4:
                continue
            if key in text_lower:
                logger.debug(f"detect_statute_name: text match '{key}' -> {canonical}")
                return canonical
        # Short codes with word boundaries
        for key in ["ppc", "crpc", "c.p.c", "cpc", "qso", "peca"]:
            if re.search(rf'\b{re.escape(key)}\b', text_lower):
                canonical = KNOWN_STATUTES_MAP.get(key)
                if canonical:
                    # validate surrounding context contains section/code language
                    if re.search(r'(section|act|code|chapter)', text_lower):
                        return canonical

        # 3) Generic Act pattern with year - high confidence
        if text_to_search:
            called = SHALL_BE_CALLED_ACT_RE.search(text_to_search)
            if called:
                title = _clean_called_act_title(
                    called.group("title"),
                    preamble=text_to_search,
                    repair_ocr=_looks_like_gazette_act_text(text_to_search),
                )
                if title and not is_short_title_clause(title) and _is_plausible_act_title(title):
                    return title

            match_year = ACT_WITH_YEAR_PATTERN.search(text_to_search)
            if match_year:
                candidate = _normalize_whitespace(match_year.group(1))
                candidate = candidate.strip(" .,:;")
                # Clean up leading "The" duplication and truncate
                if len(candidate) > 10 and len(candidate) < 150:
                    # Normalize capitalization: Title Case but preserve acronyms
                    # Avoid lowercasing fully, just return as found cleaned
                    logger.debug(f"detect_statute_name: generic year pattern -> {candidate}")
                    return candidate

            # 4) Broader Act pattern (without year) - lower confidence but still useful
            # Take first occurrence in first 2000 chars that looks like a statute name
            first_chunk = text_to_search[:2000]
            generic_matches = list(GENERIC_ACT_PATTERN.finditer(first_chunk))
            for gm in generic_matches:
                cand = _normalize_whitespace(gm.group(1))
                cand = cand.strip(" .,:;")
                # Filter out false positives like "This Act" or "The Act"
                if cand.lower() in ("this act", "the act", "said act", "an act", "the code"):
                    continue
                if len(cand) < 8 or len(cand) > 150:
                    continue
                # Must contain Act/Ordinance/Code/Constitution word and start with capital
                if not re.search(r'(Act|Ordinance|Order|Code|Rules|Constitution)', cand):
                    continue
                # Prefer if contains Pakistan, Punjab, Sindh, Federal etc.
                if re.search(r'(Pakistan|Punjab|Sindh|Khyber|Balochistan|Federal|Islamic)', cand, re.I):
                    logger.debug(f"detect_statute_name: generic pattern priority -> {cand}")
                    return cand
            # Return first generic match if exists
            if generic_matches:
                for gm in generic_matches:
                    cand = _normalize_whitespace(gm.group(1))
                    cand = cand.strip(" .,:;")
                    if cand.lower() in ("this act", "the act", "said act"):
                        continue
                    if 8 <= len(cand) <= 150:
                        return cand

            # 5) First line heuristic - scan first 20 lines for line containing Act/Ordinance
            lines = [ln.strip() for ln in text_to_search.split("\n") if ln.strip()]
            for ln in lines[:20]:
                if len(ln) > 150:
                    continue
                if re.search(r'\b(Act|Ordinance|Order|Constitution)\b', ln, re.I) and len(ln) > 10:
                    # Strip leading numbers like "1. Pakistan Penal Code"
                    ln_clean = re.sub(r'^\s*\d+[\.\)]\s*', '', ln)
                    ln_clean = _normalize_whitespace(ln_clean)
                    if len(ln_clean) >= 10:
                        return ln_clean[:150]

        # 6) Fallback: humanize URL slug — never for pdffiles / ViewerJS / encoded ids.
        if url and not looks_like_fragment_name(url) and "/pdffiles/" not in url.lower() and "viewerjs" not in url.lower():
            try:
                parsed = urlparse(url)
                slug = parsed.path.strip("/").split("/")[-1] if parsed.path else ""
                slug = unquote(slug)
                slug = re.sub(r'\.(html|php|aspx|htm)$', '', slug, flags=re.I)
                slug = slug.replace("-", " ").replace("_", " ").strip()
                slug = re.sub(r'\s+', ' ', slug)
                # Remove numeric IDs
                slug = re.sub(r'\b\d{4,}\b', '', slug).strip()
                if slug and len(slug) > 5:
                    # Title case
                    titled = slug.title()
                    # If contains act, code, ordinance etc, return it
                    if re.search(r'(Act|Code|Ordinance|Order|Constitution)', titled, re.I):
                        # Try to append year if year present in URL
                        year_match = re.search(r'(\d{4})', url)
                        if year_match and year_match.group(1) not in titled:
                            titled = f"{titled}, {year_match.group(1)}"
                        return _normalize_whitespace(titled)[:150]
                    # If slug is meaningful and url suggests statute site, return humanized
                    if any(d in url.lower() for d in ["punjablaws", "pakistanlaw", "lawnotes", "pakcode", "punjabcode", "sindhlaws"]):
                        if len(titled) > 5:
                            return _normalize_whitespace(titled)[:150]
            except Exception:
                pass

        # Final fallback
        if text_to_search and len(text_to_search.strip()) > 10:
            first_line = text_to_search.strip().split("\n")[0][:100].strip()
            if first_line and len(first_line) > 10 and len(first_line) < 120:
                return _normalize_whitespace(first_line)

        logger.info(f"detect_statute_name: could not detect, returning Unknown for url={url}")
        return "Unknown Statute"

    except Exception as e:
        logger.error(f"detect_statute_name failed for url={url}: {e}", exc_info=True)
        return "Unknown Statute"


def detect_statute_name(text: str, url: Optional[str] = None) -> str:
    """Detect a canonical statute name and reject URL-fragment leftovers."""
    detected = _detect_statute_name_from_text_or_url(text, url)
    if is_short_title_clause(detected):
        if text:
            called = SHALL_BE_CALLED_ACT_RE.search(text[:20000])
            if called:
                title = _clean_called_act_title(
                    called.group("title"),
                    preamble=text[:20000],
                    repair_ocr=_looks_like_gazette_act_text(text),
                )
                if title and _is_plausible_act_title(title):
                    detected = title
                else:
                    detected = "Unknown Statute"
            else:
                detected = "Unknown Statute"
    if looks_like_fragment_name(detected):
        logger.info("detect_statute_name: rejecting fragment name %r for url=%s", detected, url)
        return "Unknown Statute"
    return detected

# ---------------------------------------------------------------------------
# Optional helper for external integration
# ---------------------------------------------------------------------------

def parse_statute_document(
    html_or_text: str,
    url: Optional[str] = None,
    statute_name: Optional[str] = None,
    official_title: Optional[str] = None,
) -> Dict[str, object]:
    """
    Convenience wrapper that detects name and splits into sections.
    Returns dict with statute_name and sections.

    This is useful for pipeline: harvester -> text_cleaner -> this.
    """
    detected_raw = statute_name or detect_statute_name(html_or_text, url)
    detected = prefer_official_statute_title(detected_raw, official_title)
    sections = split_into_sections(html_or_text, detected)
    return {
        "statute_name": detected,
        "source_url": url,
        "section_count": len(sections),
        "sections": sections,
    }

if __name__ == "__main__":
    # Manual smoke test
    sample_html = """
    <html><head><title>Pakistan Penal Code, 1860</title></head>
    <body>
    <p class="section">1. Short title and extent - This Act shall be called the Pakistan Penal Code...</p>
    <p class="section">2. Punishment of offences committed within Pakistan - Every person shall be liable...</p>
    <div class="section-body">3. Punishment of offences beyond Pakistan - Any person liable...</div>
    </body></html>
    """
    name = detect_statute_name(sample_html, "https://punjablaws.gov.pk/laws/26a.html")
    print(f"Detected: {name}")
    secs = split_into_sections(sample_html, name)
    print(f"Sections: {len(secs)}")
    for s in secs[:3]:
        print(s)
