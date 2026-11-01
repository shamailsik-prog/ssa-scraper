"""Real pdfplumber extracts of two Balochistan gazette acts.

Animal Slaughter: https://pabalochistan.gov.pk/storage/9211/69f5e71f96d71_Balochistan-Animal-Slaughter-Control-ACT-2025.pdf
Beggary: https://pabalochistan.gov.pk/storage/9217/69f89c006bc9c_The-Balochistan-Prevention-of-Beggary-Act-2026.pdf

Each fixture is ``_clean_text`` of ``_pdfplumber_extract`` (extract_text with
x_tolerance=2, y_tolerance=2). Counts below are that extract run through
``split_into_sections``, not a shortened sample.
"""

from pathlib import Path

from scraper.parsers.statute_parser import split_into_sections
from scraper.statute_quality.section_reextract import gate_section_reextract

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statute_quality"
ANIMAL = _FIXTURES / "balochistan_animal_slaughter_control_2025_pdfplumber.txt"
BEGGARY = _FIXTURES / "balochistan_prevention_of_beggary_2025_pdfplumber.txt"


def _numbers(text: str, name: str) -> list[str]:
    return [section["section_number"] for section in split_into_sections(text, name)]


def _nine_real_sections():
    return [
        {
            "section_number": str(number),
            "section_text": (
                f"{number}. This provision shall apply and is not a distribution-list designation."
            ),
        }
        for number in range(1, 10)
    ]


def test_animal_slaughter_extract_splits_into_sections_1_through_15():
    text = ANIMAL.read_text(encoding="utf-8")
    sections = split_into_sections(text, "THE BALOCHISTAN ANIMAL SLAUGHTER CONTROL ACT, 2025")
    numbers = [section["section_number"] for section in sections]
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert numbers == [str(number) for number in range(1, 16)]
    assert "may be called" in bodies["1"]
    assert "extends to the whole" in bodies["1"]
    assert "come into force" in bodies["1"]
    assert max(len(section["section_text"]) for section in sections) < 5000

    gate = gate_section_reextract(_nine_real_sections(), sections)
    assert gate.verdict == "PASS"
    assert gate.reason == ""
    assert gate.section_count_before == 9
    assert gate.section_count_after == 15


def test_beggary_extract_splits_into_sections_1_through_21():
    text = BEGGARY.read_text(encoding="utf-8")
    sections = split_into_sections(text, "The Balochistan Prevention of Beggary Act, 2025")
    numbers = [section["section_number"] for section in sections]
    bodies = {section["section_number"]: section["section_text"] for section in sections}

    assert numbers == [str(number) for number in range(1, 22)]
    assert "1" in numbers
    assert "may be called" in bodies["1"]
    assert "Prevention of Beggary" in bodies["1"]
    assert max(len(section["section_text"]) for section in sections) < 5000
