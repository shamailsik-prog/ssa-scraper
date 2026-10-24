"""Harvest split tokens taken from the live Citation Search map (never generic DB court names)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_COURT_ARRAY_RE = re.compile(
    r"(?:courtList|courtCodes|CitationCourt(?:List)?|courts?)\s*[:=]\s*\[([^\]]{1,8000})\]",
    re.IGNORECASE,
)
_STRING_LITERAL_RE = re.compile(r"""['"]([^'"]{1,80})['"]""")


def _dedupe(values: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for v in values:
        key = v.strip()
        if not key:
            continue
        fold = key.casefold()
        if fold in seen:
            continue
        seen.add(fold)
        out.append(key)
    return out


def _options_from_field(fields: Dict[str, Any], role: str) -> List[str]:
    """Option values only (never human labels); labels duplicate values and inflate split children."""
    field = fields.get(role) or {}
    opts = [str(o).strip() for o in (field.get("options") or []) if str(o).strip()]
    for pair in field.get("option_labels") or []:
        if isinstance(pair, (list, tuple)) and pair:
            opts.append(str(pair[0]).strip())
    return _dedupe(opts)


def capture_harvest_split_vocabulary(html: str, fields: Dict[str, Any]) -> Dict[str, List[str]]:
    """Parse dashboard HTML + mapped fields into split vocabularies stored on the search map."""
    courts: List[str] = _options_from_field(fields, "court")
    for match in _COURT_ARRAY_RE.finditer(html or ""):
        for lit in _STRING_LITERAL_RE.findall(match.group(1)):
            courts.append(lit)
    keywords: List[str] = []
    for role in ("keyword", "party"):
        keywords.extend(_options_from_field(fields, role))
    return {
        "courts": _dedupe([c for c in courts if c and not c.isdigit()]),
        "keywords": _dedupe(keywords),
    }


def harvest_split_vocabulary(search_map: Dict[str, Any]) -> Dict[str, List[str]]:
    limits = search_map.get("limits") or {}
    raw = limits.get("harvest_split_vocabulary")
    if isinstance(raw, dict):
        return {
            "courts": [str(x).strip() for x in (raw.get("courts") or []) if str(x).strip()],
            "keywords": [str(x).strip() for x in (raw.get("keywords") or []) if str(x).strip()],
        }
    return {"courts": [], "keywords": []}


def court_split_values(search_map: Dict[str, Any]) -> List[str]:
    vocab = harvest_split_vocabulary(search_map)
    if vocab["courts"]:
        return vocab["courts"]
    court = (search_map.get("fields") or {}).get("court") or {}
    return _dedupe([str(o).strip() for o in (court.get("options") or []) if str(o).strip()])


def keyword_split_values(search_map: Dict[str, Any]) -> Optional[List[str]]:
    """Return split tokens for keyword dimension; empty list means skip (no a–z guess)."""
    vocab = harvest_split_vocabulary(search_map)
    if vocab["keywords"]:
        return vocab["keywords"]
    for role in ("keyword", "party"):
        opts = _options_from_field(search_map.get("fields") or {}, role)
        if opts:
            return _dedupe(opts)
    return []
