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
from typing import List, Dict, Optional, Tuple
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
SHORT_TITLE_CLAUSE_RE = re.compile(
    r'^\s*(?:section\s+\d+[A-Z]?\s*[\.\-\)\:]\s*)?(?:short\s+title[^.:\n]*[:\-\.\)]\s*)?(?:this|the)\s+'
    r'(?:act|ordinance|order|code|rules?|regulations?)\s+(?:may|shall)\s+be\s+called\b',
    re.IGNORECASE,
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

def _extract_number_title_from_chunk(chunk: str) -> Tuple[Optional[str], str, str]:
    """
    Returns (section_number, section_title, section_text_body)
    section_text_body is full chunk cleaned.
    """
    chunk_clean = _clean_section_text(chunk)
    if not chunk_clean:
        return None, "", ""

    # Try keyword form first: Section 3 ...
    m = SECTION_KEYWORD_RE.match(chunk_clean)
    if m:
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
    short_body = len(body) < 260
    if re.fullmatch(r'\d{1,4}', body):
        return True
    if TRAILING_AND_SECTION_RE.match(body):
        return True
    gazette_match = GAZETTE_FOOTNOTE_RE.search(body)
    if short_body and gazette_match and gazette_match.start() <= 6:
        return True
    annotation_match = FOOTNOTE_ANNOTATION_RE.search(body)
    if short_body and annotation_match and annotation_match.start() <= 6:
        return True
    if SECTION_RANGE_FOOTNOTE_RE.search(body) and short_body:
        return True
    return False

def is_short_title_clause(name: str) -> bool:
    return bool(SHORT_TITLE_CLAUSE_RE.match(_normalize_whitespace(name or "")))

def _is_plausible_act_title(name: str) -> bool:
    candidate = _normalize_whitespace(name or "")
    if not candidate or is_short_title_clause(candidate):
        return False
    if not (6 <= len(candidate) <= 180):
        return False
    return bool(re.search(r'\b(Act|Ordinance|Order|Code|Rules|Regulations|Constitution)\b', candidate, re.IGNORECASE))

def prefer_official_statute_title(detected_name: Optional[str], official_title: Optional[str]) -> str:
    detected = _normalize_whitespace(detected_name or "") or "Unknown Statute"
    official = _normalize_whitespace(official_title or "")
    if official and _is_plausible_act_title(official):
        if detected == "Unknown Statute" or is_short_title_clause(detected):
            return official
    return detected

def _deduplicate_sections(sections: List[Dict[str, str]], statute_name: Optional[str] = None) -> List[Dict[str, str]]:
    sanity_cap = _section_cap_for_statute(statute_name or "")
    seen = set()
    out = []
    for s in sections:
        key = s.get("section_number", "").strip().lower()
        # allow duplicate numbers if text differs substantially (>50 chars diff) - keep first only for safety
        if key in seen:
            continue
        # filter tiny sections that are likely not real
        if len(s.get("section_text", "")) < 20:
            continue
        if _is_false_section_candidate(s.get("section_text", "")):
            continue
        num_int = _parse_numeric_section_number(s.get("section_number", ""))
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

def _strategy_4_resplit_digit(text: str, statute_name: str) -> List[Dict[str, str]]:
    """
    Strategy 4: re.split digit
    Plain-text split using pattern for digit-start sections.
    Handles cases where input is raw text (not HTML) from pdfplumber/pytesseract.
    """
    try:
        plain = BeautifulSoup(text, "html.parser").get_text(separator="\n") if _is_html(text) else text
        plain = _normalize_whitespace(plain)
        # Pattern: line start with number + delimiter, ensure lookahead for content
        split_pattern = re.compile(r'(?:\n|^)\s*(?=\d+[A-Z]?\s*[\.\-\)\:\]]\s+[A-Za-z])', re.MULTILINE)
        # Instead of split losing delimiter, use finditer to slice
        matches = list(re.finditer(r'(?:^|\n)\s*(\d+[A-Z]?)\s*[\.\-\)\:\]]\s*', plain, re.MULTILINE))
        if len(matches) < 2:
            # try alternative without newline requirement
            matches = list(re.finditer(r'(\d+[A-Z]?)\s*[\.\-\)]\s+(?=[A-Z][a-z])', plain))
        sections = []
        if len(matches) >= 2:
            for i, m in enumerate(matches):
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(plain)
                chunk = plain[start:end].strip()
                if len(chunk) < 15:
                    continue
                num, title, body = _extract_number_title_from_chunk(chunk)
                if num:
                    sections.append(_make_section_dict(num, title, chunk, statute_name))
            logger.debug(f"Strategy4 re.split digit found {len(sections)}")
            return _deduplicate_sections(sections, statute_name)
        else:
            # fallback naive split by double newline and filter
            chunks = re.split(r'\n{2,}', plain)
            for chunk in chunks:
                num, title, body = _extract_number_title_from_chunk(chunk)
                if num:
                    sections.append(_make_section_dict(num, title, chunk, statute_name))
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
        matches = list(pattern.finditer(plain))
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

def split_into_sections(text: str, statute_name: Optional[str] = None) -> List[Dict[str, str]]:
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

def detect_statute_name(text: str, url: Optional[str] = None) -> str:
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

        # 6) Fallback: humanize URL slug
        if url:
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
