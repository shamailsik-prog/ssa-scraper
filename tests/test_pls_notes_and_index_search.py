"""Notes-edition journal codes and the IndexSearch harvest (plan V5 and V8, 2026-11-15).

No test here contacts PakistanLawSite. Index pages are hand-built fixtures.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scraper.config import KNOWN_REPORTERS, Settings, canonical_reporter
from scraper.parsers.citation_extractor import extract_citations, normalise_citation
from scraper.pls_quality_guards import _primary_is_reported
from scraper.pls_search_harvest_core import citation_keys_for_row, citation_lookup_keys
from scraper.tasks.pakistanlawsite import CASE_DETAIL_HARVESTS, reporter_from_citation

NOTES_CODES = ("CLCN", "YLRN", "PCrLJN", "PLCN", "PLC(CS)N")


def test_notes_edition_codes_are_known_reporters_distinct_from_the_parent():
    for code in NOTES_CODES:
        assert code in KNOWN_REPORTERS
        assert canonical_reporter(code) == code
        assert canonical_reporter(code.lower()) == code
    assert canonical_reporter("YLRN") != canonical_reporter("YLR")
    assert canonical_reporter("CLCN") != "CLC"
    assert canonical_reporter("PCrLJN") != "PCrLJ"
    assert canonical_reporter("PLCN") != "PLC"
    assert canonical_reporter("PLC(CS)N") != "PLC"
    assert canonical_reporter("PLC(CS)N") != "PLC(CS)"


@pytest.mark.parametrize(
    "raw, expected, reporter",
    [
        ("2024 CLCN 15", "2024 CLCN 15", "CLCN"),
        ("2024 C.L.C.N. 15", "2024 CLCN 15", "CLCN"),
        ("2024 YLRN 120", "2024 YLRN 120", "YLRN"),
        ("2024 Y.L.R.N. 120", "2024 YLRN 120", "YLRN"),
        ("2018 PCrLJN 41", "2018 PCrLJN 41", "PCrLJN"),
        ("2018 P.Cr.L.J.N. 41", "2018 PCrLJN 41", "PCrLJN"),
        ("2020 PLCN 5", "2020 PLCN 5", "PLCN"),
        ("2020 P.L.C.N. 5", "2020 PLCN 5", "PLCN"),
        ("2025 PLC(CS)N 45", "2025 PLC(CS)N 45", "PLC(CS)N"),
        ("2025 PLC (CS)N 45", "2025 PLC(CS)N 45", "PLC(CS)N"),
        ("2025 P L C (C.S.)N 45", "2025 PLC(CS)N 45", "PLC(CS)N"),
    ],
)
def test_glued_notes_codes_stay_their_own_journal(raw, expected, reporter):
    assert normalise_citation(raw) == expected
    hits = extract_citations(raw)
    assert [h["normalized"] for h in hits] == [expected]
    assert hits[0]["reporter"] == reporter
    assert reporter_from_citation(raw) == reporter


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2019 YLR Note 120", "2019 YLR Note 120"),
        ("2019 YLR N 120", "2019 YLR Note 120"),
        ("2018 P Cr. L J Note 41", "2018 PCrLJ Note 41"),
        ("2020 P L C Note 5", "2020 PLC Note 5"),
        ("2024 YLR 10", "2024 YLR 10"),
        ("2024 CLC 10", "2024 CLC 10"),
        ("2025 PLC (CS) 45", "2025 PLC (CS) 45"),
        ("2025 PLC(CS) 45", "2025 PLC (CS) 45"),
    ],
)
def test_existing_citation_strings_keep_their_canonical_form(raw, expected):
    """Stored judgments are keyed on these strings. A separated Note word stays that string."""
    assert normalise_citation(raw) == expected
    assert extract_citations(raw)[0]["normalized"] == expected


def test_notes_code_and_note_word_are_one_lookup_key_and_not_the_parent_page():
    """Both directions. A notes lookup must not contain the parent page, and the parent lookup must not contain the notes spellings."""
    pairs = (
        ("2024 YLRN 120", "2024 YLR Note 120", "2024 YLR 120"),
        ("2024 CLCN 15", "2024 CLC Note 15", "2024 CLC 15"),
        ("2018 PCrLJN 41", "2018 PCrLJ Note 41", "2018 PCrLJ 41"),
        ("2020 PLCN 5", "2020 PLC Note 5", "2020 PLC 5"),
        ("2025 PLC(CS)N 45", "2025 PLC (CS) Note 45", "2025 PLC (CS) 45"),
    )
    for glued, word, parent in pairs:
        held_glued = citation_lookup_keys(glued)
        held_word = citation_lookup_keys(word)
        assert glued in held_glued and word in held_glued
        assert word in held_word and glued in held_word
        assert parent not in held_glued
        assert parent not in held_word
        parent_keys = citation_lookup_keys(parent)
        assert glued not in parent_keys
        assert word not in parent_keys


def test_blank_index_search_flag_does_not_crash_settings(monkeypatch):
    monkeypatch.setenv("PLS_INDEX_SEARCH_ENABLED", "")
    fresh = Settings(DATABASE_URL="postgresql+asyncpg://x:y@localhost/db", REDIS_URL="redis://localhost/0")
    assert fresh.PLS_INDEX_SEARCH_ENABLED is False
    monkeypatch.setenv("PLS_INDEX_SEARCH_ENABLED", "   ")
    fresh = Settings(DATABASE_URL="postgresql+asyncpg://x:y@localhost/db", REDIS_URL="redis://localhost/0")
    assert fresh.PLS_INDEX_SEARCH_ENABLED is False


def test_budget_periods_follow_the_utc_calendar_date():
    from scraper.pls_index_search import roll_periods

    # 01:30 in Pakistan on 16 Nov is still 15 Nov in UTC. The stored day is the UTC date.
    state = {}
    roll_periods(state, datetime(2026, 11, 16, 1, 30, tzinfo=timezone(timedelta(hours=5))))
    assert state["day"] == "2026-11-15"
    assert state["month"] == "2026-11"


def test_non_court_journal_clears_a_leftover_court():
    from scraper.pls_index_search import INDEX_COURT_ALL_VALUE, _index_request

    pld = _index_request({"reporter": "PLD", "year": 2024, "court": "SUPREME-COURT"}, {})
    ylrn = _index_request({"reporter": "YLRN", "year": 2024}, {})
    assert pld["form"]["court"] == "SUPREME-COURT"
    assert ylrn["form"]["court"] == INDEX_COURT_ALL_VALUE == ""
    assert ylrn["form"]["court"] != pld["form"]["court"]


def _index_entry(line: str, href: str) -> str:
    return (
        '<html><body><div id="indexSearchResults"><div class="index-entry">'
        f"<span>{line}</span>"
        f'<a href="{href}">Read</a>'
        "</div></div></body></html>"
    )


def test_index_parser_accepts_word_form_notes_and_spaced_or_glued_plc_cs():
    from scraper.pls_index_search import parse_index_page

    word = parse_index_page(
        _index_entry(
            "2017 YLR Note 99 ALI VS THE STATE (LAHORE-HIGH-COURT-LAHORE)",
            "/Login/ReferenceCaseLawSearch?CaseName=2017Y99",
        )
    ).rows[0]
    assert word.citation == "2017 YLR Note 99"
    assert word.reporter == "YLR"

    spaced = parse_index_page(
        _index_entry(
            "2025 PLC (CS) 45 KHAN VS FEDERATION (SUPREME-COURT)",
            "/Login/ReferenceCaseLawSearch?CaseName=2025C45",
        )
    ).rows[0]
    assert spaced.citation == "2025 PLC (CS) 45"
    assert spaced.reporter == "PLC(CS)"

    glued = parse_index_page(
        _index_entry(
            "2025 PLC(CS) 45 KHAN VS FEDERATION (SUPREME-COURT)",
            "/Login/ReferenceCaseLawSearch?CaseName=2025C45",
        )
    ).rows[0]
    assert glued.citation == "2025 PLC (CS) 45"
    assert glued.reporter == "PLC(CS)"
    assert glued.reporter != "PLCCS"

    notes = parse_index_page(
        _index_entry(
            "2025 PLC(CS)N 45 KHAN VS FEDERATION (SUPREME-COURT)",
            "/Login/ReferenceCaseLawSearch?CaseName=2025N45",
        )
    ).rows[0]
    assert notes.citation == "2025 PLC(CS)N 45"
    assert notes.reporter == "PLC(CS)N"
    assert canonical_reporter("PLC(CS)") == "PLC(CS)"
    assert canonical_reporter("PLCCS") == "PLC(CS)"
    assert canonical_reporter("PLC(CS)N") == "PLC(CS)N"


def test_case_name_keys_are_unchanged_when_the_row_has_no_citation():
    held = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2025L8&Row=0"
    assert citation_keys_for_row({"citation": "", "detail_url": held}) == {"case:2025L8"}


def test_a_short_notes_citation_counts_as_reported():
    assert _primary_is_reported(["2024 YLRN 15"])
    assert _primary_is_reported(["2024 PLC(CS)N 9"])
    assert _primary_is_reported(["2024 CLCN 1"])


def test_index_search_uses_the_same_case_detail_quality_gate_as_search_harvest():
    assert "search_gap" in CASE_DETAIL_HARVESTS
    assert "index_search" in CASE_DETAIL_HARVESTS


def test_index_search_flag_defaults_off_and_budgets_are_conservative():
    fresh = Settings(DATABASE_URL="postgresql+asyncpg://x:y@localhost/db", REDIS_URL="redis://localhost/0")
    assert fresh.PLS_INDEX_SEARCH_ENABLED is False
    assert fresh.PLS_INDEX_SEARCH_REQUESTS_PER_DAY <= fresh.PLS_INDEX_SEARCH_REQUESTS_PER_MONTH
    assert fresh.PLS_INDEX_SEARCH_SECONDS_PER_DAY <= fresh.PLS_INDEX_SEARCH_SECONDS_PER_MONTH
    assert fresh.PLS_INDEX_SEARCH_DELAY_MIN >= 8
    assert fresh.PLS_INDEX_SEARCH_DELAY_MAX >= fresh.PLS_INDEX_SEARCH_DELAY_MIN
    assert fresh.PLS_INDEX_SEARCH_PAGE_SIZE == 50
    assert fresh.PLS_INDEX_SEARCH_MAX_PAGES_PER_TICK == 1


# Hand-built IndexSearch page. Selectors are the ones named in scraper/pls_index_search.py.
# The line shape is the field note: "2024 PLD 1 Sardar ABDUL QUDOOS VS FEDERATION OF PAKISTAN (FEDERAL-SHARIAT-COURT)".
INDEX_FIXTURE = """
<html><body>
<div id="indexSearchResults">
  <div class="index-entry">
    <span>2024 PLD 1 Sardar ABDUL QUDOOS VS FEDERATION OF PAKISTAN (FEDERAL-SHARIAT-COURT)</span>
    <a href="/Login/ReferenceCaseLawSearch?CaseName=2024F12">Read</a>
  </div>
  <div class="index-entry">
    <span>2024 YLRN 15 ALI VS THE STATE (LAHORE-HIGH-COURT-LAHORE)</span>
    <a href="/Login/GetCaseFile?caseName=2024L99">Read</a>
  </div>
  <div class="index-entry">
    <span>2024 SCMR 50 also 2024 PLD 88 KHAN VS FEDERATION (SUPREME-COURT)</span>
    <a href="/Login/ReferenceCaseLawSearch?CaseName=2024S50">Read</a>
  </div>
  <a href="/Login/IndexSearch?journal=PLD&amp;year=2024&amp;page=2">Load More</a>
