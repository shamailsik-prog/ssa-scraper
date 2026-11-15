"""PakistanLawSite Smart Search (AI) case-name enumeration (plan V6, 2026-11-15).

A natural-language query comes back as structured JSON. Two shapes are read:
the V6 envelope (`results` plus the interpretation flags) and an Elasticsearch
`hits.hits[]._source` envelope. Either may be the raw body or a
script#smartSearchPayload element. The headnote on a hit is not kept.

UNVERIFIED against a live response. The path and the query field below must be
confirmed before PLS_SMART_SEARCH_ENABLED is turned on. The query cap is 500
characters, the limit named in V6.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

from scraper.config import settings
from scraper.pls_case_enumeration import EnumeratedCase, EnumerationLimits, ListingPage, absolute_pls_url, citation_identity

# ---------------------------------------------------------------------------
# Smart Search surface. UNVERIFIED. Confirm this block on a live response.
# ---------------------------------------------------------------------------
SMART_SEARCH_PATH = "/Login/SmartSearch"
SMART_QUERY_FIELD = "q"
SMART_QUERY_SELECTOR = "textarea[name='q']"
SMART_JSON_SCRIPT = "script#smartSearchPayload"
SMART_SEARCH_STATE_KEY = "pls_smart_search"
SMART_QUERY_MAX_CHARS = 500


def limits_from_settings() -> EnumerationLimits:
    return EnumerationLimits(
        delay_min=float(settings.PLS_SMART_SEARCH_DELAY_MIN),
        delay_max=float(settings.PLS_SMART_SEARCH_DELAY_MAX),
        requests_per_day=int(settings.PLS_SMART_SEARCH_REQUESTS_PER_DAY),
        requests_per_month=int(settings.PLS_SMART_SEARCH_REQUESTS_PER_MONTH),
        seconds_per_day=float(settings.PLS_SMART_SEARCH_SECONDS_PER_DAY),
        seconds_per_month=float(settings.PLS_SMART_SEARCH_SECONDS_PER_MONTH),
        backoff_base_seconds=float(settings.PLS_SMART_SEARCH_BACKOFF_BASE_SECONDS),
        backoff_cap_seconds=float(settings.PLS_SMART_SEARCH_BACKOFF_CAP_SECONDS),
        max_listing_pages_per_tick=int(settings.PLS_SMART_SEARCH_MAX_LISTING_PAGES_PER_TICK),
        max_requests_per_run=int(settings.PLS_SMART_SEARCH_MAX_REQUESTS_PER_RUN),
    )


def smart_form_map() -> Dict[str, Any]:
    return {
        "form_action": absolute_pls_url(SMART_SEARCH_PATH),
        "fields": {
            SMART_QUERY_FIELD: {"selector": SMART_QUERY_SELECTOR, "kind": "text", "name": SMART_QUERY_FIELD},
            "page": {"selector": "input[name='page']", "kind": "text", "name": "page"},
            "submit": {"selector": "button[type='submit']", "kind": "submit"},
        },
    }


def smart_queries_from_settings() -> List[str]:
    """A JSON list, or one query per line. Blank idles. Nothing is invented."""
    raw = str(settings.PLS_SMART_SEARCH_QUERIES or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("PLS_SMART_SEARCH_QUERIES is not JSON; Smart Search idles")
            return []
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()]
    return [line.strip() for line in raw.splitlines() if line.strip()]


def smart_request(query: str, state: Dict[str, Any], *, max_chars: int) -> Optional[Dict[str, Any]]:
    """One listing request. A query longer than the cap is not sent and not truncated."""
    text = str(query or "").strip()
    if not text or len(text) > int(max_chars):
        return None
    page_no = int(state.get("page") or 1)
    next_url = state.get("next_url")
    if next_url and page_no > 1:
        target = str(next_url)
        if target.startswith("?"):
            target = SMART_SEARCH_PATH + target
        return {"url": absolute_pls_url(target), "form": None}
    form = {SMART_QUERY_FIELD: text}
    if page_no > 1:
        form["page"] = str(page_no)
    return {"url": None, "form": form, "path": SMART_SEARCH_PATH}


def _short_list(value: Any, limit: int = 12) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        text = str(item).strip()
        if text and len(text) <= 80:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _compact_interpretation(data: Dict[str, Any]) -> Dict[str, Any]:
    """The AI's own reading of the query. Headnote text is not copied."""
    interpretation = data.get("interpretation") if isinstance(data.get("interpretation"), dict) else {}
    filters = data.get("appliedFilters") if isinstance(data.get("appliedFilters"), dict) else {}
    short_filters: Dict[str, Any] = {}
    for key, value in list(filters.items())[:12]:
        if not isinstance(value, (str, int, float, bool)):
            continue
        rendered = str(value)
        if len(rendered) <= 80:
            short_filters[str(key)[:40]] = value if not isinstance(value, str) else rendered
    return {
        "search_concepts": _short_list(interpretation.get("search_concepts")),
        "appliedFilters": short_filters,
        "pinnedCitations": _short_list(data.get("pinnedCitations")),
        "titleSearchUsed": bool(data.get("titleSearchUsed")),
        "courtFilterRelaxed": bool(data.get("courtFilterRelaxed")),
        "semanticFallbackUsed": bool(data.get("semanticFallbackUsed")),
        "semanticSearchConcepts": _short_list(data.get("semanticSearchConcepts")),
    }


