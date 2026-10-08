"""Event-driven chain fetch → promote → archive mirror (scraper/tasks/chain.py). Beat stays the
safety net; these tests cover the debounced requests and the points that make them."""

from __future__ import annotations

import pytest
import redis
from sqlalchemy import select, text

from scraper.config import settings
from scraper.database import SessionLocal, engine
from scraper.fetchers import HttpFetcher, record_provenance, stage_judgment
from scraper.models import ScraperStaging
from scraper.tasks import chain
from scraper.tasks.celery_app import app
from tests.fixtures import judgment_html


@pytest.fixture
def chain_on(monkeypatch):
    """Chain enabled, debounce keys cleared, send_task recorded instead of reaching the broker."""
    monkeypatch.setattr(settings, "CORPUS_CHAIN_ENABLED", True)
    r = redis.from_url(settings.REDIS_URL)
    keys = list(r.scan_iter(match="corpus:chain:*"))
    if keys:
        r.delete(*keys)
    sent = []

    def fake_send_task(name, args=(), kwargs=None, queue=None, countdown=None):
        sent.append({"name": name, "kwargs": kwargs, "queue": queue, "countdown": countdown})

    monkeypatch.setattr(app, "send_task", fake_send_task)
    yield sent
    keys = list(r.scan_iter(match="corpus:chain:*"))
    if keys:
        r.delete(*keys)
    r.close()


def test_request_promotion_is_debounced_per_source(chain_on):
    assert chain.request_promotion("PakistanLawSite") is True
    assert chain.request_promotion("PakistanLawSite") is False  # same window: no second task
    assert chain.request_promotion("SupremeCourt") is True
    assert [s["kwargs"].get("source_name") for s in chain_on] == ["PakistanLawSite", "SupremeCourt"]
    first = chain_on[0]
    assert first["name"] == "scraper.tasks.promotion.promote_staging_records"
    assert first["queue"] == "maintenance"
    assert first["kwargs"]["limit"] == 200
    assert first["countdown"] > 0  # the staging commit lands before the run


def test_request_mirror_is_debounced(chain_on):
    assert chain.request_mirror() is True
    assert chain.request_mirror() is False
    assert [(s["name"], s["queue"]) for s in chain_on] == [("scraper.tasks.archive_mirror.mirror_pending", "maintenance")]


def test_enqueue_errors_are_swallowed_and_free_the_window(chain_on, monkeypatch):
    def broken_send_task(*a, **kw):
        raise ConnectionError("broker down")

    monkeypatch.setattr(app, "send_task", broken_send_task)
    assert chain.request_promotion("PakistanLawSite") is False
    # Nothing was queued, so the debounce key must not block the next batch.
    monkeypatch.setattr(app, "send_task", lambda name, **kw: chain_on.append({"name": name, **kw}))
    assert chain.request_promotion("PakistanLawSite") is True
    assert len(chain_on) == 1


def test_redis_errors_are_swallowed(chain_on, monkeypatch):
    def no_redis():
        raise redis.ConnectionError("redis down")

    monkeypatch.setattr(chain, "_redis", no_redis)
    assert chain.request_promotion("PakistanLawSite") is False
    assert chain.request_mirror() is False
    assert chain_on == []


def test_disabled_chain_sends_nothing(chain_on, monkeypatch):
    monkeypatch.setattr(settings, "CORPUS_CHAIN_ENABLED", False)
    assert chain.request_promotion("PakistanLawSite") is False
    assert chain.request_mirror() is False
    assert chain_on == []


async def test_promotion_is_requested_once_after_commit(db, monkeypatch):
    calls = []
    monkeypatch.setattr(chain, "request_promotion", lambda source_name=None: calls.append(source_name))
    chain.request_promotion_after_commit(db, "PakistanLawSite")
    chain.request_promotion_after_commit(db, "PakistanLawSite")
    assert calls == []  # nothing before the staging rows are committed
    await db.commit()
    assert calls == ["PakistanLawSite"]
    await db.commit()
    assert calls == ["PakistanLawSite"]  # one request per committed batch, not per later commit


async def _stage_extracted(db, source, citation: str) -> ScraperStaging:
    full_text = (
        f"Citation Name: {citation}\nMuhammad Akram versus The State\n"
        "Before Qazi Faez Isa, CJ\nJUDGMENT\n"
        "The appellant was convicted under section 302(b) of the Pakistan Penal Code, 1860."
    )
    url = f"https://www.pakistanlawsite.com/chain/{citation.replace(' ', '_')}"
    prov = await record_provenance(db, source=source, url=url, content=full_text.encode(), content_kind="text")
    st = await stage_judgment(db, source=source, prov=prov, raw_html=None, raw_text=full_text, url=url)
    st.reconciled_json = {
        "citations": [citation],
        "court": "Supreme Court of Pakistan",
        "year": 2024,
        "case_title": "Muhammad Akram versus The State",
        "judge_names": ["Qazi Faez Isa"],
        "document_type": "full_judgment",
    }
    st.status = "extracted"
    st.confidence_score = 0.99
    await db.commit()
    return st


