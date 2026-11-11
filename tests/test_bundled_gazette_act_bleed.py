"""Bundled senate/NA gazettes must not bleed a neighbouring Act into the marked one.

The URL suffix ``|XXVII of 1973`` is the Act to keep. A second ``ACT No.`` header,
including an OCR form such as ``)O(Vlt``, ends that Act. A price / Ex. Gaz. footer
is not a section. A chrome title, a Federal row with no year, or a marker that is
not in the file produces no statute sections.
"""

from __future__ import annotations

from scraper.document_validation import validate_statute_reconciled_json
from scraper.extractors.deterministic import extract_statute_deterministic

TRIBAL_AREAS = (
    "The Supreme Court and High Court (Extension of Jurisdiction to certain Tribal Areas) Act, 1973"
)
TRIBAL_URL = "https://senate.gov.pk/uploads/documents/1660546032_217.pdf|XXVII of 1973"

# p1 is Act XXIII, cut off at the price footer. p2 is Act XXVII ss.1–4, then
# Act XXVIII whose heading the scan stored as ``ACT No. )O(Vlt``.
TRIBAL_GAZETTE = """
ACT No. XXIII of 1973
An Act further to amend the West Pakistan Industrial and Commercial Employment (Standing Orders) Ordinance, 1968.
1. Short title and commencement.—This Act may be called the West Pakistan Industrial and Commercial Employment (Standing Orders) (Amendment) Act, 1973.
2. Amendment of section 1.—In the West Pakistan Ordinance No. VI of 1968, in section 1, in clause (a), the words standing orders shall be substituted and the clause continues onto the next sheet.
Price Rs. 1 [2940 Ex. Gaz.]
(59)

ACT No. XXVII of 1973
An Act to extend the jurisdiction of the Supreme Court and the High Court to certain Tribal Areas.
1. Short title and commencement.—(1) This Act may be called the Supreme Court and High Court (Extension of Jurisdiction to certain Tribal Areas) Act, 1973.
(2) It shall come into force at once.
2. Extension of Jurisdiction of Supreme Court.—The Supreme Court of Pakistan shall have the same jurisdiction in the Provincially Administered Tribal Areas of Chitral, Dir, Kalam, Swat and Malakand Protected Area as it has in the other areas of the Province.
3. Extension of Jurisdiction of High Court.—The Peshawar High Court shall have the same jurisdiction in the areas specified in section 2 as it has in the other areas of the Province.
4. Repeal.—The Supreme Court and High Court (Extension of Jurisdiction to Tribal Areas) Order, 1970 (P.O. No. 28 of 1970), is hereby repealed.
ACT No. )O(Vlt oF
1973
An Act to reconstitute and reorganize the University of Islamabad.
1. Short title.—This Act may be called the University of Islamabad Act, 1973.
2. Definitions.—Academic Council means the Academic Council of the University and Authority means an authority of the University.
"""

LEGAL_ADVISERS = "The Companies (Appointment of Legal Advisers) (Amendment) Act, 1975"
LEGAL_URL = "https://senate.gov.pk/uploads/documents/1661160371_710.pdf|LXXXI of 1975"

LEGAL_GAZETTE = """
ACT No. LXXVII of 1975
An Act to provide for the constitution and regulation of the Airports Security Force.
1. Short title, extent, application and commencement.—This Act may be called the Airports Security Force Act, 1975.
Price Ps. 25 [7149 Ex. Gaz.]

ACT No. LXXX of 1975
An Act further to amend the House Building Finance Corporation Act, 1952.
1. Short title and commencement.—This Act may be called the House Building Finance Corporation (Amendment) Act, 1975.
2. Amendment of section 1, Act XVIII of 1952.—In the House Building Finance Corporation Act, 1952, in section 1, for the words West Pakistan the words Federal Government shall be substituted.
3. Amendment of section 25, Act XVIII of 1952.—In the House Building Finance Corporation Act, 1952, in section 25, the rupee formula shall be the amount the Federal Government notifies.
4. Amendment of section 35, Act XVIII of 1952.—In the House Building Finance Corporation Act, 1952, in section 35, for the word two the word four shall be substituted.
ACT No. LXXXI of 1975
An Act to amend the Companies (Appointment of Legal Advisers) Act, 1974.
1. Short title and commencement.—(1) This Act may be called the Companies (Appointment of Legal Advisers) (Amendment) Act, 1975.
(2) It shall come into force at once.
2. Amendment of section 2, Act X of 1974.—In the Companies (Appointment of Legal Advisers) Act, 1974, in section 2, in clause (b), for the words ten lakh the words five lakh shall be substituted.
3. Repeal.—The Companies (Appointment of Legal Advisers) (Amendment) Ordinance, 1975 (XI of 1975), is hereby repealed.
"""

