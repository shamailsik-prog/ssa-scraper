"""PakistanLawSite IndexSearch harvest (plan V8, field session 2026-11-15).

The site's index lists every case in a journal volume: journal + year, and court
where the volume is split. Each page is 50 cases and continues with Load More.
This module turns that listing into case keys and hands new ones to the existing
judgment pipeline (fetch, stage, promote). It does not open the site by itself.

UNVERIFIED against a live page. Every selector, field name, and the path below
must be confirmed before PLS_INDEX_SEARCH_ENABLED is turned on. Nothing else in
the scraper should name an IndexSearch selector.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

from scraper.config import canonical_reporter, settings
from scraper.parsers.citation_extractor import PLD_COURT_CANONICAL, normalise_citation
from scraper.pls_search_harvest_core import citation_keys_for_row, citation_lookup_keys, years_for_plan

# ---------------------------------------------------------------------------
# IndexSearch surface. UNVERIFIED. Confirm this whole block on a live page.
# ---------------------------------------------------------------------------
INDEX_SEARCH_PATH = "/Login/IndexSearch"
INDEX_RESULTS_SELECTOR = "#indexSearchResults"
INDEX_ENTRY_SELECTOR = "#indexSearchResults .index-entry, #indexSearchResults tr"
INDEX_JOURNAL_SELECTOR = "select[name='journal']"
INDEX_YEAR_SELECTOR = "select[name='year']"
INDEX_COURT_SELECTOR = "select[name='court']"
INDEX_PAGE_SELECTOR = "input[name='page']"
INDEX_SUBMIT_SELECTOR = "button[type='submit']"
LOAD_MORE_LABEL = "load more"
LOAD_MORE_URL_ATTRS = ("href", "data-href", "data-url", "formaction")
DETAIL_HREF_NEEDLES = ("ReferenceCaseLawSearch", "GetCaseFile")
INDEX_SEARCH_STATE_KEY = "pls_index_search"
# Blank option of select[name=court] ("All courts"). A journal that is not
# court-split always submits this value so a court left selected for PLD cannot
# stay selected. Confirm on a live page that value="" is that option before the
# flag is turned on. If the live option is a different value, change this constant.
INDEX_COURT_ALL_VALUE = ""

# Word-form Note lines use the notes code as the row reporter, matching the glued
# line (YLR Note and YLRN are both YLRN). The citation string stays the word form,
# and that string is the stored key. This map is not copied into the citation or
# the preserve payload.
_NOTE_ROW_REPORTER = {
    "YLR": "YLRN",
    "CLC": "CLCN",
    "PCrLJ": "PCrLJN",
    "PLC": "PLCN",
    "PLC(CS)": "PLC(CS)N",
}

# A parent journal's index is not the notes edition. Subscribing to the parent
# adds the notes edition (and PLC's civil-service series) to the walk.
_NOTES_OF_PARENT = {
    "CLC": ("CLCN",),
    "YLR": ("YLRN",),
    "PCrLJ": ("PCrLJN",),
    "PCRLJ": ("PCrLJN",),
    "PLC": ("PLCN", "PLC(CS)", "PLC(CS)N"),
    "PLC(CS)": ("PLC(CS)N",),
}

_INDEX_CITE = re.compile(
    r"(?P<year>19\d{2}|20\d{2})\s+"
    r"(?P<reporter>PLC\s*\(\s*C\.?\s*S\.?\s*\)N|PLC\s*\(\s*C\.?\s*S\.?\s*\)|"
    r"PCrLJN|CLCN|YLRN|PLCN|PCrLJ|SCMR|CLC|YLR|MLD|CLD|PTD|GBLR|PLD|PLC)"
    r"(?:\s+(?P<note>Note))?"
    r"\s+(?P<page>\d+[A-Z]?)\b",
    re.IGNORECASE,
)
_TRAILING_COURT = re.compile(r"\((?P<court>[A-Za-z0-9][^)]*)\)\s*$")
_SERIES_PAREN = re.compile(r"^C\.?\s*S\.?$", re.IGNORECASE)
_CASE_NAME_PARAM = re.compile(r"(?i)caseName=")


class IndexSearchChallenge(RuntimeError):
    """The index page came back as a login wall, a verification page, or a block."""


@dataclass
class IndexRow:
    citation: str
    title: str
    court: Optional[str]
    detail_url: Optional[str]
    reporter: str
    parallel_citations: List[str] = field(default_factory=list)


@dataclass
class IndexPage:
    rows: List[IndexRow]
    next_url: Optional[str]
    recognized: bool


@dataclass(frozen=True)
class IndexSearchLimits:
    delay_min: float
    delay_max: float
    requests_per_day: int
    requests_per_month: int
    seconds_per_day: float
    seconds_per_month: float
    backoff_base_seconds: float
    backoff_cap_seconds: float
    max_index_pages_per_tick: int
    page_size: int


def limits_from_settings() -> IndexSearchLimits:
    return IndexSearchLimits(
        delay_min=float(settings.PLS_INDEX_SEARCH_DELAY_MIN),
        delay_max=float(settings.PLS_INDEX_SEARCH_DELAY_MAX),
        requests_per_day=int(settings.PLS_INDEX_SEARCH_REQUESTS_PER_DAY),
        requests_per_month=int(settings.PLS_INDEX_SEARCH_REQUESTS_PER_MONTH),
        seconds_per_day=float(settings.PLS_INDEX_SEARCH_SECONDS_PER_DAY),
        seconds_per_month=float(settings.PLS_INDEX_SEARCH_SECONDS_PER_MONTH),
        backoff_base_seconds=float(settings.PLS_INDEX_SEARCH_BACKOFF_BASE_SECONDS),
        backoff_cap_seconds=float(settings.PLS_INDEX_SEARCH_BACKOFF_CAP_SECONDS),
        max_index_pages_per_tick=int(settings.PLS_INDEX_SEARCH_MAX_PAGES_PER_TICK),
        page_size=int(settings.PLS_INDEX_SEARCH_PAGE_SIZE),
    )


def absolute_index_url(path_or_url: str) -> str:
    raw = (path_or_url or "").strip()
    base = settings.PLS_BASE_URL.rstrip("/")
    if not raw:
        return base + INDEX_SEARCH_PATH
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("?"):
        return base + INDEX_SEARCH_PATH + raw
    if raw.startswith("/"):
        return base + raw
    return urljoin(base + "/", raw)


def index_search_form_map() -> Dict[str, Any]:
    """The form submit_search fills. Selectors are the unverified block above."""
    return {
        "form_action": absolute_index_url(INDEX_SEARCH_PATH),
        "fields": {
            "reporter": {"selector": INDEX_JOURNAL_SELECTOR, "kind": "select", "name": "journal"},
            "year": {"selector": INDEX_YEAR_SELECTOR, "kind": "select", "name": "year"},
            "court": {"selector": INDEX_COURT_SELECTOR, "kind": "select", "name": "court"},
            "page": {"selector": INDEX_PAGE_SELECTOR, "kind": "text", "name": "page"},
            "submit": {"selector": INDEX_SUBMIT_SELECTOR, "kind": "submit"},
        },
    }


def journals_for_index(subscribed: Sequence[str], configured_csv: str) -> List[str]:
    configured = [part.strip() for part in (configured_csv or "").split(",") if part.strip()]
    if configured:
        return configured
    out: List[str] = []
    for reporter in subscribed:
        name = str(reporter).strip()
        if not name:
            continue
        if name not in out:
            out.append(name)
        for extra in _NOTES_OF_PARENT.get(name, ()):
            if extra not in out:
                out.append(extra)
    return out


def iter_index_queries(
    journals: Sequence[str],
    years: Sequence[int],
    *,
    court_journals: Set[str],
    courts: Sequence[str],
) -> List[Dict[str, Any]]:
    queries: List[Dict[str, Any]] = []
    for journal in journals:
        name = str(journal).strip()
        if not name:
            continue
        for year in years:
            if name in court_journals and courts:
                for court in courts:
                    court_name = str(court).strip()
                    if court_name:
                        queries.append({"reporter": name, "year": int(year), "court": court_name})
            else:
                queries.append({"reporter": name, "year": int(year)})
    return queries


def queries_from_settings(now: datetime) -> List[Dict[str, Any]]:
    journals = journals_for_index(settings.subscribed_reporters, settings.PLS_INDEX_SEARCH_JOURNALS)
    earliest = int(settings.PLS_EARLIEST_YEAR or 0) or now.year
    years = years_for_plan(earliest, now.year)
    courts = [part.strip() for part in settings.PLS_INDEX_SEARCH_COURTS.split(",") if part.strip()]
    court_journals = {part.strip() for part in settings.PLS_INDEX_SEARCH_COURT_JOURNALS.split(",") if part.strip()}
    return iter_index_queries(journals, years, court_journals=court_journals, courts=courts)


def index_search_should_yield(*, harvest_claimable: bool, session_lock_held: bool) -> Optional[str]:
    """Search harvest, including a GAP_FOCUS claim, takes the login before IndexSearch."""
    if harvest_claimable:
        return "search_harvest"
    if session_lock_held:
        return "login_session_busy"
    return None


def roll_periods(state: Dict[str, Any], now: datetime) -> None:
    """Reset day and month counters on the UTC calendar date.

    The tick passes datetime.now(timezone.utc). V9 names a monthly fair-usage
    limit and no timezone, so the stored keys stay on that UTC clock: day is
    YYYY-MM-DD and month is YYYY-MM. A restart does not move the window. This
    is not Asia/Karachi local time.
    """
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
    limits: IndexSearchLimits,
    *,
    upcoming_seconds: float = 0.0,
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
    until = state.get("backoff_until")
    if until:
        try:
            deadline = datetime.fromisoformat(str(until))
        except ValueError:
            deadline = None
        if deadline is not None and now < deadline:
            return "backoff"
    return None


def record_request(state: Dict[str, Any], seconds: float) -> None:
    state["day_requests"] = int(state.get("day_requests") or 0) + 1
    state["month_requests"] = int(state.get("month_requests") or 0) + 1
    state["day_seconds"] = float(state.get("day_seconds") or 0) + float(seconds)
    state["month_seconds"] = float(state.get("month_seconds") or 0) + float(seconds)


def note_failure(state: Dict[str, Any], now: datetime, limits: IndexSearchLimits) -> None:
    step = int(state.get("backoff_step") or 0) + 1
    delay = min(limits.backoff_cap_seconds, limits.backoff_base_seconds * (2 ** (step - 1)))
    state["backoff_step"] = step
    state["backoff_until"] = (now + timedelta(seconds=delay)).isoformat()


def clear_backoff(state: Dict[str, Any]) -> None:
    state["backoff_step"] = 0
    state["backoff_until"] = None


def _citation_court(slug: Optional[str]) -> Optional[str]:
    """Map a printed index court slug onto a PLD citation court. An unknown slug stays unset."""
    if not slug:
        return None
    key = re.sub(r"\s+", " ", slug.replace("-", " ").strip()).upper()
    key = key.replace(".", "")
    if "KASHMIR" in key:
        return "SC (AJ&K)" if "SUPREME" in key else "AJ&K"
    if key in PLD_COURT_CANONICAL:
        return PLD_COURT_CANONICAL[key]
    for known, canonical in sorted(PLD_COURT_CANONICAL.items(), key=lambda item: len(item[0]), reverse=True):
        if known in key:
            return canonical
    return None


def _normalise_detail_href(href: str) -> str:
    """CaseName is the stored parameter spelling. The fetch needs a host, which a printed href omits."""
    rewritten = _CASE_NAME_PARAM.sub("CaseName=", (href or "").strip())
    if not rewritten:
        return ""
    return absolute_index_url(rewritten)


def _load_more_url(soup: BeautifulSoup) -> Optional[str]:
    for tag in soup.find_all(["a", "button", "input"]):
        label = tag.get_text(" ", strip=True) or str(tag.get("value") or tag.get("aria-label") or "")
        if label.strip().lower() != LOAD_MORE_LABEL:
            continue
        for attr in LOAD_MORE_URL_ATTRS:
            value = tag.get(attr)
            if value:
                return str(value).strip()
        page = tag.get("data-page")
        if page:
            return f"?page={page}"
    return None


def _forms_from_hit(hit, court: Optional[str]) -> tuple[str, str, List[str]]:
    """Citation string, reporter code, and every lookup form of one index hit.

    A word-form Note stays that spelling ("2017 YLR Note 99"), which is the stored
    key. The row reporter for that line is the notes code (YLRN), the same code a
    glued line uses. A glued or spaced PLC(CS) citation keeps the stored spaced
    string, and the reporter code is PLC(CS) or PLC(CS)N, never the letters-only
    fold PLCCS.
    """
    token = canonical_reporter(hit.group("reporter")) or hit.group("reporter")
    year = hit.group("year")
    page = hit.group("page")
    note = hit.group("note")
    raw = f"{year} {token} Note {page}" if note else f"{year} {token} {page}"
    printed = normalise_citation(raw) or raw
    forms = [printed]
    citation = printed
    reporter = _NOTE_ROW_REPORTER.get(token, token) if note else token
    if token == "PLD" and not note:
        mapped = _citation_court(court)
        if mapped:
            completed = normalise_citation(f"PLD {year} {mapped} {page}")
            if completed and completed not in forms:
                forms.append(completed)
            if completed:
                citation = completed
    return citation, reporter, forms


def _row_from_entry(entry) -> Optional[IndexRow]:
    detail_url = None
    for anchor in entry.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if any(needle.lower() in href.lower() for needle in DETAIL_HREF_NEEDLES):
            detail_url = _normalise_detail_href(href)
            break
    text = entry.get_text(" ", strip=True)
    text = re.sub(r"\s+Read$", "", text, flags=re.IGNORECASE).strip()
    hits = list(_INDEX_CITE.finditer(text))
    if not hits and not detail_url:
        return None
    court = None
    court_match = _TRAILING_COURT.search(text)
    if court_match and not _SERIES_PAREN.match(court_match.group("court").strip()):
        court = court_match.group("court").strip()
    if not hits:
        return IndexRow(citation="", title=text, court=court, detail_url=detail_url, reporter="")
    primary = hits[0]
    citation, reporter, _primary_forms = _forms_from_hit(primary, court)
    parallel: List[str] = []
    for hit in hits[1:]:
        _other_citation, _other_reporter, forms = _forms_from_hit(hit, court)
        for form in forms:
            if form not in parallel:
                parallel.append(form)
    title = text[primary.end():].strip(" -")
    title = _TRAILING_COURT.sub("", title).strip(" -")
    title = _INDEX_CITE.sub("", title).strip(" -")
    title = re.sub(r"\balso\b", "", title, flags=re.IGNORECASE).strip(" -")
    return IndexRow(
        citation=citation,
        title=re.sub(r"\s+", " ", title).strip(),
        court=court,
        detail_url=detail_url,
        reporter=reporter,
        parallel_citations=parallel,
    )


def parse_index_page(html: str) -> IndexPage:
    """Read one hand-built or live index page. An unknown shell is not an empty volume."""
    soup = BeautifulSoup(html or "", "html.parser")
    next_url = _load_more_url(soup)
    rows: List[IndexRow] = []
    seen = set()
    for entry in soup.select(INDEX_ENTRY_SELECTOR):
        row = _row_from_entry(entry)
        if row is None:
            continue
        identity = (row.citation, row.detail_url, row.title)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    recognized = bool(soup.select_one(INDEX_RESULTS_SELECTOR)) or bool(rows) or bool(next_url)
    return IndexPage(rows=rows, next_url=next_url, recognized=recognized)


def _current_query(queries: Sequence[Dict[str, Any]], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not queries:
        return None
    for query in queries:
        same_reporter = query.get("reporter") == state.get("reporter")
        same_year = int(query.get("year") or 0) == int(state.get("year") or 0)
        same_court = (query.get("court") or None) == (state.get("court") or None)
        if same_reporter and same_year and same_court and state.get("reporter"):
            return query
    return queries[0]


def _index_request(query: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    page_no = int(state.get("page") or 1)
    next_url = state.get("next_url")
    if next_url and page_no > 1:
        return {"url": str(next_url), "form": None}
    # Always send the court control. Omitting it leaves the previous PLD court
    # selected, and a journal that is not court-split would be searched as that court.
    form = {
        "reporter": str(query["reporter"]),
        "year": str(query["year"]),
        "court": str(query["court"]) if query.get("court") else INDEX_COURT_ALL_VALUE,
    }
    if page_no > 1:
        form["page"] = str(page_no)
    return {"url": None, "form": form, "path": INDEX_SEARCH_PATH}


def _row_keys(row: IndexRow) -> Set[str]:
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


def _remember_query(state: Dict[str, Any], query: Dict[str, Any]) -> None:
    state["reporter"] = query.get("reporter")
    state["year"] = query.get("year")
    state["court"] = query.get("court")


async def run_index_search(
    *,
    state: Dict[str, Any],
    now: datetime,
    limits: IndexSearchLimits,
    queries: Sequence[Dict[str, Any]],
    fetch_index: Callable[[Dict[str, Any]], Awaitable[str]],
    fetch_detail: Callable[[str], Awaitable[Any]],
    preserve: Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[str]],
    known_keys: Callable[[Set[str]], Awaitable[Set[str]]],
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
    rng: Any,
    harvest_claimable: bool,
    persist: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """Walk at most one index page, then stop. Known keys are not fetched again."""
    state = dict(state or {})
    roll_periods(state, now)
    stopped = index_search_should_yield(harvest_claimable=harvest_claimable, session_lock_held=False)
    if stopped:
        return {"stopped": stopped, "state": state, "staged": 0, "known": 0}
    if not queries:
        return {"stopped": "no_journals", "state": state, "staged": 0, "known": 0}

    async def save() -> None:
        if persist is not None:
            await persist(dict(state))

    async def paced(call):
        from scraper.tasks.pakistanlawsite import PacingBudgetExceeded

        delay = float(limits.delay_min if rng is None else rng.uniform(limits.delay_min, limits.delay_max))
        block = budget_block_reason(state, now, limits, upcoming_seconds=delay)
        if block:
            return None, block
        started = monotonic()
        await sleep(delay)
        try:
            value = await call()
        except PacingBudgetExceeded:
            # The shared login budget refused before the request. Counting it would spend this
            # source's own budget on a call that never left, and backing off would double every day.
            await save()
            return None, "login_pacing"
        except Exception as exc:
            logger.warning("IndexSearch request failed: %s", type(exc).__name__)
            record_request(state, max(0.0, monotonic() - started))
            note_failure(state, now, limits)
            await save()
            return None, "backoff"
        record_request(state, max(0.0, monotonic() - started))
        await save()
        return value, None

    def stop(reason: str, *, staged: int, known: int) -> Dict[str, Any]:
        return {"stopped": reason, "state": state, "staged": staged, "known": known}

    staged = 0
    known = 0
    pages_opened = 0
    while pages_opened < max(1, int(limits.max_index_pages_per_tick)):
        query = _current_query(queries, state)
        if query is None:
            return stop("no_journals", staged=staged, known=known)
        html, failure = await paced(lambda q=query: fetch_index(_index_request(q, state)))
        if failure:
            return stop(failure, staged=staged, known=known)
        pages_opened += 1
        page = parse_index_page(html or "")
        if not page.recognized:
            note_failure(state, now, limits)
            await save()
            return stop("unrecognized_page", staged=staged, known=known)
        clear_backoff(state)
        start_row = int(state.get("row_index") or 0)
        for index, row in enumerate(page.rows):
            if index < start_row:
                continue
            keys = _row_keys(row)
            try:
                held = await known_keys(keys) if keys else set()
            except Exception as exc:
                logger.warning("IndexSearch known-key lookup failed: %s", type(exc).__name__)
                note_failure(state, now, limits)
                state["row_index"] = index
                _remember_query(state, query)
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
            detail, failure = await paced(lambda url=row.detail_url: fetch_detail(url))
            if failure:
                state["row_index"] = index
                _remember_query(state, query)
                await save()
                return stop(failure, staged=staged, known=known)
            route = {
                "harvest": "index_search",
                "query": {"reporter": query.get("reporter"), "year": query.get("year"), "court": query.get("court")},
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
                logger.warning("IndexSearch preserve failed: %s", type(exc).__name__)
                note_failure(state, now, limits)
                state["row_index"] = index
                _remember_query(state, query)
                await save()
                return stop("backoff", staged=staged, known=known)
            staged += 1
            state["row_index"] = index + 1
            clear_backoff(state)
            await save()
        page_no = int(state.get("page") or 1)
        _remember_query(state, query)
        state["row_index"] = 0
        if page.next_url:
            state["page"] = page_no + 1
            state["next_url"] = page.next_url
        else:
            state["next_url"] = None
            state["page"] = 1
            current_at = next((i for i, item in enumerate(queries) if item == query), 0)
            nxt = queries[current_at + 1] if current_at + 1 < len(queries) else queries[0]
            _remember_query(state, nxt)
        await save()
    return {"stopped": None, "state": state, "staged": staged, "known": known}