async def test_promotion_requests_mirror_only_when_it_saved_something(db, login_source, monkeypatch):
    from scraper.tasks.promotion import promote_staging_records

    mirrors = []
    monkeypatch.setattr(chain, "request_mirror", lambda: mirrors.append(1))
    await _stage_extracted(db, login_source, "PLD 2024 SC 5200")
    counts = await promote_staging_records(limit=10)
    assert counts["promoted"] == 1
    assert mirrors == [1]
    counts = await promote_staging_records(limit=10)
    assert counts["promoted"] == 0 and counts["statutes_promoted"] == 0
    assert mirrors == [1]


async def test_overlapping_promotion_run_skips_and_requests_a_follow_up(db, login_source, monkeypatch):
    from scraper.tasks.promotion import PROMOTION_LOCK_KEY, promote_staging_records_exclusive

    requested = []
    monkeypatch.setattr(chain, "request_promotion", lambda source_name=None: requested.append(source_name))
    monkeypatch.setattr(chain, "request_mirror", lambda: None)
    st = await _stage_extracted(db, login_source, "PLD 2024 SC 5201")
    async with engine.connect() as holder:
        holder = await holder.execution_options(isolation_level="AUTOCOMMIT")
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": PROMOTION_LOCK_KEY})
        try:
            assert await promote_staging_records_exclusive(limit=10, source_name="PakistanLawSite") == {"skipped": "promotion_run_in_progress"}
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": PROMOTION_LOCK_KEY})
    assert requested == ["PakistanLawSite"]
    counts = await promote_staging_records_exclusive(limit=10, source_name="PakistanLawSite")
    assert counts["promoted"] == 1
    async with SessionLocal() as verify:
        assert (await verify.execute(select(ScraperStaging.promoted_to_id).where(ScraperStaging.id == st.id))).scalar() is not None


async def test_pls_staging_requests_promotion(db, login_source, monkeypatch):
    from scraper.auth.session_manager import PageResult
    from scraper.tasks import pakistanlawsite
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from tests.fixtures import BrowserScript
    from tests.test_auth_playwright import _activate, _modal_full_judgment_text, _nosleep, _notes_only_detail_html

    requested = []
    monkeypatch.setattr(pakistanlawsite, "request_promotion_after_commit", lambda session, source_name: requested.append(source_name))
    await _activate(db, login_source)
    citation, title = "PLD 2024 SC 902", "Chain promotion request"
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2006K902&&court= &&Row=0 &&bookName=undefined"
    modal_text = _modal_full_judgment_text(citation, title)
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    result = await pipeline.preserve_and_extract(
        PageResult(
            url=detail_url,
            html=_notes_only_detail_html(citation, title),
            metadata={
                "requested_url": detail_url,
                "final_url": detail_url,
                "case_description_selector_found": True,
                "case_description_modal_found": True,
                "case_description_modal_text": modal_text,
                "case_description_modal_text_length": len(modal_text),
            },
        ),
        {"tier": 4, "row_index": 0},
        {"citation": citation, "title": title, "court": "Supreme Court", "detail_url": detail_url},
    )
    assert result == "staged"
    st = (await db.execute(select(ScraperStaging))).scalars().one()
    assert st.status == "extracted"
    assert requested == ["PakistanLawSite"]


async def test_public_staging_requests_promotion(db, source, fixture_server, monkeypatch):
    from scraper.tasks import public_pipeline
    from scraper.tasks.public_pipeline import PublicPipeline

    requested = []
    monkeypatch.setattr(public_pipeline, "request_promotion_after_commit", lambda session, source_name: requested.append(source_name))
    fixture_server.add("/robots.txt", "", status=404)
    url = fixture_server.add("/j501.html", judgment_html("PLD 2024 SC 501"))
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        pipeline = PublicPipeline(db, source, fetcher=fetcher)
        assert await pipeline.ingest_judgment(await fetcher.get(url), route={"listing": "test"}) == "staged"
    st = (await db.execute(select(ScraperStaging))).scalars().one()
    assert requested == (["SupremeCourt"] if st.status == "extracted" else [])
    assert st.status == "extracted"
