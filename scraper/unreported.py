"""
Unreported judgments: judgments from a court's own website that no law report (PLD, SCMR, ...) cites.

They used to be held for review ("no citation supported by source"), because every judgment in the
corpus is identified by its citation. They now enter the corpus marked `unreported`, identified by
court, case number and decision date, and sorted by the kind of proceeding the case number names
(bail, constitutional petition, suit, appeal, revision ...).

Everything here is deterministic and read only from the source: a case number that is not in the
listing row or the heading of the judgment is left None, never guessed.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, Optional

# Court abbreviations used in the identity of an unreported judgment, by source name.
COURT_ABBR = {
    "SupremeCourt": "SC",
    "FederalShariatCourt": "FSC",
    "IslamabadHighCourt": "IHC",
    "LahoreHighCourt": "LHC",
    "SindhHighCourt": "SHC",
    "PeshawarHighCourt": "PHC",
    "BalochistanHighCourt": "BHC",
    "AJKSupremeCourt": "AJKSC",
    "AJKHighCourt": "AJKHC",
    "SupremeAppellateCourtGB": "SACGB",
    "GBChiefCourt": "GBCC",
}

# The court a court's own website publishes, by source name (short codes of the court directory). The
# court of an unreported judgment comes from here, never from its text: a Sindh High Court order that
# discusses a Supreme Court precedent is still a Sindh High Court order.
SOURCE_COURT_CODE = {
    "SupremeCourt": "SC",
    "FederalShariatCourt": "FSC",
    "IslamabadHighCourt": "IHC",
    "LahoreHighCourt": "LHC",
    "SindhHighCourt": "SHC",
    "PeshawarHighCourt": "PHC",
    "BalochistanHighCourt": "BHC",
    "AJKSupremeCourt": "AJK-SC",
    "AJKHighCourt": "AJK-HC",
    "SupremeAppellateCourtGB": "SAC-GB",
}

PROCEEDING_LABELS = {
    "bail": "Bail",
    "habeas_corpus": "Habeas corpus",
    "constitutional_petition": "Constitutional / writ petition",
    "leave_to_appeal": "Petition for leave to appeal",
    "civil_suit": "Civil suit",
    "civil_appeal": "Civil appeal",
    "criminal_appeal": "Criminal appeal",
    "intra_court_appeal": "Intra-court appeal",
    "civil_revision": "Civil revision",
    "criminal_revision": "Criminal revision",
    "review": "Review",
    "criminal_reference": "Criminal reference",
    "tax_reference": "Tax / customs reference",
    "contempt": "Contempt",
    "execution": "Execution",
    "transfer": "Transfer application",
    "criminal_miscellaneous": "Criminal miscellaneous",
    "civil_miscellaneous": "Civil miscellaneous",
    "other": "Other",
}

# listing-row keys the court connectors keep the case number under
_ROUTE_KEYS = ("result_case_no", "portal_case_number", "case_no", "case_number")

# "<case-type words> No. <number> of <year>" or "... No.<number>/<year>" on one line of the heading
_CASE_NO = re.compile(
    r"(?:[A-Z][A-Za-z.()&']*\.?\s*){1,7}?Nos?\.?\s*[A-Z]?\s*-?\s*\d+(?:\s*-\s*[A-Z]\b)?\s*(?:of|/)\s*(?:19|20)\d{2}"
)

_CRIMINAL = r"(?:criminal|crl|cr)\b"
# ordered: the first rule that matches decides. Each pattern runs on a lower-cased, space-normalised string.
_RULES = (
    ("habeas_corpus", r"habeas|\b491\b|\d\s*-\s*h\b"),
    ("bail", r"\bbail\b|\bb\.\s*a\.|\d\s*-\s*b\b"),
    ("leave_to_appeal", r"leave to appeal|\bc\.?\s*p\.?\s*l\.?\s*a\b|\bcpla\b|\bcrl?\.?\s*p\.?\s*l\.?\s*a\b"),
    ("intra_court_appeal", r"intra[- ]?court|\bi\.?\s*c\.?\s*a\b"),
    ("criminal_reference", r"murder reference|\bm\.?\s*r\.?\s*no\b|" + _CRIMINAL + r".{0,20}reference"),
    ("tax_reference", r"(?:tax|customs|sales tax|income tax|excise).{0,30}reference|\bi\.?\s*t\.?\s*r\b|\bs\.?\s*t\.?\s*r\b"),
    ("criminal_appeal", r"jail appeal|" + _CRIMINAL + r".{0,20}appeal|\bcr\.?\s*a\b|\bcrl\.?\s*a\b"),
    ("civil_appeal", r"\br\.?\s*f\.?\s*a\b|\br\.?\s*s\.?\s*a\b|\bf\.?\s*a\.?\s*o\b|appeal"),
    ("criminal_revision", _CRIMINAL + r".{0,20}revision|\bcr\.?\s*r\b"),
    ("civil_revision", r"revision"),
    ("review", r"\breview\b"),
    ("contempt", r"contempt"),
    ("execution", r"execution"),
    ("transfer", r"transfer"),
    # "C.P." is a petition; "C.P.C." / "Cr.P.C." are the procedure codes, not a proceeding
    ("constitutional_petition", r"writ|constitution|\bconst\.?\s*p|\bc\.?\s*p\b(?!\.?\s*c\b)|\bw\.?\s*p\b|\bc\.?\s*p\.?\s*d\b"),
    ("civil_suit", r"\bsuit\b"),
    ("criminal_miscellaneous", _CRIMINAL + r".{0,20}(?:misc|application|m\.?\s*a\b)"),
    ("civil_miscellaneous", r"misc|\bc\.?\s*m\.?\s*a\b|application"),
)
_COMPILED = tuple((name, re.compile(pat)) for name, pat in _RULES)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def find_case_number(text: Optional[str], route: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The case number: from the court's listing row when it gave one, else from the judgment heading."""
    for key in _ROUTE_KEYS:
        val = _squash(str((route or {}).get(key) or ""))
        if val and re.search(r"\d", val):
            return val[:300]
    for line in (text or "")[:4000].splitlines():
        m = _CASE_NO.search(line)
        if m:
            return _squash(m.group(0))[:300]
    return None


def proceeding_type(case_number: Optional[str], text: Optional[str] = None) -> str:
    """The kind of proceeding: from the case number, else from the heading of the judgment."""
    probes = [case_number] if case_number else []
    if text:
        probes.append((text or "")[:1500])
    for probe in probes:
        low = _squash(probe).lower()
        for name, pat in _COMPILED:
            if pat.search(low):
                return name
    return "other"


def unreported_identity(source_name: str, case_number: Optional[str], decision_date: Optional[date], text_hash: str) -> str:
    """The corpus identity of an unreported judgment: 'UNREPORTED <court> <case no> (<date>)'. Without a case
    number the text fingerprint stands in, so two different judgments never share an identity."""
    court = COURT_ABBR.get(source_name, source_name)[:20]
    when = decision_date.isoformat() if decision_date else None
    if case_number:
        ident = f"UNREPORTED {court} {_squash(case_number)[:140]}"
        return f"{ident} ({when})" if when else ident
    return f"UNREPORTED {court} {when or 'undated'} #{(text_hash or '')[:12]}"
