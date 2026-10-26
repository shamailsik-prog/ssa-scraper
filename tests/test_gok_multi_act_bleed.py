"""law.gok.pk multi-act PDF bleed and name/PDF mismatch sweep.

Ground truth used only as detector fixtures (no droplet writes):

- Conciliation Courts 1962 bleeds Mirpur Town Development Authority text
  (MAIN 86215821, FREE-SITES 6321f7b0), already quarantined as
  ``multi_act_pdf_bleed:law.gok.pk``.
- Constitution 1973 rows whose PDF is a different AJK instrument
  (MAIN e0db6058 Legislative Assembly Elections Amendment Ordinance 1980;
  FREE-SITES 8a07a5ad Courts and Laws Code 1949), already quarantined as
  ``name_pdf_mismatch:law.gok.pk``.
- A numbering drop inside the same instrument is not bleed. Ulma-o-Mushaikh
  (d16bce19), Extra-Territorial Offences (32bfe59b), Law Officers, Finance
  schedule rows, and a revised-volume tail that then states that act's own
  short title stay MATCH when the stored name matches the PDF filename.
- A year-like section number before section 1 is not a restart.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from scraper.models import DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.gok_multi_act_bleed import (
    ACTION_NONE,
    ACTION_QUARANTINE,
    REASON_BLEED,
    REASON_NAME_MISMATCH,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    classify_gok_multi_act,
    truncate_at_second_act_heading,
)
from scraper.tasks.gok_multi_act_bleed import run_gok_multi_act_bleed

CONCILIATION_NAME = (
    "THE AZAD JAMMU AND KASHMIR CONCILIATION COURTS ACT, 1962 (Act II of 1962)"
)
CONCILIATION_URL = (
    "https://law.gok.pk/wp-content/uploads/2024/03/"
    "THE-AZAD-JAMMU-AND-KASHMIR-CONCILIATION-COURTS-ACT-1962-Act-II-of-1962.pdf|II"
)
MIRPUR_NAME = "The New Mirpur Town Development Authority Act, 1962"
MIRPUR_URL = (
    "https://law.gok.pk/wp-content/uploads/2024/03/"
    "THE-NEW-MIRPUR-TOWN-DEVELOPMENT-AUTHORITY-ACT-1962.pdf|III"
)
CONSTITUTION_NAME = "The Constitution of the Islamic Republic of Pakistan, 1973"
LEGISLATIVE_ASSEMBLY_URL = (
    "https://law.gok.pk/wp-content/uploads/2020/06/"
    "THE-AZAD-JAMMU-AND-KASHMIR-LEGISLATIVE-ASSEMBLY-ELECTIONS-AMENDMENT-ORDINANCE-1980.pdf"
)
COURTS_CODE_URL = (
    "https://law.gok.pk/wp-content/uploads/2019/04/"
    "THE-AZAD-JAMMU-AND-KASHMIR-COURTS-AND-LAWS-CODE-1949.pdf"
)
CONSTITUTION_URL = (
    "https://law.gok.pk/wp-content/uploads/2019/04/"
    "THE-CONSTITUTION-OF-THE-ISLAMIC-REPUBLIC-OF-PAKISTAN-1973.pdf"
)

TWO_ACT_PDF = """
THE AZAD JAMMU AND KASHMIR CONCILIATION COURTS ACT, 1962

1. Short title.—This Act may be called the Azad Jammu and Kashmir Conciliation Courts Act, 1962.

2. Definitions.—In this Act, Court means a Conciliation Court constituted under this Act.

3. Establishment.—The Government may establish Conciliation Courts for such areas as it thinks fit and shall publish a notification of every court so established.

11. Procedure.—A Conciliation Court shall follow the procedure laid down in this Act and shall record its order in writing.

THE NEW MIRPUR TOWN DEVELOPMENT AUTHORITY ACT, 1962

1. Short title.—This Act may be called the New Mirpur Town Development Authority Act, 1962.

