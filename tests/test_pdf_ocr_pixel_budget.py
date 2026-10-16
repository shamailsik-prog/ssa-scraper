"""OCR renders each page within a pixel budget (16 October 2026).

senate.gov.pk/uploads/documents/1770653701_130.pdf is a phone photo saved as a two-page PDF whose
pages are 34 x 50 inches. Rendering one at 300 DPI made a 151-megapixel image: the public worker
peaked at 1.9 GB and the kernel killed it on the 3.9 GB public server, every six hours, because the
same PDF stayed first in the Senate frontier. The render is now capped at OCR_MAX_PAGE_PIXELS; an
ordinary page (A4, letter, legal) still renders at 300 DPI.
"""

from __future__ import annotations

import io

import pdfplumber
from PIL import Image

from scraper.config import settings
from scraper.parsers import pdf_extractor


def _image_only_pdf(width_pt: float, height_pt: float) -> bytes:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width_pt, height_pt))
    c.drawImage(ImageReader(Image.new("RGB", (64, 64), "white")), 0, 0, width=width_pt, height=height_pt)
    c.showPage()
    c.save()
    return buf.getvalue()


class _FakeRender:
    def __init__(self):
        self.original = Image.new("L", (8, 8), 255)


def _record_renders(monkeypatch):
    """Record each page.to_image call without rendering: the poster page would need 1.9 GB."""
    calls = []

    def fake_to_image(page, resolution=None, **_kw):
        calls.append((float(page.width), float(page.height), resolution))
        return _FakeRender()

    monkeypatch.setattr(pdfplumber.page.Page, "to_image", fake_to_image)
    monkeypatch.setattr("pytesseract.image_to_string", lambda *a, **k: "")
    return calls


def _pixels(width_pt: float, height_pt: float, dpi: int) -> float:
    return (width_pt / 72 * dpi) * (height_pt / 72 * dpi)


def test_poster_sized_page_renders_within_the_pixel_budget(monkeypatch):
    calls = _record_renders(monkeypatch)
    pdf_extractor._ocr_extract_with_pdfplumber_images(_image_only_pdf(2438, 3568))  # the Senate page size
    assert calls, "OCR never rendered the page"
    for width_pt, height_pt, dpi in calls:
        assert dpi and dpi > 0
        assert _pixels(width_pt, height_pt, dpi) <= settings.OCR_MAX_PAGE_PIXELS


def test_ordinary_pages_still_render_at_300_dpi(monkeypatch):
    calls = _record_renders(monkeypatch)
    for size in ((595.28, 841.89), (612, 792), (612, 1008)):  # A4, letter, legal
        pdf_extractor._ocr_extract_with_pdfplumber_images(_image_only_pdf(*size))
    assert [dpi for _w, _h, dpi in calls] == [300, 300, 300]
