"""Senate-sourced statute identity sweep.

Auditor cases:
- MAIN b6869d7b three-way name / section text / current PDF
- FREE-SITES fc201e56 preamble name and pipe act-number XIII vs PDF IX
- FREE-SITES f274804f definitions-clause fragment name
- FREE-SITES 44024919 newline title and wrong year
- FREE-SITES fe3f02bd gazette header is not an act

`{pdf_url}|{label}` is a normal identity key. The label alone is not a mismatch.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.models import DocumentQuarantine, SourceProvenance, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.senate_integrity import (
    ACTION_NEEDS_REEXTRACT,
    ACTION_NONE,
    ACTION_QUARANTINE,
    VERDICT_FETCH_FAIL,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_PARTIAL,
    VERDICT_SKIPPED_SHELL,
    classify_senate_identity,
    is_senate_sourced,
    split_statute_source_url,
    title_relation,
)
from scraper.tasks.senate_statute_integrity import run_senate_statute_integrity

CDA_NAME = "Capital Development Authority (Abatement of Arbitration Proceedings) Act, 1975"
BAR_PDF = """
THE GAZETTE OF PAKISTAN
EXTRAORDINARY

LEGAL PRACTITIONERS AND BAR COUNCILS ACT, 1973

ACT NO. XXXV OF 1973

An Act to repeal and re-enact the law relating to legal practitioners and bar councils.

1. Short title.—(1) This Act may be called the Legal Practitioners and Bar Councils Act, 1973.
(2) It shall come into force at once.

2. Definitions.—In this Act, unless there is anything repugnant in the subject or context,
advocate means an advocate entered in the roll of advocates of a High Court under this Act.
Bar Council means a Bar Council constituted under this Act.
"""

FISHING_SECTION = (
    "12. Liability of owner of fishing-craft.—The owner of a fishing-craft shall be liable "
    "for loss or damage caused by the craft to another vessel, under the West Pakistan Fisheries Ordinance, 1961."
)

CDA_PDF = """
THE GAZETTE OF PAKISTAN

CAPITAL DEVELOPMENT AUTHORITY (ABATEMENT OF ARBITRATION PROCEEDINGS) ACT, 1975

ACT NO. LX OF 1975

1. Short title.—This Act may be called the Capital Development Authority (Abatement of Arbitration Proceedings) Act, 1975.

