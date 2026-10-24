"""Pure helpers for PakistanLawSite search-driven gap harvest (planner, splitter, accounting)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from scraper.config import KNOWN_REPORTERS
from scraper.keys import fit_key
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


SEARCH_HARVEST_SLOTS = (1, 2)


def normalise_reporter(reporter: Any) -> str:
    """A reporter name compared without case, spaces or dots: "P.Cr.L.J." and "PCrLJ" are one reporter."""
    return re.sub(r"[\s.]", "", str(reporter or "")).upper()


def slot1_reporter_share(csv: Optional[str]) -> FrozenSet[str]:
    """Login slot 1's reporters (PLS_SEARCH_HARVEST_SLOT1_REPORTERS), normalised. Slot 2 has everything else."""
    return frozenset(n for n in (normalise_reporter(p) for p in str(csv or "").split(",")) if n)


def search_harvest_slot_for(query_json: Dict[str, Any], slot1_share: FrozenSet[str]) -> int:
    """The login slot whose share holds a ledger row: slot 1 for its listed reporters, slot 2 for every other
    reporter and for rows with no reporter, so each row belongs to exactly one slot (handbook R3.1)."""
    return 1 if normalise_reporter((query_json or {}).get("reporter")) in slot1_share else 2


def make_query_key(query_json: Dict[str, Any]) -> str:
    normalized = normalize_query_json(query_json)
    parts = [f"{key}={normalized[key]}" for key in _QUERY_KEY_ORDER if key in normalized]
    if not parts:
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()[:16]
        return f"search:empty:{digest}"
    return fit_key("search:" + "|".join(parts))


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


def _harvest_fill_search_map(search_map: Dict[str, Any]) -> Dict[str, Any]:
    """Stored map only: never inject the party→keyword alias into submit values."""
    fields = dict(search_map.get("fields") or {})
    fields.pop("_all", None)
    out = dict(search_map)
    out["fields"] = fields
    return out


def _prune_empty_harvest_values(values: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in values.items() if v is not None and str(v).strip() != ""}


def build_harvest_form_values(
    search_map: Dict[str, Any],
    query_json: Dict[str, Any],
    cursor: Dict[str, Any],
) -> Dict[str, str]:
    """Map a harvest query onto search-form roles (extends tier frontier build_values)."""
    from scraper.tasks.pakistanlawsite import build_values

    fill_map = _harvest_fill_search_map(search_map)
    fields = fill_map.get("fields") or {}
    tier_query = {k: query_json[k] for k in ("reporter", "year", "statute", "section", "keyword") if k in query_json}
    tier_cursor = dict(cursor)
    page = int(cursor.get("page") or cursor.get("page_no") or 1)
    # Page 1 is a fresh Citation Search submit: do not pass page_no into build_values (it would fill the
    # party/keyword box as a pagination surrogate on dashboard maps without a page control).
    if page > 1:
        if "page" in cursor and "page_no" not in tier_cursor:
            tier_cursor["page_no"] = cursor["page"]
    else:
        tier_cursor.pop("page_no", None)
    values = build_values(fill_map, tier_query, tier_cursor)
    for role in ("court", "category"):
        if role in fields and query_json.get(role):
            values[role] = str(query_json[role])
    from scraper.search_form_roles import apply_harvest_query_keyword

    apply_harvest_query_keyword(values, search_map, query_json)
    if "year" in fields and query_json.get("year") and "year" not in values:
        # court/judge/party x year seeds have no reporter: submit the year so each key is a distinct search
        values["year"] = str(query_json["year"])
    for role in ("month", "bench", "party_initial", "citation"):
        if role in fields and query_json.get(role) is not None:
            values[role] = str(query_json[role])
    # judge / party / citation text with no dedicated field is searched through the free-text party/keyword box
    for role in ("judge", "party", "citation"):
        if not query_json.get(role):
            continue
        if role in fields:
            values[role] = str(query_json[role])
        elif query_json.get("keyword"):
            continue
        elif "party" in fields and "party" not in values:
            values["party"] = str(query_json[role])
        elif "keyword" in fields:
            values["keyword"] = str(query_json[role])
    if "page" in fields and page > 1:
        values["page"] = str(page)
    return _prune_empty_harvest_values(values)


