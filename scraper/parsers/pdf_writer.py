"""
Rendered corpus PDFs for HTML-only judgments (Amendment §12, §19).

A rendered copy is never presented as an original court or publisher PDF. Every page carries
the label RENDERED_COPY_LABEL in the header and the footer, the document metadata Subject
field carries it too, and the archive ledger stores document_kind='rendered_copy'.
"""

from __future__ import annotations

import io
import logging
import os
import pathlib
from typing import Dict, Optional
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

logger = logging.getLogger(__name__)

RENDERED_COPY_LABEL = "CORPUS RENDERED COPY — NOT AN ORIGINAL SOURCE PDF"
PARAGRAPH_CHUNK = 4000


def _header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica-Oblique", 7.5)
    canvas.setFillGray(0.35)
    canvas.drawString(0.8 * inch, A4[1] - 0.5 * inch, RENDERED_COPY_LABEL)
    canvas.drawRightString(A4[0] - 0.8 * inch, 0.5 * inch, f"{RENDERED_COPY_LABEL} · page {doc.page}")
    canvas.restoreState()


def _build(buffer_or_path, meta: Dict[str, object], cleaned_text: str) -> None:
    doc = SimpleDocTemplate(
        buffer_or_path,
        pagesize=A4,
        leftMargin=0.8 * inch,
        rightMargin=0.8 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
        title=str(meta.get("title") or meta.get("citation") or "Judgment"),
        author="SIKANDER AI corpus service",
        subject=RENDERED_COPY_LABEL,
        creator="SIKANDER AI corpus service (reportlab)",
    )
    styles = getSampleStyleSheet()
    m = {k: _xml_escape(str(meta.get(k, "") or "")) for k in ("court", "title", "citation", "source_name", "source_url", "content_hash", "rendered_at", "access_method")}
    story = []
    story.append(Paragraph(f"<b>{RENDERED_COPY_LABEL}</b>", styles["Heading4"]))
    story.append(
        Paragraph(
            "This document was rendered by the corpus service from the exact preserved source text. "
            "It is not a court or publisher PDF. Provenance: source {src}, URL {url}, SHA-256 of preserved text {h}, rendered {at}.".format(
                src=m["source_name"], url=m["source_url"], h=m["content_hash"], at=m["rendered_at"]
            ),
            styles["Italic"],
        )
    )
    story.append(Spacer(1, 0.2 * inch))
    caption = f"<b>{m['court']}</b><br/>{m['title']}<br/>Citation: {m['citation']}"
    story.append(Paragraph(caption, styles["Heading3"]))
    story.append(Spacer(1, 0.2 * inch))
    for para in (cleaned_text or "").split("\n\n"):
        if len(para.strip()) < 2:
            continue
        esc = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for start in range(0, len(esc), PARAGRAPH_CHUNK):
            story.append(Paragraph(esc[start : start + PARAGRAPH_CHUNK], styles["Normal"]))
        story.append(Spacer(1, 0.08 * inch))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(RENDERED_COPY_LABEL, styles["Italic"]))
    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)


def render_judgment_pdf_bytes(meta: Dict[str, object], cleaned_text: str) -> bytes:
    buf = io.BytesIO()
    _build(buf, meta, cleaned_text)
    return buf.getvalue()


def write_judgment_pdf(path: pathlib.Path, meta: Dict[str, object], cleaned_text: str) -> int:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.pdf")
    _build(str(tmp), meta, cleaned_text)
    os.replace(tmp, path)
    size = path.stat().st_size
    logger.info("rendered corpus PDF %s (%d bytes)", path, size)
    return size


def _empty_document_size() -> int:
    return len(render_judgment_pdf_bytes({"title": "x", "citation": "x"}, ""))


MIN_PDF_BYTES = _empty_document_size() + 32


def is_rendered_copy(pdf_bytes: bytes) -> bool:
    """True when the PDF carries the rendered-copy label in its metadata (Subject)."""
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            meta = pdf.metadata or {}
            if RENDERED_COPY_LABEL in str(meta.get("Subject", "")):
                return True
            first = pdf.pages[0].extract_text() if pdf.pages else ""
            return RENDERED_COPY_LABEL.split(" — ")[0] in (first or "")
    except Exception:
        return False
