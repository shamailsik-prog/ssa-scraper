"""INDIA CaseNames and court slugs must not be stored as Pakistani courts.

Live MAIN mistags (2026-10-28): CaseName YYYY{CODE}-INDIA#### was saved as a
Pakistani court because directory substring aliases hit inside the Indian name
(`lah` in Allahabad, `kar` in Karnataka, `supreme court` in Supreme Court of India).
"""

from __future__ import annotations

import pytest

from scraper.database import COURT_SEED
from scraper.extractors.deterministic import extract_judgment_deterministic
from scraper.extractors.validation import court_in_directory
from scraper.parsers.citation_extractor import canonical_court

PAKISTANI = {
    "Supreme Court of Pakistan",
    "Lahore High Court",
    "High Court of Sindh",
    "Sindh High Court",
}


def _directory() -> dict[str, str]:
    directory: dict[str, str] = {}
    for row in COURT_SEED:
        name, short, aliases = row[0], row[1], row[5]
        directory[name.lower()] = name
        directory[short.lower()] = name
        for alias in aliases:
            directory[alias.lower().replace(".", "")] = name
    return directory


def _not_pakistani(court: str | None) -> None:
    assert court
    assert court not in PAKISTANI
    low = court.lower()
    assert "pakistan" not in low
    assert "lahore" not in low
    assert "sindh" not in low
    assert "karachi" not in low


@pytest.mark.parametrize(
    "label, expected",
    [
        ("SUPREME-COURT-INDIA", "Supreme Court of India"),
        ("Supreme Court of India", "Supreme Court of India"),
        ("ALLAHABAD-HIGH-COURT-INDIA", "Allahabad High Court, India"),
        ("Allahabad High Court", "Allahabad High Court, India"),
        ("KARNATAKA-HIGH-COURT-INDIA", "Karnataka High Court, India"),
        ("MADRAS-HIGH-COURT-INDIA", "Madras High Court, India"),
        ("MADHYA-PRADESH-HIGH-COURT-INDIA", "Madhya Pradesh High Court, India"),
        ("ANDHRA-PRADESH-HIGH-COURT-INDIA", "Andhra Pradesh High Court, India"),
    ],
)
def test_india_court_label_does_not_resolve_to_a_pakistani_court(label, expected):
    resolved = court_in_directory(label, _directory())
    assert resolved == expected
    _not_pakistani(resolved)


@pytest.mark.parametrize(
    "label, expected",
    [
        ("SUPREME-COURT", "Supreme Court of Pakistan"),
        ("Supreme Court", "Supreme Court of Pakistan"),
        ("LAHORE-HIGH-COURT", "Lahore High Court"),
        ("Lahore", "Lahore High Court"),
        ("Karachi", "High Court of Sindh"),
        ("SINDH-HIGH-COURT", "High Court of Sindh"),
    ],
)
def test_pakistani_court_labels_unchanged(label, expected):
    assert court_in_directory(label, _directory()) == expected


@pytest.mark.parametrize(
    "label, needle",
    [
        ("Supreme Court of India", "India"),
        ("Allahabad High Court", "Allahabad"),
        ("KARNATAKA-HIGH-COURT-INDIA", "Karnataka"),
    ],
)
def test_canonical_court_does_not_collapse_indian_names(label, needle):
    resolved = canonical_court(label)
    assert needle in (resolved or "")
    _not_pakistani(resolved)
    assert resolved != "SC"
    assert resolved != "Lahore"
    assert resolved != "Karachi"


ALLAHABAD_BODY = (
    "1998 PTD 830\n"
    "Before V. N. Khare and S. Rafat Alam, JJ\n"
    "A---Petitioner\n"
    "versus\n"
    "B---Respondent\n"
    "Income-tax Reference No. 12 of 1997, decided on 1st March, 1998.\n"
    "JUDGMENT\n"
    + ("The reference is answered against the department. " * 20)
)


@pytest.mark.parametrize(
    "case_name, grid_court, needle",
    [
        ("1998S-INDIA6037", "Supreme Court of Pakistan", "Supreme Court of India"),
        ("1998A-INDIA6012", "Lahore High Court", "Allahabad"),
        ("1998KR-INDIA6001", "High Court of Sindh", "Karnataka"),
        ("MADRAS-HIGH-COURT-INDIA", "Lahore High Court", "Madras"),
        ("MADHYA-PRADESH-HIGH-COURT-INDIA", "Supreme Court of Pakistan", "Madhya Pradesh"),
    ],
)
def test_india_case_name_overrides_pakistani_grid_court(case_name, grid_court, needle):
    url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        f"?CaseName={case_name}&court=&Row=0&bookName=undefined"
    )
    out = extract_judgment_deterministic(
        html=None,
        text=ALLAHABAD_BODY,
        source_meta={"citation": "1998 PTD 830", "court": grid_court, "url": url},
    )
    assert needle in (out["court"] or "")
    _not_pakistani(out["court"])