MALARIA = "The Malaria Eradication Board (Repeal) Act, 1975"
MALARIA_URL = "https://senate.gov.pk/uploads/documents/1661162484_476.pdf|LXIV of 1975"

MALARIA_GAZETTE = """
ACT No. LXI of 1975
An Act further to amend the Employees' Cost of Living (Relief) Act, 1973.
1. Short title and commencement.—This Act may be called the Employees' Cost of Living (Relief) (Amendment) Act, 1975.
2. Amendment of section 3.—In the Employees' Cost of Living (Relief) Act, 1973, in section 3, the relief rate shall be the rate the Federal Government notifies for that year.
3. Repeal.—The Employees' Cost of Living (Relief) (Amendment) Ordinance, 1975, is hereby repealed.
Price Ps. 37 [8102 Ex. Gaz.]

ACT No. LXIV of 1975
An Act to repeal the Malaria Eradication Board.
1. Short title and commencement.—(1) This Act may be called the Malaria Eradication Board (Repeal) Act, 1975.
(2) It shall come into force at once.
2. Repeal and transfer.—The Malaria Eradication Board Ordinance, 1961, is hereby repealed, and the Federal Government may transfer the Board's assets and make provision for removal of difficulties arising from that repeal.
"""

TRAVEL = "The Travel Agencies Act, 1976"
TRAVEL_URL = "https://senate.gov.pk/uploads/documents/1660626546_441.pdf|XXX of 1976"

TRAVEL_GAZETTE = """
ACT No. XXVI of 1976
An Act to provide for the regulation of tourist guides.
1. Short title, extent and commencement.—This Act may be called the Pakistan Tourist Guides Act, 1976.
2. Definitions.—Guide means a person licensed under this Act to conduct tourists and who holds a valid tourist guide licence.

The Seed Analyst shall submit the report of the sample to the Federal Seed Certification authority before the next act begins.

ACT No. )O(X of 1976
An Act to provide for the regulation of travel agencies.
1. Short title, extent and commencement.—(1) This Act may be called the Travel Agencies Act, 1976.
(2) It shall come into force at once.
2. Definitions.—Travel agency means a person engaged in arranging tours for members of the public for hire.
3. Licence.—No person shall operate a travel agency without a licence issued under this Act by the Federal Government.
"""

FOLLOWING_ACT_GAZETTE = """
The following Act
ACT No. XXX of 2017
An Act to provide for compulsory teaching of the Holy Quran to Muslim students.
1. Short title and commencement.—This Act may be called the Compulsory Teaching of the Holy Quran Act, 2017.
2. Application.—This Act applies to Muslim students in educational institutions established by the Federal Government.

ACT No. II of 2013
An Act to provide for the regulation of trade organisations.
1. Short title, extent and commencement.—This Act may be called the Trade Organizations Act, 2013.
2. Definitions.—In this Act, trade organisation means an association of traders licensed by the Regulator.
"""

ALIEN_GAZETTE = """
ACT No. XLVI of 2026
An Act to amend the Financial Institutions (Recovery of Finances) Ordinance, 2001.
1. Short title.—This Act may be called the Financial Institutions (Recovery of Finances) (Amendment) Act, 2026.
2. Amendment.—In the said Ordinance, in section 2, the recovery of finances shall be read with this amendment and no other instrument.
"""

NA_CHROME_URL = (
    "https://na.gov.pk/uploads/documents/6a87f616e19a1_320.pdf"
    "|Share Print Acts of Parliament Filter by"
)


def _extract(text: str, *, url: str, title: str, source_name: str = "Senate") -> dict:
    return extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={"source_name": source_name, "url": url, "act_title": title, "jurisdiction": "Federal"},
    )


def _blob(extracted: dict) -> str:
    return "\n".join(section.get("section_text") or "" for section in extracted["sections"])


def _numbers(extracted: dict) -> set[str]:
    return {str(section.get("section_number")) for section in extracted["sections"]}


