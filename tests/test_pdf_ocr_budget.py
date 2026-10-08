"""OCR of one PDF is bounded (8 October 2026: with tesseract's default threading two public workers OCRing
side by side took about 3 minutes a page; a 47-page and a 30-page scan held both workers for hours and no
other public source ran)."""

from __future__ import annotations

import io
import os
import sys
import types

from scraper.config import settings
from scraper.parsers import pdf_extractor


def _scanned_pages(n: int) -> bytes:
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i in range(n):
        img = Image.new("RGB", (800, 200), "white")
        ImageDraw.Draw(img).text((20, 80), f"page {i + 1}", fill="black")
        c.drawImage(ImageReader(img), 30, 500, width=540, height=135)
        c.showPage()
    c.save()
    return buf.getvalue()


def _fake_tesseract(monkeypatch, clock, seconds_per_call):
    calls = []

    def image_to_string(image, config="", timeout=0):
        calls.append({"config": config, "timeout": timeout, "omp": os.environ.get("OMP_THREAD_LIMIT")})
        clock[0] += seconds_per_call
        return "Section 1. This Act may be called the Test Act. " * 3

    monkeypatch.setitem(sys.modules, "pytesseract", types.SimpleNamespace(image_to_string=image_to_string))
    monkeypatch.setattr(pdf_extractor.time, "monotonic", lambda: clock[0])
    monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
    return calls


def test_each_page_runs_single_threaded_with_a_timeout(monkeypatch):
    monkeypatch.setattr(settings, "OCR_PAGE_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(settings, "OCR_DOCUMENT_BUDGET_SECONDS", 900)
    calls = _fake_tesseract(monkeypatch, [0.0], 1)
    text = pdf_extractor.extract_pdf_text(_scanned_pages(3))
    assert "Test Act" in text
    assert len(calls) == 3
    assert all(c["timeout"] == 120 and c["omp"] == "1" for c in calls)


def test_document_past_its_budget_yields_no_text(monkeypatch):
    monkeypatch.setattr(settings, "OCR_PAGE_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(settings, "OCR_DOCUMENT_BUDGET_SECONDS", 250)
    calls = _fake_tesseract(monkeypatch, [0.0], 100)
    assert pdf_extractor.extract_pdf_text(_scanned_pages(6)) == ""
    assert len(calls) == 3  # stopped at the budget, not after all six pages


def test_no_budget_reads_every_page(monkeypatch):
    monkeypatch.setattr(settings, "OCR_DOCUMENT_BUDGET_SECONDS", 0)
    calls = _fake_tesseract(monkeypatch, [0.0], 100)
    assert "Test Act" in pdf_extractor.extract_pdf_text(_scanned_pages(6))
    assert len(calls) == 6
