"""Pure helpers for PakistanLawSite search-driven gap harvest (planner, splitter, accounting)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

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
    from scraper.search_form_roles import (
        apply_harvest_party_initial,
        apply_harvest_query_keyword,
        normalize_harvest_search_map,
    )
    from scraper.tasks.pakistanlawsite import build_values

    search_map = normalize_harvest_search_map(search_map)
    fill_map = _harvest_fill_search_map(search_map)
    fields = fill_map.get("fields") or {}
    tier_query = {k: query_json[k] for k in ("reporter", "year", "statute", "section", "keyword") if k in query_json}
    tier_cursor = dict(cursor)
    page = int(cursor.get("page") or cursor.get("page_no") or 1)
    if page > 1:
        if "page" in cursor and "page_no" not in tier_cursor:
            tier_cursor["page_no"] = cursor["page"]
    else:
        tier_cursor.pop("page_no", None)
    values = build_values(fill_map, tier_query, tier_cursor)
    for role in ("court", "category"):
        if role in fields and query_json.get(role):
            values[role] = str(query_json[role])
    apply_harvest_query_keyword(values, search_map, query_json)
    if "year" in fields and query_json.get("year") and "year" not in values:
        # court/judge/party x year seeds have no reporter: submit the year so each key is a distinct search
        values["year"] = str(query_json["year"])
    for role in ("month", "bench", "citation"):
        if role in fields and query_json.get(role) is not None:
            values[role] = str(query_json[role])
    apply_harvest_party_initial(values, search_map, query_json)
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
    from scraper.search_form_roles import normalize_harvest_search_map, resolve_harvest_fields
    from scraper.tasks.pakistanlawsite import unmapped_query_reason

    search_map = normalize_harvest_search_map(search_map)
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
    for role in ("month", "bench"):
        if query_json.get(role) is not None and role not in stored_fields:
            return f"search map cannot express {role} filter; missing role: {role}"
    if query_json.get("party_initial") is not None and not any(
        r in stored_fields or r in express_fields for r in ("party_initial", "party", "keyword")
    ):
        return "search map cannot express party_initial filter; missing role: party_initial, party, keyword"
    for role in ("judge", "party", "citation"):
        if query_json.get(role) and role not in stored_fields and "keyword" not in express_fields and "party" not in express_fields:
            return f"search map cannot express {role} query; missing roles: {role}, party/keyword"
    # A query is not narrowed if two independent text constraints write the same physical input.
    # Keyword splits alias to the party control on CitationSearch, where party/initial would overwrite it.
    text_roles = ("keyword", "party_initial", "party", "judge", "citation")
    active = [role for role in text_roles if query_json.get(role) is not None and str(query_json[role]).strip()]
    if len(active) > 1:
        base_query = {k: v for k, v in query_json.items() if k not in text_roles}
        base_values = build_harvest_form_values(search_map, base_query, cursor)
        physical_fields = ("keyword", "party_initial", "party", "judge", "citation")
        occupied: Dict[str, str] = {}
        for role in active:
            single = build_harvest_form_values(search_map, {**base_query, role: query_json[role]}, cursor)
            for field in physical_fields:
                if field not in single or single[field] == base_values.get(field):
                    continue
                previous = occupied.get(field)
                if previous is not None:
                    return (
                        f"search map cannot express {previous} and {role} independently; "
                        f"both target {field} control"
                    )
                occupied[field] = role
    if not build_harvest_form_values(search_map, query_json, cursor):
        return "search map cannot express harvest query; no usable mapped fields"
    return None


CASE_KEY_PREFIX = "case:"


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


# Site CaseName codes: 4-digit year, court/journal letters (SC, HCAJK, SCAJK, L, …), trailing digits.
CASE_NAME_CODE_PATTERN = r"\d{4}[A-Za-z]{1,8}\d{1,6}"
# Terminator must allow '&' (query string); only block another code character.
CASE_NAME_URL_SUBSTRING_PATTERN = rf"CaseName=({CASE_NAME_CODE_PATTERN})(?![0-9A-Za-z])"
_CASE_NAME_RE = re.compile(CASE_NAME_URL_SUBSTRING_PATTERN)


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


GAP_SAMPLES_LIMIT = 50


def row_gap_diagnostics(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = sorted(citation_keys_for_row(row))
    return {
        "citation": row.get("citation"),
        "title": row.get("title"),
        "detail_url": row.get("detail_url"),
        "pdf_url": row.get("pdf_url"),
        "keys": keys,
    }


def append_gap_sample(cursor: Dict[str, Any], row: Dict[str, Any]) -> None:
    samples = list(cursor.get("gap_samples") or [])
    if len(samples) >= GAP_SAMPLES_LIMIT:
        return
    samples.append(row_gap_diagnostics(row))
    cursor["gap_samples"] = samples


def unlinked_row_identity(row: Dict[str, Any]) -> str:
    keys = sorted(citation_keys_for_row(row))
    if keys:
        return "|".join(keys)
    citation = str(row.get("citation") or "").strip()
    title = str(row.get("title") or "").strip()
    return f"{citation}\0{title}"


def reset_lap_gap_diagnostics(cursor: Dict[str, Any]) -> None:
    """Start a new enumeration lap: fresh gap samples and per-lap unlinked identities."""
    cursor["gap_samples"] = []
    cursor["unlinked_keys"] = []
    cursor["rows_unlinked"] = 0


def note_unlinked_row(cursor: Dict[str, Any], row: Dict[str, Any]) -> int:
    ident = unlinked_row_identity(row)
    keys = list(cursor.get("unlinked_keys") or [])
    if ident not in keys:
        keys.append(ident)
        cursor["unlinked_keys"] = keys
    n = len(keys)
    cursor["rows_unlinked"] = n
    return n


def rows_unlinked_from_cursor(cursor: Optional[Dict[str, Any]]) -> int:
    if not cursor:
        return 0
    stored = cursor.get("unlinked_keys")
    if stored is not None:
        return len(stored)
    return int(cursor.get("rows_unlinked") or 0)


def gap_diagnostics_slice(cursor: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if cursor.get("gap_samples"):
        out["gap_samples"] = cursor["gap_samples"]
    if cursor.get("unlinked_keys"):
        out["unlinked_keys"] = cursor["unlinked_keys"]
    if cursor.get("rows_unlinked"):
        out["rows_unlinked"] = cursor["rows_unlinked"]
    return out


def cursor_reset_for_done(cursor: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve gap diagnostics when a query finishes."""
    return {"page": 1, "row_index": 0, **gap_diagnostics_slice(cursor)}


