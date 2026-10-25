"""Unit tests for PDF-shaped URL body classification (HTML judgments vs defer)."""

from scraper.fetchers import FetchResult
from scraper.judgment_document_body import (
    PDF_MISS_DEFER_ERROR,
    classify_pdf_endpoint_body,
    html_body_has_judgment_marker,
    html_judgment_word_count,
)


def _shc_order_sheet_word_html() -> str:
    """MS Word–style HTML for a bail ORDER SHEET (no %PDF), similar to caselaw.shc.gov.pk view-file bodies."""
    body_text = """
ORDER SHEET IN THE HIGH COURT OF SINDH AT KARACHI
Criminal Bail Application No. 1234 of 2024
Applicant: Muhammad Ali
Versus
The State through SHO Police Station Garden
Present: Mr. Ahmed Khan, Advocate for the applicant.
Mr. Public Prosecutor for the State.
Heard. The applicant is in custody since 12-01-2024 in case FIR No. 45/2024.
After hearing learned counsel and perusing the record, ad-interim bail is granted subject to
furnishing solvent surety in the sum of Rs. 200,000/- and P.R. bond in the like amount to the
satisfaction of the trial court. Notice to the complainant. Case adjourned to 15-04-2024.
"""
    # Word export boilerplate inflates byte size without adding judgment-marker vocabulary.
    mso_pad = (
        '<p class="MsoNormal" style="mso-margin-top-alt:auto;mso-margin-bottom-alt:auto">'
        '<span style="font-size:11.0pt;font-family:&quot;Times New Roman&quot;,serif">&#160;</span></p>'
    ) * 120
    return (
        '<html xmlns:o="urn:schemas-microsoft-com:office:office">'
        '<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
        "<!--[if gte mso 9]><xml><w:WordDocument></w:WordDocument></xml><![endif]-->"
        "</head><body>"
        f"<div>{body_text.replace(chr(10), '<br/>')}</div>{mso_pad}</body></html>"
    )


def _shc_circuit_court_word_html() -> str:
    body_text = """
IN THE HIGH COURT OF SINDH CIRCUIT COURT, LARKANA
Constitutional Petition No. D-567 of 2023
Present: Hon. Mr. Justice A.B. Khan
Petitioner: M/s ABC Industries through its Director
Versus
Respondents: Federation of Pakistan and others
The petition challenges the imposition of regulatory charges. Arguments heard at length.
For reasons to be recorded separately, the petition is disposed of with directions to the
respondents to reconsider the impugned notification within sixty days. No order as to costs.
"""
    mso_pad = '<span style="mso-bidi-font-weight:normal">&#160;</span>' * 400
    return f"<html><body><div>{body_text.replace(chr(10), '<br/>')}</div>{mso_pad}</body></html>"


def test_order_sheet_word_html_has_marker_without_judgment_keyword():
    html = _shc_order_sheet_word_html()
    assert "judgment" not in html.lower()
    assert html_body_has_judgment_marker(html)
    assert html_judgment_word_count(html) < 200


def test_classify_order_sheet_word_html_as_html_judgment():
    html = _shc_order_sheet_word_html()
    res = FetchResult(
        url="https://caselaw.shc.gov.pk/caselaw/view-file/TOKEN",
        final_url="https://caselaw.shc.gov.pk/caselaw/view-file/TOKEN",
        status_code=200,
        content=html.encode("utf-8"),
        content_type="application/pdf",
    )
    assert classify_pdf_endpoint_body(res) == "html_judgment"


def test_classify_circuit_court_petition_html_as_html_judgment():
    html = _shc_circuit_court_word_html()
    res = FetchResult(
        url="https://caselaw.shc.gov.pk/caselaw/view-file/TOKEN2",
        final_url="https://caselaw.shc.gov.pk/caselaw/view-file/TOKEN2",
        status_code=200,
        content=html.encode("utf-8"),
        content_type="application/pdf",
    )
    assert classify_pdf_endpoint_body(res) == "html_judgment"


def test_short_non_court_html_still_defers():
    res = FetchResult(
        url="http://example.test/view-file/x",
        final_url="http://example.test/view-file/x",
        status_code=200,
        content=b"<html><body>Site maintenance. Try again later.</body></html>",
        content_type="application/pdf",
    )
    assert classify_pdf_endpoint_body(res) == "defer"


def test_pdf_miss_defer_error_message_unchanged():
    assert "not an accepted HTML judgment" in PDF_MISS_DEFER_ERROR