def test_xxvii_keeps_tribal_areas_sections_1_to_4_and_drops_xxviii_after_the_repeal():
    extracted = _extract(TRIBAL_GAZETTE, url=TRIBAL_URL, title=TRIBAL_AREAS)
    blob = _blob(extracted)
    by_num = {str(section["section_number"]): section["section_text"] for section in extracted["sections"]}
    assert "Tribal Areas" in (extracted["statute_name"] or "")
    assert extracted["year_enacted"] == 1973
    assert {"1", "2", "3", "4"} <= _numbers(extracted)
    assert "Chitral" in by_num["2"]
    assert "Malakand" in by_num["2"]
    assert "Peshawar High Court" in by_num["3"]
    assert "is hereby repealed" in by_num["4"]
    assert "University of Islamabad" not in by_num["4"]
    assert "University of Islamabad" not in blob
    assert "Standing Orders" not in blob
    assert "Academic Council" not in blob
    assert "59" not in _numbers(extracted)
    assert "Ex. Gaz" not in blob
    assert "Price Rs" not in blob
    assert extracted["field_evidence"].get("bundled_gazette_unresolved") is None
    assert extracted["sections"]


def test_lxxxi_keeps_legal_advisers_and_drops_house_building_and_airports():
    extracted = _extract(LEGAL_GAZETTE, url=LEGAL_URL, title=LEGAL_ADVISERS)
    blob = _blob(extracted)
    assert "Legal Advisers" in (extracted["statute_name"] or "")
    assert {"1", "2", "3"} <= _numbers(extracted)
    assert "five lakh" in blob
    assert "Ordinance, 1975 (XI of 1975)" in blob
    assert "House Building" not in blob
    assert "Airports Security" not in blob
    assert "Ex. Gaz" not in blob
    assert "four shall be substituted" not in blob
    assert extracted["field_evidence"].get("bundled_gazette_unresolved") is None


def test_malaria_lxiv_drops_the_cola_act_and_the_price_footer():
    extracted = _extract(MALARIA_GAZETTE, url=MALARIA_URL, title=MALARIA)
    blob = _blob(extracted)
    by_num = {str(section["section_number"]): section["section_text"] for section in extracted["sections"]}
    assert "Malaria Eradication Board (Repeal) Act, 1975" in by_num["1"]
    assert "removal of difficulties" in by_num["2"]
    assert "Cost of Living" not in blob
    assert "Ex. Gaz" not in blob
    assert "37" not in _numbers(extracted)
    assert extracted["field_evidence"].get("bundled_gazette_unresolved") is None


def test_travel_agencies_xxx_is_read_from_the_ocr_header_not_tourist_guides():
    extracted = _extract(TRAVEL_GAZETTE, url=TRAVEL_URL, title=TRAVEL)
    blob = _blob(extracted)
    by_num = {str(section["section_number"]): section["section_text"] for section in extracted["sections"]}
    assert "Travel Agencies Act, 1976" in by_num["1"]
    assert "arranging tours" in by_num["2"]
    assert "operate a travel agency" in blob
    assert "Tourist Guides" not in blob
    assert "Seed Analyst" not in blob
    assert extracted["field_evidence"].get("bundled_gazette_unresolved") is None


def test_following_act_null_year_mix_emits_no_sections():
    extracted = _extract(
        FOLLOWING_ACT_GAZETTE,
        url="https://na.gov.pk/uploads/documents/6683825a6969a_538.pdf",
        title="The following Act",
        source_name="NationalAssembly",
    )
    assert extracted["sections"] == []
    assert "Holy Quran" not in _blob(extracted)
    assert "Trade Organizations" not in _blob(extracted)
    assert extracted["year_enacted"] is None or extracted["sections"] == []


def test_marker_missing_from_the_pdf_fails_closed_with_no_output():
    extracted = _extract(
        ALIEN_GAZETTE,
        url="https://na.gov.pk/uploads/documents/6a87f616e19a1_320.pdf|XXII of 1975",
        title="The Constitution (Third Amendment) Act, 1975",
        source_name="NationalAssembly",
    )
    assert extracted["sections"] == []
    assert "Financial Institutions" not in _blob(extracted)
    assert "recovery of finances" not in _blob(extracted)
    assert extracted["field_evidence"].get("bundled_gazette_marker_absent")
    assert extracted["field_evidence"].get("bundled_gazette_unresolved")


