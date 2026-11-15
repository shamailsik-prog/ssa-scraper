"""Shared case-name enumeration for PakistanLawSite Advance Search and Smart Search.

A listing is turned into case keys and handed to the existing judgment pipeline
(fetch, stage, promote). This module does not open the site. Known citations,
parallel citations, and CaseName keys use the same helpers as IndexSearch.
Day and month budgets are UTC calendar periods, the same clock IndexSearch uses.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set
from urllib.parse import parse_qs, urljoin, urlsplit

logger = logging.getLogger(__name__)

from scraper.config import settings
from scraper.pls_search_harvest_core import citation_keys_for_row, citation_lookup_keys

HARVEST_ADVANCE = "advance_search"
HARVEST_SMART = "smart_search"


class EnumerationChallenge(RuntimeError):
    """The listing or the case page came back as a login wall, a verification page, or a block."""


@dataclass
class EnumeratedCase:
    citation: str
    title: str
    court: Optional[str]
    detail_url: Optional[str]
    reporter: str
    parallel_citations: List[str] = field(default_factory=list)


@dataclass
class ListingPage:
    rows: List[EnumeratedCase]
    next_url: Optional[str]
    recognized: bool
    interpretation: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnumerationLimits:
    delay_min: float
    delay_max: float
    requests_per_day: int
    requests_per_month: int
    seconds_per_day: float
    seconds_per_month: float
    backoff_base_seconds: float
    backoff_cap_seconds: float
    max_listing_pages_per_tick: int
    max_requests_per_run: int


def absolute_pls_url(path_or_url: str) -> str:
    raw = (path_or_url or "").strip()
    base = settings.PLS_BASE_URL.rstrip("/")
    if not raw:
        return base
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("/"):
        return base + raw
    if raw.startswith("?"):
        return base + raw
    return urljoin(base + "/", raw)


def citation_identity(citation: str, court: Optional[str]) -> tuple[str, str, List[str]]:
    """Stored citation, reporter code, and lookup forms. Notes and PLD courts match IndexSearch."""
    from scraper.pls_index_search import _INDEX_CITE, _forms_from_hit
    from scraper.tasks.pakistanlawsite import reporter_from_citation

    text = str(citation or "").strip()
    match = _INDEX_CITE.search(text) if text else None
    if match is not None:
        stored, reporter, forms = _forms_from_hit(match, court)
        return stored, reporter, list(forms)
    from scraper.parsers.citation_extractor import normalise_citation

    stored = normalise_citation(text) or text
    return stored, reporter_from_citation(stored), ([stored] if stored else [])


def row_keys(row: EnumeratedCase) -> Set[str]:
    payload = {
        "citation": row.citation,
        "detail_url": row.detail_url,
        "parallel_citations": list(row.parallel_citations),
    }
    keys = citation_keys_for_row(payload)
    for extra in row.parallel_citations:
        keys |= citation_lookup_keys(extra)
    if row.citation:
        keys |= citation_lookup_keys(row.citation)
    return keys


def roll_periods(state: Dict[str, Any], now: datetime) -> None:
    """Reset day and month counters on the UTC calendar date. A restart does not move the window."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    if state.get("day") != day:
        state["day"] = day
        state["day_requests"] = 0
        state["day_seconds"] = 0.0
    if state.get("month") != month:
        state["month"] = month
        state["month_requests"] = 0
        state["month_seconds"] = 0.0


def budget_block_reason(
    state: Dict[str, Any],
    now: datetime,
    limits: EnumerationLimits,
    *,
    upcoming_seconds: float = 0.0,
    account_month_seconds: float = 0.0,
    account_month_cap: Optional[float] = None,
) -> Optional[str]:
    roll_periods(state, now)
    if int(state.get("day_requests") or 0) >= limits.requests_per_day:
        return "day_requests"
    if int(state.get("month_requests") or 0) >= limits.requests_per_month:
        return "month_requests"
    day_seconds = float(state.get("day_seconds") or 0)
    month_seconds = float(state.get("month_seconds") or 0)
    if day_seconds >= limits.seconds_per_day or day_seconds + upcoming_seconds > limits.seconds_per_day:
        return "day_seconds"
    if month_seconds >= limits.seconds_per_month or month_seconds + upcoming_seconds > limits.seconds_per_month:
        return "month_seconds"
    if account_month_cap is not None and float(account_month_cap) > 0:
        spent = month_seconds + float(account_month_seconds)
        if spent >= float(account_month_cap) or spent + float(upcoming_seconds) > float(account_month_cap):
            return "account_month_seconds"
    until = state.get("backoff_until")
    if until:
        try:
            deadline = datetime.fromisoformat(str(until))
        except ValueError:
            deadline = None
        if deadline is not None and now < deadline:
            return "backoff"
    return None


