"""Prefer extracted PDF act text over HTML listing chrome (SSA_STATUTE_PDF_FOLLOW).

Auditor 2026-10-18: ~82% of statutes have no sections. Assembly/Gazette/AJK captures often store
the listing page while the act text lives only in a linked PDF. Judgment ingest already follows
PDF links; statute ingest did not.
"""

from __future__ import annotations

from typing import Optional


def prefer_pdf_statute_text(
    html_text: str,
    pdf_text: Optional[str],
    *,
    flag_on: bool,
    min_chars: int = 1500,
) -> str:
    if not flag_on:
        return html_text
    pdf = (pdf_text or "").strip()
    html = (html_text or "").strip()
    if len(pdf) >= min_chars and len(pdf) > len(html):
        return pdf
    return html_text
