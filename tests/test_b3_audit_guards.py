"""B3 audit guards: reason_code fidelity, resolve-link strictness, refetch flags, migration 022."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from scraper.config import settings
from scraper.database import engine
from scraper.fetchers import FetchResult, PDF_MAGIC
from scraper.models import QuarantineQueue, Statute, StatuteSection, StatuteSectionVersion, StatutesStaging
from scraper.tasks.promotion import _resolve_statute_link, promote_statute_staging
from scraper.tasks.public_pipeline import PublicPipeline
from tests.test_b3_statute_promotion import _make_statute_staging


async def _quarantine_details(db, st) -> dict:
    row = (
        await db.execute(select(QuarantineQueue).where(QuarantineQueue.statutes_staging_id == st.id))
    ).scalars().one()
    return dict(row.details or {})


@pytest.mark.asyncio
async def test_statute_name_quality_quarantine_reason_code(db):
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "%PDF-1.4 stream",
            "year_enacted": 2024,
            "jurisdiction": "Federal",
            "sections": [
                {
                    "section_number": "1",
                    "section_title": "Short title",
                    "section_text": "1. This is not a real statute title but has a parseable section body.",
                }
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    details = await _quarantine_details(db, st)
    assert details.get("reason_code") == "statute_name_quality"


@pytest.mark.asyncio
async def test_listing_only_reason_code_refetch_without_overwriting_source(db):
    statute = Statute(
        name="Listed Act, 1990",
        jurisdiction="Federal",
        year_enacted=1990,
        source_url="http://legacy.example/listed-act",
        source_name="Senate",
        needs_section_refetch=False,
    )
    db.add(statute)
    await db.flush()
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Listed Act, 1990",
            "year_enacted": 2024,
            "jurisdiction": "Federal",
            "sections": [],
            "field_evidence": {"listing_only": True},
        },
    )
    st.source_url = "http://new-listing.example/page"
    await db.flush()
    assert await promote_statute_staging(db, st) == "quarantined"
    details = await _quarantine_details(db, st)
    assert details.get("reason_code") == "listing_only_statute"
    await db.refresh(statute)
    assert statute.needs_section_refetch is True
    assert statute.source_url == "http://legacy.example/listed-act"
    assert statute.year_enacted == 1990


@pytest.mark.asyncio
async def test_statute_no_year_quarantine_reason_code(db):
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Year Missing Act",
            "jurisdiction": "Federal",
            "sections": [
                {
                    "section_number": "1",
                    "section_title": "Short title",
                    "section_text": "1. This Act is the Year Missing Act with enough body text.",
                }
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert (await _quarantine_details(db, st)).get("reason_code") == "statute_no_year"


@pytest.mark.asyncio
async def test_statute_duplicate_name_quarantine_reason_code(db):
    db.add(Statute(name="Shared Title Act", jurisdiction="Federal", year_enacted=2010))
    await db.flush()
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "shared title act",
            "year_enacted": 2024,
            "jurisdiction": "Federal",
            "sections": [
                {
                    "section_number": "1",
                    "section_title": "Short title",
                    "section_text": "1. Another act that collides on canonical name.",
                }
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert (await _quarantine_details(db, st)).get("reason_code") == "statute_duplicate_name"


@pytest.mark.asyncio
async def test_empty_statute_sections_quarantine_reason_code(db):
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Whitespace Section Act, 2026",
            "year_enacted": 2026,
            "jurisdiction": "Federal",
            "sections": [{"section_number": "1", "section_title": "x", "section_text": "   "}],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert (await _quarantine_details(db, st)).get("reason_code") == "empty_statute_sections"


@pytest.mark.asyncio
async def test_same_name_different_year_quarantine_reason_code(db):
    db.add(Statute(name="Dual Year Act", jurisdiction="Federal", year_enacted=1999))
    await db.flush()
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Dual Year Act",
            "year_enacted": 2005,
            "jurisdiction": "Federal",
            "sections": [
                {
                    "section_number": "1",
                    "section_title": "Short title",
                    "section_text": "1. This incoming row disagrees on year_enacted.",
                }
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    details = await _quarantine_details(db, st)
    assert details.get("reason_code") == "statute_duplicate_name"
    assert details.get("existing_year_enacted") == 1999
    assert details.get("incoming_year_enacted") == 2005
    assert (await db.execute(select(func.count()).select_from(StatuteSection))).scalar() == 0


@pytest.mark.asyncio
async def test_successful_promote_clears_needs_section_refetch(db):
    statute = Statute(
        name="Refetch Clear Act, 2020",
        jurisdiction="Federal",
        year_enacted=2020,
        needs_section_refetch=True,
    )
    db.add(statute)
    await db.flush()
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Refetch Clear Act, 2020",
            "year_enacted": 2020,
            "jurisdiction": "Federal",
            "sections": [
                {
                    "section_number": "1",
                    "section_title": "Short title",
                    "section_text": "1. This Act may be called the Refetch Clear Act, 2020.",
                }
            ],
        },
    )
    assert await promote_statute_staging(db, st) in ("promoted", "duplicate")
    await db.refresh(statute)
    assert statute.needs_section_refetch is False


@pytest.mark.asyncio
async def test_resolve_statute_link_no_fuzzy_when_stubs_on(db):
    db.add(Statute(name="Income Tax Act, 1990", jurisdiction="Federal", year_enacted=1990))
    await db.flush()
    linked = await _resolve_statute_link(
        db,
        canonical_name="Income Tax",
        source_name="Senate",
        source_url=None,
        jurisdiction="Federal",
    )
    assert linked is None
    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 1


@pytest.mark.asyncio
async def test_resolve_statute_link_normalized_exact_match(db):
    db.add(Statute(name="Sales Tax Act, 1990", jurisdiction="Federal", year_enacted=1990))
    await db.flush()
    linked = await _resolve_statute_link(
        db,
        canonical_name="sales tax act, 1990",
        source_name="Senate",
        source_url=None,
        jurisdiction="Federal",
    )
    assert linked is not None
    assert linked.name == "Sales Tax Act, 1990"


@pytest.mark.asyncio
async def test_migration_022_upgrade_marks_seeded_statute_flags(db):
    mig = importlib.import_module("migrations.022_statute_section_refetch_flags")
    sectionless = Statute(
        name="Migration Sectionless Act",
        jurisdiction="Federal",
        year_enacted=2001,
        needs_section_refetch=False,
        quality_flag_no_year=False,
        quality_flag_duplicate_name=False,
    )
    with_section = Statute(
        name="Migration Complete Act",
        jurisdiction="Federal",
        year_enacted=2002,
        needs_section_refetch=False,
    )
    no_year = Statute(
        name="Migration No Year Act",
        jurisdiction="Federal",
        year_enacted=None,
        quality_flag_no_year=False,
    )
    dup_a = Statute(name="Migration Dup Act", jurisdiction="Federal", year_enacted=2003)
    dup_b = Statute(name="migration dup act", jurisdiction="Federal", year_enacted=2004)
    unique = Statute(name="Migration Unique Act", jurisdiction="Federal", year_enacted=2005)
    db.add_all([sectionless, with_section, no_year, dup_a, dup_b, unique])
    await db.flush()
    sec = StatuteSection(statute_id=with_section.id, section_number="1", section_title="t")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(
        section_id=sec.id,
        version_no=1,
        section_text="1. Complete act section body.",
        text_hash="c" * 64,
    )
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    await db.commit()

    async with engine.begin() as conn:
        await mig.upgrade(conn)

    await db.refresh(sectionless)
    await db.refresh(with_section)
    await db.refresh(no_year)
    await db.refresh(dup_a)
    await db.refresh(dup_b)
    await db.refresh(unique)
    assert sectionless.needs_section_refetch is True
    assert with_section.needs_section_refetch is False
    assert no_year.quality_flag_no_year is True
    assert unique.quality_flag_no_year is False
    assert dup_a.quality_flag_duplicate_name is True
    assert dup_b.quality_flag_duplicate_name is True
    assert unique.quality_flag_duplicate_name is False


def _fake_pdf_text(marker: str, repeats: int) -> bytes:
    body = f"1. Short title.- {marker}\n" + (f"{marker} body " * repeats)
    return PDF_MAGIC + b"\n" + body.encode("utf-8")


@dataclass
class _Outcome:
    data: dict
    deterministic_json: dict = None
    ai_json: dict = None
    engine: str = "test"
    confidence: float = 1.0
    errors: list = None
    conflicts: list = None
    quarantine: bool = False
    quarantine_reason: str = ""

    def __post_init__(self):
        self.deterministic_json = self.deterministic_json or {}
        self.ai_json = self.ai_json or {}
        self.errors = self.errors or []
        self.conflicts = self.conflicts or []


@pytest.mark.asyncio
async def test_ingest_statute_sets_listing_only_when_no_pdf_and_no_sections(db, source, monkeypatch):
    html = '<html><body><a href="/act.pdf">PDF</a></body></html>'
    res = FetchResult(
        url="http://127.0.0.1/listing.html",
        final_url="http://127.0.0.1/listing.html",
        status_code=200,
        content=html.encode("utf-8"),
        content_type="text/html",
    )
    pipeline = PublicPipeline(db, source)
    monkeypatch.setattr(settings, "SSA_STATUTE_PDF_FOLLOW", False)

    async def _extract_statute(**_kwargs):
        return _Outcome(
            data={
                "statute_name": "Pipeline Listing Act, 2024",
                "year_enacted": 2024,
                "jurisdiction": "Federal",
                "sections": [],
            }
        )

    monkeypatch.setattr(
        pipeline,
        "_extractor",
        lambda *a, **k: SimpleNamespace(extract_statute=AsyncMock(side_effect=_extract_statute)),
    )
    assert await pipeline.ingest_statute(res, route={}) == "staged"
    st = (await db.execute(select(StatutesStaging).order_by(StatutesStaging.created_at.desc()))).scalars().first()
    assert st is not None
    assert st.reconciled_json.get("field_evidence", {}).get("listing_only") is True


@pytest.mark.asyncio
async def test_ingest_statute_pdf_follow_picks_longest_pdf_and_clears_listing_only(db, source, monkeypatch):
    html = (
        '<html><body><a href="/short.pdf">a</a><a href="/long.pdf">b</a>'
        "Acts listing chrome</body></html>"
    )
    res = FetchResult(
        url="http://127.0.0.1/listing.html",
        final_url="http://127.0.0.1/listing.html",
        status_code=200,
        content=html.encode("utf-8"),
        content_type="text/html",
    )
    short_pdf = _fake_pdf_text("Short PDF Act", 60)
    long_pdf = _fake_pdf_text("Long PDF Act", 140)

    async def _fetch(url: str):
        if "short" in url:
            return FetchResult(url=url, final_url=url, status_code=200, content=short_pdf, content_type="application/pdf")
        if "long" in url:
            return FetchResult(url=url, final_url=url, status_code=200, content=long_pdf, content_type="application/pdf")
        raise AssertionError(url)

    def _pdf_text_with_ocr(content: bytes):
        if content.startswith(PDF_MAGIC):
            return content.split(b"\n", 1)[-1].decode("utf-8"), False
        return "", False

    import scraper.tasks.public_pipeline as public_pipeline_mod

    pipeline = PublicPipeline(db, source)

    async def bound_fetch(url: str):
        return await _fetch(url)

    pipeline.fetch = bound_fetch
    monkeypatch.setattr(public_pipeline_mod, "pdf_text_with_ocr", _pdf_text_with_ocr)
    monkeypatch.setattr(settings, "SSA_STATUTE_PDF_FOLLOW", True)
    captured_text = {}

    async def _extract_statute(**kwargs):
        captured_text["text"] = kwargs.get("text") or ""
        return _Outcome(
            data={
                "statute_name": "Pipeline PDF Act, 2024",
                "year_enacted": 2024,
                "jurisdiction": "Federal",
                "sections": [
                    {
                        "section_number": "1",
                        "section_title": "Short title",
                        "section_text": "1. Section from followed PDF.",
                    }
                ],
            }
        )

    monkeypatch.setattr(
        pipeline,
        "_extractor",
        lambda *a, **k: SimpleNamespace(extract_statute=AsyncMock(side_effect=_extract_statute)),
    )
    assert await pipeline.ingest_statute(res, route={}) == "staged"
    assert "Long PDF Act" in captured_text["text"]
    st = (await db.execute(select(StatutesStaging).order_by(StatutesStaging.created_at.desc()))).scalars().first()
    assert st is not None
    assert st.reconciled_json.get("field_evidence", {}).get("listing_only") is not True
