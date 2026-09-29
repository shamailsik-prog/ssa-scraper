"""PakistanLawSite: reach CitationSearch through the authenticated dashboard, not a bare GET."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from scraper.auth.session_manager import Browser, LoginRequired, PageResult, raise_for_verdict
from scraper.config import settings
from scraper.extractors.deterministic import _has_archived_patient_grid, _has_logout_link, introspect_search_form

logger = logging.getLogger(__name__)

_LOGIN_PATH_MARKERS = ("/login/mainpage", "/login/login", "/login/index")


class CitationSearchNavigationFailed(RuntimeError):
    """CitationSearch did not render #archivedpatientGrid (session may still be valid)."""

    def __init__(self, message: str, *, page_type: str = "other", page: Optional[PageResult] = None):
        super().__init__(message)
        self.page_type = page_type
        self.page = page
_NO_RESULTS_STUB_RE = re.compile(r"no\s+more\s+result\s+found\s+on\s+your\s+search", re.IGNORECASE)


def pls_check_url() -> str:
    explicit = (getattr(settings, "PLS_CHECK_URL", None) or "").strip()
    if explicit:
        return explicit
    return f"{settings.PLS_BASE_URL.rstrip('/')}/Login/Check"


def classify_pls_page(html: str, url: str = "", metadata: Optional[Dict[str, Any]] = None) -> str:
    """Classify an authenticated PLS HTML surface for harvest diagnostics."""
    if citation_search_surface_is_harvestable(html or "", metadata):
        return "citation_search_grid"
    text = (html or "")[:50_000]
    if _NO_RESULTS_STUB_RE.search(text):
        return "citation_search_no_results"
    url_low = (url or "").lower()
    if "/login/check" in url_low and check_page_login_required_reason(html or "", url) is None:
        probe = introspect_search_form(html or "")
        if probe.get("surface") == "query_form":
            return "dashboard_with_citation_form"
        return "dashboard"
    probe = introspect_search_form(html or "")
    if probe.get("surface") == "query_form":
        return "citation_search_form_only"
    if probe.get("surface") == "grid_surface_no_query_form":
        return "citation_search_grid_chrome_only"
    if probe.get("surface") == "no_query_form":
        return "citation_search_empty_shell"
    if check_page_login_required_reason(html or "", url):
        return "login_or_check_failure"
    return "other"


