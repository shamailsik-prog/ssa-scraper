"""PLS case-detail failure pages must be classified before they are staged as judgments."""

from __future__ import annotations

from pathlib import Path

import pytest

STUB = "Error Code:700 Sorry Something went wrong"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pls_headnote_only" / "scmr_312_short_order.txt"


def test_error_code_700_stub_is_exactly_the_staging_sample():
    assert len(STUB) == 41
    assert STUB == "Error Code:700 Sorry Something went wrong"


def test_error_code_700_stub_is_a_fetch_error():
    from scraper.extractors.pls_failure_page import detect_pls_case_detail_failure

    reason = detect_pls_case_detail_failure(STUB)
    assert reason is not None
    assert reason.startswith("fetch_error:")
    assert "700" in reason


@pytest.mark.parametrize(
    "body",
    [
        "Error Code: 700 Sorry, something went wrong",
        "Error Code :700 Sorry Something went wrong",
        "error code:700 sorry something went wrong",
        "Sorry, something went wrong",
        "Sorry Something went wrong",
        "<html><body><h1>Error Code:700 Sorry Something went wrong</h1></body></html>",
        "  Error Code:700\nSorry Something went wrong  ",
        "Error Code:500 Service Unavailable",
        "Service Unavailable",
        "Session expired",
    ],
)
def test_close_pls_failure_variants_are_fetch_errors(body):
    from scraper.extractors.pls_failure_page import detect_pls_case_detail_failure

    reason = detect_pls_case_detail_failure(body)
    assert reason is not None
    assert reason.startswith("fetch_error:")


def test_real_short_judgment_is_not_a_pls_failure_page():
    from scraper.extractors.pls_failure_page import detect_pls_case_detail_failure

    text = FIXTURE.read_text(encoding="utf-8")
    assert "JUDGMENT" in text
    assert len(text) < 800
    assert detect_pls_case_detail_failure(text) is None


def test_short_order_that_mentions_access_denied_is_not_a_failure_page():
    from scraper.extractors.pls_failure_page import detect_pls_case_detail_failure

    text = (
        "1991 SCMR 312\n"
        "IN THE SUPREME COURT OF PAKISTAN\n"
        "JUDGMENT\n"
        "The plea of access denied is rejected. Petition dismissed."
    )
    assert detect_pls_case_detail_failure(text) is None
    assert detect_pls_case_detail_failure("Access denied. Petition dismissed.") is None


def test_short_judgment_that_quotes_the_700_stub_is_not_a_failure_page():
    """A real order that mentions the site error is still a judgment body."""
    from scraper.extractors.pls_failure_page import detect_pls_case_detail_failure

    text = FIXTURE.read_text(encoding="utf-8")
    quoted = text + "\nThe portal had replied: " + STUB + "\nThe petition is dismissed."
    assert detect_pls_case_detail_failure(quoted) is None


def test_fetch_error_reason_is_retryable_until_the_refetch_cap():
    from scraper.tasks.pakistanlawsite import quarantined_capture_retryable

    reason = "fetch_error: pls_error_code_700"
    assert quarantined_capture_retryable(reason, len(reason), {}, reason) is True
    assert quarantined_capture_retryable(reason, len(reason), {"refetch_count": 3}, reason) is False