def test_section_text_is_cut_at_the_next_act_header_and_at_the_gazette_footer():
    from scraper.statute_quality.bundled_gazette import clip_section_text

    bleed = (
        "4. Repeal.—The Supreme Court and High Court (Extension of Jurisdiction to Tribal Areas) "
        "Order, 1970 (P.O. No. 28 of 1970), is hereby repealed.\n"
        "ACT No. )O(Vlt oF\n"
        "1973\n"
        "An Act to reconstitute and reorganize the University of Islamabad.\n"
        "1. Short title.—This Act may be called the University of Islamabad Act, 1973."
    )
    clipped = clip_section_text(bleed)
    assert "is hereby repealed" in clipped
    assert "University of Islamabad" not in clipped
    assert "ACT No." not in clipped

    footer = (
        "2. Repeal and transfer.—The Malaria Eradication Board Ordinance, 1961, is hereby repealed, "
        "and the Federal Government may transfer the Board's assets.\n"
        "Price Ps. 37 [8102 Ex. Gaz.]"
    )
    clipped_footer = clip_section_text(footer)
    assert "transfer the Board's assets" in clipped_footer
    assert "Ex. Gaz" not in clipped_footer
    assert "Price Ps" not in clipped_footer


def test_validator_rejects_chrome_titles_null_federal_year_marker_mismatch_and_footer_sections():
    tribal_s1 = (
        "1. Short title.—This Act may be called the Supreme Court and High Court "
        "(Extension of Jurisdiction to certain Tribal Areas) Act, 1973."
    )
    chrome_following = {
        "statute_name": "The following Act",
        "year_enacted": None,
        "jurisdiction": "Federal",
        "sections": [
            {"section_number": "1", "section_text": "1. Short title.—This Act may be called the Compulsory Teaching of the Holy Quran Act, 2017."},
            {"section_number": "2", "section_text": "2. Definitions.—In this Act, trade organisation means an association of traders licensed by the Regulator."},
        ],
    }
    ok, reason = validate_statute_reconciled_json(
        chrome_following,
        raw_text=FOLLOWING_ACT_GAZETTE,
        source_name="NationalAssembly",
        source_url="https://na.gov.pk/uploads/documents/6683825a6969a_538.pdf",
    )
    assert not ok
    assert reason == "gazette_chrome_title"

    orders = {
        "statute_name": "President's Orders and Regulations",
        "year_enacted": None,
        "jurisdiction": "Federal",
        "sections": [
            {"section_number": "1", "section_text": "1. Short title.—This Act may be called the Apostille Act, 2024."},
        ],
    }
    ok, reason = validate_statute_reconciled_json(
        orders,
        raw_text="President's Orders and Regulations",
        source_name="NationalAssembly",
        source_url=NA_CHROME_URL,
    )
    assert not ok
    assert reason == "gazette_chrome_title"

    suffix_only = {
        "statute_name": TRIBAL_AREAS,
        "year_enacted": 1973,
        "jurisdiction": "Federal",
        "sections": [{"section_number": "1", "section_text": tribal_s1}],
    }
    ok, reason = validate_statute_reconciled_json(
        suffix_only,
        raw_text=tribal_s1,
        source_name="NationalAssembly",
        source_url=NA_CHROME_URL,
    )
    assert not ok
    assert reason == "gazette_chrome_title"

    null_year = {
        "statute_name": "The Trade Organizations Act, 2013",
        "year_enacted": None,
        "jurisdiction": "Federal",
        "sections": [
            {"section_number": "1", "section_text": "1. Short title.—This Act may be called the Trade Organizations Act, 2013."},
        ],
    }
    ok, reason = validate_statute_reconciled_json(
        null_year,
        raw_text="Trade Organizations Act, 2013",
        source_name="Senate",
        source_url="https://senate.gov.pk/uploads/documents/1656907762_387.pdf|II of 2013",
    )
    assert not ok
    assert reason == "federal_null_year"

    marker_mismatch = {
        "statute_name": MALARIA,
        "year_enacted": 1975,
        "jurisdiction": "Federal",
        "sections": [
            {
                "section_number": "1",
                "section_text": (
                    "1. Short title.—This Act may be called the Employees' Cost of Living "
                    "(Relief) (Amendment) Act, 1975."
                ),
            },
        ],
    }
    ok, reason = validate_statute_reconciled_json(
        marker_mismatch,
        raw_text=MALARIA_GAZETTE,
        source_name="Senate",
        source_url=MALARIA_URL,
    )
    assert not ok
    assert reason == "marker_s1_mismatch"

    second_header = {
        "statute_name": TRIBAL_AREAS,
        "year_enacted": 1973,
        "jurisdiction": "Federal",
        "sections": [
            {"section_number": "1", "section_text": tribal_s1},
            {
                "section_number": "4",
                "section_text": (
                    "4. Repeal.—The Supreme Court and High Court (Extension of Jurisdiction to Tribal Areas) "
                    "Order, 1970 (P.O. No. 28 of 1970), is hereby repealed. "
                    "ACT No. XXVIII of 1973 An Act to reconstitute and reorganize the University of Islamabad."
                ),
            },
        ],
    }
    ok, reason = validate_statute_reconciled_json(
        second_header,
        raw_text=TRIBAL_GAZETTE,
        source_name="Senate",
        source_url=TRIBAL_URL,
    )
    assert not ok
    assert reason == "bundled_gazette_act_bleed"

    footer_section = {
        "statute_name": TRIBAL_AREAS,
        "year_enacted": 1973,
        "jurisdiction": "Federal",
        "sections": [
            {"section_number": "1", "section_text": tribal_s1},
            {"section_number": "59", "section_text": "Prlce Rs. I i2940 Ex. Gaz. (59)"},
        ],
    }
    ok, reason = validate_statute_reconciled_json(
        footer_section,
        raw_text=TRIBAL_GAZETTE,
        source_name="Senate",
        source_url=TRIBAL_URL,
    )
    assert not ok
    assert reason == "gazette_footer_or_title_as_section"

    title_as_section = {
        "statute_name": TRIBAL_AREAS,
        "year_enacted": 1973,
        "jurisdiction": "Federal",
        "sections": [
            {"section_number": "1", "section_text": tribal_s1},
            {"section_number": "2", "section_text": "The following Act"},
        ],
    }
    ok, reason = validate_statute_reconciled_json(
        title_as_section,
        raw_text=TRIBAL_GAZETTE,
        source_name="Senate",
        source_url=TRIBAL_URL,
    )
    assert not ok
    assert reason == "gazette_footer_or_title_as_section"