</div>
</body></html>
"""

EMPTY_VOLUME = """
<html><body><div id="indexSearchResults"></div></body></html>
"""

UNRECOGNIZED_PAGE = """
<html><body><p>Welcome back</p></body></html>
"""


def test_index_parser_reads_the_documented_line_load_more_and_case_link():
    from scraper.pls_index_search import parse_index_page

    page = parse_index_page(INDEX_FIXTURE)
    assert page.recognized is True
    assert page.next_url == "/Login/IndexSearch?journal=PLD&year=2024&page=2"
    assert len(page.rows) == 3
    first, notes, parallel = page.rows
    assert first.citation == "PLD 2024 FSC 1"
    assert "ABDUL QUDOOS" in first.title
    assert first.court == "FEDERAL-SHARIAT-COURT"
    assert first.detail_url.startswith("https://www.pakistanlawsite.com/")
    assert "CaseName=2024F12" in first.detail_url
    assert notes.citation == "2024 YLRN 15"
    assert notes.reporter == "YLRN"
    assert "CaseName=2024L99" in (notes.detail_url or "")
    assert parallel.citation == "2024 SCMR 50"
    assert "2024 PLD 88" in parallel.parallel_citations
    assert parallel.court == "SUPREME-COURT"


def test_empty_results_container_is_an_empty_volume_and_a_stranger_page_is_not():
    from scraper.pls_index_search import parse_index_page

    empty = parse_index_page(EMPTY_VOLUME)
    assert empty.recognized is True
    assert empty.rows == []
    assert empty.next_url is None
    stranger = parse_index_page(UNRECOGNIZED_PAGE)
    assert stranger.recognized is False
    assert stranger.rows == []


def test_load_more_follows_fifty_rows():
    from scraper.pls_index_search import parse_index_page

    entries = []
    for n in range(1, 51):
        entries.append(
            "<div class='index-entry'>"
            f"<span>2024 SCMR {n} PARTY VS STATE (SUPREME-COURT)</span>"
            f"<a href='/Login/ReferenceCaseLawSearch?CaseName=2024S{n}'>Read</a>"
            "</div>"
        )
    html = (
        "<html><body><div id='indexSearchResults'>"
        + "".join(entries)
        + "<button data-href='/Login/IndexSearch?page=2'>Load More</button>"
        "</div></body></html>"
    )
    page = parse_index_page(html)
    assert len(page.rows) == 50
    assert page.next_url == "/Login/IndexSearch?page=2"
    assert page.rows[0].citation == "2024 SCMR 1"
    assert page.rows[49].citation == "2024 SCMR 50"


def test_selectors_and_the_endpoint_live_in_one_block():
    from scraper import pls_index_search as idx

    assert idx.INDEX_SEARCH_PATH == "/Login/IndexSearch"
    assert idx.INDEX_RESULTS_SELECTOR == "#indexSearchResults"
    assert "index-entry" in idx.INDEX_ENTRY_SELECTOR
    assert idx.LOAD_MORE_LABEL == "load more"
    form = idx.index_search_form_map()
    fields = form["fields"]
    assert fields["reporter"]["selector"] == idx.INDEX_JOURNAL_SELECTOR
    assert fields["year"]["selector"] == idx.INDEX_YEAR_SELECTOR
    assert fields["court"]["selector"] == idx.INDEX_COURT_SELECTOR
    assert fields["submit"]["selector"] == idx.INDEX_SUBMIT_SELECTOR
    assert form["form_action"].endswith(idx.INDEX_SEARCH_PATH)


def test_journals_include_notes_editions_of_subscribed_parents_only():
    from scraper.pls_index_search import journals_for_index

    assert journals_for_index(["YLR", "PLC"], "") == ["YLR", "YLRN", "PLC", "PLCN", "PLC(CS)", "PLC(CS)N"]
    assert journals_for_index([], "") == []
    assert journals_for_index(["YLR"], "CLCN, PLD") == ["CLCN", "PLD"]


def test_court_dimension_is_only_where_configured():
    from scraper.pls_index_search import iter_index_queries

    queries = list(
        iter_index_queries(
            ["PLD", "YLRN"],
            [2024],
            court_journals={"PLD"},
            courts=["SUPREME-COURT", "ISLAMABAD"],
        )
    )
    assert queries == [
        {"reporter": "PLD", "year": 2024, "court": "SUPREME-COURT"},
        {"reporter": "PLD", "year": 2024, "court": "ISLAMABAD"},
        {"reporter": "YLRN", "year": 2024},
    ]


def _limits(**overrides):
    from scraper.pls_index_search import IndexSearchLimits

    base = dict(
        delay_min=12.0,
        delay_max=20.0,
        requests_per_day=3,
        requests_per_month=5,
        seconds_per_day=30.0,
        seconds_per_month=40.0,
        backoff_base_seconds=60.0,
        backoff_cap_seconds=240.0,
        max_index_pages_per_tick=1,
        page_size=50,
    )
    base.update(overrides)
    return IndexSearchLimits(**base)


def test_budget_stops_before_a_request_when_the_day_or_month_is_spent_and_resets():
    from scraper.pls_index_search import budget_block_reason, record_request, roll_periods

    now = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
    limits = _limits()
    state = {}
    roll_periods(state, now)
    assert budget_block_reason(state, now, limits, upcoming_seconds=12) is None
    for _ in range(3):
        record_request(state, seconds=1)
    assert budget_block_reason(state, now, limits, upcoming_seconds=1) == "day_requests"
    later = now + timedelta(days=1)
    roll_periods(state, later)
    assert state["day_requests"] == 0
    assert state["month_requests"] == 3
    record_request(state, seconds=1)
    record_request(state, seconds=1)
    assert budget_block_reason(state, later, limits, upcoming_seconds=1) == "month_requests"
    state["day_requests"] = 0
    state["month_requests"] = 0
    state["day_seconds"] = 25
    assert budget_block_reason(state, later, limits, upcoming_seconds=12) == "day_seconds"


def test_backoff_grows_on_each_failure_and_a_success_clears_it():
    from scraper.pls_index_search import clear_backoff, note_failure

    now = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
    limits = _limits()
    state = {}
    note_failure(state, now, limits)
    assert state["backoff_step"] == 1
    first = datetime.fromisoformat(state["backoff_until"])
    assert first - now == timedelta(seconds=60)
    note_failure(state, now, limits)
    second = datetime.fromisoformat(state["backoff_until"])
    assert second - now == timedelta(seconds=120)
    note_failure(state, now, limits)
    note_failure(state, now, limits)
    capped = datetime.fromisoformat(state["backoff_until"])
    assert capped - now == timedelta(seconds=240)
    clear_backoff(state)
    assert state["backoff_step"] == 0
    assert state["backoff_until"] is None


def test_yield_reason_names_harvest_before_a_busy_login():
    from scraper.pls_index_search import index_search_should_yield

    assert index_search_should_yield(harvest_claimable=True, session_lock_held=False) == "search_harvest"
    assert index_search_should_yield(harvest_claimable=True, session_lock_held=True) == "search_harvest"
    assert index_search_should_yield(harvest_claimable=False, session_lock_held=True) == "login_session_busy"
    assert index_search_should_yield(harvest_claimable=False, session_lock_held=False) is None


@pytest.mark.asyncio
async def test_runner_skips_known_and_parallel_keys_and_feeds_new_rows_to_preserve():
    from scraper.pls_index_search import run_index_search

    calls = {"index": 0, "detail": [], "preserved": []}

    async def fetch_index(_request):
        calls["index"] += 1
        return INDEX_FIXTURE

    async def fetch_detail(url):
        calls["detail"].append(url)
        return {"url": url, "html": "<html>body</html>"}

    async def preserve(detail, route, row):
        calls["preserved"].append((route["harvest"], row["citation"], detail["url"]))
        return "staged"

    async def known(keys):
        # The SCMR row is already held under its parallel PLD citation, and by case id.
        if "2024 PLD 88" in keys or "case:2024S50" in keys:
            return {"2024 PLD 88"}
        return set()

    async def sleep(_seconds):
        return None

    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 1.0
        return clock["t"]

    class Seq:
        def uniform(self, _a, _b):
            return 0.0

    result = await run_index_search(
        state={},
        now=datetime(2026, 11, 15, tzinfo=timezone.utc),
        limits=_limits(requests_per_day=10, requests_per_month=10, seconds_per_day=100, seconds_per_month=100),
        queries=[{"reporter": "PLD", "year": 2024, "court": "SUPREME-COURT"}],
        fetch_index=fetch_index,
        fetch_detail=fetch_detail,
        preserve=preserve,
        known_keys=known,
        sleep=sleep,
        monotonic=monotonic,
        rng=Seq(),
        harvest_claimable=False,
    )
    assert result["stopped"] is None
    assert calls["index"] == 1
    preserved_citations = [c for _h, c, _u in calls["preserved"]]
    assert "2024 SCMR 50" not in preserved_citations
    assert any(c.startswith("PLD 2024") for c in preserved_citations)
    assert "2024 YLRN 15" in preserved_citations
    assert all(h == "index_search" for h, _c, _u in calls["preserved"])
    assert result["state"]["page"] == 2 or result["state"].get("next_url")


@pytest.mark.asyncio
async def test_runner_does_not_fetch_when_it_must_yield_or_the_budget_is_spent():
    from scraper.pls_index_search import run_index_search

    async def boom(_request):
        raise AssertionError("fetched")

    result = await run_index_search(
        state={},
        now=datetime(2026, 11, 15, tzinfo=timezone.utc),
        limits=_limits(),
        queries=[{"reporter": "PLD", "year": 2024}],
        fetch_index=boom,
        fetch_detail=boom,
        preserve=boom,
        known_keys=boom,
        sleep=boom,
        monotonic=lambda: 0.0,
        rng=None,
        harvest_claimable=True,
    )
    assert result["stopped"] == "search_harvest"
    spent = {"day": "2026-11-15", "month": "2026-11", "day_requests": 3, "month_requests": 3, "day_seconds": 0, "month_seconds": 0}
    result = await run_index_search(
        state=spent,
        now=datetime(2026, 11, 15, tzinfo=timezone.utc),
        limits=_limits(),
        queries=[{"reporter": "PLD", "year": 2024}],
        fetch_index=boom,
        fetch_detail=boom,
        preserve=boom,
        known_keys=boom,
        sleep=boom,
        monotonic=lambda: 0.0,
        rng=None,
        harvest_claimable=False,
    )
    assert result["stopped"] == "day_requests"


@pytest.mark.asyncio
async def test_runner_backs_off_on_an_error_and_does_not_advance_an_unrecognized_page():
    from scraper.pls_index_search import IndexSearchChallenge, run_index_search

    async def fetch_index(_request):
        return UNRECOGNIZED_PAGE

    async def unused(*_a, **_k):
        raise AssertionError("should not fetch a detail")

    async def sleep(_seconds):
        return None

    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    result = await run_index_search(
        state={"reporter": "YLRN", "year": 2024, "page": 1},
        now=now,
        limits=_limits(),
        queries=[{"reporter": "YLRN", "year": 2024}, {"reporter": "PLD", "year": 2024}],
        fetch_index=fetch_index,
        fetch_detail=unused,
        preserve=unused,
        known_keys=unused,
        sleep=sleep,
        monotonic=lambda: 1.0,
        rng=type("R", (), {"uniform": lambda self, a, b: 0.0})(),
        harvest_claimable=False,
    )
    assert result["stopped"] == "unrecognized_page"
    assert result["state"]["reporter"] == "YLRN"
    assert result["state"]["backoff_step"] == 1

    async def challenged(_request):
        raise IndexSearchChallenge("login wall")

    result = await run_index_search(
        state={"reporter": "YLRN", "year": 2024, "page": 1},
        now=now,
        limits=_limits(),
        queries=[{"reporter": "YLRN", "year": 2024}],
        fetch_index=challenged,
        fetch_detail=unused,
        preserve=unused,
        known_keys=unused,
        sleep=sleep,
        monotonic=lambda: 1.0,
        rng=type("R", (), {"uniform": lambda self, a, b: 0.0})(),
        harvest_claimable=False,
    )
    assert result["stopped"] == "backoff"
    assert result["state"]["reporter"] == "YLRN"
    assert result["state"]["backoff_step"] == 1


def test_beat_entry_is_absent_unless_the_flag_is_on(monkeypatch):
    from scraper.config import settings
    from scraper.tasks.pls_index_search import index_search_beat_entries

    monkeypatch.setattr(settings, "PLS_INDEX_SEARCH_ENABLED", False)
    assert index_search_beat_entries() == {}
    monkeypatch.setattr(settings, "PLS_INDEX_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "PLS_INDEX_SEARCH_SCHEDULE_SECONDS", 3600)
    entries = index_search_beat_entries()
    assert set(entries) == {"pls-index-search"}
    assert entries["pls-index-search"]["task"] == "scraper.tasks.pls_index_search.pls_index_search_tick"
    from scraper.deploy_role import LOGIN_ONLY_BEAT_ENTRIES

    assert set(entries) <= LOGIN_ONLY_BEAT_ENTRIES


@pytest.mark.asyncio
async def test_yield_helper_uses_gap_focus_claimability(db, monkeypatch):
    from scraper.config import settings
    from scraper.models import PlsSearchHarvestQuery
    from scraper.tasks.pakistanlawsite import SOURCE_NAME
    from scraper.tasks.pls_index_search import index_search_yield_reason

    row = PlsSearchHarvestQuery(
        source_name=SOURCE_NAME,
        query_key="index-yield-pld-2020",
        query_json={"reporter": "PLD", "year": 2020},
        status="done",
        gap_size=4,
        cursor_json={},
    )
    db.add(row)
    await db.flush()
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_GAP_FOCUS", False)
    assert await index_search_yield_reason(db, session_lock_held=False) is None
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_GAP_FOCUS", True)
    assert await index_search_yield_reason(db, session_lock_held=False) == "search_harvest"
    await db.rollback()


@pytest.mark.asyncio
async def test_runner_persists_the_request_count_before_the_page_cursor_advances():
    from scraper.pls_index_search import run_index_search

    saved = []

    async def fetch_index(_request):
        return INDEX_FIXTURE

    async def fetch_detail(url):
        return {"url": url, "html": "<html>body</html>"}

    async def preserve(_detail, _route, _row):
        return "staged"

    async def known(_keys):
        return set()

    async def sleep(_seconds):
        return None

    async def persist(state):
        saved.append({"requests": int(state.get("day_requests") or 0), "page": state.get("page")})

    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 1.0
        return clock["t"]

    class Seq:
        def uniform(self, _a, _b):
            return 0.0

    result = await run_index_search(
        state={},
        now=datetime(2026, 11, 15, tzinfo=timezone.utc),
        limits=_limits(requests_per_day=10, requests_per_month=10, seconds_per_day=100, seconds_per_month=100),
        queries=[{"reporter": "PLD", "year": 2024, "court": "SUPREME-COURT"}],
        fetch_index=fetch_index,
        fetch_detail=fetch_detail,
        preserve=preserve,
        known_keys=known,
        sleep=sleep,
        monotonic=monotonic,
        rng=Seq(),
        harvest_claimable=False,
        persist=persist,
    )
    assert result["stopped"] is None
    assert saved
    assert saved[0]["requests"] >= 1
    assert saved[0]["page"] in (None, 1)
    assert saved[-1]["page"] == 2
    assert any(item["requests"] >= 1 and item["page"] in (None, 1) for item in saved)


async def _promote_notes(db, login_source, held: str, incoming: str) -> str:
    from sqlalchemy import func, select

    from scraper.fetchers import canonical_text_hash, record_provenance, stage_judgment
    from scraper.models import Citation, Judgment
    from scraper.parsers.text_cleaner import clean_html
    from scraper.tasks.promotion import promote_judgment_staging
    from tests.fixtures import judgment_html

    held_text = f"Held judgment body for {held}. " + ("The court dismissed the appeal. " * 40)
    existing = Judgment(
        canonical_citation=held,
        case_title="Held row",
        court_name="Lahore High Court",
        full_text=held_text,
        full_text_hash=canonical_text_hash(held_text),
        judge_names=["A Judge"],
        access_method=login_source.access_method,
        source_name="PakistanLawSite",
        source_url=f"https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=held-{held.replace(' ', '')}",
        confidence_score=0.9,
    )
    db.add(existing)
    await db.flush()
    db.add(
        Citation(
            judgment_id=existing.id,
            citation_string=held,
            raw_string=held,
            reporter="YLR",
            year=2017,
            page=99,
            is_primary=True,
        )
    )
    await db.flush()
    detail_url = f"https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=in-{incoming.replace(' ', '')}"
    full_html = judgment_html(incoming, title="Incoming Notes Case")
    full_text = clean_html(full_html)
    prov = await record_provenance(db, source=login_source, url=detail_url, content=full_html.encode("utf-8"), content_kind="html")
    st = await stage_judgment(db, source=login_source, prov=prov, raw_html=full_html, raw_text=full_text, url=detail_url)
    st.extracted_citation = incoming
    st.reconciled_json = {
        "citations": [incoming],
        "court": "Lahore High Court",
        "year": 2017,
        "case_title": "Incoming Notes Case",
        "judge_names": ["A Judge"],
        "document_type": "full_judgment",
    }
    st.status = "extracted"
    st.confidence_score = 0.99
    result = await promote_judgment_staging(db, st)
    count = (await db.execute(select(func.count()).select_from(Judgment))).scalar()
    canonicals = set((await db.execute(select(Judgment.canonical_citation))).scalars().all())
    return result, count, canonicals


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "held,incoming",
    [
        ("2017 YLR Note 99", "2017 YLRN 99"),
        ("2017 YLRN 99", "2017 YLR Note 99"),
    ],
)
async def test_notes_spellings_promote_as_the_same_judgment(db, login_source, held, incoming):
    result, count, canonicals = await _promote_notes(db, login_source, held, incoming)
    assert result == "duplicate"
    assert count == 1
    assert canonicals == {held}


@pytest.mark.asyncio
async def test_parent_page_stays_a_different_judgment_from_the_notes_edition(db, login_source):
    result, count, canonicals = await _promote_notes(db, login_source, "2017 YLR 99", "2017 YLRN 99")
    assert result == "promoted"
    assert count == 2
    assert canonicals == {"2017 YLR 99", "2017 YLRN 99"}