2. Abatement.—Every arbitration proceeding pending before the Capital Development Authority shall abate on the commencement of this Act, and no claim for compensation shall survive.
"""


def _section(number: str, text: str) -> dict:
    return {"section_number": number, "section_text": text}


def test_fetch_host_allowlist_rejects_other_hosts():
    from scraper.tasks.senate_statute_integrity import _host_allowed

    assert _host_allowed("https://senate.gov.pk/uploads/documents/a.pdf")
    assert _host_allowed("https://www.senate.gov.pk/uploads/documents/a.pdf")
    assert _host_allowed("http://pcp.gov.pk/siteimage/downloads/a.pdf")
    assert _host_allowed("http://www.pcp.gov.pk/siteimage/downloads/a.pdf")
    assert not _host_allowed("https://na.gov.pk/uploads/documents/a.pdf")
    assert not _host_allowed("https://evil.example/senate.gov.pk/a.pdf")
    assert not _host_allowed("https://senate.gov.pk.evil.example/a.pdf")


@pytest.mark.asyncio
async def test_fetch_senate_pdf_does_not_request_other_hosts(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("http client should not be constructed")

    monkeypatch.setattr("scraper.tasks.senate_statute_integrity.httpx.AsyncClient", boom)
    from scraper.tasks.senate_statute_integrity import fetch_senate_pdf

    assert await fetch_senate_pdf("https://na.gov.pk/uploads/documents/a.pdf") is None
    assert await fetch_senate_pdf("http://169.254.169.254/latest/meta-data") is None


def test_pipe_suffix_splits_fetch_url_from_label():
    fetch, label = split_statute_source_url(
        "https://senate.gov.pk/uploads/documents/1661398225_570.pdf|XIII of 1985"
    )
    assert fetch == "https://senate.gov.pk/uploads/documents/1661398225_570.pdf"
    assert label == "XIII of 1985"
    assert is_senate_sourced(fetch + "|" + label, source_name="Senate")
    assert is_senate_sourced(
        "http://pcp.gov.pk/siteimage/downloads/gazette.pdf|Act",
        source_name="GazetteOfPakistan",
    )
    assert not is_senate_sourced("https://na.gov.pk/uploads/documents/act.pdf", source_name="NationalAssembly")


def test_pipe_label_difference_is_not_a_mismatch_by_itself():
    """Bill-name suffix on a Senate PDF url is the normal identity key."""
    result = classify_senate_identity(
        stored_name=CDA_NAME,
        year_enacted=1975,
        source_url=(
            "https://senate.gov.pk/uploads/documents/1.pdf|"
            "Capital Development Authority (Abatement of Arbitration Proceedings) Bill, 1975"
        ),
        sections=[
            _section(
                "2",
                "Every arbitration proceeding pending before the Capital Development Authority shall abate "
                "on the commencement of this Act, and no claim for compensation shall survive.",
            )
        ],
        pdf_text=CDA_PDF,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert "pdf_title_disagrees" not in result.signals
    assert "act_number_disagrees" not in result.signals


def test_three_way_mismatch_b6869d7b():
    result = classify_senate_identity(
        stored_name=CDA_NAME,
        year_enacted=1975,
        source_url="https://senate.gov.pk/uploads/documents/1661398225_570.pdf|Capital Development Authority Bill",
        sections=[_section("12", FISHING_SECTION)],
        pdf_text=BAR_PDF,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert result.reason == "senate_integrity:url_act_mismatch"
    assert "pdf_title_disagrees" in result.signals
    assert "section_names_other_act" in result.signals
    assert "section_absent_from_pdf" in result.signals
    assert result.pdf_title
    assert "bar councils" in result.pdf_title.lower()


@pytest.mark.parametrize(
    ("stored", "pdf_title"),
    [
        ("Pakistan Engineering Council Act, 1976", "Pakistan Enginccring Councll Act, 1976"),
        (
            "Federal Public Service Commission Ordinance, 1977",
            "Federal Publlc Servlce Commisslon Ordinance, 1977",
        ),
        (
            "Economic Reforms (Amendment) Ordinance, 1972",
            "Economlc Reforms (Arnendment) Ordinance, 1972",
        ),
        ("National Registration Act, 1973", "Natlonal Registratlon Act, 1973"),
        ("National Registration Act, 1973", "Nat1onal Registration Act, 1973"),
    ],
)
def test_title_relation_ocr_same_act_is_same(stored, pdf_title):
    """Same instrument with scanned-title garbage must not be a different act."""
    assert title_relation(stored, pdf_title) == "same"


@pytest.mark.parametrize(
    ("stored", "other"),
    [
        ("Arms (Second Amendment) Act, 1974", "Esso Undertakings (Vesting) Act, 1976"),
        ("Banking Companies Ordinance, 1962", "Benami Transactions (Prohibition) Act, 2017"),
        ("Islamabad Capital Territory Local Government Act, 2015", "Factories Act, 1934"),
        (
            "Constitution (Eighteenth Amendment) Act, 2010",
            "Constitution (Nineteenth Amendment) Act, 2010",
        ),
        (
            "Constitution (First Amendment) Act, 1974",
            "Constitution (Fourth Amendment) Act, 1975",
        ),
        ("Arms Act, 1878", "Army Act, 1952"),
        ("Police Order, 2002", "Policy Order, 2002"),
        ("Civil Servants Act, 1973", "Civic Servants Act, 1973"),
        (
            "Capital Development Authority (Abatement of Arbitration Proceedings) Act, 1975",
            "Legal Practitioners and Bar Councils Act, 1973",
        ),
        ("Pakistan Engineering Council Act, 1976", "Pakistan Medical Commission Act, 2020"),
    ],
)
def test_title_relation_distinct_acts_stay_different(stored, other):
    assert title_relation(stored, other) == "different"


def _called_act_pdf(title: str, section_body: str, *, act_no: str | None = None) -> str:
    heading = f"ACT No. {act_no}\n" if act_no else ""
    return (
        "THE GAZETTE OF PAKISTAN\n"
        f"{heading}"
        f"{title.upper()}\n"
        f"1. Short title.—This Act may be called the {title}.\n"
        f"2. Operative text.—{section_body}\n"
    )


_OCR_SECTION = (
    "The council shall register qualified practitioners and regulate the profession "
    "throughout the country, and shall publish an annual list of persons entitled to practise."
)
# Enough body words that a missing section is real absence, not a thin extract.
_LONG_OTHER_BODY = " ".join(
    [
        "Every proprietor of a coastal fishing craft shall mark the hull and report each voyage",
        "to the harbour master before departure, keep a written manifest of crew and cargo,",
        "and pay the prescribed light dues to the port authority on arrival at the next port.",
        "The collector may detain the craft until those dues and any penalty are satisfied.",
    ]
)


@pytest.mark.parametrize(
    ("stored", "pdf_title", "year", "act_no"),
    [
        ("Pakistan Engineering Council Act, 1976", "Pakistan Enginccring Councll Act, 1976", 1976, "V of 1976"),
        (
            "Federal Public Service Commission Ordinance, 1977",
            "Federal Publlc Servlce Commisslon Ordinance, 1977",
            1977,
            "XLV of 1977",
        ),
        (
            "Economic Reforms (Amendment) Ordinance, 1972",
            "Economlc Reforms (Arnendment) Ordinance, 1972",
            1972,
            "XII of 1972",
        ),
        ("National Registration Act, 1973", "Natlonal Registratlon Act, 1973", 1973, "VII of 1973"),
    ],
)
def test_ocr_garbled_same_act_is_match_not_quarantined(stored, pdf_title, year, act_no):
    """Same act number plus an OCR-garbled short title is not a wrong PDF."""
    result = classify_senate_identity(
        stored_name=stored,
        year_enacted=year,
        source_url=f"https://senate.gov.pk/uploads/documents/ocr-same.pdf|{act_no}",
        sections=[_section("2", _OCR_SECTION)],
        pdf_text=_called_act_pdf(pdf_title, _OCR_SECTION, act_no=act_no),
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.reason is None
    assert "pdf_title_disagrees" not in result.signals
    assert result.needs_reextract is False


PCSIR_NAME = "Pakistan Council of Scientific and Industrial Research Act, 1973"
PCSIR_OCR_TITLE = "Pakistan Councll of Sclentific and Industrlal Research Act, 1973"
_PCSIR_BODY = (
    "The Council shall promote scientific and industrial research and shall "
    "establish laboratories and institutes for that purpose throughout Pakistan."
)


def test_pcsir_ocr_title_and_garbled_act_number_stays_match():
    """#364 selects the segment once the title is OCR-same, then XXX vs XXXI quarantines it.

    The printed act number is a fragile OCR compare. It must not force MISMATCH
    when the short title already agrees.
    """
    assert title_relation(PCSIR_NAME, PCSIR_OCR_TITLE) == "same"
    result = classify_senate_identity(
        stored_name=PCSIR_NAME,
        year_enacted=1973,
        source_url="https://senate.gov.pk/uploads/documents/pcsir.pdf|XXX of 1973",
        sections=[_section("3", _PCSIR_BODY)],
        pdf_text=_called_act_pdf(PCSIR_OCR_TITLE, _PCSIR_BODY, act_no="XXXI of 1973"),
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.reason is None
    assert result.needs_reextract is False
    assert "act_number_disagrees" not in result.signals
    assert "pdf_title_disagrees" not in result.signals


def test_identical_title_one_roman_misread_keeps_year_section_match():
    """A true title match plus XXX-vs-XXXI still stays MATCH, and the year section is kept."""
    stored = "Criminal Law Amendment (Special Court) Act, 1976"
    body = (
        "The special court shall try the scheduled offences listed in this Act "
        "and shall record its judgment in writing before pronouncing sentence."
    )
    result = classify_senate_identity(
        stored_name=stored,
        year_enacted=1976,
        source_url="https://senate.gov.pk/uploads/documents/same-title.pdf|XVII of 1976",
        sections=[_section("1976", body)],
        pdf_text=_called_act_pdf(stored, body, act_no="XVIII of 1976"),
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.needs_reextract is False
    assert "act_number_disagrees" not in result.signals
    assert "year_as_section_number" in result.signals


@pytest.mark.parametrize(
    ("stored", "year", "pipe_no", "pdf_no"),
    [
        ("Criminal Law Amendment (Special Court) Act, 1976", 1976, "XVII of 1976", "VII of 1976"),
        ("West Pakistan Arms (Amendment) Act, 1974", 1974, "XX of 1974", "XI of 1974"),
        ("Cantonments (Amendment) Act, 2015", 2015, "IV of 2015", "VI of 2015"),
    ],
)
def test_identical_title_distant_act_number_stays_mismatch(stored, year, pipe_no, pdf_no):
    """The same words do not excuse an act number that is more than one numeral off."""
    body = (
        "The special court shall try the scheduled offences listed in this Act "
        "and shall record its judgment in writing before pronouncing sentence."
    )
    result = classify_senate_identity(
        stored_name=stored,
        year_enacted=year,
        source_url=f"https://senate.gov.pk/uploads/documents/same-title.pdf|{pipe_no}",
        sections=[_section(str(year), body)],
        pdf_text=_called_act_pdf(stored, body, act_no=pdf_no),
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert "act_number_disagrees" in result.signals
    assert "year_as_section_number" in result.signals


def test_main_act_versus_amendment_stays_mismatch_when_section_absent():
    """Companies Act, 2017 is not Companies (Amendment) Act, 2017, even at XIX vs XV."""
    stored = "Companies Act, 2017"
    pdf_title = "Companies (Amendment) Act, 2017"
    assert title_relation(stored, pdf_title) == "same"
    result = classify_senate_identity(
        stored_name=stored,
        year_enacted=2017,
        source_url="https://senate.gov.pk/uploads/documents/companies.pdf|XIX of 2017",
        sections=[_section("2", _OCR_SECTION)],
        pdf_text=_called_act_pdf(pdf_title, _LONG_OTHER_BODY, act_no="XV of 2017"),
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert result.needs_reextract is False
    assert "act_number_disagrees" in result.signals
    assert "section_absent_from_pdf" in result.signals


def test_amendment_versus_second_amendment_stays_mismatch():
    """Ordinal wording stays significant even when the act numbers are one numeral apart."""
    stored = "Criminal Laws (Amendment) Act, 2017"
    pdf_title = "Criminal Laws (Second Amendment) Act, 2017"
    body = (
        "The schedule of offences triable by the special court is amended "
        "and the court shall record reasons for every sentence it pronounces."
    )
    assert title_relation(stored, pdf_title) == "same"
    result = classify_senate_identity(
        stored_name=stored,
        year_enacted=2017,
        source_url="https://senate.gov.pk/uploads/documents/cl-amend.pdf|XXX of 2017",
        sections=[_section("2", body)],
        pdf_text=_called_act_pdf(pdf_title, body, act_no="XXXI of 2017"),
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert "act_number_disagrees" in result.signals
    assert "pdf_title_disagrees" not in result.signals


def test_yearless_stored_title_versus_titled_year_stays_mismatch():
    """A stored title with no year does not agree with Finance Act, 2016."""
    stored = "Finance Act"
    pdf_title = "Finance Act, 2016"
    body = "Income tax rates for the tax year shall be the rates set out in the first schedule to this Act."
    assert title_relation(stored, pdf_title) == "same"
    result = classify_senate_identity(
        stored_name=stored,
        year_enacted=None,
        source_url="https://senate.gov.pk/uploads/documents/finance-bare.pdf|XXX of 2016",
        sections=[_section("2", body)],
        pdf_text=_called_act_pdf(pdf_title, body, act_no="XXXI of 2016"),
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert "act_number_disagrees" in result.signals


def test_different_year_act_number_disagrees_stays_mismatch():
    """Finance Act, 2015 against the 2016 PDF is a different instrument, not an OCR act number."""
    body = "Income tax rates for the tax year shall be the rates set out in the first schedule to this Act."
    result = classify_senate_identity(
        stored_name="Finance Act, 2015",
        year_enacted=2015,
        source_url="https://senate.gov.pk/uploads/documents/finance.pdf|X of 2015",
        sections=[_section("2", body)],
        pdf_text=_called_act_pdf("Finance Act, 2016", body, act_no="XI of 2016"),
        pdf_available=True,
    )
    assert title_relation("Finance Act, 2015", "Finance Act, 2016") == "year_differs"
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert "act_number_disagrees" in result.signals
    assert "pdf_title_disagrees" not in result.signals


@pytest.mark.parametrize(
    ("stored", "pdf_title", "year"),
    [
        ("Arms (Second Amendment) Act, 1974", "Esso Undertakings (Vesting) Act, 1976", 1974),
        ("Banking Companies Ordinance, 1962", "Benami Transactions (Prohibition) Act, 2017", 1962),
        ("Islamabad Capital Territory Local Government Act, 2015", "Factories Act, 1934", 2015),
        (
            "Constitution (Eighteenth Amendment) Act, 2010",
            "Constitution (Nineteenth Amendment) Act, 2010",
            2010,
        ),
    ],
)
def test_wrong_pdf_act_title_stays_mismatch_quarantine(stored, pdf_title, year):
    result = classify_senate_identity(
        stored_name=stored,
        year_enacted=year,
        source_url=f"https://senate.gov.pk/uploads/documents/wrong-pdf.pdf|{stored}",
        sections=[_section("2", _OCR_SECTION)],
        pdf_text=_called_act_pdf(pdf_title, _LONG_OTHER_BODY),
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert result.reason == "senate_integrity:url_act_mismatch"
    assert result.needs_reextract is False
    assert "pdf_title_disagrees" in result.signals


def test_wrong_pdf_title_and_act_number_stay_mismatch():
    """A different instrument stays MISMATCH when both the short title and the act number disagree."""
    result = classify_senate_identity(
        stored_name="Arms (Second Amendment) Act, 1974",
        year_enacted=1974,
        source_url="https://senate.gov.pk/uploads/documents/wrong-pdf.pdf|XXIX of 1974",
        sections=[_section("2", _OCR_SECTION)],
        pdf_text=_called_act_pdf("Esso Undertakings (Vesting) Act, 1976", _LONG_OTHER_BODY, act_no="XI of 1976"),
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert result.reason == "senate_integrity:url_act_mismatch"
    assert result.needs_reextract is False
    assert "pdf_title_disagrees" in result.signals
    assert "act_number_disagrees" in result.signals


def test_shared_gazette_second_act_is_not_quarantined():
    """A later act in one Gazette PDF must not be judged against the first ACT No."""
    pdf = """
    THE GAZETTE OF PAKISTAN
    EXTRAORDINARY

    ACT No. IX of 2020
    An Act to regulate coastal trade.
    1. Short title.—This Act may be called the Alpha Trading Act, 2020.
    2. Alpha duty.—Every trader shall keep a register of coastal consignments and report them to the customs collector each quarter without fail.

    ACT No. X of 2020
    An Act to regulate fishing craft.
    1. Short title.—This Act may be called the Beta Fisheries Act, 2020.
    2. Fishing craft.—The owner of a fishing craft shall mark the hull and report the voyage to the fisheries officer before leaving harbour.
    """
    result = classify_senate_identity(
        stored_name="Beta Fisheries Act, 2020",
        year_enacted=2020,
        source_url="https://senate.gov.pk/uploads/documents/shared-gazette.pdf|X of 2020",
        sections=[
            _section(
                "2",
                "The owner of a fishing craft shall mark the hull and report the voyage to the fisheries officer before leaving harbour.",
            )
        ],
        pdf_text=pdf,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert "act_number_disagrees" not in result.signals
    assert "pdf_title_disagrees" not in result.signals
    assert result.pdf_title
    assert "beta fisheries" in result.pdf_title.lower()


def test_fc201e56_preamble_name_and_act_number_xiii_vs_ix():
    pdf = """
    THE GAZETTE OF PAKISTAN
    ACT No. IX of 1985
    An Act to regulate a public authority.
    1. Short title.—This Act may be called the Public Authorities Regulation Act, 1985.
    2. Application.—This Act applies to every public authority established by the Federal Government
    and requires each authority to publish an annual report of its functions and expenditure.
    """
    result = classify_senate_identity(
        stored_name=(
            "WHEREAS it is expedient to provide for the regulation of public authorities "
            "and for matters connected therewith"
        ),
        year_enacted=1985,
        source_url="https://www.senate.gov.pk/uploads/documents/fc201e56.pdf|XIII of 1985",
        sections=[
            _section(
                "2",
                "This Act applies to every public authority established by the Federal Government "
                "and requires each authority to publish an annual report of its functions and expenditure.",
            )
        ],
        pdf_text=pdf,
        pdf_available=True,
    )
    # The stored section is the text of ACT No. IX, so the XIII label does not
    # quarantine the row. The preamble name stays a repairable partial.
    assert result.verdict == VERDICT_PARTIAL
    assert result.action == ACTION_NEEDS_REEXTRACT
    assert "act_number_disagrees" not in result.signals
    assert "preamble_junk_name" in result.signals
    assert result.needs_reextract is True


def test_act_number_mismatch_when_no_segment_matches_and_sections_absent():
    pdf = """
    THE GAZETTE OF PAKISTAN
    ACT No. IX of 1985
    An Act to regulate a public authority.
    1. Short title.—This Act may be called the Public Authorities Regulation Act, 1985.
    2. Application.—This Act applies to every public authority established by the Federal Government
    and requires each authority to publish an annual report of its functions and expenditure
    together with the auditor report for the preceding financial year.
    """
    result = classify_senate_identity(
        stored_name=(
            "WHEREAS it is expedient to provide for the regulation of public authorities "
            "and for matters connected therewith"
        ),
        year_enacted=1985,
        source_url="https://www.senate.gov.pk/uploads/documents/fc201e56.pdf|XIII of 1985",
        sections=[_section("12", FISHING_SECTION)],
        pdf_text=pdf,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert "act_number_disagrees" in result.signals
    assert "section_absent_from_pdf" in result.signals
    assert result.needs_reextract is False


def test_thin_pdf_does_not_treat_missing_sections_as_absence():
    pdf = "This Act may be called the Capital Development Authority (Abatement of Arbitration Proceedings) Act, 1975."
    result = classify_senate_identity(
        stored_name=CDA_NAME,
        year_enacted=1975,
        source_url="https://senate.gov.pk/uploads/documents/thin.pdf|LX of 1975",
        sections=[
            _section(
                "2",
                "The owner of a fishing craft shall mark the hull and report the voyage to the fisheries officer before leaving harbour.",
            )
        ],
        pdf_text=pdf,
        pdf_available=True,
    )
    assert "section_absent_from_pdf" not in result.signals
    assert result.verdict != VERDICT_MISMATCH


def test_section_absent_with_incoherent_name_is_mismatch_not_reextract():
    result = classify_senate_identity(
        stored_name="means the Board established under section 3 of this Act",
        source_url="https://senate.gov.pk/uploads/documents/incoherent.pdf|Example Bill, 1973",
        sections=[_section("12", FISHING_SECTION)],
        pdf_text=BAR_PDF,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert result.needs_reextract is False
    assert "section_absent_from_pdf" in result.signals
    assert "definitions_fragment_name" in result.signals


def test_f274804f_definitions_clause_fragment_name():
    pdf = """
    THE COMPANIES ACT, 2017
    ACT NO. XIX OF 2017
    1. Short title.—This Act may be called the Companies Act, 2017.
    2. Definitions.—In this Act, company means a company formed and registered under this Act
    and includes an existing company and a body corporate as defined in the schedule.
    """
    result = classify_senate_identity(
        stored_name="means a company formed and registered under this Act and includes an existing company",
        year_enacted=2017,
        source_url="https://senate.gov.pk/uploads/documents/f274804f.pdf|XIX of 2017",
        sections=[
            _section(
                "2",
                "In this Act, company means a company formed and registered under this Act "
                "and includes an existing company and a body corporate as defined in the schedule.",
            )
        ],
        pdf_text=pdf,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_PARTIAL
    assert result.action == ACTION_NEEDS_REEXTRACT
    assert "definitions_fragment_name" in result.signals
    assert "act_number_disagrees" not in result.signals
    assert "pdf_title_disagrees" not in result.signals
    assert result.needs_reextract is True


def test_44024919_truncated_newline_title_and_wrong_year():
    pdf = """
    THE GAZETTE OF PAKISTAN
    NATIONAL ACCOUNTABILITY ORDINANCE, 1999
    1. Short title.—This Ordinance may be called the National Accountability Ordinance, 1999.
    2. Application.—This Ordinance applies to holders of public office and to offences of corruption
    and corrupt practices as specified in this Ordinance, including inquiry and accountability proceedings.
    """
    result = classify_senate_identity(
        stored_name="The National\nAccountability Ordinance, 2002",
        year_enacted=2002,
        source_url="https://senate.gov.pk/uploads/documents/44024919.pdf|National Accountability Ordinance",
        sections=[
            _section(
                "2",
                "This Ordinance applies to holders of public office and to offences of corruption "
                "and corrupt practices as specified in this Ordinance, including inquiry and accountability proceedings.",
            )
        ],
        pdf_text=pdf,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_PARTIAL
    assert result.action == ACTION_NEEDS_REEXTRACT
    assert "truncated_title" in result.signals
    assert "year_disagrees" in result.signals
    assert "pdf_title_disagrees" not in result.signals
    assert result.needs_reextract is True


def test_fe3f02bd_gazette_header_is_not_an_act():
    pdf = """
    THE GAZETTE OF PAKISTAN
    EXTRAORDINARY
    PUBLISHED BY AUTHORITY
    PART I
    President's Orders and Regulations
    """
    result = classify_senate_identity(
        stored_name="President's Orders and Regulations",
        source_url="https://pcp.gov.pk/siteimage/downloads/fe3f02bd.pdf|President's Orders and Regulations",
        sections=[],
        pdf_text=pdf,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_PARTIAL
    assert result.action == ACTION_QUARANTINE
    assert "gazette_header_not_act" in result.signals
    assert result.needs_reextract is False
    assert result.reason == "senate_integrity:partial_identity"


def test_year_parsed_as_section_number_does_not_override_matching_title():
    """Section-number repair is a separate sweep. A matching title stays MATCH."""
    pdf = """
    BENAMI TRANSACTIONS (PROHIBITION) ACT, 2017
    1. Short title.—This Act may be called the Benami Transactions (Prohibition) Act, 2017.
    3. Prohibition.—No person shall enter into any benami transaction except as permitted by this Act,
    and any property held benami shall be liable to confiscation by the initiating officer.
    """
    result = classify_senate_identity(
        stored_name="Benami Transactions (Prohibition) Act, 2017",
        year_enacted=2017,
        source_url="https://senate.gov.pk/uploads/documents/7950868f.pdf|Benami Transactions (Prohibition) Act, 2017",
        sections=[
            _section(
                "1882",
                "No person shall enter into any benami transaction except as permitted by this Act, "
                "and any property held benami shall be liable to confiscation by the initiating officer.",
            )
        ],
        pdf_text=pdf,
        pdf_available=True,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.needs_reextract is False
    assert "year_as_section_number" in result.signals


def test_year_as_section_without_agreeing_title_stays_partial():
    """The year-shaped section number still marks PARTIAL when no short title agrees."""
    body = (
        "No person shall enter into any benami transaction except as permitted by this Act, "
        "and any property held benami shall be liable to confiscation by the initiating officer."
    )
    result = classify_senate_identity(
        stored_name="Benami Transactions (Prohibition) Act, 2017",
        year_enacted=2017,
        source_url="https://senate.gov.pk/uploads/documents/7950868f.pdf|V of 2017",
        sections=[_section("1882", body)],
        pdf_text=f"3. Prohibition.—{body}",
        pdf_available=True,
    )
    assert result.verdict == VERDICT_PARTIAL
    assert result.action == ACTION_QUARANTINE
    assert result.reason == "senate_integrity:partial_identity"
    assert result.needs_reextract is False
    assert "year_as_section_number" in result.signals
    assert "pdf_title_disagrees" not in result.signals


def test_fetch_fail_when_pdf_unavailable():
    result = classify_senate_identity(
        stored_name=CDA_NAME,
        source_url="https://senate.gov.pk/uploads/documents/missing.pdf",
        sections=[_section("1", "Short title of the Capital Development Authority Act.")],
        pdf_text=None,
        pdf_available=False,
    )
    assert result.verdict == VERDICT_FETCH_FAIL
    assert result.action == ACTION_NONE


def test_quarantined_bill_shell_is_not_a_mismatch():
    result = classify_senate_identity(
        stored_name="means the Board established under section 3 of this Act",
        source_url="https://senate.gov.pk/uploads/documents/shell.pdf|The Example (Amendment) Bill, 2020",
        sections=[],
        pdf_text=BAR_PDF,
        pdf_available=True,
        already_quarantined=True,
        section_count=0,
    )
    assert result.verdict == VERDICT_SKIPPED_SHELL
    assert result.action == ACTION_NONE
    assert result.reason is None


async def _add_section(db, statute_id, num: str, text: str) -> None:
    sec = StatuteSection(statute_id=statute_id, section_number=num)
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(
        section_id=sec.id,
        version_no=1,
        section_text=text,
        text_hash=f"{uuid4().hex}{uuid4().hex}"[:64],
    )
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    await db.flush()


@pytest.mark.asyncio
async def test_dry_run_classifies_visible_and_skips_shell_without_fetch(db):
    visible_id = uuid4()
    shell_id = uuid4()
    db.add(
        Statute(
            id=visible_id,
            name=CDA_NAME,
            jurisdiction="Federal",
            year_enacted=1975,
            source_name="Senate",
            source_url="https://senate.gov.pk/uploads/documents/1661398225_570.pdf|CDA Bill",
        )
    )
    db.add(
        Statute(
            id=shell_id,
            name="means the Board established under section 3 of this Act",
            jurisdiction="Federal",
            source_name="Senate",
            source_url="https://senate.gov.pk/uploads/documents/shell.pdf|The Example (Amendment) Bill, 2020",
        )
    )
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=shell_id,
            reason="b7_bad_document:no_parseable_sections",
            run_id="prior-shell",
        )
    )
    await db.commit()
    await _add_section(db, visible_id, "12", FISHING_SECTION)
    await db.commit()

    fetched: list[str] = []

    async def fetch_pdf(url: str):
        fetched.append(url)
        return b"%PDF-1.4 visible"

    def extract_text(data: bytes) -> str:
        return BAR_PDF

    report = await run_senate_statute_integrity(
        apply=False,
        fetch_pdf=fetch_pdf,
        extract_text=extract_text,
    )
    assert report["dry_run"] is True
    assert report["counts"][VERDICT_MISMATCH] == 1
    assert report["counts"][VERDICT_SKIPPED_SHELL] == 1
    assert fetched == ["https://senate.gov.pk/uploads/documents/1661398225_570.pdf"]
    assert report["applied"] is None
    still = (await db.get(Statute, visible_id))
    assert still.name == CDA_NAME
    q = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == visible_id))
    ).scalars().first()
    assert q is None
    section = (
        await db.execute(select(StatuteSectionVersion.section_text).join(StatuteSection))
    ).scalars().all()
    assert FISHING_SECTION in section


@pytest.mark.asyncio
async def test_apply_quarantines_visible_mismatch_without_overwriting(db):
    visible_id = uuid4()
    shell_id = uuid4()
    db.add(
        Statute(
            id=visible_id,
            name=CDA_NAME,
            jurisdiction="Federal",
            year_enacted=1975,
            source_name="Senate",
            source_url="https://senate.gov.pk/uploads/documents/1661398225_570.pdf",
            needs_section_refetch=False,
        )
    )
    db.add(
        Statute(
            id=shell_id,
            name="further to amend the Example Act in the manner hereafter appearing",
            jurisdiction="Federal",
            source_name="GazetteOfPakistan",
            source_url="http://www.pcp.gov.pk/siteimage/downloads/shell.pdf|Example Bill, 2019",
        )
    )
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=shell_id,
            reason="statute_quality:no_valid_sections",
            run_id="prior-shell",
        )
    )
    await db.commit()
    await _add_section(db, visible_id, "12", FISHING_SECTION)
    await db.commit()

    async def fetch_pdf(url: str):
        return b"%PDF-1.4 visible"

    report = await run_senate_statute_integrity(
        apply=True,
        fetch_pdf=fetch_pdf,
        extract_text=lambda data: BAR_PDF,
    )
    run_id = report["applied"]["run_id"]
    db.expire_all()
    st = await db.get(Statute, visible_id)
    assert st.name == CDA_NAME
    assert st.needs_section_refetch is False
    assert st.source_url == "https://senate.gov.pk/uploads/documents/1661398225_570.pdf"
    q = (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.doc_id == visible_id,
                DocumentQuarantine.doc_type == "statute",
            )
        )
    ).scalars().one()
    assert q.reason == "senate_integrity:url_act_mismatch"
    assert q.run_id == run_id
    text = (
        await db.execute(
            select(StatuteSectionVersion.section_text)
            .join(StatuteSection, StatuteSection.id == StatuteSectionVersion.section_id)
            .where(StatuteSection.statute_id == visible_id)
        )
    ).scalar_one()
    assert text == FISHING_SECTION
    shell_q = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == shell_id))
    ).scalars().one()
    assert shell_q.reason == "statute_quality:no_valid_sections"
    assert shell_q.run_id == "prior-shell"
    other_id = uuid4()
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=other_id,
            reason="b7_bad_document:no_parseable_sections",
            run_id=run_id,
        )
    )
    await db.commit()

    undone = await run_senate_statute_integrity(undo=run_id)
    assert undone["undone"]["quarantine_rows_removed"] == 1
    db.expire_all()
    gone = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == visible_id))
    ).scalars().first()
    assert gone is None
    kept = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == other_id))
    ).scalars().one()
    assert kept.reason == "b7_bad_document:no_parseable_sections"


@pytest.mark.asyncio
async def test_apply_partial_flags_reextract_and_keeps_text(db):
    sid = uuid4()
    name = "means a company formed and registered under this Act and includes an existing company"
    body = (
        "In this Act, company means a company formed and registered under this Act "
        "and includes an existing company and a body corporate as defined in the schedule."
    )
    pdf = """
    THE COMPANIES ACT, 2017
    ACT NO. XIX OF 2017
    1. Short title.—This Act may be called the Companies Act, 2017.
    2. Definitions.—In this Act, company means a company formed and registered under this Act
    and includes an existing company and a body corporate as defined in the schedule.
    """
    db.add(
        Statute(
            id=sid,
            name=name,
            jurisdiction="Federal",
            year_enacted=2017,
            source_name="Senate",
            source_url="https://senate.gov.pk/uploads/documents/f274804f.pdf|XIX of 2017",
            needs_section_refetch=False,
        )
    )
    await db.commit()
    await _add_section(db, sid, "2", body)
    await db.commit()

    async def fetch_pdf(url: str):
        return b"%PDF-1.4 partial"

    report = await run_senate_statute_integrity(
        apply=True,
        fetch_pdf=fetch_pdf,
        extract_text=lambda data: pdf,
    )
    assert report["counts"][VERDICT_PARTIAL] == 1
    run_id = report["applied"]["run_id"]
    db.expire_all()
    st = await db.get(Statute, sid)
    assert st.name == name
    assert st.needs_section_refetch is True
    text = (
        await db.execute(
            select(StatuteSectionVersion.section_text)
            .join(StatuteSection, StatuteSection.id == StatuteSectionVersion.section_id)
            .where(StatuteSection.statute_id == sid)
        )
    ).scalar_one()
    assert text == body
    q = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == sid))
    ).scalars().first()
    assert q is None
    assert report["applied"]["flagged_reextract"] == 1
    assert report["applied"]["quarantined"] == 0

    await run_senate_statute_integrity(undo=run_id)
    db.expire_all()
    st2 = await db.get(Statute, sid)
    assert st2.needs_section_refetch is False
    assert st2.name == name
    gone = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == sid))
    ).scalars().first()
    assert gone is None


@pytest.mark.asyncio
async def test_recorded_bytes_used_when_fetch_fails(db):
    sid = uuid4()
    url = "https://senate.gov.pk/uploads/documents/recorded.pdf"
    db.add(
        Statute(
            id=sid,
            name=CDA_NAME,
            jurisdiction="Federal",
            year_enacted=1975,
            source_name="Senate",
            source_url=url,
        )
    )
    db.add(
        SourceProvenance(
            source_name="Senate",
            access_method="public",
            source_url=url,
            content_hash="a" * 64,
            content_kind="pdf",
            raw_ref="Senate/aa/recorded.pdf",
            byte_size=8,
            document_kind="original_pdf",
        )
    )
    await db.commit()
    await _add_section(db, sid, "12", FISHING_SECTION)
    await db.commit()

    async def fetch_pdf(url: str):
        return None

    def recorded_loader(fetch_url: str) -> bytes:
        assert fetch_url == url
        return b"%PDF-recorded"

    report = await run_senate_statute_integrity(
        apply=False,
        fetch_pdf=fetch_pdf,
        recorded_loader=recorded_loader,
        extract_text=lambda data: BAR_PDF if b"recorded" in data else "",
    )
    row = next(r for r in report["rows"] if r["statute_id"] == str(sid))
    assert row["verdict"] == VERDICT_MISMATCH
    assert row["pdf_origin"] == "recorded"