async def test_case_detail_700_stub_is_not_staged_as_a_judgment(db, login_source):
    from sqlalchemy import func, select

    from scraper.auth.session_manager import PageResult
    from scraper.models import QuarantineQueue, ScraperStaging
    from scraper.parsers.citation_extractor import normalise_citation
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from tests.fixtures import BrowserScript
    from tests.test_auth_playwright import _activate, _nosleep

    await _activate(db, login_source)
    citation = "2026 PTD 1629"
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2026PTD1629"
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    page = PageResult(
        url=detail_url,
        html=f"<html><body>{STUB}</body></html>",
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": True,
            "case_description_modal_found": True,
            "case_description_modal_text": STUB,
            "case_description_modal_text_length": len(STUB),
        },
    )
    row = {"citation": citation, "title": "2026 PTD 1629", "court": "Federal Tax Ombudsman", "detail_url": detail_url}

    assert await pipeline.preserve_and_extract(page, {"harvest": "search_gap", "row_index": 0}, row) == "fetch_error"
    staging_rows = (await db.execute(select(ScraperStaging))).scalars().all()
    assert len(staging_rows) == 1
    staged = staging_rows[0]
    assert staged.status == "failed"
    assert (staged.quarantine_reason or "").startswith("fetch_error:")
    assert staged.raw_text != STUB
    assert STUB not in (staged.raw_text or "")
    assert len(staged.raw_text or "") != 41
    assert staged.extracted_citation == normalise_citation(citation)
    assert staged.reconciled_json is None
    open_reviews = (await db.execute(select(func.count()).select_from(QuarantineQueue))).scalar()
    assert open_reviews == 0

    again = await pipeline.preserve_and_extract(page, {"harvest": "search_gap", "row_index": 0}, row)
    assert again == "fetch_error"
    assert len((await db.execute(select(ScraperStaging))).scalars().all()) == 1


async def test_real_short_judgment_still_passes_the_harvest_path(db, login_source):
    """The failure detector must not swallow a short order. The headnote/confidence gate still runs."""
    from sqlalchemy import select

    from scraper.auth.session_manager import PageResult
    from scraper.models import ScraperStaging
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from tests.fixtures import BrowserScript
    from tests.test_auth_playwright import _activate, _nosleep

    await _activate(db, login_source)
    text = FIXTURE.read_text(encoding="utf-8")
    citation = "1991 SCMR 312"
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=1991SCMR312"
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    page = PageResult(
        url=detail_url,
        html=f"<html><body><pre>{text}</pre></body></html>",
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": True,
            "case_description_modal_found": True,
            "case_description_modal_text": text,
            "case_description_modal_text_length": len(text),
        },
    )
    row = {"citation": citation, "title": "State versus Abdul Rehman", "court": "Supreme Court", "detail_url": detail_url}

    result = await pipeline.preserve_and_extract(page, {"harvest": "search_gap", "row_index": 0}, row)
    assert result != "fetch_error"
    staged = (await db.execute(select(ScraperStaging))).scalars().one()
    assert "JUDGMENT" in (staged.raw_text or "")
    assert "1991 SCMR 312" in (staged.raw_text or "")
    assert not (staged.quarantine_reason or "").startswith("fetch_error:")
    assert staged.status in ("extracted", "quarantined")


async def test_search_gap_empty_modal_is_empty_not_staged(db, login_source):
    from sqlalchemy import select

    from scraper.auth.session_manager import PageResult
    from scraper.models import ScraperStaging
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from tests.fixtures import BrowserScript
    from tests.test_auth_playwright import _activate, _nosleep

    await _activate(db, login_source)
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2026CLD2"
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    page = PageResult(
        url=detail_url,
        html="<html><body><p>Citation Name: 2026 CLD 2</p></body></html>",
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": True,
            "case_description_modal_text": "",
        },
    )
    row = {"citation": "2026 CLD 2", "detail_url": detail_url}
    assert await pipeline.preserve_and_extract(page, {"harvest": "search_gap"}, row) == "empty"
    assert (await db.execute(select(ScraperStaging))).scalars().all() == []


async def test_search_gap_headnote_is_non_judgment_not_staged(db, login_source):
    from sqlalchemy import select

    from scraper.auth.session_manager import PageResult
    from scraper.models import ScraperStaging
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from tests.fixtures import BrowserScript
    from tests.test_auth_playwright import _activate, _nosleep, _notes_only_detail_html

    await _activate(db, login_source)
    citation = "2026 CLD 9"
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2026CLD9"
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    page = PageResult(
        url=detail_url,
        html=_notes_only_detail_html(citation, "Notes Only"),
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": False,
            "case_description_modal_text": None,
        },
    )
    row = {"citation": citation, "title": "Notes Only", "detail_url": detail_url}
    assert await pipeline.preserve_and_extract(page, {"harvest": "search_gap"}, row) == "non_judgment"
    assert (await db.execute(select(ScraperStaging))).scalars().all() == []