def record_request(state: Dict[str, Any], seconds: float, *, loads: int = 1) -> None:
    """Count page loads. A form listing that opens the landing page and the result counts as two."""
    count = max(1, int(loads))
    state["day_requests"] = int(state.get("day_requests") or 0) + count
    state["month_requests"] = int(state.get("month_requests") or 0) + count
    state["day_seconds"] = float(state.get("day_seconds") or 0) + float(seconds)
    state["month_seconds"] = float(state.get("month_seconds") or 0) + float(seconds)


def note_failure(state: Dict[str, Any], now: datetime, limits: EnumerationLimits) -> None:
    step = int(state.get("backoff_step") or 0) + 1
    delay = min(limits.backoff_cap_seconds, limits.backoff_base_seconds * (2 ** (step - 1)))
    state["backoff_step"] = step
    state["backoff_until"] = (now + timedelta(seconds=delay)).isoformat()


def clear_backoff(state: Dict[str, Any]) -> None:
    state["backoff_step"] = 0
    state["backoff_until"] = None


def query_text_problem(value: str) -> Optional[str]:
    """Plan rule for an operator query: no quotes, no boolean operators, no parentheses."""
    text = str(value or "")
    if '"' in text or "'" in text:
        return "quotes"
    if "(" in text or ")" in text:
        return "parentheses"
    if re.search(r"\b(?:OR|NOT)\b", text, re.IGNORECASE):
        return "boolean operator"
    return None


def page_only_number(target: str, form_path: str) -> Optional[str]:
    """The page number when a next link carries nothing else. None when the query string is real."""
    raw = str(target or "").strip()
    if not raw:
        return None
    if "://" in raw:
        parsed = urlsplit(raw)
    elif raw.startswith("?"):
        parsed = urlsplit("http://pls-page.invalid/" + raw)
    else:
        path = raw if raw.startswith("/") else "/" + raw
        parsed = urlsplit("http://pls-page.invalid" + path)
    query = parse_qs(parsed.query)
    if set(query) != {"page"}:
        return None
    number = str((query.get("page") or [""])[0]).strip()
    if not number:
        return None
    got = parsed.path or ""
    if got not in ("", "/", form_path):
        return None
    return number


def _request_loads(request: Any) -> int:
    if isinstance(request, dict):
        try:
            return max(1, int(request.get("loads") or 1))
        except (TypeError, ValueError):
            return 1
    return 1


def _blank_streak(state: Dict[str, Any], query_index: int) -> int:
    if state.get("blank_query") != query_index:
        state["blank_query"] = query_index
        state["blank_pages"] = 0
    state["blank_pages"] = int(state.get("blank_pages") or 0) + 1
    return int(state["blank_pages"])


def _clear_blank(state: Dict[str, Any]) -> None:
    state["blank_pages"] = 0
    state.pop("blank_query", None)


_BLANK_PAGE_LIMIT = 2


def _current_query(queries: Sequence[Any], state: Dict[str, Any]):
    if not queries:
        return None, 0
    idx = int(state.get("query_index") or 0)
    if idx < 0 or idx >= len(queries):
        idx = 0
    state["query_index"] = idx
    return queries[idx], idx


_LISTING_KEYS = ("listing_held", "listing_query_index", "listing_next_url", "listing_rows")