def test_a_clean_marked_act_still_passes_validation():
    extracted = _extract(TRIBAL_GAZETTE, url=TRIBAL_URL, title=TRIBAL_AREAS)
    ok, reason = validate_statute_reconciled_json(
        extracted,
        raw_text=TRIBAL_GAZETTE,
        source_name="Senate",
        source_url=TRIBAL_URL,
    )
    assert ok, reason
    assert reason == ""


def test_preview_reextract_command_defaults_to_writing_nothing():
    from uuid import UUID

    from scraper.tasks.bundled_gazette_acts import (
        CONFIRMED_BLEED_STATUTE_IDS,
        CONFIRMED_PDF_SHA256,
        build_parser,
    )

    args = build_parser().parse_args(["--preview-reextract", "--skip-fetch"])
    assert args.preview_reextract is True
    assert args.apply is False
    assert args.undo is None
    assert len(CONFIRMED_BLEED_STATUTE_IDS) >= 5
    full_ids = {
        UUID("eef57038-13dc-4b4d-b050-14cb7d490e44"): "dbdcb76b792e4a127987d275347cf989ed37bb158438455a6ea6c0971dfa3e22",
        UUID("eb8f8ae0-727a-464b-9786-db0247eca896"): "dbdcb76b792e4a127987d275347cf989ed37bb158438455a6ea6c0971dfa3e22",
        UUID("d7d3187e-9ebd-4782-aa9a-228b40d2dd96"): "03cee95b52dbd3ae1f96058937f90c945e7c48634f120da64222f4e4a1c8c616",
        UUID("f700f160-4bf3-41df-9ec5-b052b5627f77"): "03cee95b52dbd3ae1f96058937f90c945e7c48634f120da64222f4e4a1c8c616",
        UUID("17f283f3-b875-4c5d-b3ed-6a235d9ebd21"): "f9df01dc3d558ac22b082e634a1e36973d1a7e84e59ac8735e109c9ab94d8c4d",
        UUID("df206e70-1114-4676-8eec-c77fa051f268"): "f9df01dc3d558ac22b082e634a1e36973d1a7e84e59ac8735e109c9ab94d8c4d",
        UUID("65c5b7b3-674c-4726-8372-b068527cea90"): "478655ccc6f6fd1eee8f911ed1d4bedd00950b3855ba6f17ee094a7c818d2bfe",
        UUID("768e086d-329e-42d9-ade3-01900badfb8e"): "478655ccc6f6fd1eee8f911ed1d4bedd00950b3855ba6f17ee094a7c818d2bfe",
    }
    assert set(full_ids) <= set(CONFIRMED_BLEED_STATUTE_IDS)
    for statute_id, sha in full_ids.items():
        assert CONFIRMED_PDF_SHA256[statute_id] == sha


