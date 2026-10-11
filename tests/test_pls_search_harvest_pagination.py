"""Search harvest must not mark a query done after page 1 when the site total is unknown or pagination stops early."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scraper.models import PlsSearchHarvestQuery
from scraper.pls_search_harvest_core import search_harvest_may_mark_done
from scraper.tasks import pls_search_harvest as harvest


def test_search_harvest_may_mark_done_rules():
    done, reason = search_harvest_may_mark_done(site_total=None, rows_seen=284, rows_on_page=284)
    assert done is False and "unknown" in reason
    assert search_harvest_may_mark_done(site_total=600, rows_seen=284, rows_on_page=284)[0] is False
    assert search_harvest_may_mark_done(site_total=6, rows_seen=6, rows_on_page=3) == (True, "")
    assert search_harvest_may_mark_done(site_total=None, rows_seen=0, rows_on_page=0) == (True, "")


def _runner_factory(monkeypatch, db, pages, *, total=None, fail_url=None):
    clock = [0.0]

    class FakePipeline:
        def __init__(self):
            self.runner = SimpleNamespace(browser=None)
            self.local_engine = None

        async def fetch_results(self, search_map, values):
            return SimpleNamespace(html="page1", url="page1")

        async def fetch_detail(self, url):
            if fail_url and url == fail_url:
                raise RuntimeError("Target closed")
            if url in pages:
                return SimpleNamespace(html=url, url=url)
            return SimpleNamespace(html="", url=url)

        async def preserve_and_extract(self, detail, route, row):
            return "staged"

        async def sleep(self, seconds):
            clock[0] += 1

    async def extract_result_rows(self, *, html, search_map, base_url):
        rows, nxt = pages[html]
        data = {"result_rows": rows, "next_page": nxt}
        if total is not None:
            data["total_results_if_shown"] = total
        return SimpleNamespace(data=data)

    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.extract_result_rows", extract_result_rows)
    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(harvest, "unmapped_harvest_reason", lambda *a: None)
    monkeypatch.setattr(harvest, "build_harvest_form_values", lambda *a: {})
    monkeypatch.setattr(harvest, "release_page_result", lambda page: None)
    async def empty_known(db, keys):
        return set()

    monkeypatch.setattr(harvest, "load_known_citation_keys", empty_known)
    monkeypatch.setattr(harvest.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(harvest.settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0)

    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db, runner.source, runner.dry_run = db, None, True
    runner.pipeline = FakePipeline()
    runner.memory = SimpleNamespace(note_window=lambda: None)
    runner.pacing_profile = {"login_delay_min": 0, "login_delay_max": 0}

    async def no_recycle():
        return None

    runner._maybe_recycle_browser = no_recycle
    return runner


def _row(n: int) -> dict:
    return {"citation": f"2019 CLC {n}", "detail_url": f"https://example.test/case/{n}"}


@pytest.mark.asyncio
async def test_unknown_total_without_next_stays_in_progress(db, monkeypatch):
    pages = {
        "page1": ([_row(i) for i in range(1, 5)], None),
    }
    runner = _runner_factory(monkeypatch, db, pages, total=None)
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="pagination-unknown-total",
        query_json={"reporter": "CLC", "year": 2019},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    result = await runner.run_query(q, {})
    assert result["status"] == "in_progress"
    assert q.status == "in_progress"
    assert q.pages_enumerated == 1
    assert "site total unknown" in (q.last_error or "")
    await db.rollback()


@pytest.mark.asyncio
async def test_interrupted_next_page_fetch_stays_in_progress(db, monkeypatch):
    pages = {
        "page1": ([_row(1), _row(2)], "page2"),
        "page2": ([_row(3), _row(4)], None),
    }
    runner = _runner_factory(monkeypatch, db, pages, total=4, fail_url="page2")
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="pagination-interrupted",
        query_json={"reporter": "PLD", "year": 2019},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    result = await runner.run_query(q, {})
    assert result["status"] == "in_progress"
    assert q.status == "in_progress"
    assert "pagination interrupted" in (q.last_error or "")
    assert q.cursor_json.get("next_url") == "page2"
    await db.rollback()


@pytest.mark.asyncio
async def test_known_total_last_page_marks_done(db, monkeypatch):
    pages = {
        "page1": ([_row(1), _row(2)], "page2"),
        "page2": ([_row(3)], None),
    }
    runner = _runner_factory(monkeypatch, db, pages, total=3)
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="pagination-complete",
        query_json={"reporter": "PLD", "year": 2019},
        status="in_progress",
    )
    db.add(q)
    await db.flush()

    result = await runner.run_query(q, {})
    assert result["status"] == "done"
    assert q.status == "done"
    assert q.pages_enumerated == 2
    assert q.rows_seen == 3
    await db.rollback()


@pytest.mark.asyncio
async def test_reopen_premature_done_queries(db):
    q = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key="search:reporter=CLC|year=2019",
        query_json={"reporter": "CLC", "year": 2019},
        status="done",
        pages_enumerated=1,
        rows_seen=284,
        site_total_results=None,
        cursor_json={"page": 1, "row_index": 0},
    )
    db.add(q)
    await db.flush()

    preview = await harvest.reopen_premature_done_queries(db, dry_run=True)
    assert preview["would_reopen"] == 1

    applied = await harvest.reopen_premature_done_queries(db, dry_run=False)
    assert applied["reopened"] == 1
    assert q.status == "in_progress"
    assert q.pages_enumerated == 0
    assert q.rows_seen == 0
    await db.rollback()
