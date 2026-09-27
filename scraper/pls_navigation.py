"""PakistanLawSite: reach CitationSearch through the authenticated dashboard, not a bare GET."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from scraper.auth.session_manager import Browser, LoginRequired, PageResult, raise_for_verdict
from scraper.config import settings
from scraper.extractors.deterministic import _has_archived_patient_grid, _has_logout_link, introspect_search_form

logger = logging.getLogger(__name__)

_LOGIN_PATH_MARKERS = ("/login/mainpage", "/login/login", "/login/index")


def pls_check_url() -> str:
    explicit = (getattr(settings, "PLS_CHECK_URL", None) or "").strip()
    if explicit:
        return explicit
    return f"{settings.PLS_BASE_URL.rstrip('/')}/Login/Check"


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
    meta = metadata or {}
    guard = str(meta.get("content_guard") or "")
    if guard in ("archivedpatientGrid_compact", "archivedpatientGrid_snapshot_failed"):
        return True
    soup = BeautifulSoup(html or "", "html.parser")
    if _has_archived_patient_grid(soup):
        return True
    probe = introspect_search_form(html or "")
    return probe.get("surface") in ("query_form", "grid_surface_no_query_form")


def _ordered_navigation_candidates(discovery: List[Dict[str, str]]) -> List[Dict[str, str]]:
    order = {"ajax": 0, "link": 1, "direct": 9}
    candidates = sorted(discovery, key=lambda item: order.get(item.get("kind") or "link", 5))
    candidates.append({"kind": "direct", "url": settings.PLS_SEARCH_URL, "label": "PLS_SEARCH_URL"})
    return candidates


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
                    {
                        "kind": "dashboard_click",
                        "surface": introspect_search_form(clicked.html or "").get("surface"),
                        "url": clicked.url,
                    }
                )
        except Exception as exc:
            logger.warning("PakistanLawSite dashboard citation-search click failed: %s", exc)
            nav_meta["click_error"] = str(exc)[:300]

    last_page = check_page
    for candidate in _ordered_navigation_candidates(discovery):
        page = await browser.goto(candidate["url"], archived_grid_start_row=archived_grid_start_row)
        attempt = {
            "kind": candidate.get("kind"),
            "label": candidate.get("label"),
            "url": page.url,
            "surface": introspect_search_form(page.html or "").get("surface"),
        }
        nav_meta["attempts"].append(attempt)
        last_page = page
        if citation_search_surface_is_harvestable(page.html or "", page.metadata):
            page.metadata = {**(page.metadata or {}), "pls_citation_search_nav": nav_meta}
            return page

    last_page.metadata = {**(last_page.metadata or {}), "pls_citation_search_nav": nav_meta}
    return last_page


async def open_citation_search_for_harvest(browser: Browser, *, archived_grid_start_row: int = 0) -> PageResult:
    page = await open_citation_search(browser, archived_grid_start_row=archived_grid_start_row)
    raise_for_verdict(page)
    return page