def _fixture(name: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent / "fixtures" / "bundled_gazette" / name).read_text(encoding="utf-8")


def test_real_s217_text_does_not_yield_university_sections_for_xxvii():
    text = _fixture("s217.txt")
    url = "https://senate.gov.pk/uploads/documents/1660546032_217.pdf|XXVII of 1973"
    extracted = _extract(text, url=url, title=TRIBAL_AREAS)
    blob = _blob(extracted)
    assert extracted["sections"] == []
    assert "University of Islamabad" not in blob
    assert "Academic Council" not in blob
    evidence = extracted["field_evidence"]
    assert not evidence.get("bundled_gazette_marker_absent")
    assert evidence.get("bundled_gazette_unresolved")


def test_real_s710_text_keeps_legal_advisers_and_not_the_neighbours():
    text = _fixture("s710.txt")
    url = "https://senate.gov.pk/uploads/documents/1661160371_710.pdf|LXXXI of 1975"
    extracted = _extract(text, url=url, title=LEGAL_ADVISERS)
    blob = _blob(extracted)
    numbers = [str(section["section_number"]) for section in extracted["sections"]]
    assert numbers == ["1", "2", "3"]
    section_one = extracted["sections"][0]["section_text"]
    assert "Legal Advisers" in section_one
    assert "Amendment" in section_one
    assert "1975" in section_one
    assert "five lakh" in blob
    assert "House Building" not in blob
    assert "Airports" not in blob
    section_three = extracted["sections"][2]["section_text"]
    assert "repealed" in section_three.lower() or "repcaled" in section_three.lower()
    assert "M. A. HAQ" in section_three
    assert "PRINTING CORPORATION" not in section_three.upper()
    assert "PUBLICATIONS" not in section_three.upper()


def test_real_travel_agencies_file_is_not_tourist_guides():
    text = _fixture("s441.txt")
    url = "https://senate.gov.pk/uploads/documents/1660626546_441.pdf|XXX of 1976"
    extracted = _extract(text, url=url, title="The Travel Agencies Act, 1976")
    blob = _blob(extracted)
    assert "Travel Agencies Act" in (extracted["statute_name"] or "")
    assert "Travel Agencies Act" in blob
    assert "Tourist Guides Act" not in blob
    assert "Seed Analyst" not in blob
    assert not (extracted["field_evidence"] or {}).get("bundled_gazette_marker_absent")
    by_num = {str(section["section_number"]): section["section_text"] for section in extracted["sections"]}
    assert "Federal Government" in by_num["14"]
    assert not by_num["15"].lstrip().startswith("Federal Government")
    assert "official Gazette" in by_num["15"]
    assert "oftcial Gaz" in by_num["16"]
    assert "dispose" in by_num["12"]
    assert "GAZET" not in by_num["12"].upper()
    assert "manager or any partner" in by_num["4"]


def test_real_lxvi_shard_is_present_and_not_selected():
    text = _fixture("s685.txt")
    url = "https://senate.gov.pk/uploads/documents/1661143800_685.pdf|LXVI of 1976"
    extracted = _extract(text, url=url, title="Criminal Law Amendment")
    assert extracted["sections"] == []
    assert "Defence of Pakistan" not in _blob(extracted)
    evidence = extracted["field_evidence"]
    assert not evidence.get("bundled_gazette_marker_absent")
    assert evidence.get("bundled_gazette_unresolved")


