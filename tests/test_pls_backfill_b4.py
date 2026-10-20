"""B4 backfill: order-sheet titles, PLD collisions, headnote strip/flag, requalify source_name."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.extractors.validation import canonical_text_hash, reconcile_judgment
from scraper.fetchers import record_provenance, stage_judgment
from scraper.tasks.promotion import promote_judgment_staging
from scraper.models import Citation, Judgment, ScraperSource, ScraperStaging
from scraper.tasks import pls_backfill
from scraper.tasks.pls_backfill import (
    backfill_flag_urdu_headnote_mojibake,
    backfill_order_sheet_titles,
    backfill_pld_citations,
    requalify_quarantined,
    strip_urdu_headnote_mojibake,
)
from tests.test_order_sheet_title import EXPECTED_HEADER_TITLE, HEADER
from tests.test_pls_quality_guards import HEADNOTE_ONLY

TEXT = HEADER + "\nJUDGMENT\nBody text here.\n"


def _short_pls_body(n: int) -> str:
    base = "IN THE LAHORE HIGH COURT\nBefore J, J\nPetition body.\n"
    pad = "x" * max(0, n - len(base))
    return (base + pad)[:n]


@pytest.mark.asyncio
async def test_order_sheet_title_backfill_dry_run_does_not_write(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
    j = Judgment(
        canonical_citation="PLS title dry-run",
        case_title="Wrong title versus other",
        full_text=TEXT,
        full_text_hash="b" * 64,
        source_name="PakistanLawSite",
    )
    db.add(j)
    await db.commit()
    counts = await backfill_order_sheet_titles(db, limit=5, dry_run=True)
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["updated"] == 1
    assert refreshed.case_title == "Wrong title versus other"


@pytest.mark.asyncio
async def test_order_sheet_title_backfill_rejects_junk_citation_prefix(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
    junk = "1999 CLC 1 cited case versus other party"
    j = Judgment(
        canonical_citation="PLS junk title",
        case_title=junk,
        full_text=TEXT,
        full_text_hash="b2" * 32,
        source_name="PakistanLawSite",
    )
    db.add(j)
    await db.commit()
    counts = await backfill_order_sheet_titles(db, limit=5, dry_run=False)
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["updated"] == 1
    assert refreshed.case_title == EXPECTED_HEADER_TITLE


@pytest.mark.asyncio
async def test_order_sheet_title_backfill_apply_writes_valid_title(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
    j = Judgment(
        canonical_citation="PLS title apply",
        case_title="Wrong title versus other",
        full_text=TEXT,
        full_text_hash="c" * 64,
        source_name="PakistanLawSite",
    )
    db.add(j)
    await db.commit()
    counts = await backfill_order_sheet_titles(db, limit=5, dry_run=False)
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["updated"] == 1
    assert refreshed.case_title == EXPECTED_HEADER_TITLE


@pytest.mark.asyncio
async def test_pld_backfill_dry_run_does_not_write(db):
    j = Judgment(
        canonical_citation="2020 PLD 888",
        full_text_hash="d1" * 32,
        full_text="IN THE LAHORE HIGH COURT, LAHORE\nBefore J\n" + "x" * 600,
        source_name="PakistanLawSite",
    )
    db.add(j)
    await db.flush()
    db.add(Citation(judgment_id=j.id, citation_string="2020 PLD 888", raw_string="2020 PLD 888", is_primary=True))
    await db.commit()
    counts = await backfill_pld_citations(db, limit=5, dry_run=True)
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["updated"] == 1
    assert refreshed.canonical_citation == "2020 PLD 888"


@pytest.mark.asyncio
async def test_pld_backfill_skips_canonical_collision(db):
    holder = Judgment(
        canonical_citation="2020 PLD 999",
        full_text_hash="d" * 64,
        full_text="IN THE LAHORE HIGH COURT, LAHORE\nBefore J\n" + "x" * 600,
        source_name="PakistanLawSite",
    )
    target = Judgment(
        canonical_citation="PLD 2020 Lahore 999",
        full_text_hash="e" * 64,
        full_text="IN THE LAHORE HIGH COURT, LAHORE\nBefore J\n" + "y" * 600,
        source_name="PakistanLawSite",
    )
    db.add(holder)
    db.add(target)
    await db.flush()
    db.add(Citation(judgment_id=holder.id, citation_string="2020 PLD 999", raw_string="2020 PLD 999", is_primary=True))
    await db.commit()
    counts = await backfill_pld_citations(db, limit=10, dry_run=False)
    assert counts["collisions"] == 1
    assert counts["updated"] == 0


@pytest.mark.asyncio
async def test_pld_backfill_skips_citation_string_collision_only(db):
    holder = Judgment(
        canonical_citation="2021 PLD 100",
        full_text_hash="d2" * 32,
        full_text="IN THE LAHORE HIGH COURT, LAHORE\nBefore J\n" + "x" * 600,
        source_name="PakistanLawSite",
    )
    other = Judgment(
        canonical_citation="other holder",
        full_text_hash="e2" * 32,
        full_text="x" * 700,
        source_name="PakistanLawSite",
    )
    db.add(holder)
    db.add(other)
    await db.flush()
    db.add(Citation(judgment_id=holder.id, citation_string="2021 PLD 100", raw_string="2021 PLD 100", is_primary=True))
    db.add(Citation(judgment_id=other.id, citation_string="PLD 2021 Lahore 100", raw_string="PLD 2021 Lahore 100", is_primary=True))
    await db.commit()
    counts = await backfill_pld_citations(db, limit=10, dry_run=False)
    assert counts["collisions"] >= 1
    assert counts["updated"] == 0
    holder_ref = (await db.execute(select(Judgment).where(Judgment.id == holder.id))).scalars().one()
    assert holder_ref.canonical_citation == "2021 PLD 100"


@pytest.mark.asyncio
async def test_strip_headnote_dry_run_preserves_full_text_and_hash(db):
    raw = HEADNOTE_ONLY
    h = canonical_text_hash(raw)
    j = Judgment(
        canonical_citation="PLS strip dry-run",
        full_text=raw,
        full_text_hash=h,
        source_name="PakistanLawSite",
        quality_flag_urdu_headnote_mojibake=True,
    )
    db.add(j)
    await db.commit()
    counts = await strip_urdu_headnote_mojibake(db, limit=5, dry_run=True)
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["stripped"] == 1
    assert refreshed.full_text == raw
    assert refreshed.full_text_hash == h
    assert refreshed.headnote_stripped_full_text is None


@pytest.mark.asyncio
async def test_strip_headnote_apply_writes_overlay_not_full_text(db):
    raw = HEADNOTE_ONLY
    h = canonical_text_hash(raw)
    j = Judgment(
        canonical_citation="PLS strip apply",
        full_text=raw,
        full_text_hash=h,
        source_name="PakistanLawSite",
        quality_flag_urdu_headnote_mojibake=True,
    )
    db.add(j)
    await db.commit()
    counts = await strip_urdu_headnote_mojibake(db, limit=5, dry_run=False)
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["stripped"] == 1
    assert refreshed.full_text == raw
    assert refreshed.full_text_hash == h
    assert "????" not in (refreshed.headnote_stripped_full_text or "")
    assert refreshed.quality_flag_urdu_headnote_mojibake is False


@pytest.mark.asyncio
async def test_backfill_flag_urdu_headnote_mojibake(db):
    j = Judgment(
        canonical_citation="PLS flag headnote",
        full_text=HEADNOTE_ONLY,
        full_text_hash=canonical_text_hash(HEADNOTE_ONLY),
        source_name="PakistanLawSite",
        quality_flag_urdu_headnote_mojibake=False,
    )
    db.add(j)
    await db.commit()
    counts = await backfill_flag_urdu_headnote_mojibake(db, limit=5, dry_run=False)
    refreshed = (await db.execute(select(Judgment).where(Judgment.id == j.id))).scalars().one()
    assert counts["flagged"] == 1
    assert refreshed.quality_flag_urdu_headnote_mojibake is True


@pytest.mark.asyncio
async def test_requalify_short_text_honours_staging_source_name(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", True)
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    body = _short_pls_body(499)
    url = "https://www.pakistanlawsite.com/Login/Check?caseName=short1"
    prov = await record_provenance(db, source=source, url=url, content=body.encode(), content_kind="html", route={})
    st = ScraperStaging(
        provenance_id=prov.id,
        source_id=source.id,
        source_name="PakistanLawSite",
        access_method=source.access_method,
        source_url=url,
        content_hash=prov.content_hash,
        raw_text=body,
        extracted_citation="unreported docket ref",
        route_json={},
        status="quarantined",
        quarantine_reason="short",
    )
    db.add(st)
    await db.commit()
    counts = await requalify_quarantined(db, limit=1, dry_run=True)
    assert counts["still_quarantined"] >= 1
    assert any("shorter than" in k for k in counts.get("still_by_reason", {}))

    real_reconcile = reconcile_judgment

    def reconcile_without_pls_source(**kwargs):
        return real_reconcile(**{**kwargs, "source_name": None})

    monkeypatch.setattr(pls_backfill, "reconcile_judgment", reconcile_without_pls_source)
    counts2 = await requalify_quarantined(db, limit=1, dry_run=True)
    assert not any("shorter than" in k for k in counts2.get("still_by_reason", {}))


def test_requalify_task_calls_apply_not_dry_run(monkeypatch):
    seen: dict = {}

    def fake_run_requalify(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(pls_backfill, "run_requalify", fake_run_requalify)
    monkeypatch.setattr(pls_backfill, "run_async", lambda fn: fn)
    pls_backfill.requalify_quarantined_task(limit=3, skip_final=False)
    assert seen.get("dry_run") is False
    assert seen.get("limit") == 3


def test_backfill_functions_default_dry_run_true():
    import inspect

    for name in (
        "backfill_case_metadata",
        "requalify_quarantined",
        "backfill_order_sheet_titles",
        "backfill_pld_citations",
        "backfill_flag_urdu_headnote_mojibake",
        "strip_urdu_headnote_mojibake",
    ):
        sig = inspect.signature(getattr(pls_backfill, name))
        assert sig.parameters["dry_run"].default is True


@pytest.mark.asyncio
async def test_promotion_persists_urdu_headnote_quality_flag(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", True)
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    text = HEADNOTE_ONLY + "\n" + ("x" * 600)
    url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=headnote-flag"
    prov = await record_provenance(db, source=login_source, url=url, content=text.encode(), content_kind="html")
    st = await stage_judgment(db, source=login_source, prov=prov, raw_html=None, raw_text=text, url=url)
    st.reconciled_json = {
        "citations": ["PLD 2026 SC 9001"],
        "court": "Supreme Court of Pakistan",
        "year": 2026,
        "case_title": "Headnote flag promotion test",
        "judge_names": ["J"],
        "quality_flag_urdu_headnote_mojibake": True,
        "full_text_candidate": text,
    }
    st.status = "extracted"
    st.confidence_score = 0.95
    assert await promote_judgment_staging(db, st) == "promoted"
    judgment = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().one()
    assert judgment.quality_flag_urdu_headnote_mojibake is True


def test_pipeline_rejects_junk_citation_case_title(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    det = {
        "case_title": "1999 CLC 1 cited versus other",
        "citations": ["1990 CLC 1205"],
        "court": "Peshawar",
        "year": 1990,
        "full_text_candidate": HEADER,
        "judge_names": ["J"],
    }
    out = reconcile_judgment(
        deterministic=det,
        ai=None,
        raw_text=HEADER,
        court_directory={},
        min_confidence=0.5,
        source_name="PakistanLawSite",
    )
    assert out.data.get("case_title") == EXPECTED_HEADER_TITLE