12. Headquarters of the Authority.—The headquarters of the Authority shall be at Mirpur and the Authority shall meet there.
"""


def _section(number: str, text: str, title: str = "", sort_key: int | None = None) -> dict:
    row = {"section_number": number, "section_text": text, "section_title": title}
    if sort_key is not None:
        row["sort_key"] = sort_key
    return row


def _plain_sections() -> list[dict]:
    """Operative sections that do not name a second act."""
    return [
        _section("1", "This instrument extends to the whole of the territory and comes into force at once.", "Extent", 0),
        _section("2", "Words used here have the meanings assigned in this section.", "Definitions", 1),
        _section("3", "The Government may make rules for carrying this instrument into effect.", "Rules", 2),
        _section("4", "A proceeding under this instrument shall be recorded in writing.", "Procedure", 3),
    ]


def _court_sections() -> list[dict]:
    return [
        _section(
            "1",
            "This Act may be called the Azad Jammu and Kashmir Conciliation Courts Act, 1962.",
            "Short title",
            0,
        ),
        _section(
            "2",
            "In this Act, Court means a Conciliation Court constituted under this Act.",
            "Definitions",
            1,
        ),
        _section(
            "3",
            "The Government may establish a Conciliation Court for a local area by notification.",
            "Establishment of Conciliation Courts",
            2,
        ),
        _section(
            "11",
            "A Conciliation Court shall hear the parties and record a written order.",
            "Procedure of the Court",
            3,
        ),
    ]


def _mirpur_bleed_sections() -> list[dict]:
    """Conciliation row with Mirpur Town Development Authority sections mixed in."""
    foreign = [
        ("12", "Headquarters of the Authority", "The headquarters of the Authority shall be at Mirpur."),
        ("22", "Housing schemes of the Authority", "The Authority may frame housing schemes for Mirpur."),
        ("23", "Powers of the Authority", "The Authority shall exercise the powers conferred by this Act."),
        ("24", "Constitution of the Tribunal", "A Tribunal shall be constituted to hear appeals from the Authority."),
        ("25", "Authority Fund", "The Authority Fund shall be kept in the scheduled bank."),
        ("26", "Meetings of the Authority", "The Authority shall meet at least once in each month."),
        ("27", "Employees of the Authority", "The Authority may appoint such employees as it requires."),
        ("28", "Budget of the Authority", "The Authority shall approve an annual budget of its fund."),
    ]
    rows = _court_sections()
    for index, (number, title, text) in enumerate(foreign, start=4):
        rows.append(_section(number, text, title, index))
    rows.append(
        _section(
            "1898",
            "Code of Criminal Procedure, 1898. A magistrate may record a confession under that Code.",
            "CrPC fragment",
            20,
        )
    )
    return rows


def _restart_sections(*runs: list[str]) -> list[dict]:
    """Document order is the concatenation of runs. Numbers stay unique."""
    rows = []
    sort_key = 0
    for run in runs:
        for number in run:
            rows.append(
                _section(
                    number,
                    f"Section {number}. Operative text of this instrument continues for several lines.",
                    f"Provision {number}",
                    sort_key,
                )
            )
            sort_key += 1
    return rows


def test_conciliation_mirpur_bleed_is_mismatch_without_using_year_alone():
    result = classify_gok_multi_act(
        stored_name=CONCILIATION_NAME,
        source_url=CONCILIATION_URL,
        sections=_mirpur_bleed_sections(),
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert result.reason == REASON_BLEED
    assert "foreign_body_run" in result.signals
    assert "year_fragment_section" in result.signals
    assert "year_before_section_one" not in result.signals


def test_year_fragment_section_alone_does_not_quarantine():
    sections = _court_sections() + [
        _section(
            "1908",
            "Limitation Act, 1908 is cited for the period of limitation.",
            "Citation fragment",
            4,
        )
    ]
    result = classify_gok_multi_act(
        stored_name=CONCILIATION_NAME,
        source_url=CONCILIATION_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.reason is None
    assert "year_fragment_section" in result.signals
    assert "foreign_body_run" not in result.signals


def test_pipe_label_and_act_number_parenthetical_are_not_a_mismatch():
    result = classify_gok_multi_act(
        stored_name=CONCILIATION_NAME,
        source_url=CONCILIATION_URL,
        sections=_court_sections(),
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert "name_pdf_mismatch" not in result.signals
    assert result.pipe_label == "II"


def test_mirpur_own_statute_defining_authority_and_tribunal_matches():
    sections = [
        _section(
            "1",
            "This Act may be called the New Mirpur Town Development Authority Act, 1962.",
            "Short title",
            0,
        ),
        _section(
            "2",
            'In this Act, "Authority" means the New Mirpur Town Development Authority and "Tribunal" means the Tribunal constituted under this Act.',
            "Definitions",
            1,
        ),
        _section("12", "The headquarters of the Authority shall be at Mirpur.", "Headquarters of the Authority", 2),
        _section("24", "The Tribunal shall hear appeals from orders of the Authority.", "Constitution of the Tribunal", 3),
        _section("25", "The Authority Fund shall vest in the Authority.", "Authority Fund", 4),
        _section("26", "The Authority shall meet once a month.", "Meetings of the Authority", 5),
        _section("27", "The Authority may appoint employees.", "Employees of the Authority", 6),
    ]
    result = classify_gok_multi_act(
        stored_name=MIRPUR_NAME,
        source_url=MIRPUR_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert "foreign_body_run" not in result.signals
    assert "name_pdf_mismatch" not in result.signals


def test_second_act_title_mid_document_quarantines_even_without_year_fragment():
    sections = _court_sections()
    sections.append(
        _section(
            "21",
            "THE NEW MIRPUR TOWN DEVELOPMENT AUTHORITY ACT, 1962\n"
            "1. Short title.—This Act may be called the New Mirpur Town Development Authority Act, 1962.",
            "Following instrument",
            4,
        )
    )
    result = classify_gok_multi_act(
        stored_name=CONCILIATION_NAME,
        source_url=CONCILIATION_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.reason == REASON_BLEED
    assert "second_act_title" in result.signals
    assert "numbering_restart" in result.signals


def test_constitution_1973_versus_legislative_assembly_pdf_is_name_mismatch():
    result = classify_gok_multi_act(
        stored_name=CONSTITUTION_NAME,
        source_url=LEGISLATIVE_ASSEMBLY_URL,
        sections=_plain_sections(),
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.action == ACTION_QUARANTINE
    assert result.reason == REASON_NAME_MISMATCH
    assert "name_pdf_mismatch" in result.signals


def test_constitution_1973_versus_courts_and_laws_code_pdf_is_name_mismatch():
    result = classify_gok_multi_act(
        stored_name=CONSTITUTION_NAME,
        source_url=COURTS_CODE_URL,
        sections=_restart_sections(["1", "2", "3", "4", "5", "6"]),
    )
    assert result.reason == REASON_NAME_MISMATCH
    assert result.verdict == VERDICT_MISMATCH


def test_cabinet_order_versus_old_liabilities_pdf_is_name_mismatch():
    result = classify_gok_multi_act(
        stored_name="Cabinet Order No. 249 of 1950",
        source_url=(
            "https://law.gok.pk/wp-content/uploads/2024/03/"
            "THE-AZAD-KASHMIR-OLD-LIABILITIES-ABEYANCE-ACT-OF-1950.pdf"
        ),
        sections=_plain_sections(),
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.reason == REASON_NAME_MISMATCH
    assert "name_pdf_mismatch" in result.signals


def test_elections_act_versus_election_comission_pdf_is_name_mismatch():
    """``Elections`` does not token-match ``Election``, and ``Comission`` is not ``Commission``."""
    result = classify_gok_multi_act(
        stored_name="The Azad Jammu and Kashmir Elections Act, 2017",
        source_url="https://law.gok.pk/wp-content/uploads/2021/02/Election-Comission-amendment-Act-08-02-2021.pdf",
        sections=_plain_sections(),
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.reason == REASON_NAME_MISMATCH
    assert "name_pdf_mismatch" in result.signals


def test_constitution_1973_with_constitution_pdf_matches():
    result = classify_gok_multi_act(
        stored_name=CONSTITUTION_NAME,
        source_url=CONSTITUTION_URL,
        sections=_plain_sections(),
    )
    assert result.verdict == VERDICT_MATCH
    assert "name_pdf_mismatch" not in result.signals


@pytest.mark.parametrize(
    "numbers",
    [
        ["8", "9", "10", "11", "12", "13", "1", "2", "3", "4"],
        ["6", "7", "8", "9", "10", "11", "1", "2", "3"],
        ["4", "5", "6", "7", "8", "9", "10", "1", "2"],
        ["2591", "6", "7", "8", "9", "10", "11", "1", "2", "3"],
    ],
)
def test_same_instrument_numbering_drop_is_not_bleed(numbers: list[str]):
    """A drop back to section 1 without another act's title is the same instrument."""
    result = classify_gok_multi_act(
        stored_name="The Azad Jammu and Kashmir Ulma-o-Mushaikh Act, 1997",
        source_url="https://law.gok.pk/wp-content/uploads/2021/01/ULMA-O-MUSHAICH-ACT-1997.pdf",
        sections=_restart_sections(numbers),
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.reason is None
    assert "numbering_restart" not in result.signals


@pytest.mark.parametrize(
    "numbers",
    [
        ["1997", "1", "2", "3", "4", "5", "6"],
        ["1980", "1", "2", "3", "4", "5"],
        ["2591", "1", "2", "3", "4", "5", "6"],
        ["1", "2", "3", "4", "5", "6", "7", "8"],
    ],
)
def test_year_or_junk_before_section_one_is_not_a_restart(numbers: list[str]):
    result = classify_gok_multi_act(
        stored_name="The Azad Jammu and Kashmir Law Officers Act, 1994",
        source_url="https://law.gok.pk/wp-content/uploads/2021/01/LAW-OFFICERS-ACT-1994.pdf",
        sections=_restart_sections(numbers),
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert "numbering_restart" not in result.signals


def test_other_hosts_are_out_of_scope():
    result = classify_gok_multi_act(
        stored_name=CONCILIATION_NAME,
        source_url="https://senate.gov.pk/uploads/documents/conciliation.pdf",
        sections=_mirpur_bleed_sections(),
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.reason is None


def test_truncate_at_second_act_heading_keeps_the_anchor_act():
    cut = truncate_at_second_act_heading(TWO_ACT_PDF, CONCILIATION_NAME)
    assert "Conciliation Courts" in cut
    assert "NEW MIRPUR" not in cut.upper()
    assert "Headquarters of the Authority" not in cut


FINANCE_NAME = "The Azad Jammu and Kashmir Finance Act, 2023"
FINANCE_URL = "https://law.gok.pk/wp-content/uploads/2023/07/THE-AZAD-JAMMU-AND-KASHMIR-FINANCE-ACT-2023.pdf"
FINANCE_AMENDING_TEXT = """THE AZAD JAMMU AND KASHMIR FINANCE ACT, 2023

1. Short title, extent and commencement.— (1) This Act may be called the Azad Jammu and Kashmir Finance Act, 2023.
(2) It shall come into force at once.

2. Amendment of the Sales Tax Act.— In
The Azad Jammu and Kashmir Sales Tax Act, 1990
in section 3, for the words "ten percent" the words "fifteen percent" shall be substituted.

3. Repeal.— The following Act is hereby repealed, namely:—
The Azad Jammu and Kashmir Entertainments Duty Act, 1958
and the repeal shall take effect on the day this Act comes into force.
"""


EXTRA_TERRITORIAL_NAME = "The Azad Jammu and Kashmir Extra-Territorial Offences Act, 1962"
EXTRA_TERRITORIAL_URL = (
    "https://law.gok.pk/wp-content/uploads/2024/03/"
    "THE-AZAD-JAMMU-KASHMIR-EXTRA-TERRITORIAL-OFFENCES-ACT-1962-ACT-VII-OF-1962.pdf"
)
WAGES_NAME = "The Payment of Wages (Application to Azad Jammu and Kashmir) Act, 1967"
WAGES_URL = (
    "https://law.gok.pk/wp-content/uploads/2024/03/"
    "PAYMENT-OF-WAGES-APPLICATION-TO-AZAD-JAMMU-AND-KASHMIR-ACT-1967Act-IV-of-1967.pdf"
)
LAW_OFFICERS_NAME = "The Azad Jammu and Kashmir Law Officers (Terms and Conditions) Act, 2014"
LAW_OFFICERS_URL = (
    "https://law.gok.pk/wp-content/uploads/2024/03/"
    "The-Azad-Jammu-and-Kashmir-Law-Officers-Terms-and-Conditions-Act-2014-Act-I-of-2015.pdf"
)
FINANCE_1998_NAME = "The Azad Jammu and Kashmir Finance Act, 1998"
FINANCE_1998_URL = (
    "https://law.gok.pk/wp-content/uploads/2024/03/"
    "The-Azad-Jammu-and-Kashmir-Finance-Act-1998-ACT-V-OF-1998.pdf"
)
ESSENTIAL_NAME = "The Azad Jammu and Kashmir Essential Articles Control Act, 2016"
ESSENTIAL_URL = (
    "https://law.gok.pk/wp-content/uploads/2024/03/"
    "The-Azad-Jammu-and-Kashmir-Essential-Articles-Control-Act-2016-Act-XXIV-of-2016.pdf"
)


def test_volume_tail_then_same_act_short_title_matches_filename():
    """A revised-volume PDF that ends the previous instrument, then states this act's own short title, is one instrument."""
    sections = [
        _section(
            "15",
            "15. Amendment of Section 49:- After sub section (1) of "
            "Section 49 of the said Act, the following shall be deemed to be inserted, namely ; "
            "Explanation:- For the purposes of this sub-section, land which is held with a house.",
            "Amendment of Section 49",
            0,
        ),
        _section(
            "1",
            "1. Short title and commencement:- (1) This Act may be "
            "called the Azad Jammu and Kashmir Extra-territorial Offences Act, 1962. "
            "(2) It extends to the whole of the Azad Jammu and Kashmir territory. "
            "(3) It shall come into force at once.",
            "Short title and commencement",
            1,
        ),
        _section(
            "2",
            "2. Definitions:- In this Act, unless there is anything repugnant in the subject or context.",
            "Definitions",
            2,
        ),
    ]
    result = classify_gok_multi_act(
        stored_name=EXTRA_TERRITORIAL_NAME,
        source_url=EXTRA_TERRITORIAL_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.reason is None
    assert "numbering_restart" not in result.signals
    assert "second_act_title" not in result.signals
    assert "name_pdf_mismatch" not in result.signals


def test_wages_act_after_previous_instrument_tail_matches_filename():
    sections = [
        _section(
            "5",
            "5. Application of the Rules:- The Rules made under the Co-operative Societies Act, 1925 "
            "(Act VII of 1925), as they are in force in the West Pakistan, shall apply.",
            "Application of the Rules",
            0,
        ),
        _section(
            "6",
            "6. Removal of difficulties:- Where any difficulty arises in giving effect to the provisions "
            "of this Act the Government may make such orders as may be deemed necessary.",
            "Removal of difficulties",
            1,
        ),
        _section(
            "1",
            "1. Short title, extent and commencement:- (1) This Act may be called the Payment of Wages "
            "(Application to Azad Jammu and Kashmir) Act, 1967. (2) It extends to the whole of the "
            "Azad Jammu and Kashmir Territory. (3) It shall come into force at once.",
            "Short title, extent and commencement",
            2,
        ),
        _section(
            "2",
            "2. Application of the Act:- The Payment of Wages Act, 1936 (IV of 1936), shall apply to "
            "the Azad Jammu and Kashmir Territory as it applies to Pakistan.",
            "Application of the Act",
            3,
        ),
    ]
    result = classify_gok_multi_act(
        stored_name=WAGES_NAME,
        source_url=WAGES_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.reason is None
    assert "numbering_restart" not in result.signals
    assert "name_pdf_mismatch" not in result.signals


def test_date_parsed_as_section_number_is_not_a_restart():
    """``01.07.2014`` stored as section 01 is a date inside the Law Officers Act, not a second act."""
    sections = _restart_sections(["1", "2", "3", "4", "5", "6", "7"])
    sections.append(
        _section(
            "01",
            "01.07.2014. (5) The dues of law officers from 01.11.2003 to 07.04.2009 shall be paid "
            "on availability of funds in due course of time.",
            "07.2014",
            7,
        )
    )
    sections.append(
        _section(
            "8",
            "8. Traveling Allowance.- Azad Jammu and Kashmir Traveling Allowance Rules, 1971 shall apply.",
            "Traveling Allowance",
            8,
        )
    )
    result = classify_gok_multi_act(
        stored_name=LAW_OFFICERS_NAME,
        source_url=LAW_OFFICERS_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.reason is None
    assert "numbering_restart" not in result.signals


def test_finance_schedule_row_numbers_are_not_a_second_act():
    """Stamp-schedule rows ``01. Middle Classes`` are not section 1 of another statute."""
    sections = [
        _section(
            "1",
            "1. Short title, extent and Commencement:- (1) This Act may be called the Azad Jammu and Kashmir Finance Act, 1998.",
            "Short title",
            0,
        ),
        _section("20", "20. In article 47, in part A, for the words ten paisa the words three rupees shall be substituted.", "Article 47", 1),
        _section("27", "27. In article 64, for the word fifty the words one hundred shall be substituted.", "Article 64", 2),
        _section("01", "01. Middle Classes 10 10", "Middle Classes", 3),
        _section("02", "02. High Classes 15 30", "High Classes", 4),
        _section("03", "03. Inter Classes 30 30", "Inter Classes", 5),
        _section(
            "5",
            "5. Adaptation of amendment of the Sales Tax Act, 1990. In section 3, for the words ten percent the words fifteen percent shall be substituted.",
            "Sales Tax adaptation",
            6,
        ),
    ]
    result = classify_gok_multi_act(
        stored_name=FINANCE_1998_NAME,
        source_url=FINANCE_1998_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.reason is None
    assert "numbering_restart" not in result.signals
    assert "second_act_title" not in result.signals


def test_prose_line_ending_this_act_is_not_a_second_act():
    """``shall be called in question`` and a wrapped ``this Act`` line are not a new short title."""
    sections = [
        _section(
            "1",
            "1. Short title.- This Act may be called the Azad Jammu and Kashmir Essential Articles Control Act, 2016.",
            "Short title",
            0,
        ),
        _section(
            "11",
            "11. Presumption as to Orders.- (1) No order made in\n"
            "exercise of any power conferred by or under this Act\n"
            "shall be called in question in any court.\n"
            "(2) Where an order purports to have been made and signed by an authority in exercise of any power "
            "conferred by or under this Act, a court shall presume within the meaning of the Qanun-e-Shahadat Order, "
            "1984, that such order was so made by that authority.",
            "Presumption as to Orders",
            1,
        ),
    ]
    result = classify_gok_multi_act(
        stored_name=ESSENTIAL_NAME,
        source_url=ESSENTIAL_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.reason is None
    assert "second_act_title" not in result.signals
    assert "numbering_restart" not in result.signals


def test_substituted_short_title_inside_an_amending_section_is_not_a_second_act():
    sections = [
        _section(
            "1",
            "1. Short title.- This Act may be called the Azad Jammu and Kashmir Finance Act, 2012.",
            "Short title",
            0,
        ),
        _section(
            "2",
            "2. Amendment of the Sales Tax Act.- In the Azad Jammu and Kashmir Sales Tax Act, 1990, "
            "for section 1 the following shall be substituted, namely:-\n"
            "1. Short title.- This Act may be called the Azad Jammu and Kashmir Sales Tax Act, 1990.\n"
            "It shall come into force at once.",
            "Amendment of the Sales Tax Act",
            1,
        ),
    ]
    result = classify_gok_multi_act(
        stored_name=FINANCE_NAME,
        source_url=FINANCE_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.reason is None
    assert "second_act_title" not in result.signals
    assert "numbering_restart" not in result.signals


def test_restart_that_opens_a_different_act_still_quarantines():
    sections = _court_sections()
    sections.extend(
        _restart_sections(["12", "13", "14", "15", "16"])
    )
    # _restart_sections resets sort keys from 0; keep them after the court sections.
    for index, row in enumerate(sections):
        row["sort_key"] = index
    sections.append(
        _section(
            "1",
            "THE NEW MIRPUR TOWN DEVELOPMENT AUTHORITY ACT, 1962\n"
            "1. Short title.- This Act may be called the New Mirpur Town Development Authority Act, 1962.",
            "Short title",
            len(sections),
        )
    )
    result = classify_gok_multi_act(
        stored_name=CONCILIATION_NAME,
        source_url=CONCILIATION_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MISMATCH
    assert result.reason == REASON_BLEED
    assert "numbering_restart" in result.signals or "second_act_title" in result.signals


def test_amending_and_repealing_act_title_on_its_own_line_is_not_a_second_act():
    """A cited act name wrapped onto its own line is not another statute in this PDF."""
    sections = [
        _section(
            "1",
            "This Act may be called the Azad Jammu and Kashmir Finance Act, 2023.",
            "Short title",
            0,
        ),
        _section(
            "2",
            "Amendment of the Sales Tax Act.— In\n"
            "The Azad Jammu and Kashmir Sales Tax Act, 1990\n"
            "in section 3, for the words \"ten percent\" the words \"fifteen percent\" shall be substituted.",
            "Amendment of the Sales Tax Act",
            1,
        ),
        _section(
            "3",
            "The following Act is hereby repealed, namely:—\n"
            "The Azad Jammu and Kashmir Entertainments Duty Act, 1958\n"
            "and the repeal shall take effect on the day this Act comes into force.",
            "Repeal",
            2,
        ),
    ]
    result = classify_gok_multi_act(
        stored_name=FINANCE_NAME,
        source_url=FINANCE_URL,
        sections=sections,
    )
    assert result.verdict == VERDICT_MATCH
    assert result.action == ACTION_NONE
    assert result.reason is None
    assert "second_act_title" not in result.signals


def test_truncate_keeps_amending_and_repealing_citations():
    assert truncate_at_second_act_heading(FINANCE_AMENDING_TEXT, FINANCE_NAME) == FINANCE_AMENDING_TEXT


def test_truncate_still_cuts_when_a_new_short_title_follows_the_heading():
    text = (
        "1. Short title.—This Act may be called the Azad Jammu and Kashmir Conciliation Courts Act, 1962.\n"
        "In\n"
        "THE NEW MIRPUR TOWN DEVELOPMENT AUTHORITY ACT, 1962\n"
        "1. Short title.—This Act may be called the New Mirpur Town Development Authority Act, 1962.\n"
        "12. Headquarters of the Authority.—The headquarters of the Authority shall be at Mirpur.\n"
    )
    cut = truncate_at_second_act_heading(text, CONCILIATION_NAME)
    assert "Conciliation Courts" in cut
    assert "Headquarters of the Authority" not in cut


def test_truncate_ignores_inline_amendment_citation():
    text = (
        "1. Short title.—This Act may be called the Azad Jammu and Kashmir Conciliation Courts Act, 1962.\n\n"
        "2. Amendment.—Section 4 was amended by the Finance Act, 1990 and shall be read accordingly.\n"
    )
    assert truncate_at_second_act_heading(text, CONCILIATION_NAME) == text


def test_ajk_extract_does_not_merge_second_act_from_one_pdf():
    from scraper.extractors.deterministic import extract_statute_deterministic

    extracted = extract_statute_deterministic(
        html=None,
        text=TWO_ACT_PDF,
        source_meta={
            "source_name": "AJKAssembly",
            "url": CONCILIATION_URL.split("|", 1)[0],
            "act_title": CONCILIATION_NAME,
        },
    )
    blob = "\n".join((s.get("section_text") or "") for s in extracted["sections"])
    assert "Conciliation Court" in blob
    assert "Headquarters of the Authority" not in blob
    assert "New Mirpur Town Development Authority Act" not in blob


def test_reconcile_does_not_reappend_a_cut_second_act():
    from scraper.extractors.deterministic import extract_statute_deterministic
    from scraper.extractors.validation import reconcile_statute

    extracted = extract_statute_deterministic(
        html=None,
        text=TWO_ACT_PDF,
        source_meta={
            "source_name": "AJKAssembly",
            "url": CONCILIATION_URL.split("|", 1)[0],
            "act_title": CONCILIATION_NAME,
        },
    )
    cut = truncate_at_second_act_heading(TWO_ACT_PDF, extracted["statute_name"] or CONCILIATION_NAME)
    ai = {
        "sections": [
            {
                "section_number": "12",
                "section_title": "Headquarters of the Authority",
                "section_text": (
                    "12. Headquarters of the Authority.—The headquarters of the Authority "
                    "shall be at Mirpur and the Authority shall meet there."
                ),
            }
        ]
    }
    outcome = reconcile_statute(deterministic=extracted, ai=ai, raw_text=cut, min_confidence=0.2)
    blob = "\n".join((s.get("section_text") or "") for s in outcome.data["sections"])
    assert "Headquarters of the Authority" not in blob
    assert "Conciliation Court" in blob


def test_other_assembly_extract_is_not_cut_by_this_guard():
    from scraper.extractors.deterministic import extract_statute_deterministic

    extracted = extract_statute_deterministic(
        html=None,
        text=TWO_ACT_PDF,
        source_meta={
            "source_name": "NationalAssembly",
            "url": "https://www.na.gov.pk/uploads/documents/two-act.pdf",
            "act_title": CONCILIATION_NAME,
        },
    )
    blob = "\n".join((s.get("section_text") or "") for s in extracted["sections"])
    assert "Headquarters of the Authority" in blob or "New Mirpur" in blob


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _add_statute(db, *, name: str, url: str, sections: list[dict], reason: str | None = None, run_id: str = "prior"):
    statute = Statute(
        name=name,
        jurisdiction="AJK",
        source_name="AJKAssembly",
        source_url=url,
        year_enacted=1962,
    )
    db.add(statute)
    await db.flush()
    for index, sec in enumerate(sections):
        row = StatuteSection(
            statute_id=statute.id,
            section_number=sec["section_number"],
            section_title=sec.get("section_title") or None,
            sort_key=sec.get("sort_key", index),
        )
        db.add(row)
        await db.flush()
        ver = StatuteSectionVersion(
            section_id=row.id,
            version_no=1,
            section_text=sec["section_text"],
            text_hash=_text_hash(f"{statute.id}:{sec['section_number']}:{sec['section_text']}"),
        )
        db.add(ver)
        await db.flush()
        row.current_version_id = ver.id
    if reason:
        db.add(
            DocumentQuarantine(
                doc_type="statute",
                doc_id=statute.id,
                reason=reason,
                run_id=run_id,
            )
        )
    await db.commit()
    return statute


@pytest.mark.asyncio
async def test_dry_run_detects_patterns_and_writes_nothing(db):
    conciliation = await _add_statute(
        db,
        name=CONCILIATION_NAME,
        url=CONCILIATION_URL,
        sections=_mirpur_bleed_sections(),
        reason=REASON_BLEED,
        run_id="already-bleed",
    )
    constitution = await _add_statute(
        db,
        name=CONSTITUTION_NAME + " copy",
        url=LEGISLATIVE_ASSEMBLY_URL,
        sections=_plain_sections(),
        reason=REASON_NAME_MISMATCH,
        run_id="already-name",
    )
    visible = await _add_statute(
        db,
        name=CONCILIATION_NAME + " visible",
        url=CONCILIATION_URL,
        sections=_mirpur_bleed_sections(),
    )
    same_instrument = await _add_statute(
        db,
        name="The Azad Jammu and Kashmir Extra-Territorial Offences Act, 1972",
        url="https://law.gok.pk/wp-content/uploads/2021/02/EXTRA-TERRITORIAL-OFFENCES-ACT-1972.pdf",
        sections=_restart_sections(["9", "10", "11", "12", "13", "1", "2", "3", "4"]),
    )
    clean = await _add_statute(
        db,
        name=MIRPUR_NAME,
        url=MIRPUR_URL,
        sections=[
            _section("1", "This Act may be called the New Mirpur Town Development Authority Act, 1962.", "Short title", 0),
            _section(
                "2",
                '"Authority" means the New Mirpur Town Development Authority and "Tribunal" means the Tribunal under this Act.',
                "Definitions",
                1,
            ),
            _section("12", "The headquarters of the Authority shall be at Mirpur.", "Headquarters of the Authority", 2),
        ],
    )
    other_host = await _add_statute(
        db,
        name="Senate Owned Act, 2020",
        url="https://senate.gov.pk/uploads/documents/not-gok.pdf",
        sections=_restart_sections(["10", "11", "12", "13", "1", "2", "3"]),
    )
    before = (await db.execute(select(DocumentQuarantine))).scalars().all()
    before_ids = {(row.doc_type, row.doc_id, row.reason, row.run_id) for row in before}

    out = await run_gok_multi_act_bleed(apply=False)
    assert out["dry_run"] is True
    assert out["applied"] is None
    assert out["population"] == 5
    assert out["counts"][VERDICT_MISMATCH] >= 3
    by_id = {row["statute_id"]: row for row in out["rows"]}
    assert by_id[str(visible.id)]["verdict"] == VERDICT_MISMATCH
    assert by_id[str(visible.id)]["reason"] == REASON_BLEED
    assert by_id[str(visible.id)]["already_quarantined"] is False
    assert by_id[str(same_instrument.id)]["verdict"] == VERDICT_MATCH
    assert "numbering_restart" not in by_id[str(same_instrument.id)]["signals"]
    assert by_id[str(conciliation.id)]["already_quarantined"] is True
    assert by_id[str(conciliation.id)]["verdict"] == VERDICT_MISMATCH
    assert by_id[str(constitution.id)]["reason"] == REASON_NAME_MISMATCH
    assert by_id[str(clean.id)]["verdict"] == VERDICT_MATCH
    assert str(other_host.id) not in by_id

    after = (await db.execute(select(DocumentQuarantine))).scalars().all()
    assert {(row.doc_type, row.doc_id, row.reason, row.run_id) for row in after} == before_ids
    refreshed = (await db.execute(select(Statute).where(Statute.id == same_instrument.id))).scalars().one()
    assert refreshed.name == "The Azad Jammu and Kashmir Extra-Territorial Offences Act, 1972"
    assert refreshed.needs_section_refetch is False


@pytest.mark.asyncio
async def test_apply_quarantines_visible_rows_and_is_idempotent(db):
    visible = await _add_statute(
        db,
        name="The Azad Jammu and Kashmir Regulation of Services Act, 1986",
        url="https://law.gok.pk/wp-content/uploads/2021/03/REGULATION-OF-SERVICES-ACT-1986.pdf",
        sections=_mirpur_bleed_sections(),
    )
    already = await _add_statute(
        db,
        name=CONCILIATION_NAME,
        url=CONCILIATION_URL,
        sections=_mirpur_bleed_sections(),
        reason=REASON_BLEED,
        run_id="droplet-containment",
    )
    clean = await _add_statute(
        db,
        name="The Azad Jammu and Kashmir Animals Slaughter Control Act, 1970",
        url="https://www.law.gok.pk/wp-content/uploads/2022/01/ANIMALS-SLAUGHTER-CONTROL-ACT-1970.pdf",
        sections=_restart_sections(["1993", "1", "2", "3", "4", "5", "6"]),
    )
    section_text = (
        await db.execute(
            select(StatuteSectionVersion.section_text)
            .join(StatuteSection, StatuteSection.current_version_id == StatuteSectionVersion.id)
            .where(StatuteSection.statute_id == visible.id, StatuteSection.section_number == "1")
        )
    ).scalar_one()

    first = await run_gok_multi_act_bleed(apply=True)
    assert first["dry_run"] is False
    assert first["applied"]["quarantined"] == 1
    by_id = {row["statute_id"]: row for row in first["rows"]}
    assert by_id[str(clean.id)]["verdict"] == VERDICT_MATCH
    assert "numbering_restart" not in by_id[str(clean.id)]["signals"]
    assert "year_before_section_one" in by_id[str(clean.id)]["signals"]
    assert first["applied"]["run_id"]
    rows = (await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == visible.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].reason == REASON_BLEED
    assert rows[0].run_id == first["applied"]["run_id"]
    prior = (
        await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == already.id))
    ).scalars().one()
    assert prior.reason == REASON_BLEED
    assert prior.run_id == "droplet-containment"
    assert (await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == clean.id))).scalars().first() is None
    refreshed = (await db.execute(select(Statute).where(Statute.id == visible.id))).scalars().one()
    assert refreshed.name == "The Azad Jammu and Kashmir Regulation of Services Act, 1986"
    assert refreshed.needs_section_refetch is False
    still = (
        await db.execute(
            select(StatuteSectionVersion.section_text)
            .join(StatuteSection, StatuteSection.current_version_id == StatuteSectionVersion.id)
            .where(StatuteSection.statute_id == visible.id, StatuteSection.section_number == "1")
        )
    ).scalar_one()
    assert still == section_text

    second = await run_gok_multi_act_bleed(apply=True)
    assert second["applied"]["quarantined"] == 0
    again = (await db.execute(select(DocumentQuarantine).where(DocumentQuarantine.doc_id == visible.id))).scalars().all()
    assert len(again) == 1
    assert again[0].run_id == first["applied"]["run_id"]
    assert again[0].reason == REASON_BLEED


@pytest.mark.asyncio
async def test_undo_removes_only_this_runs_gok_reasons(db):
    target = await _add_statute(
        db,
        name=CONSTITUTION_NAME,
        url=COURTS_CODE_URL,
        sections=_plain_sections(),
    )
    keeper = await _add_statute(
        db,
        name="Operator Hold Act, 1955",
        url="https://law.gok.pk/wp-content/uploads/2018/01/OPERATOR-HOLD-ACT-1955.pdf",
        sections=_court_sections(),
        reason="operator:manual_review",
        run_id="human",
    )
    # Share the apply run id only if undo is careless; give the keeper a different row.
    applied = await run_gok_multi_act_bleed(apply=True)
    run_id = applied["applied"]["run_id"]
    undone = await run_gok_multi_act_bleed(undo=run_id)
    assert undone["undone"]["quarantine_rows_removed"] == 1
    left = (await db.execute(select(DocumentQuarantine))).scalars().all()
    assert [(row.doc_id, row.reason) for row in left] == [(keeper.id, "operator:manual_review")]
    still = (await db.execute(select(Statute).where(Statute.id == target.id))).scalars().one()
    assert still.name == CONSTITUTION_NAME
