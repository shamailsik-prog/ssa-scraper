"""Pure helpers for PakistanLawSite search-driven gap harvest (planner, splitter, accounting)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from scraper.config import KNOWN_REPORTERS
from scraper.parsers.citation_extractor import normalise_citation

_QUERY_KEY_ORDER = ("reporter", "year", "month", "court", "bench", "party_initial", "category", "keyword", "judge", "party", "statute", "section", "citation", "page_from", "page_to")

_ALPHA_KEYWORD_SPLITS = [chr(c) for c in range(ord("a"), ord("z") + 1)]


def normalize_query_json(query: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in query.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if key in ("year", "month", "page_from", "page_to"):
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
    if "year" in fields and query_json.get("year") and "year" not in values:
        # court/judge/party x year seeds have no reporter: submit the year so each key is a distinct search
        values["year"] = str(query_json["year"])
    for role in ("month", "bench", "party_initial", "citation"):
        if role in fields and query_json.get(role) is not None:
            values[role] = str(query_json[role])
    # judge / party / citation text with no dedicated field is searched through the free-text keyword box
    for role in ("judge", "party", "citation"):
        if query_json.get(role) and role not in fields and "keyword" in fields and "keyword" not in values:
            values["keyword"] = str(query_json[role])
        elif query_json.get(role) and role in fields:
            values[role] = str(query_json[role])
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
    if any(k in tier_query for k in ("reporter", "statute", "keyword")):
        reason = unmapped_query_reason(search_map, tier_query, tier_cursor)
        if reason:
            return reason
    elif (search_map.get("surface") or (search_map.get("limits") or {}).get("surface")) == "grid_surface_no_query_form":
        fields = search_map.get("fields") or {}
        if not any(role in fields for role in ("reporter", "year", "page", "keyword", "statute", "section", "citation_no")):
            return "CitationSearch surface is grid_surface_no_query_form; no query form is available"
    fields = search_map.get("fields") or {}
    if query_json.get("court") and "court" not in fields:
        return "search map cannot express court filter; missing role: court"
    if query_json.get("category") and "category" not in fields:
        return "search map cannot express category filter; missing role: category"
    if query_json.get("year") and "reporter" not in query_json and "year" not in fields:
        return "search map cannot express year filter; missing role: year"
    for role in ("month", "bench", "party_initial"):
        if query_json.get(role) is not None and role not in fields:
            return f"search map cannot express {role} filter; missing role: {role}"
    for role in ("judge", "party", "citation"):
        if query_json.get(role) and role not in fields and "keyword" not in fields:
            return f"search map cannot express {role} query; missing roles: {role}, keyword"
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


SPLIT_ORDER = ("month", "court", "bench", "party_initial", "keyword")
_MONTHS = list(range(1, 13))
_LEGACY_ROLES = frozenset({"court", "keyword"})


def split_oversized_query(
    query_json: Dict[str, Any],
    site_total: int,
    *,
    result_cap: int,
    court_options: Sequence[str],
    keyword_splits: Optional[Sequence[str]] = None,
    available_roles: Optional[Iterable[str]] = None,
    bench_options: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """A result list that reaches the site's cap is NOT complete. Return narrower child queries, narrowing by the
    first dimension of SPLIT_ORDER (month, court, bench, party first letter, keyword) that the parent has not
    used yet and that the search form can express (`available_roles`; default is the legacy court/keyword pair).
    Returns [] only when every expressible dimension is already used; the caller must then mark the job failed
    (exhausted), never done."""
    if site_total < result_cap:
        return []
    roles = set(available_roles) if available_roles is not None else set(_LEGACY_ROLES)
    normalized = normalize_query_json(query_json)
    for dim in SPLIT_ORDER:
        if dim in normalized or (dim == "month" and "month" not in roles):
            continue
        if dim == "month":
            return [normalize_query_json({**normalized, "month": m}) for m in _MONTHS]
        if dim == "court" and court_options:
            return [normalize_query_json({**normalized, "court": c}) for c in court_options if c]
        if dim == "bench" and "bench" in roles and bench_options:
            return [normalize_query_json({**normalized, "bench": b}) for b in bench_options if b]
        if dim == "party_initial" and "party_initial" in roles:
            return [normalize_query_json({**normalized, "party_initial": ch}) for ch in _ALPHA_KEYWORD_SPLITS]
        if dim == "keyword":
            splits = list(keyword_splits or _ALPHA_KEYWORD_SPLITS)
            return [normalize_query_json({**normalized, "keyword": kw}) for kw in splits]
    return []


def is_capped(site_total: Optional[int], rows_seen: int, result_cap: int) -> bool:
    """True when a result list must be treated as truncated: the site total reaches the cap, or (no total shown)
    the pages ended exactly at the cap."""
    if site_total is not None:
        return int(site_total) >= result_cap
    return rows_seen >= result_cap


def iter_extended_plan_queries(
    *,
    years: Sequence[int],
    courts: Sequence[str] = (),
    judges: Sequence[str] = (),
    statutes: Sequence[Dict[str, Any]] = (),
    keywords: Sequence[str] = (),
    parties: Sequence[str] = (),
) -> Iterable[Dict[str, Any]]:
    """Phase-2 seed families beyond journal x year. Every value comes from data we hold (court directory, judge
    table, statute table, caller keyword list); nothing is invented."""
    for court in courts:
        for year in years:
            yield normalize_query_json({"court": court, "year": year})
    for judge in judges:
        for year in years:
            yield normalize_query_json({"judge": judge, "year": year})
    for st in statutes:
        name = st.get("statute")
        if not name:
            continue
        sections = st.get("sections") or [None]
        for sec in sections:
            yield normalize_query_json({"statute": name, "section": sec})
    for kw in keywords:
        for court in courts or [None]:
            yield normalize_query_json({"keyword": kw, "court": court})
    for word in parties:
        for year in years:
            yield normalize_query_json({"party": word, "year": year})


def cited_citations_in_text(text: str) -> List[str]:
    """Citations that appear verbatim in a judgment body (snowball candidates). Only strings the citation grammar
    matches in the text are returned; none are constructed."""
    from scraper.parsers.citation_extractor import extract_citations

    out: List[str] = []
    seen: Set[str] = set()
    for hit in extract_citations(text or ""):
        norm = hit.get("normalized")
        if norm and norm not in seen and hit.get("page"):
            seen.add(norm)
            out.append(norm)
    return out


def page_continuity_gaps(pages: Sequence[int], *, max_step: int = 60) -> List[Tuple[int, int]]:
    """Consecutive starting pages of one reporter-year that are further apart than `max_step` pages: a judgment
    is rarely longer than that, so the jump marks a probable hole (returned as (from_page, to_page))."""
    ordered = sorted({int(p) for p in pages if p is not None})
    return [(a, b) for a, b in zip(ordered, ordered[1:]) if b - a > max_step]


def gap_report_sort_key(entry: Dict[str, Any]) -> Tuple[int, int, str]:
    gap = int(entry.get("gap_size") or 0)
    site = int(entry.get("site_total_results") or 0)
    key = str(entry.get("query_key") or "")
    return (-gap, -site, key)
