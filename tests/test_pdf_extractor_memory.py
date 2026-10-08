"""Each PDF page is released once read (8 October 2026: a long scanned AJK ordinance grew a public
worker past 2 GB, because pdfplumber kept every page's parsed objects until the document closed)."""

from __future__ import annotations

from scraper.parsers import pdf_extractor
from tests.fixtures import scanned_pdf_bytes, text_pdf_bytes


def _count_releases(monkeypatch):
    released = []
    real = pdf_extractor._release_page
    monkeypatch.setattr(pdf_extractor, "_release_page", lambda page: (released.append(page.page_number), real(page)))
    return released


def test_text_extraction_releases_every_page(monkeypatch):
    released = _count_releases(monkeypatch)
    pdf = text_pdf_bytes("\n".join(f"Section {i}. This Act may be called line {i}." for i in range(200)))  # four pages
    text, pages, _scanned = pdf_extractor._pdfplumber_extract(pdf)
    assert pages >= 3 and "Section 199" in text
    assert sorted(released) == list(range(1, pages + 1))


def test_ocr_pass_releases_every_page(monkeypatch):
    released = _count_releases(monkeypatch)
    pdf_extractor._ocr_extract_with_pdfplumber_images(scanned_pdf_bytes())
    assert released == [1]
