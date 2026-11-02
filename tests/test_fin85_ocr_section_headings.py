"""Finance Act 1985 OCR headings after the #398 rate and HS folds.

The senate text prints real headings as ``L, Short title and extent.—`` and
``3, Amendment ….—``. The line-start splitter only sees a dot, so sections
1–4 fall off as preamble. The first dot it does see is a rate row whose
column header ``Rate`` was joined on in front, and a tariff sub-row
``4. Rolled…`` has no HS code. An amending Finance Act has no Definitions
section. That is not a failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.parsers.statute_parser import (
    section_text_starts_with_tariff_schedule_row,
    split_into_sections,
)
from scraper.statute_quality.section_reextract import gate_section_reextract

FINANCE = "Finance Act, 1985"
FIXTURES = Path("tests/fixtures/statute_quality")
NWFP = "North-West Frontier Province Urban Planning Act, 1975"
NWFP_URL = "https://senate.gov.pk/uploads/documents/1661312733_937.pdf"

# Raw split_into_sections counts on the checked-in text layers. A Fin85
# heading change must not move these. The NWFP production path is isolated
# before the split; that count is asserted separately.
FIXTURE_SECTION_COUNTS = {
    "ajk_act_ii_2023_garbled_section.txt": 1,
    "ajk_page_stub_d5ba6a26.txt": 2,
    "balochistan_animal_slaughter_control_2025_pdfplumber.txt": 15,
    "balochistan_prevention_of_beggary_2025_pdfplumber.txt": 21,
    "bank_ajk_amendment_date.txt": 10,
    "clean_english_finance_act.txt": 3,
    "gazette_senate_b4e0f316.txt": 1,
    "gazette_senate_following_act.txt": 1,
    "ocr_dea868f2.txt": 1,
    "publication_notice_section1.txt": 1,
    "sections_7334221a.txt": 3,
    "senate_1660540467_508.txt": 8,
    # The Rangers prefix heading ``4, Amendment … 1959.-`` is a real comma
    # heading. Isolation drops that prefix before the NWFP extract.
    "senate_1661312733_937_boundaries.txt": 5,
    "senate_1661312733_937_text_layer.txt": 52,
    "tariff_schedule_single_mojibake.txt": 1,
    "year_citation_of_1860_and_dt.txt": 6,
}

FIN85_OCR = """
THE FINANCE ACT, 1985
WHEREAS it is expedient to make certain amendments in the fiscal laws;
It is hereby enacted as follows:—
L, Short title and extent.— (1) This Act may be called the Finance Act, 1985. (2) It extends to the whole of Pakistan.
3, Amendment ef Act XV of 1963.-- In the Wealth Tax Act, 1963, the following further amendment shall be made by this section of the Act.
3, Amendment of Act IV of 1969.— In the Customs Act, 1969, the First Schedule shall be amended in the manner set out below.
4, Amendment of Ordinance XXXI of 1979.— In the Income Tax Ordinance, 1979, the following further amendment shall be made, namely the rate schedule hereunder.
Rate
1. Where the taxable income does not exceed Rs. 25,000 the tax shall be five percent of the income.
2. Where the taxable income exceeds Rs. 25,000 but does not exceed Rs. 50,000 the tax shall be the amount specified.
4. Where the taxable income exceeds Rs. 50,000 but does not exceed Rs. 100,000 the tax shall be computed under this clause.
5. Where the taxable income exceeds Rs. 100,000 the tax shall be the amount set out in this rate of the schedule.
5. lara surcharge on imported goods.— A surcharge to be called the Iqra surcharge shall be levied on goods imported into Pakistan at the rate notified under this section.
73.15 Chain and parts thereof of iron or steel per metric ton.
4. Rolled but not further worked, of a thickness exceeding one millimetre but less than two millimetres.
08.05 Nuts, fresh or dried, whether or not shelled or peeled.
the said sum, Rs. 4,500, shall— be paid into the Federal Consolidated Fund under this Act.
"""


def _sections(text: str, name: str = FINANCE):
    return split_into_sections(text, name)


def _by_number(text: str, name: str = FINANCE) -> dict:
    return {section["section_number"]: section["section_text"] for section in _sections(text, name)}


def _numbers(text: str, name: str = "Example Act, 1975") -> list:
    return [section["section_number"] for section in _sections(text, name)]


def test_fin85_ocr_recovers_short_title_through_iqra_surcharge():
    """Comma and letter headings are the real Act. Schedule rows stay inside them."""
    sections = _sections(FIN85_OCR)
    bodies = {section["section_number"]: section["section_text"] for section in sections}
    assert [section["section_number"] for section in sections] == ["1", "2", "3", "4", "5"]

    assert bodies["1"].lstrip().startswith("1. Short title")
    assert "Finance Act, 1985" in bodies["1"]
    assert "Wealth Tax Act, 1963" in bodies["2"]
    assert "Customs Act, 1969" in bodies["3"]
    assert bodies["4"].lstrip().startswith("4. Amendment of Ordinance")
    assert "Income Tax Ordinance, 1979" in bodies["4"]
    assert "does not exceed Rs. 25,000" in bodies["4"]
    assert "exceeds Rs. 100,000" in bodies["4"]
    assert "Iqra surcharge" in bodies["5"]
    assert "per metric ton" in bodies["5"]
    assert "Rolled but not further worked" in bodies["5"]
    assert "Nuts, fresh or dried" in bodies["5"]
    assert "4,500" in bodies["5"]

    assert "Definitions" not in "\n".join(bodies.values())
    for text in bodies.values():
        first = text.lstrip()
        assert not first.startswith("Rate")
        assert not re.match(r"(?i)^where the taxable income\b", first)
        assert not re.match(r"(?i)^\d{1,2}\.\d{2}\b", first)
        assert not re.match(r"(?i)^\d{1,2}\.\s+Rolled\b", first)
        assert section_text_starts_with_tariff_schedule_row(text) is False
    assert "500" not in bodies
    assert "4500" not in bodies


def test_tariff_polluted_before_set_passes_the_five_section_parse():
    """Eleven schedule rows are not a collapse baseline. No Definitions section is required."""
    before = [
        {"section_number": "1", "section_text": "1. Where the taxable income does not exceed Rs. 25,000 the tax shall be five percent."},
        {"section_number": "2", "section_text": "2. Where the taxable income exceeds Rs. 25,000 but does not exceed Rs. 50,000 the tax shall be the amount."},
        {"section_number": "4", "section_text": "4. Where the taxable income exceeds Rs. 50,000 but does not exceed Rs. 100,000 the tax shall be computed."},
        {"section_number": "33", "section_text": "33.03 Synthetic organic colouring matter, whether or not chemically defined."},
        {"section_number": "78", "section_text": "78. 38BO5 Chromite sulphate used in the dyeing and tanning process under this schedule."},
        {"section_number": "32", "section_text": "32.03 Synthetic organic colouring matter and preparations based thereon."},
        {"section_number": "40", "section_text": "40.11 New pneumatic tyres, of rubber, of a kind used on motor cars."},
        {"section_number": "51", "section_text": "51.07 Yarn of combed wool, not put up for retail sale, per kilogram."},
        {"section_number": "56", "section_text": "56.01 Synthetic staple fibres, not carded, combed or otherwise processed."},
        {"section_number": "03", "section_text": "03) Cuprammonium hydroxide solution used in the manufacture of cuprammonium rayon."},
        {"section_number": "73", "section_text": "73.15 Chain and parts thereof, of iron or steel, per metric ton."},
    ]
    assert len(before) == 11
    assert all(section_text_starts_with_tariff_schedule_row(row["section_text"]) for row in before)
    after = _sections(FIN85_OCR)
    decision = gate_section_reextract(before, after)
    assert decision.verdict == "PASS"
    assert decision.reason == ""
    assert decision.may_apply is True
    assert decision.section_count_before == 0
    assert decision.section_count_after == 5


@pytest.mark.parametrize("mark", ["L", "I", "l", "|"])
def test_first_letter_comma_heading_is_section_one(mark):
    text = f"""
{mark}, Short title and extent.— (1) This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of Act IV of 1969.— In the Customs Act, 1969 the following further amendment shall be made by this section of the Act.
"""
    bodies = _by_number(text)
    assert bodies["1"].lstrip().startswith("1. Short title")
    assert "Amendment of Act IV of 1969" in bodies["2"]


def test_later_letter_comma_is_not_renumbered_to_section_one():
    text = """
