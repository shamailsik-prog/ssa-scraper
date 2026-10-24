"""Promotion integrity flags (audit defects #1, #2, #5): default off."""

from __future__ import annotations

import pytest

from scraper.config import settings
from scraper.document_validation import validate_judgment_reconciled_json
from scraper.extractors.validation import reconcile_judgment
from scraper.judgment_min_body import LEGACY_DOCUMENT_VALIDATION_MIN_CHARS
from scraper.pls_quality_guards import SHORT_TEXT_MIN_CHARS
from scraper.reconcile_quality import reconcile_error_requires_quarantine


def test_reconcile_error_deny_list():
    assert reconcile_error_requires_quarantine("preserved text contains mojibake (????) in the English body")
    assert reconcile_error_requires_quarantine("court mismatch: stored='Supreme Court' header='Lahore High Court'")
    assert not reconcile_error_requires_quarantine("bench_size 3 != judge count 2")


def test_reconcile_error_quarantine_flag(monkeypatch):
    monkeypatch.setattr(settings, "SSA_RECONCILE_ERROR_QUARANTINE", True)
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", True)
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    monkeypatch.setattr(settings, "SSA_COURT_HEADER_GUARD", False)
    monkeypatch.setattr(settings, "SSA_PLD_CITATION_RELABEL", False)
    text = "IN THE LAHORE HIGH COURT\nBefore X, J\n" + ("x" * 280) + " ???? " + ("y" * 280)
    det = {
        "citations": ["2020 CLC 1"],
        "court": "Lahore",
        "year": 2020,
        "full_text_candidate": text,
        "judge_names": ["X"],
        "extractor_confidence": 0.95,
    }
    out = reconcile_judgment(
        deterministic=det,
        ai=None,
        raw_text=text,
        court_directory={"lahore high court": "Lahore High Court"},
        min_confidence=0.5,
        source_name="PakistanLawSite",
    )
    assert out.quarantine
    assert any("mojibake" in e for e in out.errors)
    assert "reconcile error" in (out.quarantine_reason or "")


def test_unified_min_body_flag(monkeypatch):
    monkeypatch.setattr(settings, "SSA_UNIFIED_JUDGMENT_MIN_BODY", False)
    ok, _ = validate_judgment_reconciled_json({}, raw_text="x" * LEGACY_DOCUMENT_VALIDATION_MIN_CHARS)
    assert ok
    monkeypatch.setattr(settings, "SSA_UNIFIED_JUDGMENT_MIN_BODY", True)
    ok, reason = validate_judgment_reconciled_json({}, raw_text="x" * LEGACY_DOCUMENT_VALIDATION_MIN_CHARS)
    assert not ok
    assert reason == "empty_judgment_text"
    ok, _ = validate_judgment_reconciled_json({}, raw_text="x" * SHORT_TEXT_MIN_CHARS)
    assert ok


@pytest.mark.asyncio
async def test_promotion_full_headnote_detector(db, login_source, monkeypatch):
    from scraper.extractors.judgment_guards import detect_headnotes_only
    from scraper.fetchers import record_provenance, stage_judgment
    from scraper.tasks.promotion import promote_judgment_staging

    monkeypatch.setattr(settings, "SSA_PROMOTION_FULL_HEADNOTE_DETECTOR", True)
    monkeypatch.setattr(settings, "SSA_RECONCILE_ERROR_QUARANTINE", False)
    short_notes = "IN THE HIGH COURT\n" + ("x" * 80)
    html_notes = "<p>Notes on cases cited</p>"
    sig = detect_headnotes_only(raw_text=short_notes, raw_html=html_notes)
    assert sig is not None
    prov = await record_provenance(
        db,
        source=login_source,
        url="https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=short-body",
        content=short_notes.encode("utf-8"),
        content_kind="html",
    )
    st = await stage_judgment(
        db,
        source=login_source,
        prov=prov,
        raw_html=f"<html><body>{html_notes}</body></html>",
        raw_text=short_notes,
        url=prov.source_url,
    )
    st.reconciled_json = {
        "citations": ["2020 CLC 1"],
        "court": "Lahore",
        "year": 2020,
        "full_text": short_notes,
        "judge_names": ["J"],
        "document_type": "full_judgment",
    }
    st.status = "extracted"
    st.confidence_score = 0.95
    await db.flush()
    assert await promote_judgment_staging(db, st) == "quarantined"


@pytest.mark.asyncio
async def test_promotion_blocks_stored_reconcile_errors(db, login_source, monkeypatch):
    from tests.fixtures import JUDGMENT_TEXT
    from scraper.fetchers import record_provenance, stage_judgment
    from scraper.tasks.promotion import promote_judgment_staging

    monkeypatch.setattr(settings, "SSA_RECONCILE_ERROR_QUARANTINE", True)
    prov = await record_provenance(
        db,
        source=login_source,
        url="https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=reconcile-err",
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
        "court": "Supreme Court of Pakistan",
        "year": 2024,
        "full_text": JUDGMENT_TEXT,
        "judge_names": ["Qazi Faez Isa"],
        "document_type": "full_judgment",
    }
    st.validation_errors = ["court mismatch: stored='Supreme Court of Pakistan' header='Lahore High Court'"]
    st.status = "extracted"
    st.confidence_score = 0.95
    await db.flush()
    assert await promote_judgment_staging(db, st) == "quarantined"
