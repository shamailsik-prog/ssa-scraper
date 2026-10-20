from scraper.audit_baselines import AUDIT_2026_10_18
from scraper.statute_pdf_body import choose_best_statute_body_from_pdfs, prefer_pdf_statute_text


def test_auditor_statute_gap_is_sectionless_majority():
    assert AUDIT_2026_10_18["statutes_without_sections"] == 10_303
    assert AUDIT_2026_10_18["statutes_total"] == 12_618


def test_flag_off_keeps_html_listing_text():
    html = "Acts of the Senate\nDownload PDF"
    pdf = "1. Short title.- This Act may be called the Sample Act, 2024.\n" + ("section body " * 200)
    assert prefer_pdf_statute_text(html, pdf, flag_on=False) == html


def test_flag_on_prefers_long_pdf_over_listing_chrome():
    html = "Acts of the Senate\nDownload PDF"
    pdf = "1. Short title.- This Act may be called the Sample Act, 2024.\n" + ("operative section text " * 120)
    chosen = prefer_pdf_statute_text(html, pdf, flag_on=True)
    assert "Short title" in chosen
    assert len(chosen) > len(html)


def test_flag_on_keeps_html_when_pdf_is_thin():
    html = "1. Short title.- This Act may be called the Sample Act, 2024.\n" + ("html body " * 80)
    pdf = "%PDF-1.4 junk"
    assert prefer_pdf_statute_text(html, pdf, flag_on=True) == html


def test_choose_best_statute_body_picks_longest_qualifying_pdf():
    html = "Acts listing chrome\nDownload PDF"
    thin = "%PDF-1.4 noise"
    medium = "1. Short title.- Sample Act, 2024.\n" + ("section body " * 80)
    long = "1. Short title.- Sample Act, 2024.\n" + ("operative section text " * 120)
    chosen, followed = choose_best_statute_body_from_pdfs(html, [thin, medium, long], flag_on=True)
    assert followed is True
    assert len(chosen.strip()) == len(long.strip())
    assert len(chosen.strip()) > len(medium.strip())


def test_choose_best_picks_second_pdf_when_both_qualify_and_second_is_longer():
    html = "Acts listing chrome\nDownload PDF"
    first = "1. Short title.- First PDF.\n" + ("a" * 1600)
    second = "1. Short title.- Second PDF.\n" + ("b" * 2200)
    chosen, followed = choose_best_statute_body_from_pdfs(html, [first, second], flag_on=True)
    assert followed is True
    assert chosen == second
    assert len(chosen) > len(first)


def test_choose_best_keeps_html_when_pdf_below_min_chars_even_if_longer_than_tiny_listing():
    html = "Acts listing chrome"
    pdf = "1. Short title.- PDF extract.\n" + ("pdf section body " * 70)
    assert len(pdf) > len(html)
    assert len(pdf.strip()) < 1500
    chosen, followed = choose_best_statute_body_from_pdfs(html, [pdf], flag_on=True)
    assert followed is False
    assert chosen == html


def test_choose_best_keeps_html_when_pdf_is_long_raw_but_shorter_than_html():
    html = "1. Short title.- This Act may be called the Sample Act, 2024.\n" + ("html operative body " * 120)
    pdf = "1. Short title.- PDF extract.\n" + ("pdf body " * 200)
    assert len(pdf) >= 1500
    assert len(pdf.strip()) < len(html.strip())
    chosen, followed = choose_best_statute_body_from_pdfs(html, [pdf], flag_on=True)
    assert followed is False
    assert chosen == html
