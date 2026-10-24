"""Case-header metadata from a PakistanLawSite judgment: parties, docket number, headnotes.

The preserved text of a PLS judgment starts with a fixed header (citation line, [court], "Before
<judges>"), then the title block, a docket line and the headnote paragraphs:

    1990 C L C 1205 / [Peshawar] / Before Muhammad Bashir Khan Jehangiri, J
    Mst. KAUSAR BIBI---Petitioner
    versus
    MUHAMMAD MUSHTAQ and 6 others---Respondents
    Civil Revision No.329 of 1989, decided on 22nd November, 1989.
    (a) Civil Procedure Code (V of 1908)--- ...

Everything here is deterministic and derived only from the preserved text (raw-first rule): a value
that is not present in the text is left None, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Set

_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_DASH_TABLE = {ord(c): "-" for c in _DASHES}
_ROLES = (
    r"(?:Petitioner|Respondent|Appellant|Applicant|Plaintiff|Defendant|Complainant|Accused|Opponent|Objector|"
    r"Caveator|Revisionist|Informant|Decree[- ]?holder|Judgment[- ]?debtor|Contemnor|Intervenor|Interveners?|Proforma Respondent)"
)
_VERSUS = re.compile(r"(?:^|\s)(?:versus|vs)\.?(?=\s|$)", re.I)
_V_DOT_PARTY = re.compile(r"(?:^|\s)v\.\s+(?=[A-Za-z0-9/])", re.I)
_DATE_HEARING = re.compile(r"(?im)^\s*Dates?\s+of\s+hearing\b")
_DATE_DECISION = re.compile(r"(?im)^\s*Date\s+of\s+decision\b")
_JUDGMENT_ORDER_LINE = re.compile(r"(?im)^\s*(?:JUDGMENT|JUDGEMENT|ORDER|SHORT\s+ORDER)\s*\.?\s*$")
_IN_THE_COURT_LINE = re.compile(r"(?im)^\s*IN\s+THE\s+")
_ALSO_REPORTED_LINE = re.compile(r"(?im)^\s*(?:also\s+reported\s+as|reported\s+at)\b")
_RESP_ROLE = re.compile(rf"\s*-*\s*\b{_ROLES}s?\b", re.I)
_PET_ROLE_TAIL = re.compile(rf"\s*-+\s*{_ROLES}s?\s*[.,;]?\s*$|\s+{_ROLES}s?\s*[.,;]?\s*$", re.I)
_DECIDED = re.compile(r"(?:decided|heard)\s+on\b", re.I)
_DECIDED_SENTENCE = re.compile(r",?\s*(?:decided|heard)\s+on\b[^\n]{0,100}?\b(?:19|20)\d{2}\b\s*[.,]?", re.I)
_DOCKET_OK = re.compile(r"\b(?:Nos?\.?|Numbers?)\b.*\bof\s+(?:19|20)\d{2}\b|\b(?:19|20)\d{2}\b", re.I | re.S)
_BODY_LINE = re.compile(
    r"^(?:JUDGMENT|JUDGEMENT|ORDER|SHORT ORDER|Dates? of hearing\b|[A-Z][A-Za-z .'\-]{3,60},\s*(?:C\.?J\.?|J\.?|JJ\.?)\s*[.\-])",
)
_COUNSEL_LINE = re.compile(
    rf"(?i)\bfor\s+(?:the\s+)?(?:{_ROLES}s?|State|Appellants?|Respondents?|Petitioners?|Interveners?|Applicants?)\b\s*\.?\s*$"
)


@dataclass
class CaseMetadata:
    petitioner: Optional[str] = None
    respondent: Optional[str] = None
    docket_number: Optional[str] = None
    headnotes: Optional[str] = None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip(" \t.,;:-")


def _norm_lines(text: str) -> List[str]:
    return [ln.strip() for ln in text.replace("\xa0", " ").translate(_DASH_TABLE).split("\n") if ln.strip()]


_HEADER_REL_REF = re.compile(r"\b(?:rel|ref)\.", re.I)
_PLS_COURT_LINE = re.compile(
    r"\b(?:IN\s+THE\s+)?(SUPREME\s+COURT\s+OF\s+PAKISTAN|"
    r"LAHORE\s+HIGH\s+COURT|SINDH\s+HIGH\s+COURT|PESHAWAR\s+HIGH\s+COURT|"
    r"BALOCHISTAN\s+HIGH\s+COURT|ISLAMABAD\s+HIGH\s+COURT|"
    r"FEDERAL\s+SHARIAT\s+COURT|FEDERAL\s+CONSTITUTIONAL\s+COURT)\b",
    re.I,
)
_HEADNOTE_TOPIC = re.compile(r"(?:^|\n)\s*\([a-z]\)\s", re.I)


def _caption_cut_index(scan: str) -> Optional[int]:
    cuts: List[int] = []
    for pat in (_HEADER_REL_REF, _VERSUS, _DECIDED, _V_DOT_PARTY, _DATE_HEARING, _DATE_DECISION, _JUDGMENT_ORDER_LINE):
        m = pat.search(scan)
        if m:
            cuts.append(m.start())
    hm = _HEADNOTE_TOPIC.search(scan)
    if hm:
        cuts.append(hm.start())
    court_hits = list(_IN_THE_COURT_LINE.finditer(scan))
    if len(court_hits) >= 2:
        cuts.append(court_hits[1].start())
    return min(cuts) if cuts else None


def _leading_citation_only_lines(text: str) -> str:
    from scraper.parsers.citation_extractor import extract_citations

    kept: List[str] = []
    for ln in _norm_lines(text):
        hits = extract_citations(ln)
        if not hits:
            break
        compact = re.sub(r"\s+", "", ln)
        cite_chars = sum(len(h.get("raw") or "") for h in hits)
        if cite_chars < max(8, int(len(compact) * 0.45)):
            break
        kept.append(ln)
    return "\n".join(kept)


def judgment_header_slice(text: Optional[str]) -> str:
    """Top of judgment text before rel./ref., with caption-style cuts so party lines do not widen the head block."""
    raw = text or ""
    head_limit = max(300, min(800, int(len(raw) * 0.15)))
    window = raw[:head_limit]
    cap_cut = _caption_cut_index(window)
    if cap_cut is not None:
        window = window[:cap_cut]
    cut = _HEADER_REL_REF.search(window)
    return window[: cut.start()] if cut else window


def judgment_caption_slice(text: Optional[str]) -> str:
    """Citation caption only: lines before parties, docket, headnotes, or rel./ref. markers.

    Parallel report citations in this block may become aliases; body and headnote mentions must not."""
    raw = text or ""
    if not raw:
        return ""
    scan_limit = min(len(raw), max(2000, int(len(raw) * 0.08)))
    scan = raw[:scan_limit]
    cut = _caption_cut_index(scan)
    if cut is not None:
        return raw[:cut]
    lead = _leading_citation_only_lines(raw)
    return lead


def also_reported_citation_strings(text: Optional[str]) -> Set[str]:
    """Parallel citations on PLS 'Also reported as' lines after the title block."""
    from scraper.parsers.citation_extractor import extract_citations, normalise_citation

    raw = text or ""
    out: set[str] = set()
    for ln in _norm_lines(raw):
        if not _ALSO_REPORTED_LINE.search(ln):
            continue
        for c in extract_citations(ln):
            norm = c.get("normalized") or normalise_citation(c["raw"])
            if norm:
                out.add(norm)
    return out


def parse_pls_header_court(text: Optional[str]) -> Optional[str]:
    """Court name from the PLS header block only (not reconciled metadata)."""
    header = judgment_header_slice(text)
    if not header:
        return None
    m = _PLS_COURT_LINE.search(header)
    if not m:
        bracket = re.search(
            r"^\s*\[((?:Supreme|High)\s+Court[^\]]+|[^\]]{2,80})\]\s*$",
            header,
            re.M | re.I,
        )
        if bracket:
            label = re.sub(r"\s+", " ", bracket.group(1)).strip()
            if re.search(r"(?i)\b(?:india|bangladesh|england|u\.?k|sri\s+lanka)\b", label):
                return label
            if re.search(r"(?i)\b(?:supreme|high)\s+court\b", label):
                return label
            return label.strip()
        return None
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    if name.upper() == "SUPREME COURT OF PAKISTAN":
        return "Supreme Court of Pakistan"
    return name.title()


def _is_coram_header_line(ln: str, line_index: int) -> bool:
    low = ln.lower()
    if line_index <= 2 and re.match(r"^[\[(][A-Za-z].{1,70}[\])]$", ln):
        return True
    if low.startswith(("before", "coram")):
        return True
    if low.startswith("present") or re.match(r"^present[.:]", low):
        return bool(re.search(r"(?i)\b(?:j\.|jj\.|c\.j\.|justice|judge)\b", ln))
    return False


def _title_start(lines: List[str]) -> int:
    """Line index after the citation line, [court] and coram/present lines (not party title lines)."""
    idx = 0
    for i in range(min(len(lines), 12)):
        if _is_coram_header_line(lines[i], i):
            idx = i + 1
    return idx


def parse_case_metadata(text: Optional[str]) -> CaseMetadata:
    out = CaseMetadata()
    if not text:
        return out
    lines = _norm_lines(text[:40000])
    if not lines:
        return out
    start = _title_start(lines)
    block_lines = lines[start : start + 16]
    block = " ".join(block_lines)
    headnote_from_line = start
    vs = _VERSUS.search(block)
    after_role = ""
    if vs:
        pet = block[: vs.start()]
        rest = block[vs.end() :]
        rm = _RESP_ROLE.search(rest)
        if rm:
            res = rest[: rm.start()]
            after_role = rest[rm.end() :]
            after_role = re.sub(r"^\s*-*\s*s?\s*[.,;]?\s*", "", after_role)
        else:
            res = rest[:200]
        pet = _PET_ROLE_TAIL.sub("", pet.strip())
        out.petitioner = _clean(pet)[:1000] or None
        out.respondent = _clean(res)[:1000] or None
    else:
        pm = re.search(rf"^(.{{3,400}}?)\s*-{{1,}}\s*{_ROLES}s?\b", block, re.I)
        if pm:
            out.petitioner = _clean(pm.group(1))[:1000] or None
            after_role = block[pm.end() :]
    # docket: what follows the respondent role, up to ", decided on"
    if after_role:
        cut = _DECIDED.search(after_role)
        cand = after_role[: cut.start()] if cut else after_role[:200]
        cand = _clean(cand)
        if cand and len(cand) <= 300 and _DOCKET_OK.search(cand) and re.search(r"\d", cand):
            out.docket_number = cand[:300]
    # headnotes: start after the "decided on ..." sentence (or the respondent line), stop at the body
    consumed = 0
    hd = _DECIDED_SENTENCE.search("\n".join(lines[start : start + 20]))
    if hd:
        consumed = len("\n".join(lines[start : start + 20])[: hd.end()].split("\n"))  # lines covered incl. the current
        headnote_from_line = start + consumed
    elif vs:
        headnote_from_line = start + min(len(block_lines), 4)
    hn: List[str] = []
    for ln in lines[headnote_from_line : headnote_from_line + 250]:
        if hn and (_BODY_LINE.match(ln) or _COUNSEL_LINE.search(ln)):
            break
        if not hn and _COUNSEL_LINE.search(ln):
            break
        if re.fullmatch(r"[-\s]+", ln):
            continue
        hn.append(ln)
        if sum(len(x) for x in hn) > 8000:
            break
    joined = "\n".join(hn).strip()
    if len(joined) >= 40:
        out.headnotes = joined
    return out