1. Short title and extent.— (1) This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
I, Short title and extent.— UNIQUEWORD the note continues in this same section of the Act and is not a new heading.
2. Amendment of Act IV of 1969.— In the Customs Act, 1969 the following further amendment shall be made by this section.
"""
    bodies = _by_number(text)
    assert "UNIQUEWORD" in bodies["1"]
    assert "Amendment of Act IV of 1969" in bodies["2"]
    assert list(bodies) == ["1", "2"]


def test_comma_penalties_heading_keeps_its_number():
    text = """
11. Appeals.— Any person aggrieved by an order under this Act may appeal within thirty days of the notice.
12, Penalties.— Whoever contravenes this Act shall be punished with fine which may extend to ten thousand rupees.
"""
    bodies = _by_number(text, "Example Penalties Act, 1975")
    assert "punished with fine" in bodies["12"]
    assert bodies["12"].lstrip().startswith("12. Penalties")


def test_prose_comma_amount_is_not_a_heading():
    text = """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Payment.— The taxpayer shall pay the said sum, Rs. 4,500, shall— be credited to the Federal Consolidated Fund under this section.
"""
    bodies = _by_number(text)
    assert list(bodies) == ["1", "2"]
    assert "4,500" in bodies["2"]
    assert "4" not in bodies
    assert "500" not in bodies


def test_leading_rate_header_does_not_take_section_one():
    text = """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of Ordinance XXXI of 1979.— In the Income Tax Ordinance, 1979 the following further amendment shall be made by this section.
