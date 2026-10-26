"""Unit tests for PDF-shaped URL body classification (HTML judgments vs defer)."""

from scraper.fetchers import FetchResult
from scraper.judgment_document_body import (
    MIN_HTML_STRONG_COURT_DOCUMENT_WORDS,
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


def _shc_order_sheet_crlf_wrapped_word_html() -> str:
    """Word export hard-wraps ORDER/SHEET and court headers across line breaks (SHC view-file)."""
    lines = [
        "ORDER",
        "SHEET",
        "IN",
        "THE HIGH COURT OF SINDH AT KARACHI",
        "Criminal Bail Application No. S-748 of 2024",
        "Applicant: Muhammad Ali",
        "V/S",
        "The State through SHO Police Station Garden",
        "Present: Mr. Ahmed Khan, Advocate for the applicant.",
        "Mr. Public Prosecutor for the State.",
        "Heard. The applicant is in custody since 12-01-2024 in case FIR No. 45/2024.",
        "After hearing learned counsel and perusing the record, ad-interim bail is granted subject to",
        "furnishing solvent surety in the sum of Rs. 200,000/- and P.R. bond in the like amount to the",
        "satisfaction of the trial court. Notice to the complainant. Case adjourned to 15-04-2024.",
        "Copy to be supplied to both sides. Office is directed to communicate this order forthwith.",
    ]
    inner = "<br/>\r\n".join(lines)
    mso_pad = (
        '<p class="MsoNormal"><span style="font-size:11.0pt">&#160;</span></p>' * 80
    )
    return (
        '<html xmlns:o="urn:schemas-microsoft-com:office:office">'
        '<head><meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
        "</head><body>"
        f"<div>{inner}</div>{mso_pad}</body></html>"
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


def test_crlf_wrapped_order_sheet_has_strong_marker_without_judgment_keyword():
    html = _shc_order_sheet_crlf_wrapped_word_html()
    assert "judgment" not in html.lower()
    assert "Versus" not in html
    assert html_body_has_judgment_marker(html)
    wc = html_judgment_word_count(html)
    assert 50 <= wc < 200


def test_classify_crlf_wrapped_order_sheet_word_html_as_html_judgment():
    html = _shc_order_sheet_crlf_wrapped_word_html()
    res = FetchResult(
        url="https://caselaw.shc.gov.pk/caselaw/view-file/CRLF-TOKEN",
        final_url="https://caselaw.shc.gov.pk/caselaw/view-file/CRLF-TOKEN",
        status_code=200,
        content=html.encode("windows-1252"),
        content_type="application/pdf",
    )
    assert classify_pdf_endpoint_body(res) == "html_judgment"


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


def _word_doc_properties_preamble() -> str:
    """MSO DocumentProperties noise ahead of order text (SHC view-file 92c752f2 shape)."""
    return (
        '<!--[if gte mso 9]><xml><o:DocumentProperties>'
        "<o:Author>SHC</o:Author><o:Keywords>order</o:Keywords>"
        "<o:Company>Microsoft Corporation</o:Company>"
        "</o:DocumentProperties></xml><![endif]-->"
    )


def _shc_katcha_peshi_cr_misc_word_html() -> str:
    """Cr.Misc.A.No. + For Katcha Peshi order sheet with doc-properties preamble (92c752f2)."""
    lines = [
        "ORDER SHEET",
        "IN THE HIGH COURT OF SINDH AT LARKANA",
        "Cr.Misc.A.No.S-32 of 2007 For Katcha Peshi",
        "Present: Mr. Khan, Advocate for the applicant.",
        "Heard. Matter adjourned to next date for arguments.",
    ]
    inner = "<br/>\r\n".join(lines)
    return (
        '<html xmlns:o="urn:schemas-microsoft-com:office:office">'
        f"<head>{_word_doc_properties_preamble()}</head>"
        '<body><div class="WordSection1">'
        f"{inner}</div></body></html>"
    )


def _shc_larkana_bail_spaced_order_word_html(case_no: str) -> str:
    """Larkana bail with letter-spaced O R D E R (ffce1b56 / ba9c3b56 / c1b3d4f0)."""
    lines = [
        "IN THE HIGH COURT OF SINDH AT LARKANA",
        f"Criminal Bail Application No. {case_no}",
        "O R D E R",
        "After hearing learned counsel, ad-interim bail is granted subject to furnishing surety.",
        "Notice to the complainant.",
    ]
    inner = "<br/>\r\n".join(lines)
    return (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
        "</head><body>"
        f"<div>{inner}</div></body></html>"
    )


def _shc_larkano_bench_spaced_order_word_html() -> str:
    """LARKANO bench spelling with spaced ORDER (43a90e8b / 675299e7)."""
    lines = [
        "HIGH COURT OF SINDH LARKANO BENCH",
        "1st Criminal Bail Application No. S-274/2022",
        "O R D E R",
        "Allowed subject to furnishing solvent surety to the satisfaction of the trial court.",
    ]
    return (
        "<html><body><div>"
        + "<br/>\r\n".join(lines)
        + "</div></body></html>"
    )


def _shc_short_katcha_cp_word_html() -> str:
    """Short C.P. Katcha Peshi sheet (7813b7b0)."""
    return (
        "<html><body>"
        "IN THE HIGH COURT OF SINDH AT LARKANA<br/>"
        "C.P.S-342 of 2007 For Katcha Peshi<br/>"
        "ORDER SHEET<br/>"
        "Adjourned to 12-04-2007."
        "</body></html>"
    )


def _shc_hyderabad_ordersheet_word_html() -> str:
    """ORDERSHEET one word + C.P. Hyderabad (48aac4be)."""
    lines = [
        "ORDERSHEET",
        "IN THE HIGH COURT OF SINDH AT HYDERABAD",
        "C.P. D-2478/2022",
        "Present: Mr. Ali, Advocate for petitioner.",
        "Heard. Petition dismissed for non-prosecution with liberty to file afresh.",
        "Copy to parties.",
    ]
    return "<html><body>" + "<br/>".join(lines) + "</body></html>"


def _fetch_result_for_word_html(html: str, token: str, *, encoding: str = "utf-8") -> FetchResult:
    return FetchResult(
        url=f"https://caselaw.shc.gov.pk/caselaw/view-file/{token}",
        final_url=f"https://caselaw.shc.gov.pk/caselaw/view-file/{token}",
        status_code=200,
        content=html.encode(encoding),
        content_type="application/pdf",
    )


def test_classify_katcha_peshi_cr_misc_with_doc_properties():
    html = _shc_katcha_peshi_cr_misc_word_html()
    assert html_judgment_word_count(html) < MIN_HTML_STRONG_COURT_DOCUMENT_WORDS
    assert classify_pdf_endpoint_body(_fetch_result_for_word_html(html, "92c752f2")) == "html_judgment"


def test_classify_larkana_bail_spaced_order_cases():
    for token, case_no in (
        ("ffce1b56", "S-35/2023"),
        ("ba9c3b56", "S-29/2023"),
        ("c1b3d4f0", "S-414/2022"),
    ):
        html = _shc_larkana_bail_spaced_order_word_html(case_no)
        res = _fetch_result_for_word_html(html, token, encoding="windows-1252")
        assert classify_pdf_endpoint_body(res) == "html_judgment"


def test_classify_larkano_bench_spaced_order():
    html = _shc_larkano_bench_spaced_order_word_html()
    res = _fetch_result_for_word_html(html, "675299e7")
    assert classify_pdf_endpoint_body(res) == "html_judgment"
    assert classify_pdf_endpoint_body(_fetch_result_for_word_html(html, "43a90e8b")) == "html_judgment"


def test_classify_short_katcha_cp_order_sheet():
    html = _shc_short_katcha_cp_word_html()
    assert classify_pdf_endpoint_body(_fetch_result_for_word_html(html, "7813b7b0")) == "html_judgment"


def test_classify_hyderabad_ordersheet_one_word():
    html = _shc_hyderabad_ordersheet_word_html()
    assert "ordersheet" in html.lower()
    assert "order sheet" not in html.lower().replace("ordersheet", "")
    assert classify_pdf_endpoint_body(_fetch_result_for_word_html(html, "48aac4be")) == "html_judgment"


def _balochistan_hc_html_judgment_body() -> str:
    """~60-word HTML judgment body (#347 Balochistan HC strong-court path)."""
    return """
    <html><body>
    IN THE HIGH COURT OF BALOCHISTAN AT QUETTA
    Constitution Petition No. 123 of 2024
    Muhammad Ali Versus Government of Balochistan and others
    Heard. For reasons to be recorded separately, this petition is dismissed with costs.
    The impugned notification is set aside and the respondents are directed to decide afresh
    within sixty days in accordance with law after hearing the petitioner.
    Office is directed to communicate this order to the parties and to the trial court.
    </body></html>
    """


def _long_html_judgment_with_hearing_roster_citations() -> str:
    """Full judgment (200+ words) citing hearings, rosters, and Lahore HC precedent."""
    padding = (
        "The petitioner argued that the impugned order violated settled principles of natural justice "
        "and that the record did not support the findings returned by the trial court. "
        "Learned counsel relied upon the roster practice of this court and noted that the matter was "
        "earlier fixed for hearing before a division bench. Reference was made to cause list entries "
        "in an allied case and to the Lahore High Court judgment reported as PLD 2010 Lahore 100. "
    ) * 4
    body = f"""
    IN THE HIGH COURT OF SINDH AT KARACHI
    Constitution Petition No. D-999 of 2024
    ABC Industries Versus Federation of Pakistan and others
    Judgment
    {padding}
    For the foregoing reasons, the petition is disposed of with directions to the respondents.
    """
    return f"<html><body>{body.replace(chr(10), '<br/>')}</body></html>"


def test_classify_balochistan_hc_body_via_in_the_high_court_of_strong_marker():
    html = _balochistan_hc_html_judgment_body()
    wc = html_judgment_word_count(html)
    assert 50 <= wc < 200
    assert html_body_has_judgment_marker(html)
    res = FetchResult(
        url="https://example.test/view-file/balochistan-hc",
        final_url="https://example.test/view-file/balochistan-hc",
        status_code=200,
        content=html.encode("utf-8"),
        content_type="application/pdf",
    )
    assert classify_pdf_endpoint_body(res) == "html_judgment"


def test_long_judgment_with_hearing_roster_and_lahore_citation_still_accepts():
    html = _long_html_judgment_with_hearing_roster_citations()
    assert html_judgment_word_count(html) >= 200
    assert html_body_has_judgment_marker(html)
    res = FetchResult(
        url="https://caselaw.shc.gov.pk/caselaw/view-file/LONG-JUDG",
        final_url="https://caselaw.shc.gov.pk/caselaw/view-file/LONG-JUDG",
        status_code=200,
        content=html.encode("utf-8"),
        content_type="application/pdf",
    )
    assert classify_pdf_endpoint_body(res) == "html_judgment"


def test_auditor_probes_defer_not_html_judgment():
    """Cause lists, adjournment notices, Lahore rosters, and SHC header filler must not pass."""
    probes = {
        "cause_list": """
        IN THE HIGH COURT OF SINDH AT KARACHI
        DAILY CAUSE LIST FOR CRIMINAL BAIL MATTERS
        ORDER SHEET
        1. Criminal Bail Application No. S-100/2024
        2. Criminal Bail Application No. S-101/2024
        For hearing on 12-03-2024. Roster published by Registrar.
        """,
        "cp_adjournment_notice": """
        IN THE HIGH COURT OF SINDH AT HYDERABAD
        ORDER SHEET
        C.P. D-100/2024
        ADJOURNMENT NOTICE: Matter adjourned for next date without arguments.
        For information of parties only.
        """,
        "lahore_roster": """
        IN THE HIGH COURT OF PUNJAB AT LAHORE
        ORDER SHEET
        Criminal roster for the week. Bail matters listed for hearing.
        """,
        "shc_header_filler": (
            "IN THE HIGH COURT OF SINDH AT KARACHI " + " ".join(["procedural"] * 60)
        ),
    }
    for html in probes.values():
        res = _fetch_result_for_word_html(
            f"<html><body>{html.replace(chr(10), '<br/>')}</body></html>",
            "probe",
        )
        assert classify_pdf_endpoint_body(res) == "defer"


def test_windows_1252_nbsp_letter_spaced_order_has_marker():
    html = (
        "<html><body>IN THE HIGH COURT OF SINDH AT LARKANA\r\n"
        "Criminal Bail Application No. S-35/2023\r\n"
        "O\xa0R\xa0D\xa0E\xa0R\r\n"
        "Bail granted.</body></html>"
    )
    res = _fetch_result_for_word_html(html, "ffce1b56", encoding="windows-1252")
    assert html_body_has_judgment_marker(res.text)
    assert classify_pdf_endpoint_body(res) == "html_judgment"
