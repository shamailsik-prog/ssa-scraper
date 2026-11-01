"""OCR act headings in senate.gov.pk 1661312733_937.pdf.

The text layer (pdfplumber, the same tolerances as ``pdf_text_layer``) prints
three Acts and none of them match a clean ``ACT No. … of YYYY``:

* ``AC_l _No. XXXTV 1975`` — Rangers XXXIV. ``T`` of ACT is ``l`` with an
  underscore, ``IV`` is ``TV``, and ``of`` was left on the previous line.
* ``ACT No. XXXV on 1975`` — NWFP Urban Planning. ``of`` is ``on``, and the
  preamble is ``.4 Act`` rather than ``An Act``.
* ``{-CT No. XXXVI 1975`` — the next Act. The leading ``A`` is ``{-``, and
  ``of`` is again on the previous line.

A later pdfplumber reading of the same PDF keeps ``or`` on the heading line
(``XXXTV or 1975``). That form is the same f/r slip, so it is a boundary too.

``Acr No. xt or 2015`` (Credit Bureaus) is not one of these slips. A heading
that uses ``on`` but is not followed by an Act preamble is not a boundary.
"""

from __future__ import annotations

from pathlib import Path

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.statute_quality.bundled_gazette import isolate_bundled_act
from scraper.statute_quality.senate_integrity import act_segments

FIXTURE = Path("tests/fixtures/statute_quality/senate_1661312733_937_boundaries.txt")
NWFP = "North-West Frontier Province Urban Planning Act, 1975"
PDF_URL = "https://senate.gov.pk/uploads/documents/1661312733_937.pdf"


def _blob(extracted: dict) -> str:
    return "\n".join(section.get("section_text") or "" for section in extracted["sections"])


def test_nwfp_gazette_ocr_headings_are_three_act_boundaries():
    """The bundled gazette splits into Rangers 34, NWFP 35, and the next Act 36."""
    segments = act_segments(FIXTURE.read_text())
    assert [segment.act_number for segment in segments] == [(34, 1975), (35, 1975), (36, 1975)]
    assert "Rargers" in segments[0].text
    assert "Rargers" not in segments[1].text
    assert "Pakistan Rangers" not in segments[1].text
    assert "Urban Planning Act, 1975" in segments[1].text
    assert "12) \" Board \"" in segments[1].text
    assert "Public Servicc Commission" in segments[2].text
    assert "Public Servicc Commission" not in segments[1].text
    # The citation inside the third Act is not a fourth boundary.
    assert "Act No. XD(" in segments[2].text


def test_nwfp_title_and_pipe_both_drop_rangers_and_the_later_act():
    text = FIXTURE.read_text()
    by_title = isolate_bundled_act(text, target_name=NWFP, source_url=PDF_URL)
    assert by_title.status == "isolated"
    assert by_title.act_year == 1975
    assert by_title.act_label == "35 of 1975"
    assert "Urban Planning Act, 1975" in by_title.text
    assert "Rargers" not in by_title.text
    assert "Public Servicc Commission" not in by_title.text

    by_pipe = isolate_bundled_act(text, target_name=NWFP, source_url=f"{PDF_URL}|XXXV of 1975")
    assert by_pipe.status == "isolated"
    assert by_pipe.act_label == "XXXV of 1975"
    assert "Rargers" not in by_pipe.text
    assert by_pipe.text == by_title.text


def test_nwfp_extract_keeps_urban_planning_and_drops_rangers():
    extracted = extract_statute_deterministic(
        html=None,
        text=FIXTURE.read_text(),
        source_meta={
            "source_name": "Senate",
            "url": PDF_URL,
            "act_title": NWFP,
        },
    )
    blob = _blob(extracted)
    assert "bundled_gazette_unresolved" not in extracted["field_evidence"]
    assert extracted["field_evidence"].get("bundled_gazette_isolated") == "35 of 1975"
    assert "Urban Planning Act, 1975" in blob
    assert "Rargers" not in blob
    assert "Pakistan Rangers" not in blob
    assert "Public Servicc Commission" not in blob
    assert extracted["sections"]
    assert extracted["sections"][0]["section_number"] == "1"


def test_same_line_or_is_the_of_preposition_on_these_headings():
    """``extract_text()`` without the pipeline tolerance keeps ``or`` on the line."""
    text = """
AC_l _No. XXXTV or 1975
An Act turth to amend the West Pakistan Rangers Ordinance, 1959.
1. Short title.—This Act may be called the West Pakistan Rangers (Amendment) Act, 1975.
2. General amendment.—In the West Pakistan Rargers Ordinance, 1959, for the words West Pakistan Rangers the words Pakistan Rangers shall be substituted.

ACT No. XXXV on 1975
.4 Act ao establish a system of urban planning for the North-West Frontier Province.
1. Short title.—This Act may be called the North-West Frontier Province Urban Planning Act, 1975.
2. Definitions.—Board means the Provincial Urban Development Board.

{-CT No. XXXVI or 1975
An Act to amend the North-West Frontier Province Public Service Commission Act, 1973.
1. Short title.—This Act may be called the North-West Frontier Province Public Service Commission (Amendment) Act, 1975.
"""
    segments = act_segments(text)
    assert [segment.act_number for segment in segments] == [(34, 1975), (35, 1975), (36, 1975)]
    assert "Rargers" not in segments[1].text
    assert "Urban Planning Act, 1975" in segments[1].text


def test_credit_bureaus_acr_no_xt_is_not_an_act_boundary():
    """``Acr No. xt or 2015`` stays outside this OCR budget."""
    text = """
ACT No. XI of 2015
An Act to provide for the regulation of credit bureaus.
1. Short title.—This Act may be called the Credit Bureaus Act, 2015.
2. Definitions.—Acr No. xt or 2015 is how the scan printed the heading above, and the following words stay in this section.
An Act to provide for something else is a quotation here, not a new statute.
3. Duty.—A credit bureau shall keep the record required by this Act.
"""
    segments = act_segments(text)
    assert len(segments) == 1
    assert segments[0].act_number == (11, 2015)
    assert "credit bureau shall keep" in segments[0].text


def test_bare_ct_no_is_not_an_act_boundary():
    """Deleting the leading A is not enough. ``{-CT`` needs the punctuation."""
    text = """
ACT No. I of 1973
An Act to provide for an example.
1. Short title.—This Act may be called the Example Act, 1973.
2. Note.—The schedule stops here.
CT No. II of 1974
An Act to amend the Example Act, 1973.
3. Duty.—The authority shall publish the schedule.
"""
    segments = act_segments(text)
    assert len(segments) == 1
    assert "publish the schedule" in segments[0].text


def test_on_for_of_without_an_act_preamble_is_not_a_boundary():
    text = """
ACT No. I of 1973
An Act to provide for an example.
1. Short title.—This Act may be called the Example Act, 1973.
2. Definitions.—The schedule cites ACT No. XXXV on 1975 and then continues with the words of this section alone.
3. Duty.—The authority shall publish the schedule.
"""
    segments = act_segments(text)
    assert len(segments) == 1
    assert "publish the schedule" in segments[0].text
