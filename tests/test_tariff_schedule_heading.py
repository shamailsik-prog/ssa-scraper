"""HS and services schedule rows must not become Act sections.

Auditor 2026-11-02. Finance Acts and sales-tax statutes print tariff lines
``32.03 Synthetic organic…``, ``08.05 Nuts…``, and ``98.01 hotels…``. The
line-start splitter takes the first dot as the end of a section number, so
those rows are stored as sections 32, 08, and 98 and hide the real headings.
OCR reads the same rows as ``0E`` / ``0I`` plus the tariff text.

``7.30 p.m.`` and ``11.10.2018`` share the NN.NN shape and are not schedule
rows. A real ``7.`` or ``32.`` heading, and a subsection ``(1)``, stay.
"""

from scraper.parsers.statute_parser import (
    section_text_starts_with_tariff_schedule_row,
    split_into_sections,
)

FINANCE = "Finance Act, 1985"
FACTORIES = "Balochistan Factories Act, 2021"


def _by_number(text: str, name: str) -> dict:
    sections = split_into_sections(text, name)
    return {section["section_number"]: section["section_text"] for section in sections}


def test_hs_and_services_rows_are_schedule_content_not_sections():
    text = """
1. Short title, extent and commencement.— (1) This Act may be called the Finance Act, 1985. (2) It extends to the whole of Pakistan. (3) It shall come into force at once.
2. Amendment of the First Schedule.— In the Customs Act, 1969, the First Schedule shall be amended in the manner appearing below.
32.03 Synthetic organic colouring matter, whether or not chemically defined.
08.05 Nuts, fresh or dried, whether or not shelled or peeled.
98.01 hotels
98.01 Services provided by hotels, restaurants and marriage halls.
7. Income tax.— (1) The tax shall be charged in the manner provided in this section of the Act. (2) The rate shall be the rate set out for the year.
32. Definitions.— In this Act, unless there is anything repugnant in the subject or context, tax means the tax under this Act.
"""
    bodies = _by_number(text, FINANCE)

    assert "08" not in bodies
    assert "98" not in bodies
    assert "32.03" not in bodies
    assert "08.05" not in bodies
    assert "98.01" not in bodies
    assert "Income tax" in bodies["7"]
    assert "(1) The tax shall be charged" in bodies["7"]
    assert "(2) The rate shall be the rate" in bodies["7"]
    assert "Definitions" in bodies["32"]
    assert "tax means the tax" in bodies["32"]
    schedule = bodies["2"]
    assert "Synthetic organic colouring matter" in schedule
    assert "Nuts, fresh or dried" in schedule
    assert "98.01 hotels" in schedule
    assert "marriage halls" in schedule


def test_tariff_code_on_its_own_line_stays_schedule_content():
    text = """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Customs.— In the Customs Act, 1969, in the First Schedule, the following further amendments shall be made.
32.03.
Synthetic organic colouring matter, whether or not chemically defined, and preparations based thereon.
3. Sales tax.— The sales tax shall be charged at the rate specified in this section of the Act for the year.
"""
    bodies = _by_number(text, FINANCE)
    assert "32" not in bodies
    assert "Synthetic organic colouring matter" in bodies["2"]
    assert "Sales tax" in bodies["3"]


def test_ocr_chapter_tokens_with_tariff_text_are_schedule_content():
    text = """
1. Short title.— This Act may be called the Finance Act, 1974 and it extends to the whole of Pakistan.
2. Amendment of customs duties.— The First Schedule to the Customs Act, 1969 shall be amended as set out below.
0E.05 Nuts, fresh or dried, whether or not shelled or peeled.
0I.01 hotels, restaurants and marriage halls.
0E. 08.05 Coconuts, Brazil nuts and cashew nuts, fresh or dried.
0I. 98.01 Services provided by hotels, restaurants and caterers.
3. Income tax.— The rate of income tax shall be the rate specified in this section of the Act.
"""
    bodies = _by_number(text, "Finance Act, 1974")
    assert "0E" not in bodies
    assert "0I" not in bodies
    assert "08" not in bodies
    assert "98" not in bodies
    schedule = bodies["2"]
    assert "Nuts, fresh or dried" in schedule
    assert "Coconuts, Brazil nuts" in schedule
    assert "marriage halls" in schedule
    assert "Services provided by hotels" in schedule
    assert "Income tax" in bodies["3"]


