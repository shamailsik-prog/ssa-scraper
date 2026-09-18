
"""
Pakistani Legal Citation Extractor - Production Module
Section 15 Compliance - FINAL_SCRAPER_PROMPT_1.md

Implements 8 CITATION_PATTERNS and 4 STATUTE_PATTERNS as compiled regex objects.
Supports dual-credential failover context (handled at caller) - extractor is stateless.

Reporters Covered:
  PLD  - Pakistan Legal Decisions
  SCMR - Supreme Court Monthly Review
  CLC  - Civil Law Cases
  YLR  - Yearly Law Reports
  MLD  - Monthly Law Digest
  PCrLJ - Pakistan Criminal Law Journal
  PTD  - Pakistan Tax Decisions
  PTCL - Pakistan Company Law / Tax Cases (PTCL variant)

Statutes Covered:
  SECTION, ARTICLE, ORDER_RULE, ACT_YEAR
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from scraper.parsers.statute_parser import KNOWN_STATUTES_MAP

# ============================================================================
# 8 CITATION PATTERNS - Pakistani Legal Reporters
# ============================================================================

# Court normalization for PLD
PLD_COURT_CANONICAL = {
    "SC": "SC",
    "SUPREME COURT": "SC",
    "SUPREME COURT OF PAKISTAN": "SC",
    "SUPREME COURT OF PAKISTAN SC": "SC",
    "FSC": "FSC",
    "FEDERAL SHARIAT COURT": "FSC",
    "FED. SHARIAT COURT": "FSC",
    "LAHORE": "Lahore",
    "LAH": "Lahore",
    "LAHORE HIGH COURT": "Lahore",
    "LAHORE H.C.": "Lahore",
    "KARACHI": "Karachi",
    "KAR": "Karachi",
    "SINDH": "Sindh",
    "SINDH HIGH COURT": "Sindh",
    "PESHAWAR": "Peshawar",
    "PESH": "Peshawar",
    "PESHAWAR HIGH COURT": "Peshawar",
    "BALOCHISTAN": "Balochistan",
    "BALOCHISTAN HIGH COURT": "Balochistan",
    "QUETTA": "Balochistan",
    "ISLAMABAD": "Islamabad",
    "ISLAMABAD HIGH COURT": "Islamabad",
    "IHC": "Islamabad",
    "AJK": "AJ&K",
    "AZAD JAMMU AND KASHMIR": "AJ&K",
    "GILGIT BALTISTAN": "GB",
    "GB": "GB",
}

# PLD: PLD 2023 SC 123, PLD 2020 Lahore 1, PLD 2023 Federal Shariat Court 45
_PLD_COURT_ALTERNATIVES = (
    r"SC|Supreme Court(?: of Pakistan)?|FSC|Federal Shariat Court|"
    r"Lahore(?: High Court)?|Lah\.?|Sindh(?: High Court)?|Karachi|Kar\.?|"
    r"Peshawar(?: High Court)?|Pesh\.?|Balochistan(?: High Court)?|Quetta|"
    r"Islamabad(?: High Court)?|IHC|AJ&?K|Azad Jammu and Kashmir|Gilgit Baltistan|GB"
)

CITATION_PATTERNS: Dict[str, re.Pattern] = {
    "PLD": re.compile(
        rf"""
        \bPLD
        \s+
        (?P<year>19\d{{2}}|20\d{{2}})
        \s+
        (?P<court>{_PLD_COURT_ALTERNATIVES})
        \s+
        (?P<page>\d+[A-Z]?)
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    "SCMR": re.compile(
        r"""
        \b
        (?P<year>19\d{2}|20\d{2})
        \s+
        SCMR
        \s+
        (?P<page>\d+[A-Z]?)(?:\s*\(\s*(?P<court>SC|Supreme Court)\s*\))?
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    "CLC": re.compile(
        r"""
        \b
        (?P<year>19\d{2}|20\d{2})
        \s+
        CLC
        \s*
        (?:\(\s*(?P<court>[A-Za-z\s\.\-&]+?)\s*\))?
        \s+
        (?P<page>\d+[A-Z]?)
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    "YLR": re.compile(
        r"""
        \b
        (?P<year>19\d{2}|20\d{2})
        \s+
        YLR
        \s*
        (?:\(\s*(?P<court>[A-Za-z\s\.\-&]+?)\s*\))?
        \s+
        (?P<page>\d+[A-Z]?)
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    "MLD": re.compile(
        r"""
        \b
        (?P<year>19\d{2}|20\d{2})
        \s+
        MLD
        \s*
        (?:\(\s*(?P<court>[A-Za-z\s\.\-&]+?)\s*\))?
        \s+
        (?P<page>\d+[A-Z]?)
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    "PCrLJ": re.compile(
        r"""
        \b
        (?P<year>19\d{2}|20\d{2})
        \s+
        P\s*Cr\.?\s*L\.?\s*J\.?
        \s*
        (?:\(\s*(?P<court>[A-Za-z\s\.\-&]+?)\s*\))?
        \s+
        (?P<page>\d+[A-Z]?)
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    "PTD": re.compile(
        r"""
        \b
        (?P<year>19\d{2}|20\d{2})
        \s+
        PTD
        \s+
        (?P<page>\d+[A-Z]?)
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    "PTCL": re.compile(
        r"""
        \b
        (?P<year>19\d{2}|20\d{2})
        \s+
        P(?:TCL|TCLR|CLD)
        \s+
        (?P<page>\d+[A-Z]?)
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
}

# ============================================================================
# 4 STATUTE PATTERNS
# ============================================================================

STATUTE_PATTERNS: Dict[str, re.Pattern] = {
    # Section 302(b) of PPC / Section 302 PPC / S. 302 Cr.PC / u/s 302 PPC
    "SECTION": re.compile(
        r"""
        \b
        (?:U\s*/\s*S|Under\s+Section|Section|S\.)
        \s*
        (?P<section>\d+[A-Z]?(?:\s*\(\s*[a-z0-9]+\s*\)\s*)*(?:\s*[,&]\s*\d+[A-Z]?)*)
        (?:\s*\(\s*[a-z]\)\s*)*
        \s*
        (?:of\s+)?(?:the\s+)?
        (?P<act>
            PPC|Pakistan\s+Penal\s+Code|
            Cr\.?\s*P\.?\s*C\.?|Criminal\s+Procedure\s+Code|
            C\.?\s*P\.?\s*C\.?|Civil\s+Procedure\s+Code|
            Qanun-e-Shahadat|Constitution|
            P\.?P\.?C\.?
        )?
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    # Article 199, Article 184(3) of the Constitution, Art. 10-A
    "ARTICLE": re.compile(
        r"""
        \b
        (?:Article|Art\.)
        \s*
        (?P<article>\d+[A-Z]?(?:\s*\(\s*\d+\s*\))?(?:\s*\(\s*[a-z]\s*\))?)
        (?:\s*\(\s*\d+[A-Z]?\s*\)\s*)*
        \s*
        (?:of\s+the\s+)?
        (?P<source>Constitution(?:\s+of\s+Pakistan(?:\s+,\s*1973)?)?)?
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    # Order VII Rule 11 CPC, Order 39 Rule 1 & 2
    "ORDER_RULE": re.compile(
        r"""
        \b
        Order
        \s+
        (?P<order>[IVXLCDM]+|\d+[A-Z]?)
        \s*,?\s*
        (?:Rule\s+(?P<rule>\d+[A-Z]?(?:\s*,\s*\d+)*)
            (?:\s*&\s*\d+)?
        )?
        \s*
        (?:of\s+)?(?:the\s+)?(?P<code>C\.?\s*P\.?\s*C\.?)
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    # Prevention of Electronic Crimes Act, 2016 / Income Tax Ordinance, 2001 / Companies Act, 2017
    "ACT_YEAR": re.compile(
        r"""
        \b
        (?P<act_name>
            (?:[A-Z][A-Za-z\s\-\']+?)\s+
            (?:Act|Ordinance|Order|Rules|Regulations)
        )
        \s*,?\s*
        (?P<year>19\d{2}|20\d{2})
        \b
        """,
        re.VERBOSE,
    ),
}

# Secondary pattern for generic Act Year without capture issues - fallback
_STATUTE_ACT_CLEAN = re.compile(
    r"^(?P<name>.+?)\s+(?P<type>Act|Ordinance|Order|Rules|Regulations)$",
    re.IGNORECASE,
)

INSTRUMENT_MENTION_PATTERNS: Dict[str, re.Pattern] = {
    "SRO": re.compile(
        r"""
        \b
        S\.?\s*R\.?\s*O\.?
        \s*(?:No\.?\s*)?
        (?P<number>[A-Z0-9]+(?:\s*\([A-Z0-9]+\))?)
        \s*(?:/|of)\s*
        (?P<year>19\d{2}|20\d{2})
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    "ACT_NO": re.compile(
        r"""
        \b
        Act\s+No\.?\s*
        (?P<number>[IVXLCDM]+|\d{1,5}[A-Z]?)
        \s+of\s+
        (?P<year>19\d{2}|20\d{2})
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    "ORDINANCE_NO": re.compile(
        r"""
        \b
        Ordinance\s+No\.?\s*
        (?P<number>[IVXLCDM]+|\d{1,5}[A-Z]?)
        \s+of\s+
        (?P<year>19\d{2}|20\d{2})
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
}

STATUTE_NAME_WITH_YEAR_PATTERN = re.compile(
    r"""
    \b
    (?P<name>
        [A-Z][A-Za-z0-9\s\-\&\.'/,()]{3,140}?
        \s+
        (?:Act|Ordinance|Code|Rules|Regulations|Order|Constitution)
    )
    \s*,?\s*
    (?P<year>19\d{2}|20\d{2})
    \b
    """,
    re.VERBOSE,
)


# ============================================================================
# Helpers
# ============================================================================

def _canonical_court(raw_court: Optional[str]) -> Optional[str]:
    if not raw_court:
        return None
    key = re.sub(r"\s+", " ", raw_court.strip()).upper()
    key = re.sub(r"\.", "", key)
    if key in PLD_COURT_CANONICAL:
        return PLD_COURT_CANONICAL[key]
    for k, v in PLD_COURT_CANONICAL.items():
        if k in key or key in k:
            return v
    return raw_court.strip().title()


def _clean_page(page: str) -> str:
    if not page:
        return page
    return page.strip().upper()


def _dedupe_citations(citations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique: List[Dict[str, Any]] = []
    for c in citations:
        norm = c.get("normalized") or c.get("raw", "").upper()
        dedup_key = re.sub(r"\s+", " ", norm.strip())
        if dedup_key not in seen:
            seen.add(dedup_key)
            unique.append(c)
    return unique


def _normalized_statute_key(name: str) -> str:
    key = (name or "").strip().lower()
    key = re.sub(r"[.,()]", "", key)
    key = re.sub(r"\s+", " ", key)
    return key


def canonicalise_statute_name(name: Optional[str], year: Optional[int] = None) -> Optional[str]:
    if not name:
        return None
    key = _normalized_statute_key(name)
    canonical = KNOWN_STATUTES_MAP.get(key)
    if canonical is None:
        key_without_year = re.sub(r"\s*,?\s*(19\d{2}|20\d{2})\s*$", "", key)
        canonical = KNOWN_STATUTES_MAP.get(key_without_year)
    if canonical is None:
        cleaned = re.sub(r"\s+", " ", name.strip().strip(".,;"))
        canonical = cleaned
    if year and re.search(r"\b(19|20)\d{2}\b", canonical) is None:
        canonical = f"{canonical}, {year}"
    return canonical


def _snippet(text: str, start: int, end: int, window: int = 60) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - window) : min(len(text), end + window)]).strip()


def _normalise_mention_number(number: str) -> str:
    value = re.sub(r"\s+", "", (number or "").upper())
    value = re.sub(r"^\((.+)\)$", r"\1", value)
    return value


def _dedupe_mentions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for item in sorted(items, key=lambda r: r.get("span", (0, 0))[0]):
        key = (
            item.get("mention_type"),
            item.get("normalized"),
            item.get("section_number"),
            item.get("year"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def normalise_instrument_mention(raw: str, mention_type: str) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    stype = (mention_type or "").upper()
    pattern = INSTRUMENT_MENTION_PATTERNS.get(stype)
    if pattern is None:
        return re.sub(r"\s+", " ", raw.strip())
    match = pattern.search(raw)
    if not match:
        return re.sub(r"\s+", " ", raw.strip())
    number = _normalise_mention_number(match.group("number"))
    year = int(match.group("year"))
    if stype == "SRO":
        return f"S.R.O. {number}/{year}"
    if stype == "ACT_NO":
        return f"Act No. {number} of {year}"
    return f"Ordinance No. {number} of {year}"


# ============================================================================
# Core Functions
# ============================================================================

def normalise_citation(raw: str) -> str:
    """
    Normalise a raw Pakistani legal citation string.

    Steps:
    - Collapse whitespace
    - Uppercase reporter tokens
    - Canonicalise court names for PLD
    - Standardise PCrLJ to PCrLJ, PTCL variants to PTCL, etc.
    - Ensure format: [PLD] YEAR COURT PAGE or YEAR REPORTER [COURT] PAGE

    Args:
        raw: Raw citation string e.g. "pld 2023  sc  123" or "2023 pcr.lj 45"

    Returns:
        Normalised string e.g. "PLD 2023 SC 123" or "2023 PCrLJ 45"
    """
    if not raw or not isinstance(raw, str):
        return ""

    s = re.sub(r"\s+", " ", raw.strip())
    if not s:
        return ""

    m_pld = CITATION_PATTERNS["PLD"].search(s)
    if m_pld:
        year = m_pld.group("year")
        court_raw = m_pld.group("court")
        page = _clean_page(m_pld.group("page"))
        court_canonical = _canonical_court(court_raw) or court_raw.strip()
        if court_canonical in ("SC", "FSC", "IHC", "AJ&K", "GB"):
            court_out = court_canonical
        else:
            court_out = court_canonical
        return f"PLD {year} {court_out} {page}"

    upper_s = s.upper()
    if re.search(r"P\s*CR\.?\s*L\.?\s*J", upper_s, re.IGNORECASE):
        m = CITATION_PATTERNS["PCrLJ"].search(s)
        if m:
            year = m.group("year")
            page = _clean_page(m.group("page"))
            court = m.group("court")
            if court:
                court_norm = _canonical_court(court)
                return f"{year} PCrLJ ({court_norm}) {page}"
            return f"{year} PCrLJ {page}"
        s = re.sub(r"P\s*Cr\.?\s*L\.?\s*J\.?", "PCrLJ", s, flags=re.IGNORECASE)

    if re.search(r"\bPTCLR\b|\bPCLD\b", upper_s):
        s = re.sub(r"\bPTCLR\b|\bPCLD\b", "PTCL", s, flags=re.IGNORECASE)
    for rep in ["SCMR", "CLC", "YLR", "MLD", "PTD", "PTCL", "PLD"]:
        s = re.sub(rf"\b{rep}\b", rep, s, flags=re.IGNORECASE)

    for reporter_key in ["SCMR", "CLC", "YLR", "MLD", "PTD", "PTCL"]:
        pat = CITATION_PATTERNS[reporter_key]
        m = pat.search(s)
        if m:
            year = m.group("year")
            page = _clean_page(m.group("page"))
            rep_out = "PCrLJ" if reporter_key == "PCrLJ" else reporter_key
            court = m.groupdict().get("court")
            if court:
                court_norm = _canonical_court(court)
                return f"{year} {rep_out} ({court_norm}) {page}"
            return f"{year} {rep_out} {page}"

    # Check PCrLJ again after clean
    m = CITATION_PATTERNS["PCrLJ"].search(s)
    if m:
        year = m.group("year")
        page = _clean_page(m.group("page"))
        court = m.groupdict().get("court")
        if court:
            court_norm = _canonical_court(court)
            return f"{year} PCrLJ ({court_norm}) {page}"
        return f"{year} PCrLJ {page}"

    def _paren_court_repl(match):
        inside = match.group(1)
        canon = _canonical_court(inside) or inside
        return f"({canon})"

    s = re.sub(r"\(\s*([^)]+?)\s*\)", _paren_court_repl, s)
    s = re.sub(r"\s+", " ", s.strip())
    s = re.sub(r"\bpld\b", "PLD", s, flags=re.IGNORECASE)
    s = re.sub(r"\bscmr\b", "SCMR", s, flags=re.IGNORECASE)
    s = re.sub(r"\bclc\b", "CLC", s, flags=re.IGNORECASE)
    s = re.sub(r"\bylr\b", "YLR", s, flags=re.IGNORECASE)
    s = re.sub(r"\bmld\b", "MLD", s, flags=re.IGNORECASE)
    s = re.sub(r"\bptd\b", "PTD", s, flags=re.IGNORECASE)
    s = re.sub(r"\bptcl\b", "PTCL", s, flags=re.IGNORECASE)
    s = re.sub(r"\bpcrlj\b", "PCrLJ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bpcr\.lj\b", "PCrLJ", s, flags=re.IGNORECASE)
    return s


def extract_citations(text: str) -> List[Dict[str, Any]]:
    """
    Extract all Pakistani legal citations from text.

    Args:
        text: Input judgment text / OCR text

    Returns:
        List of dicts: {
            "raw": str (exact match),
            "reporter": str (PLD, SCMR, CLC, YLR, MLD, PCrLJ, PTD, PTCL),
            "year": int,
            "court": Optional[str],
            "page": str,
            "normalized": str,
            "span": Tuple[int,int]
        }
    """
    if not text or not isinstance(text, str):
        return []

    results: List[Dict[str, Any]] = []

    for reporter, pattern in CITATION_PATTERNS.items():
        for match in pattern.finditer(text):
            raw = match.group(0)
            gd = match.groupdict()
            year_str = gd.get("year")
            try:
                year = int(year_str) if year_str else None
            except ValueError:
                year = None

            court_raw = gd.get("court")
            court = _canonical_court(court_raw) if court_raw else None

            page_raw = gd.get("page") or ""
            page = _clean_page(page_raw)

            if year is not None and not (1947 <= year <= 2030):
                continue
            if not page:
                continue

            normalized = normalise_citation(raw)

            if not re.match(r"^\d+[A-Z]?$", page):
                if not re.match(r"^\d+", page):
                    continue

            entry = {
                "raw": raw.strip(),
                "reporter": "PCrLJ" if reporter == "PCrLJ" else reporter,
                "year": year,
                "court": court,
                "page": page,
                "normalized": normalized,
                "span": (match.start(), match.end()),
            }
            results.append(entry)

    results.sort(key=lambda x: x["span"][0])
    results = _dedupe_citations(results)
    return results


def extract_statutes(text: str) -> List[Dict[str, Any]]:
    """
    Extract statute references from text.

    Returns list of dicts: {
        "raw": str,
        "type": "SECTION"|"ARTICLE"|"ORDER_RULE"|"ACT_YEAR",
        "section": Optional[str],
        "act": Optional[str],
        "article": Optional[str],
        "order": Optional[str],
        "rule": Optional[str],
        "year": Optional[int],
        "normalized": str,
        "span": (int,int)
    }
    """
    if not text or not isinstance(text, str):
        return []

    results: List[Dict[str, Any]] = []
    seen_raw_spans = set()

    for stype, pattern in STATUTE_PATTERNS.items():
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            raw = match.group(0).strip()
            if not raw or len(raw) < 3:
                continue
            overlap = False
            for (s_prev, e_prev) in seen_raw_spans:
                if not (span[1] <= s_prev or span[0] >= e_prev):
                    existing_len = e_prev - s_prev
                    current_len = span[1] - span[0]
                    if current_len <= existing_len:
                        overlap = True
                        break
            if overlap:
                continue

            gd = match.groupdict()
            normalized = re.sub(r"\s+", " ", raw.strip())

            entry: Dict[str, Any] = {
                "raw": raw,
                "type": stype,
                "normalized": normalized,
                "span": span,
            }

            if stype == "SECTION":
                entry["section"] = (gd.get("section") or "").strip()
                act = (gd.get("act") or "").strip()
                entry["act"] = act if act else None
                if entry["act"]:
                    act_u = entry["act"].upper()
                    if "PPC" in act_u or "PENAL" in act_u:
                        entry["act"] = "PPC"
                        entry["normalized"] = f"Section {entry['section']} PPC"
                    elif "CR" in act_u and "P" in act_u and "PROCEDURE" in act_u or act_u.strip() == "CR.P.C." or "CRPC" in act_u.replace(".", "").replace(" ", ""):
                        if "CIVIL" not in act_u:
                            entry["act"] = "CrPC"
                            entry["normalized"] = f"Section {entry['section']} CrPC"
                        else:
                            entry["act"] = "CPC"
                    elif "CPC" in act_u.replace(".", "").replace(" ", "") or "CIVIL" in act_u:
                        entry["act"] = "CPC"
                        entry["normalized"] = f"Section {entry['section']} CPC"

            elif stype == "ARTICLE":
                entry["article"] = (gd.get("article") or "").strip()
                entry["source"] = (gd.get("source") or "Constitution").strip() or "Constitution"
                entry["normalized"] = f"Article {entry['article']} of the Constitution"

            elif stype == "ORDER_RULE":
                entry["order"] = (gd.get("order") or "").strip()
                entry["rule"] = (gd.get("rule") or "").strip() or None
                entry["code"] = (gd.get("code") or "CPC").strip()
                code_norm = "CPC" if "CPC" in entry["code"].upper().replace(".", "") else entry["code"]
                if entry["rule"]:
                    entry["normalized"] = f"Order {entry['order']} Rule {entry['rule']} {code_norm}"
                else:
                    entry["normalized"] = f"Order {entry['order']} {code_norm}"

            elif stype == "ACT_YEAR":
                act_name = (gd.get("act_name") or "").strip()
                year_str = gd.get("year")
                try:
                    year = int(year_str) if year_str else None
                except ValueError:
                    year = None
                entry["act_name"] = act_name
                entry["year"] = year
                if year is None or not (1947 <= year <= 2030):
                    continue
                entry["normalized"] = f"{act_name}, {year}"

            results.append(entry)
            seen_raw_spans.add(span)

    results.sort(key=lambda x: x["span"][0])
    deduped: List[Dict[str, Any]] = []
    seen_norm = set()
    for r in results:
        key = r["normalized"].upper()
        if key not in seen_norm:
            seen_norm.add(key)
            deduped.append(r)
    return deduped


def extract_instrument_mentions(text: str) -> List[Dict[str, Any]]:
    """
    Extract Gazette-style instrument references (S.R.O., Act No., Ordinance No.).
    """
    if not text or not isinstance(text, str):
        return []
    mentions: List[Dict[str, Any]] = []
    for raw_type, pattern in INSTRUMENT_MENTION_PATTERNS.items():
        for match in pattern.finditer(text):
            try:
                year = int(match.group("year"))
            except (TypeError, ValueError):
                continue
            if year < 1947 or year > 2035:
                continue
            raw = match.group(0).strip()
            number = _normalise_mention_number(match.group("number"))
            normalized = normalise_instrument_mention(raw, raw_type)
            span = (match.start(), match.end())
            mentions.append(
                {
                    "raw": raw,
                    "mention_type": raw_type.lower(),
                    "number": number,
                    "year": year,
                    "normalized": normalized,
                    "span": span,
                    "source_snippet": _snippet(text, span[0], span[1]),
                }
            )
    return _dedupe_mentions(mentions)


def extract_statute_mentions(text: str) -> List[Dict[str, Any]]:
    """
    Extract statute mentions with canonical names and optional section/article links.
    """
    if not text or not isinstance(text, str):
        return []
    mentions: List[Dict[str, Any]] = []

    for match in STATUTE_NAME_WITH_YEAR_PATTERN.finditer(text):
        raw = match.group(0).strip()
        raw_name = re.sub(r"\s+", " ", match.group("name").strip())
        year = int(match.group("year"))
        if year < 1947 or year > 2035:
            continue
        canonical = canonicalise_statute_name(raw_name, year)
        span = (match.start(), match.end())
        mentions.append(
            {
                "raw": raw,
                "mention_type": "name_year",
                "statute_name": raw_name,
                "canonical_statute_name": canonical,
                "section_number": None,
                "year": year,
                "normalized": canonical or raw_name,
                "span": span,
                "source_snippet": _snippet(text, span[0], span[1]),
            }
        )

    for hit in extract_statutes(text):
        statute_name = hit.get("act_name") or hit.get("act") or hit.get("source")
        if not statute_name and hit.get("type") == "ARTICLE":
            statute_name = "Constitution of the Islamic Republic of Pakistan, 1973"
        section_number = hit.get("section") or hit.get("article") or hit.get("rule")
        year = hit.get("year")
        canonical = canonicalise_statute_name(str(statute_name), year) if statute_name else None
        if not canonical and not section_number:
            continue
        span = hit.get("span") or (0, 0)
        normalized = canonical or str(statute_name)
        if section_number:
            normalized = f"{normalized} §{section_number}" if normalized else f"§{section_number}"
        mentions.append(
            {
                "raw": hit.get("raw"),
                "mention_type": hit.get("type", "statute_ref").lower(),
                "statute_name": statute_name,
                "canonical_statute_name": canonical,
                "section_number": str(section_number) if section_number is not None else None,
                "year": year,
                "normalized": normalized,
                "span": span,
                "source_snippet": _snippet(text, span[0], span[1]),
            }
        )
    return _dedupe_mentions(mentions)


def score_confidence(fields_dict: Dict[str, Any]) -> float:
    """
    Calculate confidence score for extracted case data.

    Allowed returns: 1.0, 0.8, 0.6, 0.4, 0.2, 0.1

    Scoring logic (deterministic):
    - 1.0: All critical fields present: citation (or list>=1), court, year>=1947,
           case_title / title with length >=10, judges/bench >=1, judgment_text/content length >=500
    - 0.8: Minor omission: missing judges but rest present, OR missing title but has citation+court+year+content_length>=300
    - 0.6: Has citation + court + year, plus either title or content_length>=100, missing judges
    - 0.4: Only citation + court + year OR citation + title, but missing content and judges
    - 0.2: Only citation OR only statute + partial court/year, weak evidence
    - 0.1: Minimal / empty dict, or no citation/year/court

    Returns:
        float in {1.0,0.8,0.6,0.4,0.2,0.1}
    """
    if not fields_dict or not isinstance(fields_dict, dict):
        return 0.1

    data = {str(k).lower(): v for k, v in fields_dict.items()}

    def _has(key_alternatives: List[str]) -> bool:
        for k in key_alternatives:
            if k.lower() in data:
                v = data[k.lower()]
                if v is None:
                    continue
                if isinstance(v, str) and v.strip() == "":
                    continue
                if isinstance(v, (list, tuple, set)) and len(v) == 0:
                    continue
                if isinstance(v, dict) and len(v) == 0:
                    continue
                return True
        return False

    def _get_first(keys: List[str], default=None):
        for k in keys:
            if k.lower() in data:
                v = data[k.lower()]
                if v is not None and v != "" and v != []:
                    return v
        return default

    has_citation = _has(["citation", "citations", "normalized_citation"])
    has_court = _has(["court", "court_name", "court_abbreviation"])
    year_val = _get_first(["year", "case_year", "judgment_year"])
    has_year = False
    if year_val is not None:
        try:
            y = int(str(year_val).strip()[:4]) if isinstance(year_val, str) else int(year_val)
            if 1947 <= y <= 2030:
                has_year = True
        except (ValueError, TypeError):
            has_year = False

    has_title = _has(["title", "case_title", "case_name", "petitioner", "caption"])
    title_val = _get_first(["title", "case_title", "case_name"], "")
    title_len = len(str(title_val).strip()) if title_val else 0

    has_judges = _has(["judges", "judge", "bench", "author_judge"])

    content_val = _get_first(["content", "judgment_text", "text", "full_text", "body", "judgement"])
    content_len = len(str(content_val)) if content_val else 0

    has_statutes = _has(["statutes", "statute"])

    citation_count = 0
    cit = _get_first(["citations"])
    if isinstance(cit, (list, tuple)):
        citation_count = len(cit)
    elif _has(["citation"]):
        citation_count = 1

    if has_citation and has_court and has_year and has_title and title_len >= 10 and has_judges and content_len >= 500:
        return 1.0

    if has_citation and has_court and has_year and has_judges and content_len >= 500:
        if title_len >= 10:
            return 1.0
        return 0.8

    if has_citation and has_court and has_year:
        if (has_title and title_len >= 5 and content_len >= 300) or (has_judges and content_len >= 300):
            return 0.8
        if has_title and has_judges and content_len >= 100:
            return 0.8
        if has_title and citation_count >= 1 and content_len >= 300:
            return 0.8

    if has_citation and has_court and has_year:
        if has_title or content_len >= 100 or has_judges:
            return 0.6
        return 0.6

    if has_citation and (has_court or has_year):
        return 0.4
    if has_citation and has_title:
        return 0.4
    if has_court and has_year and (has_title or content_len >= 200 or has_statutes):
        return 0.4

    if has_citation or citation_count >= 1:
        return 0.2
    if has_statutes and (has_court or has_year):
        return 0.2
    if has_title and (has_court or has_year):
        return 0.2

    return 0.1


def _self_test() -> None:
    sample = """
    In PLD 2023 SC 123 it was held that Section 302(b) PPC is applicable.
    Also reported as 2023 SCMR 456 and 2023 CLC (Lahore) 789.
    Refer Art. 184(3) of the Constitution and Order VII Rule 11 CPC.
    See also 2022 PCr.LJ 100, 2021 MLD 55, 2020 YLR 10, 2023 PTD 200, 2023 PTCL 150.
    Prevention of Electronic Crimes Act, 2016 is relevant.
    PLD 2020 Lahore 1
    """
    print("Citations:", extract_citations(sample))
    print("Statutes:", extract_statutes(sample))
    print("Normalized PLD test:", normalise_citation("pld 2023 sc 123"))
    print("Normalized PCrLJ test:", normalise_citation("2023 pcr.lj 100"))
    print("Score test:", score_confidence({
        "citation": "PLD 2023 SC 123",
        "court": "SC",
        "year": 2023,
        "title": "State vs. Ahmed",
        "judges": ["Justice Isa"],
        "content": "a" * 600
    }))


if __name__ == "__main__":
    _self_test()
