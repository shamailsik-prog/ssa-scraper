"""CLD reporter-year gaps must close or be fetched, and empty citation keys must not starve that frontier.

Live stall on tip f3414bd1 (2026-11-02): search:reporter=CLD|year=2005 finished with
rows_seen=372, rows_known=335, rows_new=372, gap_size=37, pages_enumerated=1 and staged
nothing. The queue then spent every tick on search:citation=1967 PTD / 1979 SCMR keys
that return zero rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import is_citation_only_query, refreshed_rows_known
from scraper.tasks import pls_search_harvest as harvest


def test_refreshed_rows_known_replaces_stale_single_page_count():
    assert (
        refreshed_rows_known(
            rows_seen=372,
            rows_known=335,
            known_on_page=372,
            detail_resolved=0,
            confirming_single_page=True,
        )
        == 372
    )


def test_refreshed_rows_known_credits_duplicate_and_staged_details():
    assert (
        refreshed_rows_known(
            rows_seen=372,
            rows_known=335,
            known_on_page=335,
            detail_resolved=37,
            confirming_single_page=True,
        )
        == 372
    )
    assert (
        refreshed_rows_known(
            rows_seen=10,
            rows_known=4,
            known_on_page=4,
            detail_resolved=2,
            confirming_single_page=False,
        )
        == 6
    )


def test_citation_only_query_is_the_empty_lookup_shape():
    assert is_citation_only_query({"citation": "1967 PTD 139"})
    assert not is_citation_only_query({"reporter": "CLD", "year": 2005})
    assert not is_citation_only_query({"citation": "1967 PTD 139", "reporter": "PTD"})
    assert not is_citation_only_query({"citation": "1967 PTD 139", "year": 1967})


def _runner(monkeypatch, db, pages, *, known=None, preserve="staged"):
    fetched = []

    class FakePipeline:
        def __init__(self):
            self.runner = SimpleNamespace(browser=None)
            self.local_engine = None

        async def fetch_results(self, search_map, values):
            return SimpleNamespace(html="page1", url="page1")

        async def fetch_detail(self, url):
            fetched.append(url)
            return SimpleNamespace(html="", url=url)

        async def preserve_and_extract(self, detail, route, row):
            if callable(preserve):
                return preserve(detail, route, row)
            return preserve

        async def sleep(self, seconds):
            return None

    async def extract_result_rows(self, *, html, search_map, base_url):
        rows, nxt = pages[html]
        return SimpleNamespace(data={"result_rows": rows, "next_page": nxt})

    async def load_known(db, keys):
        return set(known or ())

    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.extract_result_rows", extract_result_rows)
    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(harvest, "unmapped_harvest_reason", lambda *a: None)
    monkeypatch.setattr(harvest, "build_harvest_form_values", lambda *a: {})
    monkeypatch.setattr(harvest, "release_page_result", lambda page: None)
    monkeypatch.setattr(harvest, "parse_total_results_from_html", lambda html: None)
    monkeypatch.setattr(harvest, "load_known_citation_keys", load_known)
    monkeypatch.setattr(harvest.settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0)

    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db, runner.source, runner.dry_run = db, None, False
    runner.pipeline = FakePipeline()
    runner.memory = SimpleNamespace(note_window=lambda: None)
    runner.pacing_profile = {"login_delay_min": 0, "login_delay_max": 0}
    runner.spot_check_between_pages = False

    async def no_recycle():
        return None

    runner._maybe_recycle_browser = no_recycle
    runner.fetched = fetched
    return runner


def _row(citation: str, url: str) -> dict:
    return {"citation": citation, "detail_url": url}


@pytest.mark.asyncio
async def test_confirming_reread_refreshes_stale_known_count_and_does_not_refetch(db, monkeypatch):
    """rows_new stayed equal to rows_seen because it is the lifetime staged counter.

    The confirming reread kept the stale rows_known, so gap_size stayed 37 and no
    detail fetch ran for rows the corpus already holds.
    """
    citations = [f"2005 CLD {i}" for i in range(1, 5)]
    rows = [_row(c, f"https://example.test/{c}") for c in citations]
    runner = _runner(monkeypatch, db, {"page1": (rows, None)}, known=set(citations))
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2005",
        query_json={"reporter": "CLD", "year": 2005},
        status="in_progress",
        pages_enumerated=1,
        rows_seen=4,
        rows_known=2,
        rows_new=4,
        gap_size=2,
        cursor_json={"page": 1, "row_index": 0},
    )
    db.add(q)
    await db.flush()

    result = await runner.run_query(q, {})

    assert runner.fetched == []
    assert result["rows_seen"] == 4
    assert result["rows_known"] == 4
    assert result["rows_new"] == 4
    assert result["gap_size"] == 0
    assert result["staged_this_tick"] == 0
    assert q.status == "done"
    await db.rollback()


@pytest.mark.asyncio
async def test_duplicate_detail_closes_the_gap_without_staging(db, monkeypatch):
    rows = [
        _row("2005 CLD 1", "https://example.test/1"),
        _row("2005 CLD 2", "https://example.test/2"),
    ]
    runner = _runner(
        monkeypatch,
        db,
        {"page1": (rows, None)},
        known={"2005 CLD 1"},
        preserve="duplicate",
    )
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2004",
        query_json={"reporter": "CLD", "year": 2004},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    first = await runner.run_query(q, {})
    assert runner.fetched == ["https://example.test/2"]
    assert first["rows_seen"] == 2
    assert first["rows_known"] == 2
    assert first["rows_new"] == 0
    assert first["gap_size"] == 0
    assert first["staged_this_tick"] == 0
    assert q.status == "in_progress"

    second = await runner.run_query(q, {})
    assert second["status"] == "done"
    assert q.gap_size == 0
    assert q.rows_new == 0
    await db.rollback()


@pytest.mark.asyncio
async def test_unknown_linked_row_is_fetched_and_counted_known_when_staged(db, monkeypatch):
    rows = [_row("2005 CLD 9", "https://example.test/9")]
    runner = _runner(monkeypatch, db, {"page1": (rows, None)}, known=set(), preserve="staged")
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2003",
        query_json={"reporter": "CLD", "year": 2003},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    result = await runner.run_query(q, {})
    assert runner.fetched == ["https://example.test/9"]
    assert result["staged_this_tick"] == 1
    assert result["rows_new"] == 1
    assert result["rows_known"] == 1
    assert result["gap_size"] == 0
    await db.rollback()


@pytest.mark.asyncio
async def test_confirming_reread_retries_unresolved_linked_row_instead_of_finishing(db, monkeypatch):
    rows = [
        _row("2005 CLD 1", "https://example.test/1"),
        _row("2005 CLD 2", "https://example.test/2"),
    ]
    runner = _runner(
        monkeypatch,
        db,
        {"page1": (rows, None)},
        known={"2005 CLD 1"},
        preserve="fetch_error",
    )
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2005",
        query_json={"reporter": "CLD", "year": 2005},
        status="in_progress",
        pages_enumerated=1,
        rows_seen=2,
        rows_known=1,
        rows_new=2,
        gap_size=1,
        cursor_json={"page": 1, "row_index": 0},
    )
    db.add(q)
    await db.flush()

    first = await runner.run_query(q, {})
    assert first["staged_this_tick"] == 0
    assert q.status == "in_progress"
    assert "unmatched" in (q.last_error or "")
    assert runner.fetched == ["https://example.test/2"]

    samples = (q.cursor_json or {}).get("unresolved_samples") or []
    assert samples and samples[0]["result"] == "fetch_error"
    assert samples[0]["citation"] == "2005 CLD 2"
    assert samples[0]["detail_url"] == "https://example.test/2"

    second = await runner.run_query(q, {})
    assert q.status == "done"
    assert (q.last_error or "").startswith("unresolved linked gap")
    assert second["gap_size"] == 1
    assert second["staged_this_tick"] == 0
    assert (q.cursor_json or {}).get("unresolved_samples")
    await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve_result", ["non_judgment", "empty"])
async def test_non_judgment_or_empty_detail_closes_gap_without_staging(db, monkeypatch, caplog, preserve_result):
    """CLD|2026 finished seen=102 known=101 gap=1 staged=0 after two detail fetches.

    preserve_and_extract returned neither staged nor duplicate. A non-judgment or empty
    body is not a missing judgment: count it known, keep staged_this_tick at 0, and
    record the result. fetch_error stays on the two-try path (see the test above).
    """
    import logging

    caplog.set_level(logging.INFO)
    rows = [
        _row("2026 CLD 1", "https://example.test/1"),
        _row("2026 CLD 2", "https://example.test/2"),
    ]
    runner = _runner(
        monkeypatch,
        db,
        {"page1": (rows, None)},
        known={"2026 CLD 1"},
        preserve=preserve_result,
    )
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2026",
        query_json={"reporter": "CLD", "year": 2026},
        status="in_progress",
        pages_enumerated=1,
        rows_seen=2,
        rows_known=1,
        rows_new=1,
        gap_size=1,
        cursor_json={"page": 1, "row_index": 0},
    )
    db.add(q)
    await db.flush()

    result = await runner.run_query(q, {})
    assert runner.fetched == ["https://example.test/2"]
    assert result["staged_this_tick"] == 0
    assert result["rows_seen"] == 2
    assert result["rows_known"] == 2
    assert result["rows_new"] == 1
    assert result["gap_size"] == 0
    assert result["status"] == "done"
    assert not (q.last_error or "").startswith("unresolved linked gap")
    samples = (q.cursor_json or {}).get("unresolved_samples") or []
    assert len(samples) == 1
    assert samples[0]["result"] == preserve_result
    assert samples[0]["citation"] == "2026 CLD 2"
    assert samples[0]["detail_url"] == "https://example.test/2"
    assert any(
        preserve_result in r.message and "2026 CLD 2" in r.message and "https://example.test/2" in r.message
        for r in caplog.records
    )
    await db.rollback()


@pytest.mark.asyncio
async def test_pick_prefers_reporter_year_over_a_higher_priority_citation(db):
    now = datetime.now(timezone.utc)
    citation = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:citation=1967 PTD 139",
        query_json={"citation": "1967 PTD 139"},
        status="pending",
        priority=1,
        last_run_at=now - timedelta(days=30),
    )
    reporter = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2005",
        query_json={"reporter": "CLD", "year": 2005},
        status="pending",
        priority=900,
        last_run_at=now,
    )
    db.add(citation)
    db.add(reporter)
    await db.flush()

    picked = await harvest.pick_next_query(db)
    assert picked is not None
    assert picked.query_key == "search:reporter=CLD|year=2005"
    await db.rollback()


@pytest.mark.asyncio
async def test_automatic_claim_skips_citation_keys_when_no_reporter_year_is_pending(db):
    citation = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:citation=1979 SCMR 625",
        query_json={"citation": "1979 SCMR 625"},
        status="pending",
        priority=1,
    )
    db.add(citation)
    await db.flush()

    row, lock = await harvest.claim_next_query(db, 2, redis_client=None)
    assert row is None and lock is None
    assert citation.status == "pending"
    await db.rollback()


@pytest.mark.asyncio
async def test_claim_keeps_citation_plus_year_as_a_structured_search(db):
    """A citation together with a year is a structured search, not a snowball lookup."""
    structured = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:year=1967|citation=1967 PTD 139",
        query_json={"citation": "1967 PTD 139", "year": 1967},
        status="pending",
        priority=200,
    )
    db.add(structured)
    await db.flush()

    row, lock = await harvest.claim_next_query(db, 2, redis_client=None)
    assert row is not None and row.query_key == structured.query_key
    if lock is not None:
        await lock.release()
    await db.rollback()


async def _no_seed(db, dry_run=False, search_map=None):
    return {"inserted": 0}


@pytest.mark.asyncio
async def test_frontier_reopens_fresh_gap_despite_citation_backlog_and_cooldown(db, monkeypatch):
    monkeypatch.setattr(harvest.settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", "CLD,PLD")
    monkeypatch.setattr(harvest, "seed_search_harvest_plan", _no_seed)
    now = datetime.now(timezone.utc)
    gapped = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2005",
        query_json={"reporter": "CLD", "year": 2005},
        status="done",
        priority=121,
        pages_enumerated=1,
        rows_seen=372,
        rows_known=335,
        rows_new=372,
        gap_size=37,
        last_run_at=now,
        cursor_json={"page": 1, "row_index": 0},
    )
    citation = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:citation=1968 PTD 73",
        query_json={"citation": "1968 PTD 73"},
        status="pending",
        priority=200,
    )
    db.add(gapped)
    db.add(citation)
    await db.flush()

    opened = await harvest.ensure_search_harvest_frontier(db, reporter_shard=0)
    assert opened["pending"] >= 1
    assert gapped.status == "pending"
    assert gapped.rows_seen == 372
    assert gapped.rows_new == 372
    assert citation.status == "pending"
    await db.rollback()


@pytest.mark.asyncio
async def test_frontier_leaves_unresolved_gap_alone_until_cooldown(db, monkeypatch):
    monkeypatch.setattr(harvest.settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", "CLD,PLD")
    monkeypatch.setattr(harvest, "seed_search_harvest_plan", _no_seed)
    now = datetime.now(timezone.utc)
    gapped = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2005",
        query_json={"reporter": "CLD", "year": 2005},
        status="done",
        gap_size=37,
        rows_seen=372,
        rows_known=335,
        last_run_at=now,
        last_error="unresolved linked gap: 37 row(s) still unmatched after detail fetch",
    )
    db.add(gapped)
    await db.flush()

    opened = await harvest.ensure_search_harvest_frontier(db, reporter_shard=0)
    assert opened["pending"] == 0
    assert gapped.status == "done"
    await db.rollback()


@pytest.mark.asyncio
async def test_frontier_rewalks_newest_cooled_reporter_year_before_citations(db, monkeypatch):
    monkeypatch.setattr(harvest.settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", "CLD,PLD")
    monkeypatch.setattr(harvest, "seed_search_harvest_plan", _no_seed)
    now = datetime.now(timezone.utc)
    old = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=1990",
        query_json={"reporter": "CLD", "year": 1990},
        status="done",
        gap_size=0,
        pages_enumerated=1,
        last_run_at=now - timedelta(hours=8),
    )
    recent = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2025",
        query_json={"reporter": "CLD", "year": 2025},
        status="done",
        gap_size=0,
        pages_enumerated=2,
        cursor_json={"page": 2, "row_index": 4},
        last_run_at=now - timedelta(hours=7),
    )
    citation = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:citation=1979 SCMR 62",
        query_json={"citation": "1979 SCMR 62"},
        status="pending",
        priority=1,
    )
    db.add(old)
    db.add(recent)
    db.add(citation)
    await db.flush()

    monkeypatch.setattr(harvest, "REPORTER_YEAR_REOPEN_LIMIT", 1)
    opened = await harvest.ensure_search_harvest_frontier(db, reporter_shard=0)
    assert opened["pending"] == 1
    assert recent.status == "pending"
    assert recent.pages_enumerated == 0
    assert (recent.cursor_json or {}).get("page") == 1
    assert old.status == "done"
    assert citation.status == "pending"
    await db.rollback()


@pytest.mark.asyncio
async def test_site_total_only_gap_is_not_reopened_inside_cooldown_after_two_finishes(db, monkeypatch):
    """A site total above rows_seen, with every seen row already known, must not reopen forever.

    The first confirming finish may be searched again immediately. The second finish records a
    terminal marker, and the next reopen inside the cooldown leaves the row done.
    """
    monkeypatch.setattr(harvest.settings, "PLS_SEARCH_HARVEST_SLOT1_REPORTERS", "CLD,PLD")
    runner = _runner(monkeypatch, db, {"page1": ([], None)})
    now = datetime.now(timezone.utc)
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLD|year=2001",
        query_json={"reporter": "CLD", "year": 2001},
        status="in_progress",
        pages_enumerated=1,
        rows_seen=10,
        rows_known=10,
        rows_new=10,
        gap_size=30,
        site_total_results=40,
        last_run_at=now,
        cursor_json={"page": 1, "row_index": 0},
    )
    db.add(q)
    await db.flush()

    first = await runner.run_query(q, {})
    assert first["status"] == "done"
    assert first["gap_size"] == 30
    assert first["rows_seen"] == 10
    assert first["rows_known"] == 10

    opened = await harvest.reopen_gapped_search_harvest_queries(db, reporter_shard=0)
    assert opened == 1
    assert q.status == "pending"

    second = await runner.run_query(q, {})
    assert second["status"] == "done"
    assert second["gap_size"] == 30

    again = await harvest.reopen_gapped_search_harvest_queries(db, reporter_shard=0)
    assert again == 0
    assert q.status == "done"
    await db.rollback()