def unmapped_harvest_reason(search_map: Dict[str, Any], query_json: Dict[str, Any], cursor: Dict[str, Any]) -> Optional[str]:
    from scraper.search_form_roles import resolve_harvest_fields
    from scraper.tasks.pakistanlawsite import unmapped_query_reason

    express_fields = resolve_harvest_fields(search_map)
    express_map = dict(search_map)
    express_map["fields"] = express_fields
    tier_query = {k: query_json[k] for k in ("reporter", "year", "statute", "section", "keyword") if k in query_json}
    tier_cursor = dict(cursor)
    page = int(cursor.get("page") or cursor.get("page_no") or 1)
    if page > 1 and "page" in cursor and "page_no" not in tier_cursor:
        tier_cursor["page_no"] = cursor["page"]
    elif page <= 1:
        tier_cursor.pop("page_no", None)
    if any(k in tier_query for k in ("reporter", "statute", "keyword")):
        reason = unmapped_query_reason(express_map, tier_query, tier_cursor)
        if reason:
            return reason
    elif (search_map.get("surface") or (search_map.get("limits") or {}).get("surface")) == "grid_surface_no_query_form":
        if not any(
            role in express_fields
            for role in ("reporter", "year", "page", "keyword", "party", "statute", "section", "citation_no")
        ):
            return "CitationSearch surface is grid_surface_no_query_form; no query form is available"
    stored_fields = _harvest_fill_search_map(search_map).get("fields") or {}
    if query_json.get("court") and "court" not in stored_fields:
        return "search map cannot express court filter; missing role: court"
    if query_json.get("category") and "category" not in stored_fields:
        return "search map cannot express category filter; missing role: category"
    if query_json.get("year") and "reporter" not in query_json and "year" not in stored_fields:
        return "search map cannot express year filter; missing role: year"
    for role in ("month", "bench", "party_initial"):
        if query_json.get(role) is not None and role not in stored_fields:
            return f"search map cannot express {role} filter; missing role: {role}"
    for role in ("judge", "party", "citation"):
        if query_json.get(role) and role not in stored_fields and "keyword" not in express_fields and "party" not in express_fields:
            return f"search map cannot express {role} query; missing roles: {role}, keyword"
    if query_json.get("keyword") and "keyword" not in express_fields and "party" not in stored_fields:
        return "search map cannot express keyword query; missing usable role: keyword"
    if not build_harvest_form_values(search_map, query_json, cursor):
        return "search map cannot express harvest query; no usable mapped fields"
    return None


CASE_KEY_PREFIX = "case:"


def harvest_result_fingerprint_from_keys(keys: Set[str]) -> str:
    """Stable hash of a result row-set (citation / case-name keys) for split-effectiveness checks."""
    if not keys:
        return ""
    payload = "\n".join(sorted(keys))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def harvest_result_fingerprint(rows: Sequence[Dict[str, Any]]) -> str:
    keys: Set[str] = set()
    for row in rows:
        keys.update(citation_keys_for_row(row))
    return harvest_result_fingerprint_from_keys(keys)


def citation_keys_for_row(row: Dict[str, Any]) -> Set[str]:
    """Keys that identify a result row's judgment: its citation (raw and normalised) and, from its detail link, its
    CaseName ("case:2025L8"). Search-harvest rows carry no citation column, so without the CaseName every row
    already held looked new and was fetched again."""
    keys: Set[str] = set()
    raw = str(row.get("citation") or "").strip()
    if raw:
        keys.add(raw)
        normalized = normalise_citation(raw)
        if normalized:
            keys.add(normalized)
    name = case_name_from_url(row.get("detail_url") or row.get("pdf_url"))
    if name:
        keys.add(CASE_KEY_PREFIX + name)
    return keys


_CASE_NAME_RE = re.compile(r"CaseName=(\d{4}[A-Za-z]{1,3}\d{1,6})(?![0-9A-Za-z])")


