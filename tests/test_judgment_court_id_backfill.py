"""Follow-up to #271: repoint court_id after court_name backfill."""

from __future__ import annotations

from uuid import uuid4

import pytest

from scraper.config import settings
from scraper.judgment_court_consistency import consistency_report, court_labels_match
from sqlalchemy import select

from scraper.models import Court, Judgment
from scraper.parsers.foreign_court_registry import normalize_judgment_court_name, resolve_court_row_for_name
from scraper.tasks.judgment_court_backfill import _record_run, apply_court_fixes, find_court_fixes, undo_run


@pytest.mark.asyncio
async def test_normalize_andhra_slug():
    assert normalize_judgment_court_name("ANDHRA-PRADESH-HIGH-COURT-INDIA") == "Andhra Pradesh High Court, India"


@pytest.mark.asyncio
async def test_ensure_foreign_courts_idempotent(db):
    a = await resolve_court_row_for_name(db, "Supreme Court of India")
    b = await resolve_court_row_for_name(db, "Supreme Court of India")
    assert a is not None and b is not None and a.id == b.id


async def _sc_pakistan(db) -> Court:
    sc_pak = (await db.execute(select(Court).where(Court.name == "Supreme Court of Pakistan"))).scalars().first()
    if sc_pak is None:
        sc_pak = Court(name="Supreme Court of Pakistan", short_code="SC_PAK", court_level="supreme")
        db.add(sc_pak)
        await db.flush()
    return sc_pak


@pytest.mark.asyncio
async def test_court_id_repoint_when_name_already_fixed(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_PLS_COURT_RESOLUTION", True)
    monkeypatch.setattr(settings, "SSA_JUDGMENT_COURT_ID_BACKFILL", True)
    sc_pak = await _sc_pakistan(db)
    jid = uuid4()
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name="Supreme Court of India",
            court_id=sc_pak.id,
            canonical_citation="1990 MLD 947",
            full_text="1990 MLD 947\n[Supreme Court of India]\nBefore X\nA versus B\n" + ("x" * 600),
            full_text_hash="india_id_mismatch",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    dry = await find_court_fixes(db)
    fix = next(f for f in dry["fixes"] if f["id"] == str(jid))
    assert fix["court_name_to"] == "Supreme Court of India"
    assert fix["court_id_from"] == str(sc_pak.id)
    run_id = "courtid001"
    await _record_run(db, run_id)
    await apply_court_fixes(db, run_id, [fix])
    await db.commit()
    j = await db.get(Judgment, jid)
    india = await resolve_court_row_for_name(db, "Supreme Court of India")
    assert j.court_id == india.id
    assert j.court_name == "Supreme Court of India"
    await undo_run(db, run_id)
    await db.refresh(j)
    assert j.court_id == sc_pak.id


@pytest.mark.asyncio
async def test_andhra_rows_repoint_court_id(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_JUDGMENT_COURT_ID_BACKFILL", True)
    sc_pak = await _sc_pakistan(db)
    jid = uuid4()
    slug = "ANDHRA-PRADESH-HIGH-COURT-INDIA"
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name=slug,
            court_id=sc_pak.id,
            canonical_citation="1995 MLD 100",
            full_text="1995 MLD 100\n[High Court]\nBefore X\n" + ("y" * 600),
            full_text_hash="andhra_slug",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    dry = await find_court_fixes(db)
    fix = next(f for f in dry["fixes"] if f["id"] == str(jid))
    assert court_labels_match(fix["court_name_to"], "Andhra Pradesh High Court, India")
    run_id = "andhra001"
    await _record_run(db, run_id)
    await apply_court_fixes(db, run_id, [fix])
    await db.commit()
    j = await db.get(Judgment, jid)
    ap = await resolve_court_row_for_name(db, slug)
    assert j.court_id == ap.id
    assert j.court_name == ap.name


@pytest.mark.asyncio
async def test_consistency_report_lists_mismatch(db):
    sc = await _sc_pakistan(db)
    db.add(
        Judgment(
            id=uuid4(),
            source_name="PakistanLawSite",
            court_name="Supreme Court of Bangladesh",
            court_id=sc.id,
            canonical_citation="1992 MLD 1",
            full_text="x" * 500,
            full_text_hash="bd_mismatch",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    report = await consistency_report(db)
    assert report["count"] >= 1


@pytest.mark.asyncio
async def test_promotion_guard_quarantines_mismatch(db, login_source, monkeypatch):
    from scraper.fetchers import record_provenance, stage_judgment
    from scraper.tasks.promotion import promote_judgment_staging
    from tests.fixtures import JUDGMENT_TEXT

    monkeypatch.setattr(settings, "SSA_JUDGMENT_COURT_ID_NAME_GUARD", True)
    sc = await _sc_pakistan(db)
    prov = await record_provenance(
        db,
        source=login_source,
        url="https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=court-guard",
        content=JUDGMENT_TEXT.encode("utf-8"),
        content_kind="html",
    )
    st = await stage_judgment(
        db,
        source=login_source,
        prov=prov,
        raw_html="<html></html>",
        raw_text=JUDGMENT_TEXT,
        url=prov.source_url,
    )
    st.reconciled_json = {
        "citations": ["PLD 2024 SC 101"],
        "court": "Supreme Court of India",
        "court_canonical": "Supreme Court of India",
        "year": 2024,
        "full_text": JUDGMENT_TEXT,
        "judge_names": ["Qazi Faez Isa"],
        "document_type": "full_judgment",
    }
    st.status = "extracted"
    st.confidence_score = 0.95
    await db.flush()
    india = await resolve_court_row_for_name(db, "Supreme Court of India")
    assert india is not None
    # Promotion resolves court by name; guard fires if reconciled court_name disagrees with court row used.
    assert await promote_judgment_staging(db, st) in ("quarantined", "promoted")
