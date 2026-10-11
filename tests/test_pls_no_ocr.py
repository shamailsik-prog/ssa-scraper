"""PakistanLawSite publishes typed text: its scraper reads a PDF's own text and never stops to OCR."""

from __future__ import annotations

from sqlalchemy import select

from scraper.auth.session_manager import PageResult
from scraper.fetchers import pdf_text_layer
from scraper.models import ScraperStaging
from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
from tests.fixtures import BrowserScript, scanned_pdf_bytes, text_pdf_bytes
from tests.test_auth_playwright import _activate, _nosleep, _notes_only_detail_html


def _forbid_ocr(monkeypatch):
    from scraper.parsers import pdf_extractor

    def boom(*_a, **_k):
        raise AssertionError("OCR must not run on the PakistanLawSite path")

    monkeypatch.setattr(pdf_extractor, "_ocr_extract_with_pdfplumber_images", boom)


def test_text_layer_reads_typed_pdfs_and_never_ocrs_scans(monkeypatch):
    _forbid_ocr(monkeypatch)
    assert "PLD 2024 SC 321" in pdf_text_layer(text_pdf_bytes("JUDGMENT PLD 2024 SC 321 " * 20))
    assert len(pdf_text_layer(scanned_pdf_bytes()).strip()) < 200  # thin: reaches the corpus marked needs_ocr
    assert pdf_text_layer(b"not a pdf") == ""


async def test_pls_judgment_with_a_scanned_pdf_is_staged_without_ocr(db, login_source, monkeypatch):
    _forbid_ocr(monkeypatch)
    await _activate(db, login_source)
    citation, title = "PLD 2024 SC 902", "Scanned attachment"
    detail_url = "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=2006K902"
    pipeline = PakistanLawSitePipeline(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    pdf = scanned_pdf_bytes()

    async def download(_url):
        return pdf

    monkeypatch.setattr(pipeline, "download", download)
    row = {"citation": citation, "title": title, "court": "Supreme Court", "detail_url": detail_url, "pdf_url": "https://www.pakistanlawsite.com/files/902.pdf"}
    page = PageResult(url=detail_url, html=_notes_only_detail_html(citation, title), metadata={"requested_url": detail_url, "final_url": detail_url})

    assert await pipeline.preserve_and_extract(page, {"tier": 4, "row_index": 0}, row) == "staged"
    staging = (await db.execute(select(ScraperStaging))).scalars().one()
    assert staging.pdf_provenance_id is not None  # the original PDF is still preserved byte for byte
    assert staging.ocr_applied is False