def _store_listing(state: Dict[str, Any], page: ListingPage, query_index: int) -> None:
    """Keep the parsed listing on the cursor. The page HTML is not stored."""
    state["listing_held"] = True
    state["listing_query_index"] = query_index
    state["listing_next_url"] = page.next_url
    state["listing_rows"] = [
        {
            "citation": row.citation,
            "title": row.title,
            "court": row.court,
            "detail_url": row.detail_url,
            "reporter": row.reporter,
            "parallel_citations": list(row.parallel_citations),
        }
        for row in page.rows
    ]


def _clear_listing(state: Dict[str, Any]) -> None:
    for key in _LISTING_KEYS:
        state.pop(key, None)


def _cached_listing(state: Dict[str, Any], query_index: int) -> Optional[ListingPage]:
    if state.get("listing_held") is not True:
        return None
    if state.get("listing_query_index") != query_index:
        return None
    raw_rows = state.get("listing_rows")
    if not isinstance(raw_rows, list):
        return None
    rows: List[EnumeratedCase] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        parallel = item.get("parallel_citations")
        if not isinstance(parallel, list):
            parallel = []
        court = item.get("court")
        detail = item.get("detail_url")
        rows.append(
            EnumeratedCase(
                citation=str(item.get("citation") or ""),
                title=str(item.get("title") or ""),
                court=str(court) if court else None,
                detail_url=str(detail) if detail else None,
                reporter=str(item.get("reporter") or ""),
                parallel_citations=[str(value) for value in parallel],
            )
        )
    next_url = state.get("listing_next_url")
    return ListingPage(rows=rows, next_url=str(next_url) if next_url else None, recognized=True)