def case_name_from_url(url: Optional[str]) -> Optional[str]:
    m = _CASE_NAME_RE.search(url or "")
    return m.group(1).upper() if m else None


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
            if keyword_splits is None:
                from scraper.config import settings

                if getattr(settings, "PLS_SEARCH_HARVEST_LEGACY_ALPHA_KEYWORD_SPLITS", False):
                    splits = list(_ALPHA_KEYWORD_SPLITS)
                else:
                    continue
            else:
                splits = list(keyword_splits)
                if not splits:
                    continue
            return [normalize_query_json({**normalized, "keyword": kw}) for kw in splits]
    return []


def is_journal_year_query(query_json: Dict[str, Any]) -> bool:
    """Reporter × year with no narrower split dimensions already set."""
    normalized = normalize_query_json(query_json)
    if not normalized.get("reporter") or normalized.get("year") is None:
        return False
    for dim in SPLIT_ORDER:
        if dim in normalized:
            return False
    extra = set(normalized.keys()) - {"reporter", "year"}
    return not extra


def journal_year_truncation_rows(result_cap: int) -> int:
    from scraper.config import settings

    floor = int(getattr(settings, "PLS_SEARCH_HARVEST_JOURNAL_YEAR_TRUNCATION_ROWS", 0) or 0)
    if floor > 0:
        return min(floor, result_cap)
    return result_cap


def harvest_result_cap() -> int:
    from scraper.config import settings

    legacy = int(getattr(settings, "PLS_SEARCH_RESULT_CAP", 500) or 500)
    if getattr(settings, "PLS_SEARCH_HARVEST_USE_OBSERVED_400_CAP", False):
        return int(getattr(settings, "PLS_SEARCH_HARVEST_OBSERVED_RESULT_CAP", 400) or 400)
    return legacy


def should_auto_split_journal_year(
    query_json: Dict[str, Any],
    *,
    rows_seen: int,
    site_total: Optional[int],
    result_cap: int,
    has_next_page: bool,
) -> bool:
    from scraper.config import settings

    if not getattr(settings, "PLS_SEARCH_HARVEST_AUTO_SPLIT_JOURNAL_YEAR", False):
        return False
    if has_next_page or not is_journal_year_query(query_json):
        return False
    if site_total is not None and rows_seen >= int(site_total):
        return False
    trunc = journal_year_truncation_rows(result_cap)
    if site_total is not None and int(site_total) < trunc:
        return False
    if site_total is not None and rows_seen < int(site_total):
        return rows_seen >= trunc or int(site_total) >= trunc
    return rows_seen >= trunc


def split_dimension_added(parent: Dict[str, Any], child: Dict[str, Any]) -> Optional[str]:
    p = normalize_query_json(parent)
    c = normalize_query_json(child)
    for dim in SPLIT_ORDER:
        if dim in c and dim not in p:
            return dim
    return None


def split_children_union_incomplete(
    parent_rows_seen: int,
    child_key_sets: Sequence[Set[str]],
) -> bool:
    """True when finished split children do not account for every row the parent enumerated."""
    if parent_rows_seen <= 0 or not child_key_sets:
        return False
    union: Set[str] = set()
    for keys in child_key_sets:
        union |= keys
    return len(union) < parent_rows_seen


def split_children_cover_rows(
    parent: Dict[str, Any],
    children: Sequence[Dict[str, Any]],
    *,
    rows_seen: int,
    result_cap: int,
) -> bool:
    """Deprecated pre-split heuristic; callers should enqueue when children exist and use split_children_union_incomplete after children finish."""
    return bool(children)


def is_capped(site_total: Optional[int], rows_seen: int, result_cap: int) -> bool:
    """True when a result list must be treated as truncated: the site total reaches the cap, or (no total shown)
    the pages ended exactly at the cap."""
    if site_total is not None:
        return int(site_total) >= result_cap
    return rows_seen >= result_cap


def search_harvest_may_mark_done(
    *,
    site_total: Optional[int],
    rows_seen: int,
    rows_on_page: int,
) -> Tuple[bool, str]:
    """When the extractor reports no next-page link, decide if the query may be marked done.

    Done only for a genuinely empty page, or when the site total is known and every row was seen.
    """
    if rows_on_page == 0:
        return True, ""
    if site_total is None:
        return False, "pagination ended without next link and site total unknown"
    if rows_seen < int(site_total):
        return (
            False,
            f"pagination ended without next link with rows_seen={rows_seen}<site_total={site_total}",
        )
    return True, ""


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
