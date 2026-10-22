"""B7/R1 K3: document_validation quarantines at promotion and public_pipeline without corpus rows."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from scraper.fetchers import FetchResult, record_provenance, stage_judgment, stage_statute
from scraper.models import Judgment, ScraperSource, StatutesStaging
from scraper.tasks.promotion import promote_judgment_staging, promote_statute_staging
from scraper.tasks.public_pipeline import PublicPipeline
from tests.test_b3_statute_promotion import _make_statute_staging


@pytest.mark.asyncio
async def test_promotion_quarantines_short_judgment_with_document_validation(db):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "SupremeCourt"))).scalars().first()
    full_text = "JUDGMENT\n" + ("The court dismisses the appeal. " * 3)
    full_text = full_text[:100]
    assert len(full_text) < 140
    prov = await record_provenance(db, source=source, url="http://127.0.0.1/j.txt", content=full_text.encode(), content_kind="text")
    st = await stage_judgment(db, source=source, prov=prov, raw_html=None, raw_text=full_text, url="http://127.0.0.1/j.txt")
    st.status = "extracted"
    st.reconciled_json = {
        "citations": ["PLD 2024 SC 8801"],
        "court": "Supreme Court of Pakistan",
        "year": 2024,
        "case_title": "Test v State",
        "judge_names": ["Justice A"],
        "document_type": "full_judgment",
    }
    assert await promote_judgment_staging(db, st) == "quarantined"
    assert (st.quarantine_reason or "").startswith("document_validation:")
    assert (await db.execute(select(func.count()).select_from(Judgment))).scalar() == 0


@pytest.mark.asyncio
async def test_promotion_quarantines_sectionless_statute_with_document_validation(db):
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "No Sections Act, 2024",
            "year_enacted": 2024,
            "jurisdiction": "Federal",
            "sections": [],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert (st.quarantine_reason or "").startswith("document_validation:")
    from scraper.models import Statute

    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 0


@pytest.mark.asyncio
async def test_public_pipeline_quarantines_sectionless_statute_with_document_validation(db, source, monkeypatch):
    html = "<html><body>Acts listing chrome only</body></html>"
    res = FetchResult(
        url="http://127.0.0.1/list.html",
        final_url="http://127.0.0.1/list.html",
        status_code=200,
        content=html.encode(),
        content_type="text/html",
    )
    pipeline = PublicPipeline(db, source)

    async def _extract_statute(**kwargs):
        return type(
            "Outcome",
            (),
            {
                "data": {
                    "statute_name": "Pipeline Empty Act, 2024",
                    "year_enacted": 2024,
                    "jurisdiction": "Federal",
                    "sections": [],
                },
                "deterministic_json": {},
                "ai_json": {},
                "engine": "deterministic",
                "confidence": 0.9,
                "errors": [],
                "conflicts": [],
                "quarantine": False,
                "quarantine_reason": None,
            },
        )()

    monkeypatch.setattr(pipeline, "_extractor", lambda *a, **k: type("E", (), {"extract_statute": AsyncMock(side_effect=_extract_statute)})())
    assert await pipeline.ingest_statute(res, route={}) == "staged"
    st = (await db.execute(select(StatutesStaging).order_by(StatutesStaging.created_at.desc()))).scalars().first()
    assert st is not None
    assert st.status == "quarantined"
    assert (st.quarantine_reason or "").startswith("document_validation:")
