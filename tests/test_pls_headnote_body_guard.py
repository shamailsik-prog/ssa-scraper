"""PLS long headnote-only body guard (SSA_PLS_HEADNOTE_BODY_GUARD)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.config import Settings, settings
from scraper.extractors.judgment_guards import (
    detect_headnote_only_no_body,
    guard_reason,
    judgment_is_full_ready,
)
from scraper.models import DocumentQuarantine, Judgment, ScraperStaging
from scraper.tasks.promotion import canonical_text_hash, promote_judgment_staging
from scraper.tasks.judgment_headnote_body_scan import (
    apply_headnote_body_quarantine,
    find_headnote_only_promoted,
    undo_headnote_body_scan,
)
from scraper.tasks.pakistanlawsite import quarantined_capture_retryable

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pls_headnote_only"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _long_headnote_over_5k() -> str:
    base = _read("long_lahore_headnote.txt")
    head, tail = base.split("Date(s) of hearing:", 1)
    return head + ("z" * 5200) + "\nDate(s) of hearing:" + tail


def test_mutant_headnote_body_guard_default_off():
    assert Settings.model_fields["SSA_PLS_HEADNOTE_BODY_GUARD"].default is False


def test_detect_long_headnote_after_hearing_date_line():
    text = _read("long_lahore_headnote.txt")
    signal = detect_headnote_only_no_body(text)
    assert signal is not None
    assert signal.signal == "no_body_after_hearing_or_counsel"


def test_short_scmr_order_with_judgment_marker_not_flagged():
    assert detect_headnote_only_no_body(_read("scmr_312_short_order.txt")) is None


def test_judgment_is_full_ready_rejects_long_headnote_when_guard_on(monkeypatch):
    monkeypatch.setattr(settings, "SSA_PLS_HEADNOTE_BODY_GUARD", True)
    long_hn = _long_headnote_over_5k()
    assert not judgment_is_full_ready(long_hn, ["Abdul Qadeer Chaudhry"])
    assert judgment_is_full_ready(_read("scmr_312_short_order.txt") + ("z" * 5000), ["Naimuddin Ahmed"])


def test_quarantined_capture_retryable_includes_headnote_body_reason():
    reason = guard_reason(
        detect_headnote_only_no_body(_read("long_lahore_headnote.txt"))  # type: ignore[arg-type]
    )
    assert quarantined_capture_retryable(reason, 9000, {}, None)


@pytest.mark.asyncio
async def test_promotion_quarantines_headnote_body_when_flag_on(db, login_source, monkeypatch):
    monkeypatch.setattr(settings, "SSA_PLS_HEADNOTE_BODY_GUARD", True)
    from scraper.fetchers import record_provenance, stage_judgment

    text = _long_headnote_over_5k()
    prov = await record_provenance(
        db,
        source=login_source,
        url="https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=headnote-body",
        content=text.encode("utf-8"),
        content_kind="html",
    )
    st = await stage_judgment(
        db,
        source=login_source,
        prov=prov,
        raw_html="<html></html>",
        raw_text=text,
        url=prov.source_url,
    )
    st.reconciled_json = {
        "citations": ["PLD 1991 Lahore 200"],
        "case_title": "Muhammad Akram versus The State",
        "court": "Lahore",
        "year": 1991,
        "judge_names": ["Abdul Qadeer Chaudhry"],
        "document_type": "full_judgment",
        "full_text": text,
    }
    st.status = "extracted"
    st.confidence_score = 0.95
    await db.flush()
    outcome = await promote_judgment_staging(db, st)
    assert outcome == "quarantined"
    assert "no_body_after_hearing" in (st.quarantine_reason or "")


@pytest.mark.asyncio
async def test_scan_apply_and_undo_document_quarantine(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_PLS_HEADNOTE_BODY_GUARD", True)
    jid = uuid4()
    text = _long_headnote_over_5k()
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name="Lahore High Court",
            canonical_citation="PLD 1991 Lahore 200",
            case_title="Muhammad Akram versus The State",
            full_text=text,
            full_text_hash=canonical_text_hash(text),
            judge_names=["Abdul Qadeer Chaudhry"],
            access_method="rendered_copy",
        )
    )
    await db.commit()
    report = await find_headnote_only_promoted(db, limit=50)
    assert report["count"] >= 1
    run_id = "testrun1"
    applied = await apply_headnote_body_quarantine(db, run_id, report["fixes"])
    assert applied >= 1
    await db.commit()
    row = (
        await db.execute(
            select(DocumentQuarantine).where(DocumentQuarantine.doc_id == jid, DocumentQuarantine.run_id == run_id)
        )
    ).scalars().first()
    assert row is not None
    removed = await undo_headnote_body_scan(db, run_id)
    await db.commit()
    assert removed >= 1
    assert (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == jid))
    ).scalars().first() is None


def test_capture_modal_script_waits_for_body_marker_not_reporter_prefix():
    import inspect

    from scraper.auth import session_manager

    source = inspect.getsource(session_manager.PlaywrightBrowser._capture_case_description_modal)
    assert "bodyMarker" in source
    assert "CLC|SCMR|PLD" not in source
    assert "stableReads" in source


@pytest.mark.asyncio
async def test_scan_apply_revalidates_corrected_judgment_and_source(db):
    """A stale scan must not hide a real judgment completed before --apply."""
    jid = uuid4()
    text = _long_headnote_over_5k()
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name="Lahore High Court",
            canonical_citation="PLD 1992 Lahore 201",
            case_title="Stale scan regression",
            full_text=text,
            full_text_hash=canonical_text_hash(text),
            judge_names=["Abdul Qadeer Chaudhry"],
            access_method="rendered_copy",
        )
    )
    await db.commit()
    report = await find_headnote_only_promoted(db)
    stale = next(row for row in report["fixes"] if row["id"] == str(jid))
    completed = text + "\nJUDGMENT\n" + ("This is the complete court judgment text. " * 20)
    judgment = await db.get(Judgment, jid)
    judgment.full_text = completed
    judgment.full_text_hash = canonical_text_hash(completed)
    await db.commit()
    assert await apply_headnote_body_quarantine(db, "stale_scan_regression", [stale]) == 0
    assert (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == jid))
    ).scalars().first() is None
    judgment.source_name = "AnotherSource"
    judgment.full_text = text
    judgment.full_text_hash = canonical_text_hash(text)
    await db.commit()
    assert await apply_headnote_body_quarantine(db, "wrong_source_regression", [stale]) == 0


@pytest.mark.asyncio
async def test_scan_undo_uses_durable_run_id_not_redis(db):
    """Undo remains possible after Redis restart/TTL expiration."""
    jid = uuid4()
    db.add(
        DocumentQuarantine(
            doc_type="judgment",
            doc_id=jid,
            run_id="durable_headnote_undo",
            reason="headnote_only: no_body_after_hearing_or_counsel",
        )
    )
    await db.commit()
    assert await undo_headnote_body_scan(db, "durable_headnote_undo") == 1
    await db.commit()
    assert (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == jid))
    ).scalars().first() is None
