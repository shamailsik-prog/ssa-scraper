"""PakistanLawSite Advance Search case-name enumeration (plan V1, field session 2026-11-15).

Multi-field: court, judge, lawyer, party, keyword, rule, act, section, and a second
act/section pair. There is no year control. Results are the recorded case table:
tr.caseType, a citation cell, a party cell, a court cell, and a Read button whose
casetypeid is the CaseName.

UNVERIFIED against a live Advance Search form. Every selector and the field name
below must be confirmed before PLS_ADVANCE_SEARCH_ENABLED is turned on. The result
row shape is the one already captured on keyword listings in this repo.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

from scraper.config import settings
from scraper.pls_case_enumeration import EnumeratedCase, EnumerationLimits, ListingPage, absolute_pls_url

# ---------------------------------------------------------------------------
# Advance Search surface. The path is the one the route census already records.
# Field selectors are UNVERIFIED. Confirm this block on a live form.
# ---------------------------------------------------------------------------
ADVANCE_SEARCH_PATH = "/Login/AdvanceSearch"
ADVANCE_RESULTS_SELECTOR = "#advanceSearchResults"
ADVANCE_ENTRY_SELECTOR = "tr.caseType"
ADVANCE_CASE_ID_ATTRS = ("casetypeid", "data-casetypeid")
LOAD_MORE_LABEL = "load more"
LOAD_MORE_URL_ATTRS = ("href", "data-href", "data-url", "formaction")
ADVANCE_SEARCH_STATE_KEY = "pls_advance_search"
ADVANCE_QUERY_FIELDS = (
    "court",
    "judge",
    "lawyer",
    "party",
    "keyword",
    "rule",
    "act",
    "section",
    "act2",
    "section2",
)
_FIELD_SELECTORS = {
    "court": ("select[name='court']", "select"),
    "judge": ("input[name='judge']", "text"),
    "lawyer": ("input[name='lawyer']", "text"),
    "party": ("input[name='party']", "text"),
    "keyword": ("input[name='keyword']", "text"),
    "rule": ("input[name='rule']", "text"),
    "act": ("input[name='act']", "text"),
    "section": ("input[name='section']", "text"),
    "act2": ("input[name='act2']", "text"),
    "section2": ("input[name='section2']", "text"),
    "page": ("input[name='page']", "text"),
}


def limits_from_settings() -> EnumerationLimits:
    return EnumerationLimits(
        delay_min=float(settings.PLS_ADVANCE_SEARCH_DELAY_MIN),
        delay_max=float(settings.PLS_ADVANCE_SEARCH_DELAY_MAX),
        requests_per_day=int(settings.PLS_ADVANCE_SEARCH_REQUESTS_PER_DAY),
        requests_per_month=int(settings.PLS_ADVANCE_SEARCH_REQUESTS_PER_MONTH),
        seconds_per_day=float(settings.PLS_ADVANCE_SEARCH_SECONDS_PER_DAY),
        seconds_per_month=float(settings.PLS_ADVANCE_SEARCH_SECONDS_PER_MONTH),
        backoff_base_seconds=float(settings.PLS_ADVANCE_SEARCH_BACKOFF_BASE_SECONDS),
        backoff_cap_seconds=float(settings.PLS_ADVANCE_SEARCH_BACKOFF_CAP_SECONDS),
        max_listing_pages_per_tick=int(settings.PLS_ADVANCE_SEARCH_MAX_LISTING_PAGES_PER_TICK),
        max_requests_per_run=int(settings.PLS_ADVANCE_SEARCH_MAX_REQUESTS_PER_RUN),
    )


def advance_form_map() -> Dict[str, Any]:
    """The form submit_search fills. There is no year field."""
    fields: Dict[str, Any] = {}
    for name, (selector, kind) in _FIELD_SELECTORS.items():
        fields[name] = {"selector": selector, "kind": kind, "name": name}
    fields["submit"] = {"selector": "button[type='submit']", "kind": "submit"}
    return {"form_action": absolute_pls_url(ADVANCE_SEARCH_PATH), "fields": fields}


def advance_queries_from_settings() -> List[Dict[str, str]]:
    """Configured queries only. A blank list idles. A year on a query is discarded."""
    raw = str(settings.PLS_ADVANCE_SEARCH_QUERIES or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("PLS_ADVANCE_SEARCH_QUERIES is not JSON; Advance Search idles")
        return []
    if not isinstance(data, list):
        return []
    queries: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        query: Dict[str, str] = {}
        for key in ADVANCE_QUERY_FIELDS:
            value = str(item.get(key) or "").strip()
            if value:
                query[key] = value
        if query:
            queries.append(query)
    return queries


def advance_request(query: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One listing request. Year is never submitted, including when the query carries one."""
    if not isinstance(query, dict):
        return None
    filled = {key: str(query.get(key) or "").strip() for key in ADVANCE_QUERY_FIELDS}
    if not any(filled.values()):
        return None
    page_no = int(state.get("page") or 1)
    next_url = state.get("next_url")
    if next_url and page_no > 1:
        target = str(next_url)
        if target.startswith("?"):
            target = ADVANCE_SEARCH_PATH + target
        return {"url": absolute_pls_url(target), "form": None}
    form = dict(filled)
    if page_no > 1:
        form["page"] = str(page_no)
    return {"url": None, "form": form, "path": ADVANCE_SEARCH_PATH}


