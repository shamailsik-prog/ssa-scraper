"""Advance Search and Smart Search case-name enumeration (plan V1 and V6).

No test here contacts PakistanLawSite. Advance pages are synthetic HTML in the
recorded tr.caseType / casetypeid shape. Smart payloads are synthetic JSON.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scraper.config import Settings, settings
from scraper.pls_search_harvest_core import citation_keys_for_row, citation_lookup_keys

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
HEADNOTE = "HEADNOTE-SENTINEL the accused was granted bail under section 489-F after a long discussion."


def _fresh(**overrides) -> Settings:
    base = dict(DATABASE_URL="postgresql+asyncpg://x:y@localhost/db", REDIS_URL="redis://localhost/0")
    base.update(overrides)
    return Settings(**base)


def test_flags_default_off_and_budgets_are_conservative():
    fresh = _fresh()
    assert fresh.PLS_ADVANCE_SEARCH_ENABLED is False
    assert fresh.PLS_SMART_SEARCH_ENABLED is False
    assert fresh.PLS_ADVANCE_SEARCH_QUERIES == ""
    assert fresh.PLS_SMART_SEARCH_QUERIES == ""
    for prefix in ("PLS_ADVANCE_SEARCH", "PLS_SMART_SEARCH"):
        assert getattr(fresh, f"{prefix}_DELAY_MIN") >= 8
        assert getattr(fresh, f"{prefix}_DELAY_MAX") >= getattr(fresh, f"{prefix}_DELAY_MIN")
        assert 0 < getattr(fresh, f"{prefix}_REQUESTS_PER_DAY") <= getattr(fresh, f"{prefix}_REQUESTS_PER_MONTH")
        assert 0 < getattr(fresh, f"{prefix}_SECONDS_PER_DAY") <= getattr(fresh, f"{prefix}_SECONDS_PER_MONTH")
        assert 0 < getattr(fresh, f"{prefix}_BACKOFF_BASE_SECONDS") <= getattr(fresh, f"{prefix}_BACKOFF_CAP_SECONDS")
        assert getattr(fresh, f"{prefix}_MAX_LISTING_PAGES_PER_TICK") == 1
        cap = getattr(fresh, f"{prefix}_MAX_REQUESTS_PER_RUN")
        assert 0 < cap <= getattr(fresh, f"{prefix}_REQUESTS_PER_DAY")
        assert getattr(fresh, f"{prefix}_SCHEDULE_SECONDS") > 0
    assert fresh.PLS_SMART_SEARCH_QUERY_MAX_CHARS == 500


def test_blank_flags_are_off(monkeypatch):
    monkeypatch.setenv("PLS_ADVANCE_SEARCH_ENABLED", "")
    monkeypatch.setenv("PLS_SMART_SEARCH_ENABLED", "   ")
    fresh = _fresh()
    assert fresh.PLS_ADVANCE_SEARCH_ENABLED is False
    assert fresh.PLS_SMART_SEARCH_ENABLED is False


def test_smart_query_cap_cannot_be_raised_past_500_characters():
    with pytest.raises(ValueError):
        _fresh(PLS_SMART_SEARCH_QUERY_MAX_CHARS=501)


def test_case_detail_gate_includes_both_harvests_and_keeps_the_existing_ones():
    from scraper.pls_case_enumeration import HARVEST_ADVANCE, HARVEST_SMART
    from scraper.tasks.pakistanlawsite import CASE_DETAIL_HARVESTS

    assert HARVEST_ADVANCE == "advance_search"
    assert HARVEST_SMART == "smart_search"
    assert {"search_gap", "index_search", HARVEST_ADVANCE, HARVEST_SMART} <= CASE_DETAIL_HARVESTS


def test_beat_entries_are_absent_unless_the_flag_is_on(monkeypatch):
    from scraper.deploy_role import LOGIN_ONLY_BEAT_ENTRIES
    from scraper.tasks.pls_case_enumeration import ADVANCE_TASK, SMART_TASK, case_enumeration_beat_entries

    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_ENABLED", False)
    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_ENABLED", False)
    assert case_enumeration_beat_entries() == {}

    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_SCHEDULE_SECONDS", 3600)
    entries = case_enumeration_beat_entries()
    assert set(entries) == {"pls-advance-search"}
    assert entries["pls-advance-search"]["task"] == ADVANCE_TASK
    assert set(entries) <= LOGIN_ONLY_BEAT_ENTRIES

    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_ENABLED", False)
    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_SCHEDULE_SECONDS", 3600)
    entries = case_enumeration_beat_entries()
    assert set(entries) == {"pls-smart-search"}
    assert entries["pls-smart-search"]["task"] == SMART_TASK
    assert set(entries) <= LOGIN_ONLY_BEAT_ENTRIES


def test_lock_bearing_task_names_match_the_ticks():
    from scraper.pls_lock_celery import PLS_LOCK_BEARING_TASKS
    from scraper.tasks.pls_case_enumeration import ADVANCE_TASK, SMART_TASK

    assert ADVANCE_TASK in PLS_LOCK_BEARING_TASKS
    assert SMART_TASK in PLS_LOCK_BEARING_TASKS


def _limits(**overrides):
    from scraper.pls_case_enumeration import EnumerationLimits

    base = dict(
        delay_min=12.0,
        delay_max=20.0,
        requests_per_day=10,
        requests_per_month=20,
        seconds_per_day=100.0,
        seconds_per_month=200.0,
        backoff_base_seconds=60.0,
        backoff_cap_seconds=240.0,
        max_listing_pages_per_tick=1,
        max_requests_per_run=8,
    )
    base.update(overrides)
    return EnumerationLimits(**base)


class _Zero:
    def uniform(self, _a, _b):
        return 0.0


def _clock():
    box = {"t": 0.0}

    def monotonic():
        box["t"] += 1.0
        return box["t"]

    return monotonic


async def _asleep(_seconds):
    return None


ADVANCE_ROW = """
<tr class="caseType">
  <td>{serial}</td>
  <td> {citation} </td>
  <td>{title} <font color:red>Versus</font> THE STATE<br/>
      <span style="color:darkred;">Honorable Justice Yahya Afridi</span>
  </td>
  <td>{court}</td>
  <td><input type="button" casetypeid="{case_name}" class="btn btn-success courtWiseSearchBtn" value="Read" /></td>
