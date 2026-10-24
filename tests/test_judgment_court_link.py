"""Link NULL court_id PLS judgments to court rows (#292 follow-up)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from scraper.config import settings
from scraper.judgment_court_consistency import consistency_report
from scraper.judgment_court_link_registry import (
    ajk_high_court_from_header,
    pls_court_label_key,
    resolve_null_court_id_link,
)
from scraper.models import Court, Judgment
from scraper.tasks.judgment_court_link import apply_court_id_links, find_court_id_links, undo_run


def test_ajk_single_judge_header_is_high_court():
    text = (
        "1979 CLC 288\n"
        "[Azad J & K]\n"
        "Before Muhammad Yousaf, J\n"
        "A versus B\n"
    )
    assert ajk_high_court_from_header(text)
    res = resolve_null_court_id_link("SUPREME-COURT-AZAD-KASHMIR", text)
    assert res is not None
    assert res.target_court_name == "High Court of Azad Jammu and Kashmir"


def test_manual_review_supreme_court_slug():
    res = resolve_null_court_id_link("SUPREME-COURT", "PLD 2020 SC 1\n")
    assert res is not None and res.manual_review


def test_pls_label_key_normalizes_hyphens():
    assert pls_court_label_key("SUPREME-COURT-AZAD-KASHMIR") == "supreme court azad kashmir"


@pytest.mark.asyncio
async def test_link_dry_run_does_not_create_courts(db):
    before = (await db.execute(select(func.count()).select_from(Court))).scalar()
    db.add(
        Judgment(
            id=uuid4(),
            source_name="PakistanLawSite",
            court_name="SUPREME-COURT-AZAD-KASHMIR",
            court_id=None,
            canonical_citation="1990 CLC 1",
            full_text="1990 CLC 1\n[Supreme Court (AJ&K)]\nBefore A and B, JJ\n" + ("x" * 400),
            full_text_hash="ajk_sc_link_dry",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    out = await find_court_id_links(db, create=False)
    after = (await db.execute(select(func.count()).select_from(Court))).scalar()
    assert after == before
    assert out["count"] >= 1


@pytest.mark.asyncio
async def test_link_apply_and_undo_restore_court_id(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_JUDGMENT_COURT_ID_BACKFILL", True)
    ajk_sc = (
        await db.execute(select(Court).where(Court.short_code == "AJK-SC"))
    ).scalars().first()
    if ajk_sc is None:
        ajk_sc = Court(
            name="Azad Jammu and Kashmir Supreme Court",
            short_code="AJK-SC",
            court_level="supreme_court",
        )
        db.add(ajk_sc)
        await db.flush()
    jid = uuid4()
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name="SUPREME-COURT-AZAD-KASHMIR",
            court_id=None,
            canonical_citation="1991 CLC 2",
            full_text="1991 CLC 2\n[Supreme Court (AJ&K)]\nBefore A and B, JJ\n" + ("y" * 400),
            full_text_hash="ajk_sc_link_apply",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    info = await find_court_id_links(db, create=True)
    link = next(x for x in info["links"] if x["id"] == str(jid))
    run_id = "linktest01"
    await apply_court_id_links(db, run_id, [link])
    await db.commit()
    j = await db.get(Judgment, jid)
    assert j.court_id == ajk_sc.id
    await undo_run(db, run_id)
    await db.refresh(j)
    assert j.court_id is None


@pytest.mark.asyncio
async def test_consistency_report_splits_null_and_mismatch(db):
    sc = (
        await db.execute(select(Court).where(Court.name == "Supreme Court of Pakistan"))
    ).scalars().first()
    if sc is None:
        sc = Court(name="Supreme Court of Pakistan", short_code="SC_PAK", court_level="supreme")
        db.add(sc)
        await db.flush()
    db.add(
        Judgment(
            id=uuid4(),
            source_name="PakistanLawSite",
            court_name="Federal Shariat Court",
            court_id=None,
            canonical_citation="2000 CLC 3",
            full_text="x" * 500,
            full_text_hash="null_court_id_row",
            access_method="rendered_copy",
        )
    )
    db.add(
        Judgment(
            id=uuid4(),
            source_name="PakistanLawSite",
            court_name="Supreme Court of Bangladesh",
            court_id=sc.id,
            canonical_citation="2000 CLC 4",
            full_text="x" * 500,
            full_text_hash="mismatch_row",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    report = await consistency_report(db)
    assert report["null_court_id_count"] >= 1
    assert report["real_mismatch_count"] >= 1


@pytest.mark.asyncio
async def test_consistency_limit_does_not_hide_real_mismatch(db):
    """Normalized alias candidates must not consume the SQL/display limit."""
    from scraper.judgment_court_consistency import find_judgment_court_mismatches

    ap = (await db.execute(select(Court).where(Court.name == "Andhra Pradesh High Court, India"))).scalars().first()
    if ap is None:
        ap = Court(name="Andhra Pradesh High Court, India", short_code="AP_HC_LIMIT_TEST", court_level="high")
        db.add(ap)
    pk = (await db.execute(select(Court).where(Court.name == "Supreme Court of Pakistan"))).scalars().first()
    if pk is None:
        pk = Court(name="Supreme Court of Pakistan", short_code="SC_PAK_LIMIT_TEST", court_level="supreme")
        db.add(pk)
    await db.flush()
    alias_id = uuid4()
    mismatch_id = uuid4()
    db.add_all([
        Judgment(
            id=alias_id,
            source_name="PakistanLawSite",
            court_name="ANDHRA-PRADESH-HIGH-COURT-INDIA",
            court_id=ap.id,
            canonical_citation="0000 CLC 1",
            full_text="x" * 500,
            full_text_hash="consistency_alias_limit",
            access_method="rendered_copy",
        ),
        Judgment(
            id=mismatch_id,
            source_name="PakistanLawSite",
            court_name="Supreme Court of Bangladesh",
            court_id=pk.id,
            canonical_citation="0000 CLC 2",
            full_text="x" * 500,
            full_text_hash="consistency_true_mismatch_limit",
            access_method="rendered_copy",
        ),
    ])
    await db.commit()
    matches = await find_judgment_court_mismatches(db, limit=1)
    assert len(matches) == 1
    assert matches[0]["id"] == str(mismatch_id)
    report = await consistency_report(db, limit=1)
    assert report["real_mismatch_count"] == 1
    assert len(report["mismatches"]) == 1
    assert report["mismatches"][0]["id"] == str(mismatch_id)
    zero_display = await consistency_report(db, limit=0)
    assert zero_display["real_mismatch_count"] == 1
    assert zero_display["mismatches"] == []