def _load_more_url(soup: BeautifulSoup) -> Optional[str]:
    nodes = list(soup.select("#readMoreAdvanceSearch"))
    nodes.extend(soup.find_all(["a", "button", "input"]))
    seen = set()
    for tag in nodes:
        if id(tag) in seen:
            continue
        seen.add(id(tag))
        label = tag.get_text(" ", strip=True) or str(tag.get("value") or tag.get("aria-label") or "")
        named = str(tag.get("id") or "") == "readMoreAdvanceSearch"
        if not named and label.strip().lower() != LOAD_MORE_LABEL:
            continue
        for attr in LOAD_MORE_URL_ATTRS:
            value = tag.get(attr)
            if value:
                return str(value).strip()
        page = tag.get("data-page")
        if page:
            return f"?page={page}"
    return None


def _case_name(tag) -> str:
    if tag is None:
        return ""
    for attr in ADVANCE_CASE_ID_ATTRS:
        value = tag.get(attr)
        if value:
            return str(value).strip()
    return ""


def _party_title(cell) -> str:
    for span in cell.find_all("span"):
        span.decompose()
    text = cell.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _row_from_entry(entry) -> Optional[EnumeratedCase]:
    from scraper.pls_case_enumeration import citation_identity

    detail = ""
    case_name = ""
    for tag in entry.find_all(["input", "button", "a"]):
        case_name = _case_name(tag)
        if case_name:
            break
        href = str(tag.get("href") or "")
        if "casename=" in href.lower():
            detail = absolute_pls_url(re.sub(r"(?i)caseName=", "CaseName=", href))
            case_name = ""
            break
    if case_name:
        detail = absolute_pls_url(f"/Login/ReferenceCaseLawSearch?CaseName={case_name}")
    cells = entry.find_all("td")
    citation_text = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
    court = cells[3].get_text(" ", strip=True) if len(cells) > 3 else ""
    title = _party_title(cells[2]) if len(cells) > 2 else ""
    if not citation_text and not detail:
        return None
    citation, reporter, _forms = citation_identity(citation_text, court or None)
    return EnumeratedCase(
        citation=citation,
        title=title,
        court=court or None,
        detail_url=detail or None,
        reporter=reporter,
    )


def parse_advance_page(html: str) -> ListingPage:
    """Read one hand-built or live Advance Search page. An unknown shell is not an empty result."""
    soup = BeautifulSoup(html or "", "html.parser")
    next_url = _load_more_url(soup)
    rows: List[EnumeratedCase] = []
    seen = set()
    for entry in soup.select(ADVANCE_ENTRY_SELECTOR):
        row = _row_from_entry(entry)
        if row is None:
            continue
        identity = (row.citation, row.detail_url, row.title)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    recognized = bool(soup.select_one(ADVANCE_RESULTS_SELECTOR)) or bool(rows) or bool(next_url)
    return ListingPage(rows=rows, next_url=next_url, recognized=recognized)