def unlinked_gap_credit(rows_seen: int, rows_known: int, rows_unlinked: int) -> int:
    """Linkless rows may only offset the seen-but-unknown portion of the gap."""
    seen_unknown = max(0, int(rows_seen) - int(rows_known))
    return min(max(0, int(rows_unlinked or 0)), seen_unknown)


def compute_gap_size(
    site_total: Optional[int],
    rows_known: int,
    rows_seen: int,
    *,
    rows_unlinked: int = 0,
) -> int:
    credited = unlinked_gap_credit(rows_seen, rows_known, rows_unlinked)
    if site_total is not None and site_total > rows_known:
        return max(0, int(site_total - rows_known - credited))
    if rows_seen > rows_known:
        return max(0, int(rows_seen - rows_known - credited))
    return 0


def unlinked_done_safety_error(rows_seen: int, rows_unlinked: int) -> str:
    if rows_unlinked <= 0:
        return ""
    if rows_unlinked > 25:
        return f"too many unmatched rows without detail_url ({rows_unlinked}>25)"
    if rows_seen > 0 and (rows_unlinked / rows_seen) > 0.05:
        return (
            f"unmatched-without-link ratio {rows_unlinked}/{rows_seen} exceeds 5%; not marking done"
        )
    return ""


def search_harvest_may_mark_done_unlinked(
    *,
    site_total: Optional[int],
    rows_seen: int,
    rows_known: int,
    rows_unlinked: int,
    pagination_confirmed: bool = False,
) -> Tuple[bool, str]:
    """Mark done_unlinked only after a confirmed last page when enumeration is complete (rows_seen meets or
    exceeds the site total, or the site showed no total) and every unseen row on the page is linkless."""
    if not pagination_confirmed or rows_unlinked <= 0:
        return False, ""
    safety = unlinked_done_safety_error(rows_seen, rows_unlinked)
    if safety:
        return False, safety
    if int(rows_seen) - int(rows_known) != int(rows_unlinked):
        return False, ""
    if site_total is not None and rows_seen < int(site_total):
        return False, ""
    return (
        True,
        f"pagination ended; {rows_unlinked} unmatched row(s) without detail_url on the last page",
    )


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
    pagination_gap: bool = False,
    search_map: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """A result list that reaches the site's cap is NOT complete. Return narrower child queries, narrowing by the
    first dimension of SPLIT_ORDER (month, court, bench, party first letter, keyword) that the parent has not
    used yet and that the search form can express (`available_roles`; default is the legacy court/keyword pair).
    Returns [] only when every expressible dimension is already used; the caller must then mark the job failed
    (exhausted), never done.

    ``pagination_gap``: pagination ended before every site-reported row was seen (PLD false-gap / linkless stall)."""
    if not pagination_gap and site_total < result_cap:
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
            if search_map is not None:
                from scraper.search_form_roles import keyword_splits_expressible

                if not keyword_splits_expressible(search_map):
                    logger.warning(
                        "PLS harvest skipping keyword split dimension: party/keyword control not expressible "
                        "on search map (query=%s)",
                        normalized,
                    )
                    continue
            splits = list(keyword_splits or _ALPHA_KEYWORD_SPLITS)
            return [normalize_query_json({**normalized, "keyword": kw}) for kw in splits]
    return []


def keyword_child_redundant_with_parent(
    *,
    parent_site_total: Optional[int],
    parent_rows_seen: int,
    child_site_total: Optional[int],
    child_rows_seen: int,
) -> bool:
    """True when a keyword/party letter child did not narrow vs its parent.

    When both parent and child ``site_total_results`` are known, redundancy is decided from equal totals
    (#340). When either total is unknown (common when the site omits the total element), fall back to
    equal enumerated ``rows_seen`` counts after pagination completed.
    """
    if parent_site_total is not None and child_site_total is not None:
        return int(child_site_total) == int(parent_site_total)
    if parent_site_total is None and child_site_total is None:
        return int(parent_rows_seen) == int(child_rows_seen)
    return False


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
    Unlinkable rows use status done_unlinked via search_harvest_may_mark_done_unlinked instead.
    """
    # An empty last page is not evidence of completeness when the site reports
    # more results than we have enumerated. Never turn a pagination gap into "done".
    if site_total is not None and rows_seen < int(site_total):
        return (
            False,
            f"pagination ended without next link with rows_seen={rows_seen}<site_total={site_total}",
        )
    # Only a genuinely empty query can finish without an independently known total.
    # An empty page after prior rows can be a transient pagination/access failure.
    if rows_on_page == 0 and rows_seen == 0:
        return True, ""
    if site_total is None:
        return False, "pagination ended without next link and site total unknown"
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