def test_real_xvi_file_does_not_emit_a_different_act():
    text = _fixture("s678.txt")
    url = "https://senate.gov.pk/uploads/documents/1660629226_678.pdf|XVI of 1976"
    extracted = _extract(text, url=url, title="Legal Practitioners and Bar Councils (Amendment) Act, 1976")
    assert extracted["sections"] == []
    assert "Old-Age" not in _blob(extracted)
    assert extracted["field_evidence"].get("bundled_gazette_marker_absent")


def test_malaria_scan_with_only_an_unclean_lxiv_emits_nothing():
    text = _fixture("s476.txt")
    url = "https://senate.gov.pk/uploads/documents/1661162484_476.pdf|LXIV of 1975"
    extracted = _extract(text, url=url, title=MALARIA)
    assert extracted["sections"] == []
    assert "Cost of Living" not in _blob(extracted)
    assert not extracted["field_evidence"].get("bundled_gazette_marker_absent")


PAT_FEEDER = "Land Reforms (Baluchistan Pat Feeder Canal) Regulation (Amendment) Act, 1976"
PAT_FEEDER_URL = "https://senate.gov.pk/uploads/documents/1660623691_478.pdf|XXXIX of 1976"


def test_real_pat_feeder_name_is_the_short_title_not_the_act_number_heading():
    text = _fixture("s478.txt")
    extracted = _extract(text, url=PAT_FEEDER_URL, title="XXXIX ol 1976 An Act")
    blob = _blob(extracted)
    assert extracted["statute_name"] == PAT_FEEDER
    assert "1" in _numbers(extracted)
    assert "2" in _numbers(extracted)
    assert "Motor Vehicles" not in blob
    assert "Pat Feeder" in blob
    section_two = next(section["section_text"] for section in extracted["sections"] if section["section_number"] == "2")
    assert section_two.rstrip().endswith("1976.")
    foreign = text + """
ACT No. XL of 1976
An Act to amend the Motor Vehicles Act for a foreign instrument.
1. Short title.—This Act may be called the Foreign Instrument Act, 1976.
2. Extent.—This Act extends only to the foreign instrument and to no other.
"""
    kept = _extract(foreign, url=PAT_FEEDER_URL, title="XXXIX ol 1976 An Act")
    kept_blob = _blob(kept)
    assert "Foreign Instrument" not in kept_blob
    assert "Motor Vehicles" not in kept_blob
    kept_two = next(section["section_text"] for section in kept["sections"] if section["section_number"] == "2")
    assert kept_two.rstrip().endswith("1976.")


def test_numeral_heading_name_is_held():
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "XXXIX ol 1976 An Act",
            "year_enacted": 1976,
            "jurisdiction": "Federal",
            "sections": [
                {
                    "section_number": "1",
                    "section_text": "1. Short title. This Act may be called the Example Act, 1976.",
                }
            ],
        },
        raw_text="1. Short title.",
        source_name="Senate",
        source_url=PAT_FEEDER_URL,
    )
    assert not ok
    assert reason == "numeral_heading_name"


def test_unique_xxvll_above_act_no_is_kept_and_paren_form_is_not():
    shared = """
ACT No. I of 1973
An Act to provide for an example neighbour.
1. Short title.—This Act may be called the Example Act, 1973.
2. Extent.—This Act extends to the example district.
"""
    above = shared + """
xxvll
ACT No. oF
1973
An Act to extend the jurisdiction of the courts.
1. Short title.—This Act may be called the Tribal Areas Act, 1973.
2. Extent.—This Act extends to the named tribal areas.
"""
    extracted = _extract(above, url=TRIBAL_URL, title="XXVII ol 1973 An Act")
    blob = _blob(extracted)
    assert [section["section_number"] for section in extracted["sections"]] == ["1", "2"]
    assert "named tribal areas" in blob
    assert "example district" not in blob
    paren = shared + """
ACT No. )O(Vlt oF
1973
An Act to reconstitute and reorganize the University of Islamabad.
1. Short title.—This Act may be called the University of Islamabad Act, 1973.
2. Definitions.—Academic Council means the Academic Council of the University.
"""
    rejected = _extract(paren, url=TRIBAL_URL, title="XXVII ol 1973 An Act")
    assert rejected["sections"] == []
    assert "University of Islamabad" not in _blob(rejected)
    assert "Academic Council" not in _blob(rejected)


