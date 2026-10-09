"""A search-harvest tick gives the single login worker back after PLS_SEARCH_HARVEST_TICK_SECONDS.

On 8 October 2026 one tick ran for 4,549 seconds; every case-ID walk tick queued behind it expired
unused, so the walk never ran. A tick now stops between rows (or pages) once its time is up and the
next tick resumes the query from its cursor."""

from __future__ import annotations

from types import SimpleNamespace

from scraper.config import search_harvest_tick_expiry, settings
from scraper.models import PlsSearchHarvestQuery
from scraper.tasks import pls_search_harvest as harvest


def _row(n: int) -> dict:
    return {"citation": f"2001 SCMR {n}", "detail_url": f"https://example.test/case/{n}"}


PAGES = {
    "page1": ([_row(1), _row(2), _row(3)], "page2"),
    "page2": ([_row(4), _row(5), _row(6)], None),
}


class FakePipeline:
    def __init__(self, clock):
        self.clock = clock
        self.fetched_details = []
        self.runner = SimpleNamespace(browser=None)
        self.local_engine = None

    async def fetch_results(self, search_map, values):
        return SimpleNamespace(html="page1", url="page1")

    async def fetch_detail(self, url):
        if url in PAGES:
            return SimpleNamespace(html=url, url=url)
        self.fetched_details.append(url)
        return SimpleNamespace(html="", url=url)

    async def preserve_and_extract(self, detail, route, row):
        return "staged"

    async def sleep(self, seconds):
        self.clock[0] += 100  # each paced step costs 100 seconds


def _runner(db, monkeypatch, clock):
    async def extract_result_rows(self, *, html, search_map, base_url):
        rows, nxt = PAGES[html]
        return SimpleNamespace(data={"result_rows": rows, "next_page": nxt, "total_results_if_shown": 6})

    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.extract_result_rows", extract_result_rows)
    monkeypatch.setattr("scraper.extractors.hybrid_extractor.HybridExtractor.__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(harvest, "unmapped_harvest_reason", lambda *a: None)
    monkeypatch.setattr(harvest, "build_harvest_form_values", lambda *a: {})
    monkeypatch.setattr(harvest, "release_page_result", lambda page: None)
    monkeypatch.setattr(harvest.time, "monotonic", lambda: clock[0])

    runner = harvest.SearchHarvestRunner.__new__(harvest.SearchHarvestRunner)
    runner.db, runner.source, runner.dry_run = db, None, False
    runner.pipeline = FakePipeline(clock)
    runner.memory = SimpleNamespace(note_window=lambda: None)
    runner.pacing_profile = {"login_delay_min": 1, "login_delay_max": 1}

    async def no_recycle():
        return None

    runner._maybe_recycle_browser = no_recycle
    return runner


async def test_tick_stops_when_its_time_is_up_and_the_next_tick_resumes(db, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 250)
    clock = [0.0]
    q = PlsSearchHarvestQuery(source_name="PakistanLawSite", query_key="budget-test", query_json={}, status="in_progress")
    db.add(q)
    await db.flush()

    runner = _runner(db, monkeypatch, clock)
    first = await runner.run_query(q, {})
    assert first["out_of_time"] is True and q.status == "in_progress"
    assert runner.pipeline.fetched_details == [f"https://example.test/case/{n}" for n in (1, 2, 3)]
    # page 1 finished before the time ran out: the next tick starts on page 2
    assert q.cursor_json == {"page": 2, "row_index": 0, "next_url": "page2"} and q.pages_enumerated == 1

    clock[0] = 0.0
    runner = _runner(db, monkeypatch, clock)
    second = await runner.run_query(q, {})
    assert second["out_of_time"] is False and q.status == "done"  # time ran out only after the last row
    assert runner.pipeline.fetched_details == [f"https://example.test/case/{n}" for n in (4, 5, 6)]
    assert q.rows_seen == 6 and q.rows_new == 6

    await db.rollback()


async def test_tick_stopped_mid_page_resumes_at_the_next_row_without_double_counting(db, monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 150)
    clock = [0.0]
    q = PlsSearchHarvestQuery(source_name="PakistanLawSite", query_key="budget-test-2", query_json={}, status="in_progress")
    db.add(q)
    await db.flush()

    runner = _runner(db, monkeypatch, clock)
    first = await runner.run_query(q, {})
    assert first["out_of_time"] is True
    assert runner.pipeline.fetched_details == ["https://example.test/case/1", "https://example.test/case/2"]
    assert q.cursor_json["row_index"] == 2 and q.pages_enumerated == 0 and q.rows_seen == 0

    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0)
    runner = _runner(db, monkeypatch, clock)
    second = await runner.run_query(q, {})
    assert q.status == "done" and second["out_of_time"] is False
    assert runner.pipeline.fetched_details == [f"https://example.test/case/{n}" for n in (3, 4, 5, 6)]
    assert q.rows_seen == 6 and q.rows_new == 6

    await db.rollback()