def test_real_integer_headings_are_not_tariff_rows():
    text = """
1. Short title.— This Act may be called the Example Act, 2021 and it extends to the whole of the Province.
7. Hours.— The daily hours of work shall not exceed the hours specified in this section of the Act.
08. Weekly rest.— Every worker shall be allowed one holiday in each week under this section of the Act.
32. Definitions.— In this Act, unless there is anything repugnant in the subject or context, factory means the premises.
"""
    bodies = _by_number(text, FACTORIES)
    assert "Hours" in bodies["7"]
    assert "Weekly rest" in bodies["08"]
    assert "factory means the premises" in bodies["32"]
    assert not section_text_starts_with_tariff_schedule_row(bodies["7"])
    assert not section_text_starts_with_tariff_schedule_row(bodies["08"])
    assert not section_text_starts_with_tariff_schedule_row(bodies["32"])


def test_factories_time_of_day_is_not_a_tariff_schedule_row():
    line = "7.30 p.m. the sitting of the Assembly shall stand adjourned until the next day of the session."
    assert section_text_starts_with_tariff_schedule_row(line) is False
    assert section_text_starts_with_tariff_schedule_row("7.30 p.m.") is False
    assert section_text_starts_with_tariff_schedule_row("7.30 a.m. work in the factory shall stop for the day.") is False
    text = """
6. Hours of work.— No adult worker shall be required to work in a factory for more than nine hours in any day under this section.
7. Weekly hours.— No adult worker shall be required or allowed to work in a factory for more than forty-eight hours in any week.
7.30 p.m. the sitting of the Assembly shall stand adjourned until the next day of the session.
8. Overtime.— Overtime shall be paid at twice the ordinary rate of wages for the overtime worked under this section.
"""
    bodies = _by_number(text, FACTORIES)
    assert "Weekly hours" in bodies["7"]
    assert "7.30 p.m." not in bodies["7"]
    assert "7.30 p.m." not in bodies["6"]
    assert "Overtime" in bodies["8"]


def test_reconcile_does_not_store_an_hs_code_as_a_section_number():
    """An engine that returns the heading token ``32.03`` must not add a section.

    The line-start splitter stores the chapter (``32``) and the row text. A
    second pass accepts ``^[0-9]{1,2}\\.[0-9]{2}$`` when that token is at the
    start of a line. Both forms stay schedule text.
    """
    from scraper.extractors.validation import reconcile_statute

    raw = (
        "1. Short title. This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.\n"
        "32.03 Synthetic organic colouring matter, whether or not chemically defined.\n"
        "0E. 08.05 Nuts, fresh or dried, whether or not shelled or peeled.\n"
        "7.30 p.m. the sitting of the Assembly shall stand adjourned until the next day.\n"
    )
    deterministic = {
        "statute_name": "Finance Act, 1985",
        "sections": [
            {
                "section_number": "1",
                "section_text": "1. Short title. This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.",
            }
        ],
        "extractor_confidence": 0.9,
    }
    ai = {
        "sections": [
            {
                "section_number": "32.03",
                "section_text": "Synthetic organic colouring matter, whether or not chemically defined.",
            },
            {
                "section_number": "0E",
                "section_text": "08.05 Nuts, fresh or dried, whether or not shelled or peeled.",
            },
            {
                "section_number": "7.30",
                "section_text": "p.m. the sitting of the Assembly shall stand adjourned until the next day.",
            },
        ],
        "extractor_confidence": 0.9,
    }
    outcome = reconcile_statute(deterministic=deterministic, ai=ai, raw_text=raw, min_confidence=0.2)
    numbers = [section["section_number"] for section in outcome.data["sections"]]
    assert "32.03" not in numbers
    assert "0E" not in numbers
    # A clock token is not a tariff row. This guard must not drop it.
    assert "7.30" in numbers
    assert "Synthetic organic" not in outcome.data["sections"][0]["section_text"]


def test_amendment_date_is_not_a_tariff_schedule_row():
    line = "11.10.2018. Substituted by the Finance Act note in the official gazette volume of this amending instrument."
    assert section_text_starts_with_tariff_schedule_row(line) is False
    assert section_text_starts_with_tariff_schedule_row("2.03.2007. In subsection (1) for the words the Bank the following shall be read.") is False
    text = """
1. Short title.— This Act may be called the Finance Act, 1985 and it extends to the whole of Pakistan.
2. Amendment.— In the Customs Act, 1969 the following further amendment shall be made by this provision of the Act.
11.10.2018. Substituted by the Finance Act note in the official gazette volume of this amending instrument.
3. Sales tax.— The sales tax shall be charged at the rate specified in this section of the Act for the year.
"""
    bodies = _by_number(text, FINANCE)
    assert "11" not in bodies
    assert "2018" not in bodies
    assert "official gazette volume" in bodies["2"]
    assert "Sales tax" in bodies["3"]
