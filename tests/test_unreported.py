"""Unreported judgments: a judgment from a court's own website that no law report has cited yet."""

from __future__ import annotations

from datetime import date

import pytest

from scraper.unreported import find_case_number, proceeding_type, unreported_identity


@pytest.mark.parametrize(
    "text,expected",
    [
        ("IN THE HIGH COURT OF SINDH AT KARACHI\nCriminal Bail Application No. 1234 of 2024\nAli v. The State", "Criminal Bail Application No. 1234 of 2024"),
        ("ORDER SHEET\nC.P. No. D-5678 of 2023\nDate Order with signature", "C.P. No. D-5678 of 2023"),
        ("Writ Petition No.123/2024\nversus", "Writ Petition No.123/2024"),
        ("Crl. Misc. No. 45-B of 2023", "Crl. Misc. No. 45-B of 2023"),
        ("Suit No. 1020 of 2019 decided on", "Suit No. 1020 of 2019"),
    ],
)
def test_the_case_number_is_read_from_the_judgment_heading(text, expected):
    assert find_case_number(text) == expected


def test_the_listing_rows_case_number_wins_over_the_text():
    assert find_case_number("Suit No. 1 of 2019", route={"result_case_no": "Civil Appeal No. 7/2020"}) == "Civil Appeal No. 7/2020"


def test_no_case_number_is_none_not_a_guess():
    assert find_case_number("The petitioner seeks relief under Article 199.") is None


@pytest.mark.parametrize(
    "case_no,expected",
    [
        ("Criminal Bail Application No. 1234 of 2024", "bail"),
        ("Crl. Misc. No. 45-B of 2023", "bail"),
        ("Cr. Misc. No. 12-H of 2022", "habeas_corpus"),
        ("C.P. No. D-5678 of 2023", "constitutional_petition"),
        ("Writ Petition No.123/2024", "constitutional_petition"),
        ("Civil Petition for Leave to Appeal No. 9 of 2021", "leave_to_appeal"),
        ("Suit No. 1020 of 2019", "civil_suit"),
        ("Criminal Appeal No. 99 of 2021", "criminal_appeal"),
        ("Jail Appeal No. 4 of 2020", "criminal_appeal"),
        ("R.F.A. No. 12 of 2020", "civil_appeal"),
        ("F.A.O. No. 3 of 2018", "civil_appeal"),
        ("I.C.A. No. 15 of 2022", "intra_court_appeal"),
        ("Civil Revision No.329 of 1989", "civil_revision"),
        ("Crl. Revision No. 8 of 2019", "criminal_revision"),
        ("Review Petition No. 5 of 2021", "review"),
        ("Murder Reference No. 25 of 1980", "criminal_reference"),
        ("Contempt Application No. 3 of 2020", "contempt"),
        ("Execution Application No. 11 of 2017", "execution"),
        ("C.M.A. No. 400 of 2022", "civil_miscellaneous"),
        ("Something No. 1 of 2020", "other"),
    ],
)
def test_proceeding_type_comes_from_the_case_number(case_no, expected):
    assert proceeding_type(case_no) == expected


def test_without_a_case_number_the_heading_of_the_text_is_used():
    assert proceeding_type(None, text="ORDER ON BAIL APPLICATION\nThe applicant seeks post-arrest bail") == "bail"
    assert proceeding_type(None, text="") == "other"


def test_identity_is_court_case_number_and_date():
    ident = unreported_identity("SindhHighCourt", "C.P. No. D-5678 of 2023", date(2024, 5, 1), "a" * 64)
    assert ident == "UNREPORTED SHC C.P. No. D-5678 of 2023 (2024-05-01)"


def test_identity_without_a_case_number_falls_back_to_the_text_fingerprint():
    ident = unreported_identity("IslamabadHighCourt", None, None, "0123456789abcdef" * 4)
    assert ident == "UNREPORTED IHC undated #0123456789ab"
    assert len(unreported_identity("X" * 300, "No. 1 of 2020 " * 40, None, "f" * 64)) <= 200


def test_the_procedure_codes_are_not_mistaken_for_a_petition():
    assert proceeding_type(None, text="Order XXXIX Rules 1 and 2 C.P.C. read with section 151 C.P.C.") == "other"
    assert proceeding_type("C.P. No. 12 of 2022") == "constitutional_petition"