def test_queued_ticks_expire_after_about_one_tick(monkeypatch):
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 1200)
    assert search_harvest_tick_expiry() == 1200
    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0)
    assert search_harvest_tick_expiry() == 3600


async def test_cursor_and_staged_rows_are_saved_after_every_row_and_survive_a_killed_tick(db, monkeypatch):
    """9 October 2026 (PR #169, stalls #6 and #7): the cursor dict was changed in place and assigned back, so after
    the first flush SQLAlchemy saw no change and the stored row_index stayed at 1-9 while the tick advanced to 67+;
    and nothing was committed until the tick ended, so a killed tick lost everything. Each tick restarted near the
    top and re-fetched ~65 rows. Progress is now written and committed after every row."""
    from sqlalchemy import delete, select

    from scraper.database import SessionLocal

    monkeypatch.setattr(settings, "PLS_SEARCH_HARVEST_TICK_SECONDS", 0)
    clock = [0.0]
    q = PlsSearchHarvestQuery(source_name="PakistanLawSite", query_key="kill-test", query_json={}, status="in_progress")
    db.add(q)
    await db.commit()
    try:
        runner = _runner(db, monkeypatch, clock)
        real = runner.pipeline.preserve_and_extract

        async def killed_on_row_3(detail, route, row):
            if detail.url.endswith("/3"):
                raise RuntimeError("worker killed")
            return await real(detail, route, row)

        runner.pipeline.preserve_and_extract = killed_on_row_3
        try:
            await runner.run_query(q, {})
        except RuntimeError:
            pass
        await db.rollback()  # what the dead worker's open transaction amounts to
        async with SessionLocal() as other:
            saved = (await other.execute(select(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.query_key == "kill-test"))).scalars().one()
            assert saved.cursor_json["row_index"] == 2 and saved.rows_new == 2
    finally:
        await db.rollback()
        await db.execute(delete(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.query_key == "kill-test"))
        await db.commit()


async def test_search_rows_without_a_citation_are_known_by_their_case_name(db):
    """PR #169 stall #7: search-harvest result rows carry no citation (the grid has no citation column), so
    citation_keys_for_row gave no key and every row already held was fetched again (~65 per tick). A row's
    CaseName (its judgment's identity on the site) now marks it known when any staged or promoted capture has it."""
    import hashlib

    from scraper.models import ScraperStaging, SourceProvenance
    from scraper.pls_search_harvest_core import citation_keys_for_row

    held = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2025L8&court=&Row=0&bookName=undefined"
    prov = SourceProvenance(source_name="PakistanLawSite", access_method="login_session", source_url=held, content_hash=hashlib.sha256(held.encode()).hexdigest(), content_kind="html")
    db.add(prov)
    await db.flush()
    db.add(ScraperStaging(source_name="PakistanLawSite", access_method="login_session", source_url=held, provenance_id=prov.id, content_hash="x" * 64, status="quarantined"))
    await db.flush()

    held_row = {"citation": "", "detail_url": held.replace("Row=0", "Row=5")}
    new_row = {"detail_url": held.replace("2025L8", "2025L80")}
    keys = citation_keys_for_row(held_row) | citation_keys_for_row(new_row)
    assert keys == {"case:2025L8", "case:2025L80"}
    assert await harvest.load_known_citation_keys(db, keys) == {"case:2025L8"}
    await db.rollback()