Rate
1. Where the taxable income does not exceed Rs. 25,000 the tax shall be five percent of the income.
2. Where the taxable income exceeds Rs. 25,000 the tax shall be the amount specified in this schedule.
3. Sales tax.— The sales tax shall be charged at the rate specified in this section of the Act for the year.
"""
    bodies = _by_number(text)
    assert list(bodies) == ["1", "2", "3"]
    assert bodies["1"].lstrip().startswith("1. Short title")
    assert "does not exceed Rs. 25,000" in bodies["2"]
    assert bodies["2"].lstrip().startswith("2. Amendment")
    assert "Sales tax" in bodies["3"]
    assert section_text_starts_with_tariff_schedule_row(
        "Rate 1. Where the taxable income does not exceed Rs. 25,000."
    ) is True
    assert section_text_starts_with_tariff_schedule_row(
        "Rate\n1. Where the taxable income does not exceed Rs. 25,000 the tax shall be five percent."
    ) is True


def test_rolled_subrow_folds_only_after_a_tariff_row():
    text = """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of the First Schedule.— In the Customs Act, 1969 the First Schedule shall be amended as set out below.
73.15 Chain and parts thereof of iron or steel per metric ton.
4. Rolled but not further worked, of a thickness exceeding one millimetre but less than two millimetres.
3. Sales tax.— The sales tax shall be charged at the rate specified in this section of the Act for the year.
"""
    bodies = _by_number(text)
    assert list(bodies) == ["1", "2", "3"]
    assert "Rolled but not further worked" in bodies["2"]
    assert bodies["2"].lstrip().startswith("2. Amendment")
    assert "Sales tax" in bodies["3"]

    kept = """
1. Short title.— This Act may be called the Example Act, 1975 and it extends to the whole of the Province.
2. Hours.— No adult worker shall be required to work in a factory for more than nine hours in any day.
4. Other provisions.— The following provisions of this Act shall apply to the factory and the occupier.
"""
    other = _by_number(kept, "Example Factories Act, 1975")
    assert "Other provisions" in other["4"]
    assert other["4"].lstrip().startswith("4. Other provisions")


def test_hot_work_heading_after_a_tariff_row_stays_a_section():
    text = """
1. Short title.— This Act may be called the Example Factories Act, 1975 and it extends to the Province.
2. Amendment of the Schedule.— The schedule of processes shall be amended as set out below in this section.
73.15 Chain and parts thereof of iron or steel per metric ton.
4. Hot work is prohibited in a factory under this Act.— The occupier shall stop that work at once.
"""
    bodies = _by_number(text, "Example Factories Act, 1975")
    assert "stop that work" in bodies["4"]
    assert bodies["4"].lstrip().startswith("4. Hot work")


def test_text_after_enacted_as_follows_is_kept_when_section_one_has_no_heading():
    text = """