def discover_citation_search_entrypoints(html: str, base_url: str) -> List[Dict[str, str]]:
    """Find dashboard links and AJAX URLs that load the citation search UI."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: List[Dict[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, href: str, label: str = "") -> None:
        absolute = urljoin(base_url, href)
        if absolute in seen:
            for item in found:
                if item["url"] == absolute and kind == "ajax":
                    item["kind"] = "ajax"
            return
        seen.add(absolute)
        found.append({"kind": kind, "url": absolute, "label": (label or href)[:120]})

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        text = anchor.get_text(" ", strip=True)
        low = f"{href} {text}".lower()
        if "citationsearch" in low or "citation search" in low:
            add("link", href, text)
    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        for match in re.finditer(r"""url\s*:\s*['"]([^'"]+)['"]""", body, flags=re.IGNORECASE):
            href = match.group(1)
            low = href.lower()
            if "statue" in low or "citation" in low or "search" in low:
                add("ajax", href, href)
        for match in re.finditer(r"""['"]((/Login/[^'"]+)|(/login/[^'"]+))['"]""", body, flags=re.IGNORECASE):
            path = match.group(1)
            low = path.lower()
            if "statue" in low or "citation" in low or "search" in low:
                add("ajax", path, path)
    return found


def check_page_login_required_reason(html: str, url: str = "") -> Optional[str]:
    """Return a login reason when /Login/Check is not an authenticated dashboard."""
    url_low = (url or "").lower()
    if any(marker in url_low for marker in _LOGIN_PATH_MARKERS):
        return "login surface URL"
    soup = BeautifulSoup(html or "", "html.parser")
    low = (html or "").lower()
    if 'type="password"' in low or "type='password'" in low:
        if any(token in low for token in ("mainloginform", "login.username", "login.password")):
            return "password login form on dashboard check"
    if not _has_logout_link(soup):
        return "dashboard check missing logout link"
    return None


def citation_search_surface_is_harvestable(html: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
    """True only when the live citation table (#archivedpatientGrid) is present.

    Dashboard AJAX fragments and whatsNewTable-style pages may classify as
    grid_surface_no_query_form without the real citation grid; they must not
    short-circuit navigation before the direct /Login/CitationSearch load.
    """
    meta = metadata or {}
    guard = str(meta.get("content_guard") or "")
    if guard in ("archivedpatientGrid_compact", "archivedpatientGrid_snapshot_failed"):
        return True
    soup = BeautifulSoup(html or "", "html.parser")
    return _has_archived_patient_grid(soup)


def _probe_to_search_map(probe: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for field in probe.get("fields") or []:
        role = field.get("role") or field.get("name")
        if not role:
            continue
        fields[str(role)] = {
            "name": field.get("name"),
            "selector": field.get("selector"),
            "kind": field.get("kind") or "text",
        }
    return {"fields": fields}


def _warmup_search_values(probe: Dict[str, Any]) -> Dict[str, str]:
    """Default citation-search warmup values (year, reporter, court, category) from settings."""
    values: Dict[str, str] = {}
    reporters = list(settings.subscribed_reporters or [])
    current_year = datetime.now(timezone.utc).year
    year = int(getattr(settings, "PLS_EARLIEST_YEAR", 0) or 0) or current_year
    fields_by_role = {f.get("role"): f for f in (probe.get("fields") or []) if f.get("role")}
    if "year" in fields_by_role:
        values["year"] = str(year)
    if "reporter" in fields_by_role and reporters:
        rep_field = fields_by_role["reporter"]
        opts = rep_field.get("options") or []
        pick = reporters[0]
        if opts and pick not in opts:
            for candidate in reporters:
                if candidate in opts:
                    pick = candidate
                    break
        values["reporter"] = pick
    for role in ("court", "category"):
        field = fields_by_role.get(role)
        if not field:
            continue
        opts = [str(o) for o in (field.get("options") or []) if str(o).strip()]
        if opts:
            values[role] = opts[0]
    return values


def _attempt_record(page: PageResult, candidate: Dict[str, str], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "kind": candidate.get("kind"),
        "label": candidate.get("label"),
        "url": page.url,
        "surface": introspect_search_form(page.html or "").get("surface"),
        "page_type": classify_pls_page(page.html or "", page.url or "", page.metadata),
    }
    if extra:
        record.update(extra)
    return record


def _ordered_navigation_candidates(discovery: List[Dict[str, str]]) -> List[Dict[str, str]]:
    order = {"ajax": 0, "link": 1, "direct_referer": 2, "direct": 9}
    candidates = sorted(discovery, key=lambda item: order.get(item.get("kind") or "link", 5))
    candidates.append(
        {
            "kind": "direct_referer",
            "url": settings.PLS_SEARCH_URL,
            "label": "PLS_SEARCH_URL with Referer=/Login/Check",
        }
    )
    return candidates


async def _submit_warmup_citation_query_form(
    browser: Browser,
    html: str,
    *,
    via: str,
) -> Optional[PageResult]:
    """POST the CitationSearch query form using warmup field values."""
    probe = introspect_search_form(html or "")
    if probe.get("surface") != "query_form":
        return None
    search_map = _probe_to_search_map(probe)
    if not search_map.get("fields"):
        return None
    values = _warmup_search_values(probe)
    page = await browser.submit_search(search_map, values)
    page.metadata = {
        **(page.metadata or {}),
        "pls_citation_search_nav": {"via": via, "warmup_values": values},
    }
    return page


async def _walk_navigation_candidates(
    browser: Browser,
    *,
    discovery: List[Dict[str, str]],
    referer: str,
    nav_meta: Dict[str, Any],
    archived_grid_start_row: int,
) -> PageResult:
    """Try discovered entrypoints and direct_referer; return the last page loaded."""
    last_page: Optional[PageResult] = None
    for candidate in _ordered_navigation_candidates(discovery):
        goto_kwargs: Dict[str, Any] = {"archived_grid_start_row": archived_grid_start_row}
        if candidate.get("kind") == "direct_referer":
            goto_kwargs["referer"] = referer
        page = await browser.goto(candidate["url"], **goto_kwargs)
        attempt = _attempt_record(page, candidate)
        nav_meta["attempts"].append(attempt)
        last_page = page
        if citation_search_surface_is_harvestable(page.html or "", page.metadata):
            page.metadata = {**(page.metadata or {}), "pls_citation_search_nav": nav_meta}
            return page
    assert last_page is not None
    return last_page


async def _apply_query_form_fallbacks(
    browser: Browser,
    *,
    check_url: str,
    check_html: str,
    last_page: PageResult,
    nav_meta: Dict[str, Any],
    archived_grid_start_row: int,
) -> PageResult:
    """CitationSearch form POST and dashboard form POST after candidate navigation misses the grid."""
    last_kind_before_fallback = classify_pls_page(last_page.html or "", last_page.url or "", last_page.metadata)
    if last_kind_before_fallback == "citation_search_form_only":
        citation_form_page = await _submit_warmup_citation_query_form(
            browser,
            last_page.html or "",
            via="citation_search_form_submit",
        )
        if citation_form_page is not None:
            nav_meta["attempts"].append(
                _attempt_record(
                    citation_form_page,
                    {"kind": "citation_form", "label": "citation_search_form_submit"},
                    extra={
                        "warmup": (citation_form_page.metadata or {}).get("pls_citation_search_nav", {}).get(
                            "warmup_values"
                        )
                    },
                )
            )
            if citation_search_surface_is_harvestable(citation_form_page.html or "", citation_form_page.metadata):
                citation_form_page.metadata = {
                    **(citation_form_page.metadata or {}),
                    "pls_citation_search_nav": {**nav_meta, "via": "citation_search_form_submit"},
                }
                return citation_form_page
            submit_kind = classify_pls_page(
                citation_form_page.html or "",
                citation_form_page.url or "",
                citation_form_page.metadata,
            )
            if submit_kind == "citation_search_no_results":
                last_page = citation_form_page
            else:
                after_probe = introspect_search_form(citation_form_page.html or "")
                if after_probe.get("surface") == "query_form":
                    last_page = citation_form_page

    form_page = await _submit_dashboard_citation_form(
        browser,
        check_url=check_url,
        check_html=check_html,
        archived_grid_start_row=archived_grid_start_row,
    )
    if form_page is not None:
        nav_meta["attempts"].append(
            _attempt_record(
                form_page,
                {"kind": "dashboard_form", "label": "dashboard_citation_form_submit"},
                extra={"warmup": (form_page.metadata or {}).get("pls_citation_search_nav", {}).get("warmup_values")},
            )
        )
        last_page = form_page
        if citation_search_surface_is_harvestable(form_page.html or "", form_page.metadata):
            form_page.metadata = {
                **(form_page.metadata or {}),
                "pls_citation_search_nav": {**nav_meta, "via": "dashboard_form_submit"},
            }
            return form_page
    elif introspect_search_form(check_html or "").get("surface") != "query_form":
        nav_meta["attempts"].append(
            {
                "kind": "dashboard_form",
                "label": "dashboard_citation_form_submit",
                "skipped": "no_query_form_on_login_check",
            }
        )
    return last_page


async def _submit_dashboard_citation_form(
    browser: Browser,
    *,
    check_url: str,
    check_html: str,
    archived_grid_start_row: int,
) -> Optional[PageResult]:
    probe = introspect_search_form(check_html or "")
    if probe.get("surface") != "query_form":
        return None
    await browser.goto(check_url)
    return await _submit_warmup_citation_query_form(
        browser,
        check_html,
        via="dashboard_form_submit",
    )


async def open_citation_search(browser: Browser, *, archived_grid_start_row: int = 0) -> PageResult:
    """Load /Login/Check, then follow discovered routes until a harvestable search surface appears."""
    check_url = pls_check_url()
    check_page = await browser.goto(check_url)
    login_reason = check_page_login_required_reason(check_page.html or "", check_page.url or check_url)
    if login_reason:
        raise LoginRequired(f"{login_reason} (landed on {check_page.url or check_url})")

    base = f"{urlsplit(check_page.url or check_url).scheme}://{urlsplit(check_page.url or check_url).netloc}"
    discovery = discover_citation_search_entrypoints(check_page.html or "", base)
    nav_meta: Dict[str, Any] = {
        "check_url": check_url,
        "discovered": discovery,
        "attempts": [],
    }
    logger.info(
        "PakistanLawSite citation-search navigation: check ok; discovered %s entrypoint(s): %s",
        len(discovery),
        ", ".join(f"{d.get('kind')}:{d.get('label')}" for d in discovery) or "(none)",
    )

    clicker = getattr(browser, "activate_citation_search_from_dashboard", None)
    if callable(clicker):
        try:
            clicked = await clicker(check_page=check_page, discovery=discovery, archived_grid_start_row=archived_grid_start_row)
            if clicked is not None and citation_search_surface_is_harvestable(clicked.html or "", clicked.metadata):
                clicked.metadata = {**(clicked.metadata or {}), "pls_citation_search_nav": {**nav_meta, "via": "dashboard_click"}}
                return clicked
            if clicked is not None:
                nav_meta["attempts"].append(
                    _attempt_record(
                        clicked,
                        {"kind": "dashboard_click", "label": "dashboard_click"},
                    )
                )
        except Exception as exc:
            logger.warning("PakistanLawSite dashboard citation-search click failed: %s", exc)
            nav_meta["click_error"] = str(exc)[:300]

    referer = check_page.url or check_url
    last_page = await _walk_navigation_candidates(
        browser,
        discovery=discovery,
        referer=referer,
        nav_meta=nav_meta,
        archived_grid_start_row=archived_grid_start_row,
    )
    if citation_search_surface_is_harvestable(last_page.html or "", last_page.metadata):
        return last_page

    last_page = await _apply_query_form_fallbacks(
        browser,
        check_url=check_url,
        check_html=check_page.html or "",
        last_page=last_page,
        nav_meta=nav_meta,
        archived_grid_start_row=archived_grid_start_row,
    )
    if citation_search_surface_is_harvestable(last_page.html or "", last_page.metadata):
        return last_page

    last_kind = classify_pls_page(last_page.html or "", last_page.url or "", last_page.metadata)
    if last_kind == "citation_search_no_results" and not nav_meta.get("stub_dashboard_recovery"):
        nav_meta["stub_dashboard_recovery"] = True
        fresh_check = await browser.goto(check_url)
        nav_meta["attempts"].append(
            _attempt_record(
                fresh_check,
                {"kind": "dashboard_refresh", "label": "refresh_login_check_after_no_results_stub"},
            )
        )
        fresh_base = f"{urlsplit(fresh_check.url or check_url).scheme}://{urlsplit(fresh_check.url or check_url).netloc}"
        fresh_discovery = discover_citation_search_entrypoints(fresh_check.html or "", fresh_base)
        fresh_referer = fresh_check.url or check_url
        last_page = await _walk_navigation_candidates(
            browser,
            discovery=fresh_discovery,
            referer=fresh_referer,
            nav_meta=nav_meta,
            archived_grid_start_row=archived_grid_start_row,
        )
        if citation_search_surface_is_harvestable(last_page.html or "", last_page.metadata):
            return last_page
        last_page = await _apply_query_form_fallbacks(
            browser,
            check_url=check_url,
            check_html=fresh_check.html or "",
            last_page=last_page,
            nav_meta=nav_meta,
            archived_grid_start_row=archived_grid_start_row,
        )
        if citation_search_surface_is_harvestable(last_page.html or "", last_page.metadata):
            return last_page

    last_kind = classify_pls_page(last_page.html or "", last_page.url or "", last_page.metadata)
    logger.error(
        "PakistanLawSite citation-search navigation failed: final page_type=%s url=%s attempts=%s",
        last_kind,
        last_page.url,
        nav_meta.get("attempts"),
    )
    last_page.metadata = {
        **(last_page.metadata or {}),
        "pls_citation_search_nav": nav_meta,
        "pls_page_type": last_kind,
    }
    return last_page


def _raise_unless_citation_grid_harvestable(page: PageResult) -> PageResult:
    """Grid harvest only: #archivedpatientGrid required (never the no-results stub)."""
    page_type = classify_pls_page(page.html or "", page.url or "", page.metadata)
    page.metadata = {**(page.metadata or {}), "pls_page_type": page_type}
    if page_type == "citation_search_no_results":
        raise CitationSearchNavigationFailed(
            "CitationSearch returned the no-results stub (not #archivedpatientGrid)",
            page_type=page_type,
            page=page,
        )
    if not citation_search_surface_is_harvestable(page.html or "", page.metadata):
        nav = (page.metadata or {}).get("pls_citation_search_nav") or {}
        raise CitationSearchNavigationFailed(
            f"CitationSearch navigation did not reach #archivedpatientGrid; attempts={len(nav.get('attempts') or [])}",
            page_type=page_type,
            page=page,
        )
    raise_for_verdict(page)
    return page


async def open_citation_grid_for_window(
    browser: Browser,
    *,
    archived_grid_start_row: int = 0,
) -> PageResult:
    """Reload the citation grid for the next window: fast referer GET, then one full dashboard navigation on stub."""
    check_url = pls_check_url()
    page = await browser.goto(
        settings.PLS_SEARCH_URL,
        referer=check_url,
        archived_grid_start_row=archived_grid_start_row,
    )
    page.metadata = {
        **(page.metadata or {}),
        "pls_citation_search_nav": {"via": "citation_grid_fast_reload", "attempts": []},
    }
    if citation_search_surface_is_harvestable(page.html or "", page.metadata):
        return _raise_unless_citation_grid_harvestable(page)

    page_type = classify_pls_page(page.html or "", page.url or "", page.metadata)
    if page_type == "citation_search_no_results":
        logger.warning(
            "PakistanLawSite citation-grid fast reload returned no-results stub at row=%s; retrying via dashboard navigation once",
            archived_grid_start_row,
        )
        page = await open_citation_search(browser, archived_grid_start_row=archived_grid_start_row)
        return _raise_unless_citation_grid_harvestable(page)

    return _raise_unless_citation_grid_harvestable(page)


async def open_citation_search_for_harvest(browser: Browser, *, archived_grid_start_row: int = 0) -> PageResult:
    page = await open_citation_search(browser, archived_grid_start_row=archived_grid_start_row)
    page_type = classify_pls_page(page.html or "", page.url or "", page.metadata)
    page.metadata = {**(page.metadata or {}), "pls_page_type": page_type}
    if page_type == "citation_search_no_results":
        raise CitationSearchNavigationFailed(
            "CitationSearch returned the no-results stub (not #archivedpatientGrid)",
            page_type=page_type,
            page=page,
        )
    if citation_search_surface_is_harvestable(page.html or "", page.metadata):
        raise_for_verdict(page)
        return page
    probe = introspect_search_form(page.html or "")
    if probe.get("surface") == "query_form" and (probe.get("fields") or []):
        # Authenticated CitationSearch query form (form-based harvest); not a grid-only chrome page.
        raise_for_verdict(page)
        return page
    nav = (page.metadata or {}).get("pls_citation_search_nav") or {}
    raise CitationSearchNavigationFailed(
        f"CitationSearch navigation did not reach #archivedpatientGrid; attempts={len(nav.get('attempts') or [])}",
        page_type=page_type,
        page=page,
    )
