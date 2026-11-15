"""Listing-page verdicts for Advance Search and Smart Search.

Case summaries contain ordinary words such as "access denied" and "one-time
password". Those words must not halt the PakistanLawSite source. A real block,
captcha, or login page still raises.
"""

from __future__ import annotations

from scraper.auth.session_manager import PageResult, raise_for_verdict


def raise_for_listing(page: PageResult, *, surface: str) -> None:
    """Classify one listing response. The original page HTML is left intact."""
    body = page.html or ""
    if surface == "smart":
        from scraper.pls_smart_search import smart_results_document

        if smart_results_document(body):
            body = ""
    elif surface == "advance":
        from scraper.pls_advance_search import advance_marker_html

        body = advance_marker_html(body)
    checked = PageResult(
        url=page.url,
        html=body,
        status=page.status,
        content_type=page.content_type,
    )
    raise_for_verdict(checked)