THE FINANCE ACT, 1985
WHEREAS it is expedient to amend the fiscal laws for the year of this Act.
It is hereby enacted as follows:—
This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan and shall come into force at once.
2. Amendment of Act IV of 1969.— In the Customs Act, 1969 the following further amendment shall be made by this section.
3. Sales tax.— The sales tax shall be charged at the rate specified in this section of the Act for the year.
"""
    bodies = _by_number(text)
    assert list(bodies) == ["1", "2", "3"]
    assert "may be called the Finance Act, 1985" in bodies["1"]
    assert "Amendment of Act IV of 1969" in bodies["2"]
    assert "Sales tax" in bodies["3"]


def test_parser_shape_corpus_dry_count():
    """Twenty heading shapes. The dry-count is their section total, same idea as #397."""
    shapes = [
        ("period short title", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of Act IV of 1969.— In the Customs Act, 1969 the following further amendment shall be made.
""", FINANCE, ["1", "2"]),
        ("paren short title and penalties", """
1) Short title. This Act may be called the Example Penalties Act, 1975 and it extends to the whole Province.
12) Penalties. Whoever contravenes this Act shall be punished with fine which may extend to ten thousand rupees.
""", "Example Penalties Act, 1975", ["1", "12"]),
        ("definitions", """
1. Short title.— This Act may be called the Example Act, 1975 and it extends to the Province.
2. Definitions.— In this Act, unless there is anything repugnant in the subject or context, tax means the tax.
""", "Example Act, 1975", ["1", "2"]),
        ("income tax heading", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
7. Income tax.— (1) The tax shall be charged in the manner provided in this section of the Act.
""", FINANCE, ["1", "7"]),
        ("hs fold", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of the First Schedule.— In the Customs Act, 1969 the First Schedule shall be amended as set out below.
32.03 Synthetic organic colouring matter, whether or not chemically defined.
""", FINANCE, ["1", "2"]),
        ("clock is not a section", """
6. Hours of work.— No adult worker shall be required to work in a factory for more than nine hours in any day.
7. Weekly hours.— No adult worker shall be required to work in a factory for more than forty-eight hours in any week.
7.30 p.m. the sitting of the Assembly shall stand adjourned until the next day of the session.
8. Overtime.— Overtime shall be paid at twice the ordinary rate of wages for the overtime worked under this section.
""", "Example Factories Act, 1975", ["6", "7", "8"]),
        ("comma gap between period headings", """
21. Adoption of regulations.— A Local Area Authority may adopt Land Development Regulations pursuant to this Chapter.
22, Implementation of approved Local Area Plan. The Local Area Authority shall be responsible for implementing the approved plan.
23. Modification of delegation.— The Board may by notified order modify the terms of the delegation of functions.
""", "Example Act, 1975", ["21", "22", "23"]),
        ("repeated thirty five", """
35. Approval of restrictions.— Regulations of land development shall be subject to the prior approval of the planning agency.
35. Public participation and review.— The provisions of section 19 relating to Local Area Plans shall apply to the review.
37. Delegation of enforcement.— Government may delegate enforcement functions to another government agency.
""", "Example Act, 1975", ["35", "36", "37"]),
        ("fin85 ocr", FIN85_OCR, FINANCE, ["1", "2", "3", "4", "5"]),
        ("rate header", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of Ordinance XXXI of 1979.— In the Income Tax Ordinance, 1979 the following further amendment shall be made.
Rate
1. Where the taxable income does not exceed Rs. 25,000 the tax shall be five percent of the income.
3. Sales tax.— The sales tax shall be charged at the rate specified in this section of the Act for the year.
""", FINANCE, ["1", "2", "3"]),
        ("zero pad chemical", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of the First Schedule.— In the Customs Act, 1969 the First Schedule shall be amended as set out below.
03) Cuprammonium hydroxide solution used in the manufacture of cuprammonium rayon under this schedule.
3. Income tax.— The rate of income tax shall be the rate specified in this section of the Act.
""", FINANCE, ["1", "2", "3"]),
        ("mangled hs", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of the First Schedule.— In the Customs Act, 1969 the First Schedule shall be amended as set out below.
78. 38BO5 Chromite sulphate used in the dyeing and tanning process under this schedule of the Act.
3. Income tax.— The rate of income tax shall be the rate specified in this section of the Act.
""", FINANCE, ["1", "2", "3"]),
        ("amendment date", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment.— In the Customs Act, 1969 the following further amendment shall be made by this provision of the Act.
11.10.2018. Substituted by the Finance Act note in the official gazette volume of this amending instrument.
3. Sales tax.— The sales tax shall be charged at the rate specified in this section of the Act for the year.
""", FINANCE, ["1", "2", "3"]),
        ("prose amount", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Payment.— The taxpayer shall pay the said sum, Rs. 4,500, shall— be credited to the Federal Consolidated Fund.
""", FINANCE, ["1", "2"]),
        ("comma penalties", """
11. Appeals.— Any person aggrieved by an order under this Act may appeal within thirty days of the notice.
12, Penalties.— Whoever contravenes this Act shall be punished with fine which may extend to ten thousand rupees.
""", "Example Penalties Act, 1975", ["11", "12"]),
        ("other provisions after hs", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of the First Schedule.— In the Customs Act, 1969 the First Schedule shall be amended as set out below.
32.03 Synthetic organic colouring matter, whether or not chemically defined.
4. Other provisions.— The following provisions of this Act shall apply to the factory and the occupier.
""", FINANCE, ["1", "2", "4"]),
        ("letter short title snaps the following amendment", """
L, Short title and extent.— (1) This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
3, Amendment of Act IV of 1969.— In the Customs Act, 1969 the following further amendment shall be made by this section.
""", FINANCE, ["1", "2"]),
        ("rolled subrow", """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment of the First Schedule.— In the Customs Act, 1969 the First Schedule shall be amended as set out below.
73.15 Chain and parts thereof of iron or steel per metric ton.
4. Rolled but not further worked, of a thickness exceeding one millimetre but less than two millimetres.
3. Sales tax.— The sales tax shall be charged at the rate specified in this section of the Act for the year.
""", FINANCE, ["1", "2", "3"]),
        ("enacted as follows", """
It is hereby enacted as follows:—
This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan and shall come into force at once.
2. Amendment of Act IV of 1969.— In the Customs Act, 1969 the following further amendment shall be made by this section.
3. Sales tax.— The sales tax shall be charged at the rate specified in this section of the Act for the year.
""", FINANCE, ["1", "2", "3"]),
        ("hot work heading", """
1. Short title.— This Act may be called the Example Factories Act, 1975 and it extends to the Province.
2. Amendment of the Schedule.— The schedule of processes shall be amended as set out below in this section of the Act.
73.15 Chain and parts thereof of iron or steel per metric ton.
4. Hot work is prohibited in a factory under this Act.— The occupier shall stop that work at once.
""", "Example Factories Act, 1975", ["1", "2", "4"]),
    ]
    assert len(shapes) == 20
    counts = []
    for name, text, statute, expected in shapes:
        got = [section["section_number"] for section in _sections(text, statute)]
        assert got == expected, name
        counts.append(len(got))
    # 8 shapes of 2, 11 of 3, and the Fin85 five: 16 + 33 + 5.
    assert sum(counts) == 54


def test_checked_in_statute_fixtures_keep_their_section_counts():
    counts = []
    for name, expected in FIXTURE_SECTION_COUNTS.items():
        text = (FIXTURES / name).read_text(errors="replace")
        got = len(split_into_sections(text, name))
        assert got == expected, name
        counts.append(got)
    assert len(FIXTURE_SECTION_COUNTS) == 16
    assert sum(counts) == 131

    extracted = extract_statute_deterministic(
        html=None,
        text=(FIXTURES / "senate_1661312733_937_text_layer.txt").read_text(),
        source_meta={"source_name": "Senate", "url": NWFP_URL, "act_title": NWFP},
    )
    bodies = {section["section_number"]: section["section_text"] for section in extracted["sections"]}
    assert len(extracted["sections"]) == 52
    assert "Appohtmert of ncmbers" in bodies["4"]
    assert "Rargers" not in "\n".join(bodies.values())
