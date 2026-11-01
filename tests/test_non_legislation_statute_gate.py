"""Non-legislation must not become a statute.

Team.aspx on sindhlaws.gov.pk is a staff page. A PDF whose only title is
``GOVERNMENT OF SINDH`` is an office routing sheet. A real Act title stays.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from scraper.document_validation import validate_statute_reconciled_json
from scraper.models import Statute
from scraper.tasks.promotion import promote_statute_staging
from tests.test_b3_statute_promotion import _make_statute_staging

TEAM_URL = "https://sindhlaws.gov.pk/Team.aspx"
BARE_NAME = "GOVERNMENT OF SINDH"
DEPARTMENT_NAME = (
    "Law, Parliamentary Affairs & Criminal Prosecution Department, Government of Sindh"
)
REAL_ACT = "The Limitation Act, 1908"
_OFFICE = (
    "The Secretary shall keep the staff roster and the office telephone list for internal routing."
)


def _office_payload(name: str) -> dict:
    return {
        "statute_name": name,
        "year_enacted": 2026,
        "jurisdiction": "Sindh",
        "sections": [
            {"section_number": "1", "section_text": _OFFICE},
            {"section_number": "2", "section_text": "The Additional Secretary shall route files to the section officer."},
            {"section_number": "3", "section_text": "The Section Officer shall record the duty telephone for the day."},
            {"section_number": "4", "section_text": "The Office Superintendent shall file the staff directory each week."},
        ],
    }


def test_team_aspx_url_is_rejected_and_a_pdf_statute_url_is_not():
    from scraper.statute_non_legislation_gate import (
        HTML_TEAM_CONTACT_REASON,
        sindhlaws_html_statute_reject_reason,
        sindhlaws_non_statute_html_url,
    )

    assert sindhlaws_non_statute_html_url(TEAM_URL)
    assert sindhlaws_non_statute_html_url("https://www.sindhlaws.gov.pk/Team.aspx")
    assert sindhlaws_non_statute_html_url("https://sindhlaws.gov.pk/contact.aspx")
    assert sindhlaws_non_statute_html_url("https://sindhlaws.gov.pk/setup/publications/staff.html")
    assert sindhlaws_html_statute_reject_reason(kind="statute", url=TEAM_URL) == HTML_TEAM_CONTACT_REASON

    assert not sindhlaws_non_statute_html_url(
        "https://sindhlaws.gov.pk/setup/publications/PUB-26-000048.pdf"
    )
    assert not sindhlaws_non_statute_html_url(
        "https://sindhlaws.gov.pk/GazetteDetail.aspx?X=ACT&Year=2026"
    )
    assert not sindhlaws_non_statute_html_url("https://sindhlaws.gov.pk/Gazette.aspx?pg=ACT")
    assert not sindhlaws_non_statute_html_url("https://sindhlaws.gov.pk/")
    assert not sindhlaws_non_statute_html_url("https://sindhlaws.gov.pk/Library.aspx")
    assert not sindhlaws_non_statute_html_url(
        "https://pas.gov.pk/uploads/acts/sindh-finance-act-2024.html"
    )
    assert sindhlaws_html_statute_reject_reason(kind="judgment", url=TEAM_URL) is None


def test_bare_government_name_is_rejected():
    from scraper.statute_non_legislation_gate import (
        BARE_NAME_REASON,
        title_lacks_legislative_word,
    )

    assert title_lacks_legislative_word(BARE_NAME)
    assert title_lacks_legislative_word("  government   of   the   punjab ")
    assert title_lacks_legislative_word(DEPARTMENT_NAME)
    assert title_lacks_legislative_word("Law and Order Department, Government of Sindh")

    ok, reason = validate_statute_reconciled_json(_office_payload(BARE_NAME), raw_text=_OFFICE)
    assert ok is False
    assert reason == BARE_NAME_REASON

    ok, reason = validate_statute_reconciled_json(
        _office_payload(DEPARTMENT_NAME),
        raw_text=_OFFICE,
    )
    assert ok is False
    assert reason == BARE_NAME_REASON


def test_real_act_title_still_passes():
    from scraper.statute_non_legislation_gate import title_lacks_legislative_word

    for title in (
        REAL_ACT,
        "Pakistan Penal Code, 1860",
        "Income Tax Ordinance, 2001",
        "Civil Servants (Appointment, Promotion and Transfer) Rules, 1973",
        "The Sindh Seed Regulation, 2016",
        "Qanun-e-Shahadat Order, 1984",
        "Police Order, 2002",
        "The Constitution of the Islamic Republic of Pakistan",
        "The Sindh Local Government Ordinance, 1979",
        "سندھ لوکل گورنمنٹ (ترمیمی) ایکٹ، 2023",
    ):
        assert title_lacks_legislative_word(title) is False, title

    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": REAL_ACT,
            "year_enacted": 1908,
            "sections": [
                {
                    "section_number": "1",
                    "section_text": "This Act may be called the Limitation Act, 1908.",
                }
            ],
        },
        raw_text="This Act may be called the Limitation Act, 1908.",
    )
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_promotion_writes_no_statute_for_bare_government_name(db):
    from scraper.statute_non_legislation_gate import BARE_NAME_REASON

    st = await _make_statute_staging(db, _office_payload(BARE_NAME))
    assert await promote_statute_staging(db, st) == "quarantined"
    assert BARE_NAME_REASON in (st.quarantine_reason or "")
    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 0