async def test_search_gap_annexure_short_text_is_not_staged(db, login_source):
    """A 1-2 digit annexure whose body is only "Versus" is not a judgment to stage."""
    from sqlalchemy import select

    from scraper.auth.session_manager import PageResult
    from scraper.models import ScraperStaging
    from scraper.pls_search_harvest_core import ANNEXURE_SHORT_TEXT_REASON
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from tests.fixtures import BrowserScript
    from tests.test_auth_playwright import _activate, _nosleep

    await _activate(db, login_source)
    detail_url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=1990HC-A1&court=&Row=0&bookName=undefined"
    )
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    page = PageResult(
        url=detail_url,
        html="<html><body><p>Versus</p></body></html>",
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": True,
            "case_description_modal_found": True,
            "case_description_modal_text": "Versus",
            "case_description_modal_text_length": len("Versus"),
        },
    )
    row = {"citation": None, "title": "Versus", "pdf_url": None, "detail_url": detail_url}
    route = {"harvest": "search_gap", "row_index": 0}
    assert await pipeline.preserve_and_extract(page, route, row) == ANNEXURE_SHORT_TEXT_REASON
    assert route["pls_detail_failure"] == ANNEXURE_SHORT_TEXT_REASON
    assert (await db.execute(select(ScraperStaging))).scalars().all() == []


async def test_search_gap_ajk_short_body_with_citation_is_not_an_annexure_signal(db, login_source):
    from scraper.auth.session_manager import PageResult
    from scraper.pls_search_harvest_core import ANNEXURE_SHORT_TEXT_REASON
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from tests.fixtures import BrowserScript
    from tests.test_auth_playwright import _activate, _nosleep

    await _activate(db, login_source)
    detail_url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=1990HC-A2001&court=&Row=0&bookName=undefined"
    )
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    page = PageResult(
        url=detail_url,
        html="<html><body><p>Versus</p></body></html>",
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": True,
            "case_description_modal_found": True,
            "case_description_modal_text": "Versus",
            "case_description_modal_text_length": len("Versus"),
        },
    )
    row = {
        "citation": "1990 PLC (CS) 733",
        "title": "Versus",
        "pdf_url": None,
        "detail_url": detail_url,
    }
    route = {"harvest": "search_gap"}
    result = await pipeline.preserve_and_extract(page, route, row)
    assert result != ANNEXURE_SHORT_TEXT_REASON
    assert result != "non_judgment_annexure"
    assert "pls_detail_failure" not in route


async def test_search_gap_700_records_the_failure_reason_on_the_route(db, login_source):
    from scraper.auth.session_manager import PageResult
    from scraper.extractors.pls_failure_page import FETCH_ERROR_700
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from tests.fixtures import BrowserScript
    from tests.test_auth_playwright import _activate, _nosleep

    await _activate(db, login_source)
    detail_url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=1990HC-A1&court=&Row=0&bookName=undefined"
    )
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    page = PageResult(
        url=detail_url,
        html=f"<html><body>{STUB}</body></html>",
        metadata={
            "requested_url": detail_url,
            "final_url": detail_url,
            "case_description_selector_found": True,
            "case_description_modal_found": True,
            "case_description_modal_text": STUB,
            "case_description_modal_text_length": len(STUB),
        },
    )
    row = {"citation": None, "title": "Versus", "pdf_url": None, "detail_url": detail_url}
    route = {"harvest": "search_gap", "row_index": 0}
    assert await pipeline.preserve_and_extract(page, route, row) == "fetch_error"
    assert route["pls_detail_failure"] == FETCH_ERROR_700


def test_citation_grid_refetches_fetch_error_then_skips_when_capped():
    from scraper.tasks.pakistanlawsite import staged_citation_disposition

    reason = "fetch_error: pls_error_code_700"
    assert staged_citation_disposition("failed", reason, len(reason), {}, reason) == "refetch"
    assert staged_citation_disposition("failed", reason, len(reason), {"refetch_count": 3}, reason) == "skip"
    assert staged_citation_disposition("failed", "rejected by reviewer", 5000, {}, "") == "ignore"
    assert staged_citation_disposition("quarantined", "headnote_only: body_short", 8000, {}, "") == "refetch"
    assert staged_citation_disposition("extracted", None, 20000, {}, "") == "skip"