async def run_case_enumeration(
    *,
    harvest: str,
    state: Dict[str, Any],
    now: datetime,
    limits: EnumerationLimits,
    queries: Sequence[Any],
    build_request: Callable[[Any, Dict[str, Any]], Optional[Dict[str, Any]]],
    parse_listing: Callable[[str], ListingPage],
    fetch_listing: Callable[[Dict[str, Any]], Awaitable[str]],
    fetch_detail: Callable[[str], Awaitable[Any]],
    preserve: Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[str]],
    known_keys: Callable[[Set[str]], Awaitable[Set[str]]],
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
    rng: Any,
    harvest_claimable: bool,
    persist: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    account_month_seconds: float = 0.0,
    account_month_cap: Optional[float] = None,
) -> Dict[str, Any]:
    """Open at most one listing page per tick unless the page cap says otherwise.

    The hard per-run cap counts listing pages and case pages in this call. It stops
    before the request that would pass it, and the cursor stays on the unfetched row.
    The listing rows stay on the cursor too, so the next tick continues with case
    pages and does not spend another day or month request repeating the listing.
    """
    state = dict(state or {})
    roll_periods(state, now)
    if harvest_claimable:
        return {"stopped": "search_harvest", "state": state, "staged": 0, "known": 0}
    if not queries:
        return {"stopped": "no_queries", "state": state, "staged": 0, "known": 0}

    async def save() -> None:
        if persist is not None:
            await persist(dict(state))

    run_requests = 0

    async def paced(call, *, loads: int = 1):
        nonlocal run_requests
        from scraper.tasks.pakistanlawsite import PacingBudgetExceeded

        count = max(1, int(loads))
        if run_requests + count > int(limits.max_requests_per_run):
            return None, "run_cap"
        delay = float(limits.delay_min if rng is None else rng.uniform(limits.delay_min, limits.delay_max))
        block = budget_block_reason(
            state,
            now,
            limits,
            upcoming_seconds=delay,
            account_month_seconds=account_month_seconds,
            account_month_cap=account_month_cap,
        )
        if block:
            return None, block
        started = monotonic()
        await sleep(delay)
        try:
            value = await call()
        except PacingBudgetExceeded:
            await save()
            return None, "login_pacing"
        except Exception as exc:
            logger.warning("%s request failed: %s", harvest, type(exc).__name__)
            record_request(state, max(0.0, monotonic() - started), loads=count)
            run_requests += count
            note_failure(state, now, limits)
            await save()
            return None, "backoff"
        record_request(state, max(0.0, monotonic() - started), loads=count)
        run_requests += count
        await save()
        return value, None

    def stop(reason: str, *, staged: int, known: int) -> Dict[str, Any]:
        return {"stopped": reason, "state": state, "staged": staged, "known": known}

    staged = 0
    known = 0
    pages_opened = 0
    skipped_queries = 0
    page_budget = max(1, int(limits.max_listing_pages_per_tick))
    while pages_opened < page_budget:
        query, idx = _current_query(queries, state)
        if query is None:
            return stop("no_queries", staged=staged, known=known)
        page = _cached_listing(state, idx)
        if page is None:
            request = build_request(query, state)
            if request is None:
                _clear_listing(state)
                skipped_queries += 1
                state["query_index"] = (idx + 1) % len(queries)
                state["page"] = 1
                state["row_index"] = 0
                state["next_url"] = None
                if skipped_queries >= len(queries):
                    await save()
                    return stop("no_sendable_query", staged=staged, known=known)
                continue
            html, failure = await paced(lambda req=request: fetch_listing(req), loads=_request_loads(request))
            if failure:
                return stop(failure, staged=staged, known=known)
            page = parse_listing(html or "")
            if not page.recognized or not page.rows:
                _clear_listing(state)
                streak = _blank_streak(state, idx)
                if streak >= _BLANK_PAGE_LIMIT:
                    logger.warning("%s skipped query %s after %s unrecognized or empty pages", harvest, idx, streak)
                    _clear_blank(state)
                    state["query_index"] = (idx + 1) % len(queries)
                    state["page"] = 1
                    state["row_index"] = 0
                    state["next_url"] = None
                    await save()
                    return stop("empty_query", staged=staged, known=known)
                if not page.recognized:
                    note_failure(state, now, limits)
                    await save()
                    return stop("unrecognized_page", staged=staged, known=known)
            else:
                _clear_blank(state)
                if page.interpretation:
                    state["last_interpretation"] = dict(page.interpretation)
                _store_listing(state, page, idx)
                await save()
        pages_opened += 1
        clear_backoff(state)
        start_row = int(state.get("row_index") or 0)
        for index, row in enumerate(page.rows):
            if index < start_row:
                continue
            keys = row_keys(row)
            try:
                held = await known_keys(keys) if keys else set()
            except Exception as exc:
                logger.warning("%s known-key lookup failed: %s", harvest, type(exc).__name__)
                note_failure(state, now, limits)
                state["row_index"] = index
                state["query_index"] = idx
                await save()
                return stop("backoff", staged=staged, known=known)
            if keys and keys & set(held or ()):
                known += 1
                state["row_index"] = index + 1
                continue
            if not row.detail_url:
                state["row_index"] = index + 1
                state["unlinked"] = int(state.get("unlinked") or 0) + 1
                continue
            detail, failure = await paced(lambda url=row.detail_url: fetch_detail(url), loads=1)
            if failure:
                state["row_index"] = index
                state["query_index"] = idx
                await save()
                return stop(failure, staged=staged, known=known)
            route = {
                "harvest": harvest,
                "query_index": idx,
                "page": int(state.get("page") or 1),
                "row_index": index,
            }
            payload = {
                "citation": row.citation,
                "title": row.title,
                "court": row.court,
                "detail_url": row.detail_url,
                "parallel_citations": list(row.parallel_citations),
            }
            try:
                await preserve(detail, route, payload)
            except Exception as exc:
                logger.warning("%s preserve failed: %s", harvest, type(exc).__name__)
                note_failure(state, now, limits)
                state["row_index"] = index
                state["query_index"] = idx
                await save()
                return stop("backoff", staged=staged, known=known)
            staged += 1
            state["row_index"] = index + 1
            clear_backoff(state)
            await save()
        _clear_listing(state)
        page_no = int(state.get("page") or 1)
        state["query_index"] = idx
        state["row_index"] = 0
        if page.next_url:
            state["page"] = page_no + 1
            state["next_url"] = page.next_url
        else:
            state["next_url"] = None
            state["page"] = 1
            state["query_index"] = (idx + 1) % len(queries)
        await save()
    return {"stopped": None, "state": state, "staged": staged, "known": known}