</tr>
"""


def _advance_html(*rows: str, more: str = "") -> str:
    body = "".join(rows)
    return f"<html><body><div id='advanceSearchResults'><table><tbody>{body}</tbody></table>{more}</div></body></html>"


def _advance_row(serial, citation, title, court, case_name) -> str:
    return ADVANCE_ROW.format(serial=serial, citation=citation, title=title, court=court, case_name=case_name)


def test_advance_parser_reads_the_recorded_case_row_and_load_more():
    from scraper.pls_advance_search import parse_advance_page

    html = _advance_html(
        _advance_row(1, "2024 PLD 1", "Sardar ABDUL QUDOOS", "FEDERAL-SHARIAT-COURT", "2024F12"),
        _advance_row(2, "2017 YLR Note 99", "ALI", "LAHORE-HIGH-COURT-LAHORE", "2017Y99"),
        more="<a href='/Login/AdvanceSearch?page=2'>Load More</a>",
    )
    page = parse_advance_page(html)
    assert page.recognized is True
    assert page.next_url == "/Login/AdvanceSearch?page=2"
    assert len(page.rows) == 2
    first, notes = page.rows
    assert first.citation == "PLD 2024 FSC 1"
    assert first.reporter == "PLD"
    assert "ABDUL QUDOOS" in first.title
    assert "Yahya Afridi" not in first.title
    assert first.court == "FEDERAL-SHARIAT-COURT"
    assert first.detail_url.startswith("https://www.pakistanlawsite.com/")
    assert "CaseName=2024F12" in first.detail_url
    assert notes.citation == "2017 YLR Note 99"
    assert notes.reporter == "YLRN"
    assert "CaseName=2017Y99" in (notes.detail_url or "")
    assert citation_lookup_keys(notes.citation) >= {"2017 YLR Note 99", "2017 YLRN 99"}


def test_advance_empty_container_is_recognized_and_a_stranger_page_is_not():
    from scraper.pls_advance_search import parse_advance_page

    empty = parse_advance_page("<html><body><div id='advanceSearchResults'></div></body></html>")
    assert empty.recognized is True and empty.rows == [] and empty.next_url is None
    stranger = parse_advance_page("<html><body><p>Welcome back</p></body></html>")
    assert stranger.recognized is False and stranger.rows == []


def test_advance_form_has_the_multi_field_controls_and_no_year():
    from scraper.pls_advance_search import ADVANCE_QUERY_FIELDS, ADVANCE_SEARCH_PATH, advance_form_map, advance_request

    form = advance_form_map()
    names = {spec["name"] for spec in form["fields"].values() if spec.get("name")}
    assert names == set(ADVANCE_QUERY_FIELDS) | {"page"}
    assert "year" not in names
    assert form["form_action"].endswith(ADVANCE_SEARCH_PATH)
    request = advance_request({"keyword": "bail", "judge": "Yahya Afridi", "year": "2020"}, {"page": 2})
    assert request["form"]["keyword"] == "bail"
    assert request["form"]["judge"] == "Yahya Afridi"
    assert request["form"]["court"] == ""
    assert request["form"]["act2"] == ""
    assert request["form"]["section2"] == ""
    assert request["form"]["page"] == "2"
    assert "year" not in request["form"]
    assert advance_request({"year": "1826"}, {}) is None


def test_advance_queries_drop_a_year_and_idle_when_blank_or_invalid(monkeypatch):
    from scraper.pls_advance_search import advance_queries_from_settings

    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_QUERIES", "")
    assert advance_queries_from_settings() == []
    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_QUERIES", "not-json")
    assert advance_queries_from_settings() == []
    payload = [{"court": "SUPREME-COURT", "judge": "Yahya Afridi", "keyword": "bail", "year": 2020, "act": "PPC", "section": "489-F", "act2": "CrPC", "section2": "497", "rule": "Order XXXIX", "lawyer": "Khan", "party": "State"}]
    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_QUERIES", json.dumps(payload))
    queries = advance_queries_from_settings()
    assert queries == [
        {
            "court": "SUPREME-COURT",
            "judge": "Yahya Afridi",
            "lawyer": "Khan",
            "party": "State",
            "keyword": "bail",
            "rule": "Order XXXIX",
            "act": "PPC",
            "section": "489-F",
            "act2": "CrPC",
            "section2": "497",
        }
    ]
    assert "year" not in queries[0]


def _smart_hit(**overrides):
    hit = {
        "citation": "2005 YLR 1362",
        "case_title": "ALI VS THE STATE",
        "court_display": "LAHORE-HIGH-COURT-LAHORE",
        "benchofjudges": ["Justice Example"],
        "year": 2005,
        "category": "YLR",
        "page_number": 1362,
        "legal_provisions": ["section 489-F"],
        "headnote": HEADNOTE,
        "case_name": "2005L4281",
        "case_link": "/Login/ReferenceCaseLawSearch?CaseName=2005L4281",
    }
    hit.update(overrides)
    return hit


def _flags(**overrides):
    meta = {
        "interpretation": {"search_concepts": ["pre-arrest bail", "489-F"]},
        "appliedFilters": {"subject": "bail"},
        "pinnedCitations": ["2005 YLR 1362"],
        "titleSearchUsed": False,
        "courtFilterRelaxed": True,
        "semanticFallbackUsed": True,
        "semanticSearchConcepts": ["cheque dishonour"],
    }
    meta.update(overrides)
    return meta


def test_smart_parser_reads_the_envelope_and_the_elasticsearch_shape():
    from scraper.pls_smart_search import parse_smart_search

    envelope = {"results": [_smart_hit(), _smart_hit(citation="2017 YLR Note 99", case_name="2017Y99", case_title="NOTES ROW", parallel_citations=["2017 YLRN 99"])], **_flags(), "next_url": "/Login/SmartSearch?page=2"}
    page = parse_smart_search(json.dumps(envelope))
    assert page.recognized is True
    assert page.next_url == "/Login/SmartSearch?page=2"
    assert len(page.rows) == 2
    first = page.rows[0]
    assert first.citation == "2005 YLR 1362"
    assert first.reporter == "YLR"
    assert first.title == "ALI VS THE STATE"
    assert first.court == "LAHORE-HIGH-COURT-LAHORE"
    assert "CaseName=2005L4281" in (first.detail_url or "")
    assert HEADNOTE not in first.title
    assert page.rows[1].reporter == "YLRN"
    assert "2017 YLRN 99" in page.rows[1].parallel_citations
    assert page.interpretation["semanticFallbackUsed"] is True
    assert page.interpretation["search_concepts"] == ["pre-arrest bail", "489-F"]
    assert HEADNOTE not in json.dumps(page.interpretation)

    elastic = {"hits": {"hits": [{"_source": _smart_hit(citation="2024 SCMR 50", case_name="2024S50")}]}, **_flags(semanticFallbackUsed=False), "page": 1, "total_pages": 3}
    wrapped = "<html><body><script id='smartSearchPayload' type='application/json'>" + json.dumps(elastic) + "</script></body></html>"
    other = parse_smart_search(wrapped)
    assert other.recognized is True
    assert other.rows[0].citation == "2024 SCMR 50"
    assert "CaseName=2024S50" in (other.rows[0].detail_url or "")
    assert other.next_url == "?page=2"
    assert other.interpretation["semanticFallbackUsed"] is False
    assert HEADNOTE not in json.dumps(other.interpretation)


def test_smart_stranger_payload_is_not_an_empty_result():
    from scraper.pls_smart_search import parse_smart_search

    stranger = parse_smart_search("<html><body><p>Welcome back</p></body></html>")
    assert stranger.recognized is False and stranger.rows == []
    empty = parse_smart_search(json.dumps({"results": [], **_flags()}))
    assert empty.recognized is True and empty.rows == []


def test_smart_request_drops_a_query_over_the_500_character_cap():
    from scraper.pls_smart_search import SMART_SEARCH_PATH, smart_request

    short = smart_request("judgments of Justice Yahya Afridi on bail", {}, max_chars=500)
    assert short["form"] == {"q": "judgments of Justice Yahya Afridi on bail"}
    assert short["path"] == SMART_SEARCH_PATH
    assert smart_request("x" * 500, {}, max_chars=500)["form"]["q"] == "x" * 500
    assert smart_request("x" * 501, {}, max_chars=500) is None


def test_smart_queries_accept_json_or_lines_and_idle_when_blank(monkeypatch):
    from scraper.pls_smart_search import smart_queries_from_settings

    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_QUERIES", "")
    assert smart_queries_from_settings() == []
    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_QUERIES", "[")
    assert smart_queries_from_settings() == []
    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_QUERIES", json.dumps(["pre-arrest bail in cheque dishonour cases under 489-F"]))
    assert smart_queries_from_settings() == ["pre-arrest bail in cheque dishonour cases under 489-F"]
    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_QUERIES", "one query\n\nsecond query\n")
    assert smart_queries_from_settings() == ["one query", "second query"]


def _budget_cases():
    spent_day = {"day": "2026-11-15", "month": "2026-11", "day_requests": 3, "month_requests": 3, "day_seconds": 0, "month_seconds": 0}
    spent_month = {"day": "2026-11-16", "month": "2026-11", "day_requests": 0, "month_requests": 5, "day_seconds": 0, "month_seconds": 0}
    spent_seconds = {"day": "2026-11-15", "month": "2026-11", "day_requests": 0, "month_requests": 0, "day_seconds": 90, "month_seconds": 90}
    spent_month_seconds = {"day": "2026-11-15", "month": "2026-11", "day_requests": 0, "month_requests": 0, "day_seconds": 0, "month_seconds": 190}
    backoff = {"day": "2026-11-15", "month": "2026-11", "day_requests": 0, "month_requests": 0, "backoff_until": (NOW + timedelta(minutes=5)).isoformat()}
    return [
        (spent_day, "day_requests"),
        (spent_month, "month_requests"),
        (spent_seconds, "day_seconds"),
        (spent_month_seconds, "month_seconds"),
        (backoff, "backoff"),
    ]


@pytest.mark.parametrize("state,reason", _budget_cases())
async def test_each_budget_gate_stops_before_a_request(state, reason):
    from scraper.pls_case_enumeration import budget_block_reason, run_case_enumeration

    limits = _limits(requests_per_day=3, requests_per_month=5, seconds_per_day=100, seconds_per_month=200)
    assert budget_block_reason(dict(state), NOW, limits, upcoming_seconds=12) == reason

    async def boom(*_a, **_k):
        raise AssertionError("request left")

    result = await run_case_enumeration(
        harvest="advance_search",
        state=dict(state),
        now=NOW,
        limits=limits,
        queries=[{"keyword": "bail"}],
        build_request=lambda query, _state: {"form": dict(query), "path": "/Login/AdvanceSearch"},
        parse_listing=lambda _html: None,
        fetch_listing=boom,
        fetch_detail=boom,
        preserve=boom,
        known_keys=boom,
        sleep=boom,
        monotonic=lambda: 0.0,
        rng=None,
        harvest_claimable=False,
    )
    assert result["stopped"] == reason
    assert result["staged"] == 0


async def test_yield_and_blank_queries_do_not_fetch():
    from scraper.pls_case_enumeration import run_case_enumeration

    async def boom(*_a, **_k):
        raise AssertionError("request left")

    yielded = await run_case_enumeration(
        harvest="smart_search",
        state={},
        now=NOW,
        limits=_limits(),
        queries=["bail"],
        build_request=lambda query, _state: {"form": {"q": query}},
        parse_listing=boom,
        fetch_listing=boom,
        fetch_detail=boom,
        preserve=boom,
        known_keys=boom,
        sleep=boom,
        monotonic=lambda: 0.0,
        rng=None,
        harvest_claimable=True,
    )
    assert yielded["stopped"] == "search_harvest"
    idle = await run_case_enumeration(
        harvest="advance_search",
        state={},
        now=NOW,
        limits=_limits(),
        queries=[],
        build_request=lambda *_a: {"form": {}},
        parse_listing=boom,
        fetch_listing=boom,
        fetch_detail=boom,
        preserve=boom,
        known_keys=boom,
        sleep=boom,
        monotonic=lambda: 0.0,
        rng=None,
        harvest_claimable=False,
    )
    assert idle["stopped"] == "no_queries"


async def test_run_cap_stops_before_the_next_request_and_resumes_on_that_row():
    from scraper.pls_advance_search import parse_advance_page
    from scraper.pls_case_enumeration import HARVEST_ADVANCE, run_case_enumeration

    html = _advance_html(
        _advance_row(1, "2024 SCMR 1", "ONE", "SUPREME-COURT", "2024S1"),
        _advance_row(2, "2024 SCMR 2", "TWO", "SUPREME-COURT", "2024S2"),
        _advance_row(3, "2024 SCMR 3", "THREE", "SUPREME-COURT", "2024S3"),
    )
    calls = {"listing": 0, "detail": []}

    async def fetch_listing(_request):
        calls["listing"] += 1
        return html

    async def fetch_detail(url):
        calls["detail"].append(url)
        return {"url": url}

    async def preserve(_detail, route, row):
        assert route["harvest"] == HARVEST_ADVANCE
        assert HEADNOTE not in json.dumps(row)
        return "staged"

    async def known(_keys):
        return set()

    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    result = await run_case_enumeration(
        harvest=HARVEST_ADVANCE,
        state={},
        now=NOW,
        limits=_limits(max_requests_per_run=2, requests_per_day=50, requests_per_month=50),
        queries=[{"keyword": "bail"}],
        build_request=lambda query, state: {"form": {"keyword": query["keyword"], "page": state.get("page")}},
        parse_listing=parse_advance_page,
        fetch_listing=fetch_listing,
        fetch_detail=fetch_detail,
        preserve=preserve,
        known_keys=known,
        sleep=sleep,
        monotonic=_clock(),
        rng=_Zero(),
        harvest_claimable=False,
    )
    assert result["stopped"] == "run_cap"
    assert calls["listing"] == 1
    assert len(calls["detail"]) == 1
    assert len(sleeps) == 2
    assert result["staged"] == 1
    assert result["state"]["row_index"] == 1
    assert result["state"]["query_index"] == 0


async def test_known_alias_parallel_and_case_name_are_not_fetched_again():
    from scraper.pls_advance_search import parse_advance_page
    from scraper.pls_case_enumeration import run_case_enumeration

    html = _advance_html(
        _advance_row(1, "2017 YLR Note 99", "HELD NOTES", "LAHORE-HIGH-COURT-LAHORE", "2017Y99"),
        _advance_row(2, "2024 SCMR 50", "PARALLEL", "SUPREME-COURT", "2024S50"),
        _advance_row(3, "2024 SCMR 8", "NEW", "SUPREME-COURT", "2024S8"),
    )
    # The second row's keys must include a parallel the parser does not see on this HTML
    # line. The smart-search runner test covers parallel_citations. Here the case id is the held key.
    details = []

    async def fetch_listing(_request):
        return html

    async def fetch_detail(url):
        details.append(url)
        return {"url": url}

    preserved = []

    async def preserve(_detail, route, row):
        preserved.append(row["citation"])
        keys = citation_keys_for_row(row)
        assert route["harvest"] == "advance_search"
        assert keys
        return "staged"

    async def known(keys):
        if "2017 YLRN 99" in keys or "case:2017Y99" in keys:
            return {"2017 YLRN 99"}
        if "case:2024S50" in keys:
            return {"case:2024S50"}
        return set()

    result = await run_case_enumeration(
        harvest="advance_search",
        state={},
        now=NOW,
        limits=_limits(max_requests_per_run=8),
        queries=[{"keyword": "bail"}],
        build_request=lambda query, _state: {"form": {"keyword": "bail"}},
        parse_listing=parse_advance_page,
        fetch_listing=fetch_listing,
        fetch_detail=fetch_detail,
        preserve=preserve,
        known_keys=known,
        sleep=_asleep,
        monotonic=_clock(),
        rng=_Zero(),
        harvest_claimable=False,
    )
    assert preserved == ["2024 SCMR 8"]
    assert all("2017Y99" not in url and "2024S50" not in url for url in details)
    assert any("2024S8" in url for url in details)
    assert result["known"] == 2
    assert result["staged"] == 1


async def test_smart_known_parallel_is_not_fetched_and_headnote_stays_out_of_the_pipeline():
    from scraper.pls_case_enumeration import HARVEST_SMART, run_case_enumeration
    from scraper.pls_smart_search import parse_smart_search, smart_request

    payload = json.dumps(
        {
            "results": [
                _smart_hit(parallel_citations=["2024 PLD 88"]),
                _smart_hit(citation="2024 SCMR 8", case_name="2024S8", case_title="NEW CASE", headnote=HEADNOTE),
            ],
            **_flags(),
        }
    )
    preserved = []

    async def fetch_listing(request):
        assert HEADNOTE not in json.dumps(request)
        assert len(request["form"]["q"]) <= 500
        return payload

    async def fetch_detail(url):
        return {"url": url, "html": "<html>judgment</html>"}

    async def preserve(detail, route, row):
        preserved.append((route["harvest"], row, detail["html"]))
        return "staged"

    async def known(keys):
        if "2024 PLD 88" in keys or "2005 YLR 1362" in keys:
            return {"2024 PLD 88"}
        return set()

    result = await run_case_enumeration(
        harvest=HARVEST_SMART,
        state={},
        now=NOW,
        limits=_limits(),
        queries=["judgments of Justice Yahya Afridi on bail"],
        build_request=lambda query, state: smart_request(query, state, max_chars=500),
        parse_listing=parse_smart_search,
        fetch_listing=fetch_listing,
        fetch_detail=fetch_detail,
        preserve=preserve,
        known_keys=known,
        sleep=_asleep,
        monotonic=_clock(),
        rng=_Zero(),
        harvest_claimable=False,
    )
    assert result["known"] == 1
    assert len(preserved) == 1
    harvest, row, body = preserved[0]
    assert harvest == HARVEST_SMART
    assert row["citation"] == "2024 SCMR 8"
    assert "CaseName=2024S8" in row["detail_url"]
    assert body == "<html>judgment</html>"
    assert HEADNOTE not in json.dumps(row)
    assert HEADNOTE not in json.dumps(result["state"])
    assert result["state"]["last_interpretation"]["semanticFallbackUsed"] is True


async def test_over_long_smart_query_is_not_sent_and_a_later_short_one_is():
    from scraper.pls_case_enumeration import run_case_enumeration
    from scraper.pls_smart_search import parse_smart_search, smart_request

    sent = []

    async def fetch_listing(request):
        sent.append(request["form"]["q"])
        return json.dumps({"results": [], **_flags()})

    async def unused(*_a, **_k):
        raise AssertionError("detail")

    result = await run_case_enumeration(
        harvest="smart_search",
        state={},
        now=NOW,
        limits=_limits(),
        queries=["y" * 501, "pre-arrest bail"],
        build_request=lambda query, state: smart_request(query, state, max_chars=500),
        parse_listing=parse_smart_search,
        fetch_listing=fetch_listing,
        fetch_detail=unused,
        preserve=unused,
        known_keys=unused,
        sleep=_asleep,
        monotonic=_clock(),
        rng=_Zero(),
        harvest_claimable=False,
    )
    assert sent == ["pre-arrest bail"]
    assert result["stopped"] is None


async def test_only_an_over_long_query_makes_no_request():
    from scraper.pls_case_enumeration import run_case_enumeration
    from scraper.pls_smart_search import smart_request

    async def boom(*_a, **_k):
        raise AssertionError("request left")

    result = await run_case_enumeration(
        harvest="smart_search",
        state={},
        now=NOW,
        limits=_limits(),
        queries=["z" * 501],
        build_request=lambda query, state: smart_request(query, state, max_chars=500),
        parse_listing=boom,
        fetch_listing=boom,
        fetch_detail=boom,
        preserve=boom,
        known_keys=boom,
        sleep=_asleep,
        monotonic=_clock(),
        rng=_Zero(),
        harvest_claimable=False,
    )
    assert result["stopped"] == "no_sendable_query"


async def test_unrecognized_page_does_not_advance_the_query_cursor():
    from scraper.pls_advance_search import parse_advance_page
    from scraper.pls_case_enumeration import run_case_enumeration

    async def fetch_listing(_request):
        return "<html><body><p>Welcome back</p></body></html>"

    async def unused(*_a, **_k):
        raise AssertionError("detail")

    result = await run_case_enumeration(
        harvest="advance_search",
        state={"query_index": 0, "page": 1},
        now=NOW,
        limits=_limits(),
        queries=[{"keyword": "bail"}, {"keyword": "remand"}],
        build_request=lambda query, _state: {"form": {"keyword": query["keyword"]}},
        parse_listing=parse_advance_page,
        fetch_listing=fetch_listing,
        fetch_detail=unused,
        preserve=unused,
        known_keys=unused,
        sleep=_asleep,
        monotonic=_clock(),
        rng=_Zero(),
        harvest_claimable=False,
    )
    assert result["stopped"] == "unrecognized_page"
    assert result["state"]["query_index"] == 0
    assert result["state"]["backoff_step"] == 1


async def test_login_pacing_refusal_does_not_spend_this_source_budget():
    from scraper.pls_case_enumeration import run_case_enumeration
    from scraper.tasks.pakistanlawsite import PacingBudgetExceeded

    async def fetch_listing(_request):
        raise PacingBudgetExceeded("day")

    async def unused(*_a, **_k):
        raise AssertionError("detail")

    result = await run_case_enumeration(
        harvest="advance_search",
        state={},
        now=NOW,
        limits=_limits(),
        queries=[{"keyword": "bail"}],
        build_request=lambda query, _state: {"form": {"keyword": "bail"}},
        parse_listing=lambda _html: None,
        fetch_listing=fetch_listing,
        fetch_detail=unused,
        preserve=unused,
        known_keys=unused,
        sleep=_asleep,
        monotonic=_clock(),
        rng=_Zero(),
        harvest_claimable=False,
    )
    assert result["stopped"] == "login_pacing"
    assert int(result["state"].get("day_requests") or 0) == 0
    assert int(result["state"].get("backoff_step") or 0) == 0


async def test_a_crash_after_the_listing_keeps_the_counted_request():
    from scraper.pls_case_enumeration import run_case_enumeration

    saved = []

    async def fetch_listing(_request):
        return "<html></html>"

    async def unused(*_a, **_k):
        raise AssertionError("should not run")

    async def persist(state):
        saved.append(int(state.get("day_requests") or 0))

    def crash(_html):
        raise RuntimeError("crash after the listing request")

    with pytest.raises(RuntimeError, match="crash after the listing request"):
        await run_case_enumeration(
            harvest="advance_search",
            state={},
            now=NOW,
            limits=_limits(),
            queries=[{"keyword": "bail"}],
            build_request=lambda query, _state: {"form": {"keyword": "bail"}},
            parse_listing=crash,
            fetch_listing=fetch_listing,
            fetch_detail=unused,
            preserve=unused,
            known_keys=unused,
            sleep=_asleep,
            monotonic=_clock(),
            rng=_Zero(),
            harvest_claimable=False,
            persist=persist,
        )
    assert saved and saved[0] >= 1


async def test_one_listing_page_per_tick_stores_load_more_without_fetching_it():
    from scraper.pls_advance_search import parse_advance_page
    from scraper.pls_case_enumeration import run_case_enumeration

    html = _advance_html(
        _advance_row(1, "2024 SCMR 1", "ONE", "SUPREME-COURT", "2024S1"),
        more="<button data-href='/Login/AdvanceSearch?page=2'>Load More</button>",
    )
    listings = []

    async def fetch_listing(request):
        listings.append(request)
        return html

    async def fetch_detail(url):
        return {"url": url}

    async def preserve(*_a):
        return "staged"

    async def known(_keys):
        return set()

    result = await run_case_enumeration(
        harvest="advance_search",
        state={},
        now=NOW,
        limits=_limits(max_listing_pages_per_tick=1, max_requests_per_run=8),
        queries=[{"keyword": "bail"}],
        build_request=lambda query, state: {"url": state.get("next_url"), "form": None} if state.get("next_url") and int(state.get("page") or 1) > 1 else {"form": {"keyword": "bail"}, "path": "/Login/AdvanceSearch"},
        parse_listing=parse_advance_page,
        fetch_listing=fetch_listing,
        fetch_detail=fetch_detail,
        preserve=preserve,
        known_keys=known,
        sleep=_asleep,
        monotonic=_clock(),
        rng=_Zero(),
        harvest_claimable=False,
    )
    assert len(listings) == 1
    assert result["state"]["page"] == 2
    assert result["state"]["next_url"] == "/Login/AdvanceSearch?page=2"
    assert result["stopped"] is None


@pytest.mark.parametrize("harvest", ["advance_search", "smart_search"])
async def test_a_full_judgment_on_these_harvests_is_staged(db, login_source, harvest):
    """The non-judgment gate still lets a real judgment through. A harvest in the gate is not a blanket rejection."""
    from pathlib import Path

    from sqlalchemy import select

    from scraper.auth.session_manager import PageResult
    from scraper.models import ScraperStaging
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from tests.fixtures import BrowserScript
    from tests.test_auth_playwright import _activate, _nosleep

    await _activate(db, login_source)
    text = (Path(__file__).resolve().parent / "fixtures" / "pls_headnote_only" / "scmr_312_short_order.txt").read_text(encoding="utf-8")
    citation = "1991 SCMR 312"
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=1991SCMR312"
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    page = PageResult(
        url=detail_url,
        html=f"<html><body><pre>{text}</pre></body></html>",
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": True,
            "case_description_modal_found": True,
            "case_description_modal_text": text,
            "case_description_modal_text_length": len(text),
        },
    )
    row = {"citation": citation, "title": "State versus Abdul Rehman", "court": "Supreme Court", "detail_url": detail_url}
    assert await pipeline.preserve_and_extract(page, {"harvest": harvest, "row_index": 0}, row) == "staged"
    staged = (await db.execute(select(ScraperStaging))).scalars().one()
    assert "1991 SCMR 312" in (staged.raw_text or "")


def test_ticks_stay_idle_when_the_flags_are_off(monkeypatch):
    from scraper.tasks.pls_case_enumeration import pls_advance_search_tick, pls_smart_search_tick

    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_ENABLED", False)
    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_ENABLED", False)
    assert "off" in pls_advance_search_tick()["skipped"]
    assert "off" in pls_smart_search_tick()["skipped"]


async def test_async_tick_off_does_not_touch_the_database():
    from scraper.tasks.pls_case_enumeration import run_advance_search_tick, run_smart_search_tick

    class Exploding:
        async def execute(self, *_a, **_k):
            raise AssertionError("database used while the flag is off")

        async def commit(self):
            raise AssertionError("commit while the flag is off")

    assert "off" in (await run_advance_search_tick(Exploding()))["skipped"]
    assert "off" in (await run_smart_search_tick(Exploding()))["skipped"]


async def test_an_enabled_flag_with_no_queries_does_not_take_the_login(monkeypatch):
    from scraper.tasks.pls_case_enumeration import run_advance_search_tick, run_smart_search_tick

    class Exploding:
        async def execute(self, *_a, **_k):
            raise AssertionError("database used with no queries")

        async def commit(self):
            raise AssertionError("commit with no queries")

    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_QUERIES", "")
    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "PLS_SMART_SEARCH_QUERIES", "")
    monkeypatch.setattr(type(settings), "login_scraping_effective", property(lambda self: True))
    assert (await run_advance_search_tick(Exploding()))["skipped"] == "no_queries"
    assert (await run_smart_search_tick(Exploding()))["skipped"] == "no_queries"


async def test_tick_commits_the_cursor_under_its_own_state_key(db, login_source, monkeypatch):
    from scraper.auth.session_manager import merge_source_config
    from scraper.pls_case_enumeration import HARVEST_ADVANCE
    from scraper.tasks import pls_case_enumeration as tick_mod

    login_source.state = "ACTIVE"
    login_source.is_active = True
    await db.flush()
    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "PLS_ADVANCE_SEARCH_QUERIES", '[{"keyword": "bail"}]')
    monkeypatch.setattr(type(settings), "login_scraping_effective", property(lambda self: True))

    class _Lock:
        async def acquire(self):
            return None

        async def release(self):
            return None

    monkeypatch.setattr(tick_mod, "SessionLock", lambda *_a, **_k: _Lock())

    async def claim(_pipeline):
        return object()

    async def release(_pipeline):
        return None

    async def mode(_db):
        return "updates"

    async def no_yield(_db, *, session_lock_held):
        return None

    merges = []
    real_merge = merge_source_config

    async def spy_merge(session, source, patch):
        merges.append(dict(patch))
        return await real_merge(session, source, patch)

    async def fake_run(**kwargs):
        await kwargs["persist"]({"day_requests": 1, "day": "2026-11-15", "month": "2026-11", "page": 1})
        return {"stopped": None, "state": {"day_requests": 1, "page": 1}, "staged": 0, "known": 0}

    monkeypatch.setattr(tick_mod, "claim_free_slot", claim)
    monkeypatch.setattr(tick_mod, "release_claimed_slot", release)
    monkeypatch.setattr(tick_mod, "get_harvest_mode", mode)
    monkeypatch.setattr(tick_mod, "enumeration_yield_reason", no_yield)
    monkeypatch.setattr(tick_mod, "merge_source_config", spy_merge)
    monkeypatch.setattr(tick_mod, "run_case_enumeration", fake_run)

    result = await tick_mod.run_advance_search_tick(db)
    assert merges
    saved = merges[0]["pls_advance_search"]
    assert int(saved.get("day_requests") or 0) == 1
    assert result["state"]["day_requests"] == 1
    assert HARVEST_ADVANCE == "advance_search"