def test_acr_no_xxii_dotted_year_keeps_the_constitution_act():
    text = _fixture("s605.txt")
    url = "https://senate.gov.pk/uploads/documents/1661400088_605.pdf|XXII of 1975"
    extracted = _extract(text, url=url, title="XXII ol 1975 An Act")
    blob = _blob(extracted)
    assert extracted["sections"]
    assert "197" not in _numbers(extracted)
    assert "Political Parties" not in blob
    assert "Constitution" in blob
    assert not (extracted["field_evidence"] or {}).get("bundled_gazette_marker_absent")
    assert not (extracted["field_evidence"] or {}).get("bundled_gazette_unresolved")


def test_schedule_items_after_the_last_section_are_kept():
    text = """
ACT No. IV of 1974
An Act to provide for a short demonstration.
1. Short title.—This Act may be called the Demonstration Act, 1974.
2. Extent.—This Act extends to the districts named in the Schedule.
3. Power.—The Federal Government may notify the districts in the Schedule.
SCHEDULE
(See section 2)
1. The first item in the schedule lists the districts of Chitral and Dir.
2. The second item in the schedule lists the district of Swat.
"""
    extracted = _extract(
        text,
        url="https://senate.gov.pk/uploads/documents/demo.pdf|IV of 1974",
        title="The Demonstration Act, 1974",
    )
    blob = _blob(extracted)
    assert "Chitral and Dir" in blob
    assert "district of Swat" in blob
    assert "SCHEDULE" in blob


def test_federal_gazette_omitted_year_key_is_held():
    payload = {
        "statute_name": TRIBAL_AREAS,
        "jurisdiction": "Federal",
        "sections": [
            {
                "section_number": "1",
                "section_text": "1. Short title.—This Act may be called the Supreme Court and High Court Act, 1973.",
            }
        ],
    }
    ok, reason = validate_statute_reconciled_json(
        payload,
        raw_text=TRIBAL_GAZETTE,
        source_name="Senate",
        source_url=TRIBAL_URL,
    )
    assert not ok
    assert reason == "federal_null_year"
    ok, reason = validate_statute_reconciled_json(
        payload,
        raw_text="1. Short title.",
        source_name="PakistanCode",
        source_url="https://pakistancode.gov.pk/english/sample",
    )
    assert reason != "federal_null_year"


def test_main_337584d6_replacement_pdf_is_not_adopted():
    import hashlib
    from uuid import UUID

    from scraper.tasks.bundled_gazette_acts import (
        CONFIRMED_PDF_SHA256,
        LIVE_NA_320_SHA,
        PIN_UNTRUSTED,
        accept_pinned_pdf,
    )

    text = _fixture("na320.txt")
    url = "https://na.gov.pk/uploads/documents/6a87f616e19a1_320.pdf"
    extracted = _extract(text, url=url, title="President's Orders and Regulations", source_name="NationalAssembly")
    assert "Financial Institutions" in (extracted["statute_name"] or "")
    assert _numbers(extracted) == {"420", "2", "3", "4"}
    main_id = UUID("337584d6-c37a-4cfb-99bd-6e933f5b195b")
    twin_id = UUID("bbc0bfca-6a0e-4eef-b771-532f4f3a4041")
    assert CONFIRMED_PDF_SHA256[main_id] == PIN_UNTRUSTED
    assert CONFIRMED_PDF_SHA256[twin_id] == PIN_UNTRUSTED
    assert LIVE_NA_320_SHA not in set(CONFIRMED_PDF_SHA256.values())
    live = b"%PDF-1.4\n" + text.encode("utf-8")
    overwritten = hashlib.sha256(live).hexdigest()
    data, origin = accept_pinned_pdf(
        (("fetched", live), ("recorded", live)),
        pinned_sha=PIN_UNTRUSTED,
        recorded_sha=overwritten,
    )
    assert data is None
    assert origin == "sha_mismatch"
    authentic = b"%PDF-1.4\ntribal areas bytes"
    pin = hashlib.sha256(authentic).hexdigest()
    other = b"%PDF-1.4\nfinancial institutions"
    kept, kept_origin = accept_pinned_pdf((("fetched", other), ("recorded", authentic)), pinned_sha=pin)
    assert kept == authentic
    assert kept_origin == "recorded"
    rejected, rejected_origin = accept_pinned_pdf((("fetched", other),), pinned_sha=pin, recorded_sha=pin)
    assert rejected is None
    assert rejected_origin == "sha_mismatch"