def _load_payload(payload: str) -> Optional[Dict[str, Any]]:
    text = (payload or "").strip()
    if not text:
        return None
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    soup = BeautifulSoup(text, "html.parser")
    node = soup.select_one(SMART_JSON_SCRIPT)
    if node is None:
        return None
    raw = node.string if node.string is not None else node.get_text()
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _hit_list(data: Dict[str, Any]) -> Optional[List[Any]]:
    results = data.get("results")
    if isinstance(results, list):
        return results
    hits = data.get("hits")
    if isinstance(hits, dict) and isinstance(hits.get("hits"), list):
        return list(hits.get("hits") or [])
    return None


def _next_url(data: Dict[str, Any]) -> Optional[str]:
    explicit = data.get("next_url") or data.get("next")
    if explicit:
        return str(explicit).strip() or None
    page = data.get("page")
    total = data.get("total_pages")
    if isinstance(page, int) and isinstance(total, int) and page < total:
        return f"?page={page + 1}"
    return None


def _case_from_hit(hit: Any) -> Optional[EnumeratedCase]:
    if not isinstance(hit, dict):
        return None
    source = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
    citation_raw = str(source.get("citation") or "").strip()
    title = re.sub(r"\s+", " ", str(source.get("case_title") or source.get("title") or "")).strip()
    court_raw = source.get("court_display") or source.get("court")
    court = str(court_raw).strip() if court_raw else None
    case_name = str(source.get("case_name") or source.get("caseName") or "").strip()
    link = str(source.get("case_link") or source.get("link") or source.get("url") or "").strip()
    if case_name:
        detail = absolute_pls_url(f"/Login/ReferenceCaseLawSearch?CaseName={case_name}")
    elif link:
        detail = absolute_pls_url(link)
    else:
        detail = ""
    citation, reporter, _forms = citation_identity(citation_raw, court) if citation_raw else ("", "", [])
    if not citation and not detail and not title:
        return None
    parallel: List[str] = []
    for extra in source.get("parallel_citations") or []:
        text = str(extra).strip()
        if not text:
            continue
        stored, _reporter, forms = citation_identity(text, court)
        for form in forms or ([stored] if stored else []):
            if form and form != citation and form not in parallel:
                parallel.append(form)
    return EnumeratedCase(
        citation=citation,
        title=title,
        court=court,
        detail_url=detail or None,
        reporter=reporter,
        parallel_citations=parallel,
    )


def parse_smart_search(payload: str) -> ListingPage:
    """Read one Smart Search body. A page that is neither JSON shape is not an empty result."""
    data = _load_payload(payload)
    if data is None:
        return ListingPage(rows=[], next_url=None, recognized=False)
    hits = _hit_list(data)
    if hits is None:
        return ListingPage(rows=[], next_url=None, recognized=False)
    rows: List[EnumeratedCase] = []
    seen = set()
    for hit in hits:
        row = _case_from_hit(hit)
        if row is None:
            continue
        identity = (row.citation, row.detail_url, row.title)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    return ListingPage(
        rows=rows,
        next_url=_next_url(data),
        recognized=True,
        interpretation=_compact_interpretation(data),
    )
