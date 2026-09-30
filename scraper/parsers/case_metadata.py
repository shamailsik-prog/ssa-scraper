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
from typing import List, Optional

_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_DASH_TABLE = {ord(c): "-" for c in _DASHES}
_ROLES = (
    r"(?:Petitioner|Respondent|Appellant|Applicant|Plaintiff|Defendant|Complainant|Accused|Opponent|Objector|"
    r"Caveator|Revisionist|Informant|Decree[- ]?holder|Judgment[- ]?debtor|Contemnor|Intervenor|Interveners?|Proforma Respondent)"
)
_VERSUS = re.compile(r"(?:^|\s)(?:versus|vs)\.?(?=\s|$)", re.I)
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


def _title_start(lines: List[str]) -> int:
    """Line index after the citation line, [court] and 'Before ...' lines."""
    idx = 0
    for i in range(min(len(lines), 5)):
        ln = lines[i]
        low = ln.lower()
        if low.startswith(("before", "coram")) or (i <= 2 and re.match(r"^[\[(][A-Za-z].{1,70}[\])]$", ln)):
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