def test_pakistani_case_name_keeps_pakistani_court():
    url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=2025S701&court=&Row=0&bookName=undefined"
    )
    text = (
        "2025 SCMR 1\n"
        "IN THE SUPREME COURT OF PAKISTAN\n"
        "Before A, J\n"
        "C versus D\n"
        "JUDGMENT\n"
        + ("The appeal is dismissed. " * 20)
    )
    out = extract_judgment_deterministic(
        html=None,
        text=text,
        source_meta={"citation": "2025 SCMR 1", "court": "SUPREME-COURT", "url": url},
    )
    assert out["court"] in ("SUPREME-COURT", "Supreme Court of Pakistan", "Supreme Court")


def test_lahore_case_name_keeps_lahore():
    url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=1980L238&court=&Row=0&bookName=undefined"
    )
    text = (
        "1980 CLC 367\n"
        "[Lahore High Court]\n"
        "Before A, J\n"
        "E versus F\n"
        "JUDGMENT\n"
        + ("The petition is allowed. " * 20)
    )
    out = extract_judgment_deterministic(
        html=None,
        text=text,
        source_meta={"citation": "1980 CLC 367", "court": "Lahore High Court", "url": url},
    )
    assert out["court"] == "Lahore High Court"


@pytest.mark.asyncio
async def test_promotion_stores_allahabad_not_lahore(db, login_source):
    from scraper.fetchers import record_provenance, stage_judgment
    from scraper.models import Judgment
    from scraper.tasks.promotion import promote_judgment_staging
    from sqlalchemy import select

    url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=1998A-INDIA6012&court=&Row=0&bookName=undefined"
    )
    prov = await record_provenance(
        db,
        source=login_source,
        url=url,
        content=ALLAHABAD_BODY.encode("utf-8"),
        content_kind="text",
    )
    st = await stage_judgment(
        db,
        source=login_source,
        prov=prov,
        raw_html=None,
        raw_text=ALLAHABAD_BODY,
        url=url,
    )
    st.reconciled_json = {
        "citations": ["1998 PTD 830"],
        "court": "Lahore High Court",
        "court_canonical": "Lahore High Court",
        "year": 1998,
        "case_title": "A versus B",
        "judge_names": ["V. N. Khare", "S. Rafat Alam"],
        "document_type": "full_judgment",
    }
    st.extracted_citation = "1998 PTD 830"
    st.status = "extracted"
    st.confidence_score = 0.95
    await db.flush()

    assert await promote_judgment_staging(db, st) == "promoted"
    judgment = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().first()
    assert judgment is not None
    assert "Allahabad" in (judgment.court_name or "")
    _not_pakistani(judgment.court_name)


@pytest.mark.asyncio
async def test_promotion_rejects_unknown_india_code_tagged_lahore(db, login_source):
    from scraper.fetchers import record_provenance, stage_judgment
    from scraper.models import Judgment
    from scraper.tasks.promotion import promote_judgment_staging
    from sqlalchemy import func, select

    url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=1998ZZ-INDIA6099&court=&Row=0&bookName=undefined"
    )
    prov = await record_provenance(
        db,
        source=login_source,
        url=url,
        content=ALLAHABAD_BODY.encode("utf-8"),
        content_kind="text",
    )
    st = await stage_judgment(
        db,
        source=login_source,
        prov=prov,
        raw_html=None,
        raw_text=ALLAHABAD_BODY,
        url=url,
    )
    st.reconciled_json = {
        "citations": ["1998 PTD 831"],
        "court": "Lahore High Court",
        "court_canonical": "Lahore High Court",
        "year": 1998,
        "case_title": "C versus D",
        "judge_names": ["V. N. Khare"],
        "document_type": "full_judgment",
    }
    st.extracted_citation = "1998 PTD 831"
    st.status = "extracted"
    st.confidence_score = 0.95
    await db.flush()

    assert await promote_judgment_staging(db, st) == "quarantined"
    assert "india_case_pakistani_court" in (st.quarantine_reason or "")
    assert (await db.execute(select(func.count()).select_from(Judgment))).scalar() == 0


@pytest.mark.asyncio
async def test_promotion_keeps_pakistani_supreme_court_case(db, login_source):
    from scraper.fetchers import record_provenance, stage_judgment
    from scraper.models import Judgment
    from scraper.tasks.promotion import promote_judgment_staging
    from sqlalchemy import select

    text = (
        "2025 SCMR 14\n"
        "IN THE SUPREME COURT OF PAKISTAN\n"
        "Before A, J\n"
        "Province versus Qasim\n"
        "JUDGMENT\n"
        + ("The appeal fails. " * 30)
    )
    url = (
        "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch"
        "?CaseName=2025S702&court=&Row=0&bookName=undefined"
    )
    prov = await record_provenance(
        db, source=login_source, url=url, content=text.encode("utf-8"), content_kind="text"
    )
    st = await stage_judgment(
        db, source=login_source, prov=prov, raw_html=None, raw_text=text, url=url
    )
    st.reconciled_json = {
        "citations": ["2025 SCMR 14"],
        "court": "Supreme Court of Pakistan",
        "court_canonical": "Supreme Court of Pakistan",
        "year": 2025,
        "case_title": "Province versus Qasim",
        "judge_names": ["A"],
        "document_type": "full_judgment",
    }
    st.extracted_citation = "2025 SCMR 14"
    st.status = "extracted"
    st.confidence_score = 0.95
    await db.flush()
    assert await promote_judgment_staging(db, st) == "promoted"
    judgment = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().first()
    assert judgment.court_name == "Supreme Court of Pakistan"
