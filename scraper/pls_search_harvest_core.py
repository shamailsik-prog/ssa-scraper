"""Pure helpers for PakistanLawSite search-driven gap harvest (planner, splitter, accounting)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from scraper.config import KNOWN_REPORTERS
from scraper.parsers.citation_extractor import normalise_citation

_QUERY_KEY_ORDER = ("reporter", "year", "court", "category", "keyword", "statute", "section")

_ALPHA_KEYWORD_SPLITS = [chr(c) for c in range(ord("a"), ord("z") + 1)]


def normalize_query_json(query: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in query.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if key == "year":
            out[key] = int(text)
        else:
            out[key] = text
    return out


def make_query_key(query_json: Dict[str, Any]) -> str:
    normalized = normalize_query_json(query_json)
    parts = [f"{key}={normalized[key]}" for key in _QUERY_KEY_ORDER if key in normalized]
    if not parts:
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()[:16]
        return f"search:empty:{digest}"
    return "search:" + "|".join(parts)


def reporters_for_plan(
    *,
    subscribed: Sequence[str],
    map_reporters: Sequence[str],
    fallback: Sequence[str] = KNOWN_REPORTERS,
) -> List[str]:
    if subscribed:
        return list(subscribed)
    if map_reporters:
        return list(map_reporters)
    return list(fallback)


def years_for_plan(earliest_year: int, current_year: int) -> List[int]:
    if earliest_year <= 0:
        earliest_year = current_year
    start = min(current_year, earliest_year)
    end = max(current_year, earliest_year)
    return list(range(end, start - 1, -1))


def iter_base_plan_queries(
    reporters: Sequence[str],
    years: Sequence[int],
    *,
    courts: Optional[Sequence[Optional[str]]] = None,
    keywords: Optional[Sequence[Optional[str]]] = None,
) -> Iterable[Dict[str, Any]]:
    """Generate systematic (reporter × year × optional court × optional keyword) queries."""
    court_values = list(courts) if courts is not None else [None]
    keyword_values = list(keywords) if keywords is not None else [None]
    for reporter in reporters:
        rep = str(reporter).strip()
        if not rep:
            continue
        for year in years:
            for court in court_values:
                for keyword in keyword_values:
                    q: Dict[str, Any] = {"reporter": rep, "year": int(year)}
                    if court:
                        q["court"] = str(court)
                    if keyword:
                        q["keyword"] = str(keyword)
                    yield normalize_query_json(q)


def build_harvest_form_values(
    search_map: Dict[str, Any],
    query_json: Dict[str, Any],
    cursor: Dict[str, Any],
) -> Dict[str, str]:
    """Map a harvest query onto search-form roles (extends tier frontier build_values)."""
    from scraper.tasks.pakistanlawsite import build_values

    tier_query = {k: query_json[k] for k in ("reporter", "year", "statute", "section", "keyword") if k in query_json}
    tier_cursor = dict(cursor)
    if "page" in cursor and "page_no" not in cursor:
        tier_cursor["page_no"] = cursor["page"]
    values = build_values(search_map, tier_query, tier_cursor)
    fields = search_map.get("fields") or {}
    for role in ("court", "category"):
        if role in fields and query_json.get(role):
            values[role] = str(query_json[role])
    if "keyword" in fields and query_json.get("keyword") and "keyword" not in values:
        values["keyword"] = str(query_json["keyword"])
    if "page" in fields:
        page = cursor.get("page") or cursor.get("page_no") or 1
        values["page"] = str(page)
    return values


def unmapped_harvest_reason(search_map: Dict[str, Any], query_json: Dict[str, Any], cursor: Dict[str, Any]) -> Optional[str]:
    from scraper.tasks.pakistanlawsite import unmapped_query_reason

    tier_query = {k: query_json[k] for k in ("reporter", "year", "statute", "section", "keyword") if k in query_json}
    tier_cursor = dict(cursor)
    if "page" in cursor and "page_no" not in cursor:
        tier_cursor["page_no"] = cursor["page"]
    reason = unmapped_query_reason(search_map, tier_query, tier_cursor)
    if reason:
        return reason
    fields = search_map.get("fields") or {}
    if query_json.get("court") and "court" not in fields:
        return "search map cannot express court filter; missing role: court"
    if query_json.get("category") and "category" not in fields:
        return "search map cannot express category filter; missing role: category"
    if not build_harvest_form_values(search_map, query_json, cursor):
        return "search map cannot express harvest query; no usable mapped fields"
    return None


def citation_keys_for_row(row: Dict[str, Any]) -> Set[str]:
    keys: Set[str] = set()
    raw = str(row.get("citation") or "").strip()
    if raw:
        keys.add(raw)
        normalized = normalise_citation(raw)
        if normalized:
            keys.add(normalized)
    return keys


def partition_rows_by_known(
    rows: Sequence[Dict[str, Any]],
    known_citations: Set[str],
) -> Tuple[int, List[Dict[str, Any]]]:
    known = 0
    new_rows: List[Dict[str, Any]] = []
    for row in rows:
        keys = citation_keys_for_row(row)
        if keys and keys & known_citations:
            known += 1
        else:
            new_rows.append(row)
    return known, new_rows


def compute_gap_size(
    site_total: Optional[int],
    rows_known: int,
    rows_seen: int,
) -> int:
    if site_total is not None and site_total > rows_known:
        return int(site_total - rows_known)
    if rows_seen > rows_known:
        return int(rows_seen - rows_known)
    return 0


def parse_total_results_from_html(html: str) -> Optional[int]:
    text = (html or "")[:8000]
    match = re.search(r"(?i)(\d[\d,]*)\s+(?:results?|records?|cases?)\s+found", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def split_oversized_query(
    query_json: Dict[str, Any],
    site_total: int,
    *,
    result_cap: int,
    court_options: Sequence[str],
    keyword_splits: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """When site_total exceeds the cap, return finer child queries (court, then keyword prefix)."""
    if site_total <= result_cap:
        return []
    normalized = normalize_query_json(query_json)
    if not normalized.get("court") and court_options:
        return [normalize_query_json({**normalized, "court": court}) for court in court_options if court]
    if not normalized.get("keyword"):
        splits = list(keyword_splits or _ALPHA_KEYWORD_SPLITS)
        return [normalize_query_json({**normalized, "keyword": kw}) for kw in splits]
    return []


def gap_report_sort_key(entry: Dict[str, Any]) -> Tuple[int, int, str]:
    gap = int(entry.get("gap_size") or 0)
    site = int(entry.get("site_total_results") or 0)
    key = str(entry.get("query_key") or "")
    return (-gap, -site, key)
