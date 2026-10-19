"""B1 regression: judgment identity (citation vs hash, scoped hash unique, courtless PLD)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from scraper.fetchers import canonical_text_hash, record_provenance, stage_judgment
from scraper.models import Court, Judgment, QuarantineQueue
from scraper.tasks.promotion import pls_own_citation, promote_judgment_staging

_PAD = " The petition is allowed for the reasons recorded. " * 35


def _judgment_body(citation_line: str, court_line: str, docket: str, extra: str = "") -> str:
    return (
        f"{citation_line}\n{court_line}\n{docket}\n"
        f"Ali Khan versus The State\nBefore Hon. Mr. Justice Example\n"
        f"Decided on 1st January, 2024.\nJUDGMENT\n{_PAD}{extra}"
    )


async def _stage_pls(db, login_source, *, extracted: str, citations: list[str], body: str, court: str):
    prov = await record_provenance(
        db,
        source=login_source,
        url=f"https://www.pakistanlawsite.com/case/{extracted}",
        content=body.encode(),
        content_kind="text",
    )
    st = await stage_judgment(
        db,
        source=login_source,
        prov=prov,
        raw_html=None,
        raw_text=body,
        url=prov.source_url,
    )
    st.extracted_citation = extracted
    st.reconciled_json = {
        "citations": citations,
        "court": court,
        "court_canonical": court,
        "year": 2024,
        "decision_date": "2024-01-01",
        "case_title": "Ali Khan versus The State",
        "judge_names": ["Example J."],
        "document_type": "full_judgment",
    }
    st.status = "extracted"
    st.confidence_score = 0.99
    st.raw_text_hash = canonical_text_hash(body)
    return st


@pytest.mark.asyncio
async def test_citation_and_hash_pointing_at_different_judgments_quarantines_identity_conflict(db, login_source):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    text_a = _judgment_body("PLD 2024 SC 100", "IN THE SUPREME COURT OF PAKISTAN", "C.P. No. 1 of 2024", " body A ")
    text_b = _judgment_body("PLD 2024 SC 200", "IN THE SUPREME COURT OF PAKISTAN", "C.P. No. 2 of 2024", " body B ")
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC 100",
            full_text=text_a,
            full_text_hash=canonical_text_hash(text_a),
            court_id=sc.id,
            court_name=sc.name,
            docket_number="C.P. No. 1 of 2024",
            decision_date=date(2024, 1, 1),
            reporter="PLD",
            source_name="PakistanLawSite",
        )
    )
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC 200",
            full_text=text_b,
            full_text_hash=canonical_text_hash(text_b),
            court_id=sc.id,
            court_name=sc.name,
            docket_number="C.P. No. 2 of 2024",
            decision_date=date(2024, 1, 1),
            reporter="PLD",
            source_name="PakistanLawSite",
        )
    )
    await db.commit()

    st = await _stage_pls(
        db,
        login_source,
        extracted="PLD 2024 SC 100",
        citations=["PLD 2024 SC 100"],
        body=text_b,
        court="Supreme Court of Pakistan",
    )
    assert await promote_judgment_staging(db, st) == "quarantined"
    q = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == st.id))).scalars().one()
    assert "identity_conflict" in (q.reason or "")


@pytest.mark.asyncio
async def test_identical_text_different_court_and_docket_quarantines_identity_conflict(db, login_source):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    lhc = (await db.execute(select(Court).where(Court.short_code == "LHC"))).scalars().one()
    shared = _judgment_body("PLD 2024 SC 301", "IN THE SUPREME COURT OF PAKISTAN", "C.P. No. 10 of 2024")
    h = canonical_text_hash(shared)
    db.add(
        Judgment(
            canonical_citation="PLD 2024 SC 301",
            full_text=shared,
            full_text_hash=h,
            court_id=sc.id,
            court_name=sc.name,
            docket_number="C.P. No. 10 of 2024",
            decision_date=date(2024, 1, 1),
            reporter="PLD",
            source_name="PakistanLawSite",
        )
    )
    await db.commit()

    st = await _stage_pls(
        db,
        login_source,
        extracted="PLD 2024 Lahore 301",
        citations=["PLD 2024 Lahore 301"],
        body=shared,
        court="Lahore High Court",
    )
    assert await promote_judgment_staging(db, st) == "quarantined"
    q = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == st.id))).scalars().one()
    assert "identity_conflict" in (q.reason or "")
    assert len((await db.execute(select(Judgment).where(Judgment.full_text_hash == h))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_unreported_not_relabelled_when_hash_matches_but_docket_differs(db, login_source):
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    body = _judgment_body("PLD 2020 SC 9", "IN THE SUPREME COURT OF PAKISTAN", "C.P. No. 99 of 2020")
    h = canonical_text_hash(body)
    unrep = Judgment(
        canonical_citation="UNREPORTED SC C.P. No. 1 of 2020 (2020-01-01)",
        full_text=body,
        full_text_hash=h,
        court_id=sc.id,
        court_name=sc.name,
        docket_number="C.P. No. 1 of 2020",
        decision_date=date(2020, 1, 1),
        unreported=True,
        source_name="SupremeCourt",
    )
    db.add(unrep)
    await db.commit()
    old_citation = unrep.canonical_citation

    st = await _stage_pls(
        db,
        login_source,
        extracted="PLD 2020 SC 9",
        citations=["PLD 2020 SC 9"],
        body=body,
        court="Supreme Court of Pakistan",
    )
    st.reconciled_json["decision_date"] = "2020-01-01"
    assert await promote_judgment_staging(db, st) == "quarantined"
    await db.refresh(unrep)
    assert unrep.canonical_citation == old_citation and unrep.unreported is True


@pytest.mark.asyncio
async def test_courtless_pld_picks_header_court_and_quarantines_ambiguous_outside_header(db, login_source):
    sc_header = "IN THE SUPREME COURT OF PAKISTAN"
    body_ok = (
        "PLD 2026 SC 7\n"
        f"{sc_header}\nC.P. No. 1 of 2026\n"
        "PLD 2026 Lahore 7 rel. cited for contrast.\n"
        f"Ali Khan versus The State\n{_PAD}"
    )
    res = pls_own_citation("2026 PLD 7", ["PLD 2026 SC 7", "PLD 2026 Lahore 7"], page_text=body_ok)
    assert res.citation == "PLD 2026 SC 7" and res.quarantine_reason is None

    body_bad = (
        f"{sc_header}\nC.P. No. 2 of 2026\n"
        f"Ali Khan versus The State\n{_PAD}"
        "Later in the body PLD 2026 Lahore 7 and PLD 2026 SC 7 appear without being this judgment.\n"
    )
    res_bad = pls_own_citation("2026 PLD 7", ["PLD 2026 Lahore 7", "PLD 2026 SC 7"], page_text=body_bad)
    assert res_bad.quarantine_reason == "pld_court_ambiguous"

    st = await _stage_pls(
        db,
        login_source,
        extracted="2026 PLD 7",
        citations=["PLD 2026 SC 7", "PLD 2026 Lahore 7"],
        body=body_bad,
        court="Supreme Court of Pakistan",
    )
    assert await promote_judgment_staging(db, st) == "quarantined"


@pytest.mark.parametrize("grid", ("2026 PLD 7", "PLD 2026 7"))
def test_reversed_courtless_pld_grid_order_agrees(grid):
    body = (
        "PLD 2026 SC 7\nIN THE SUPREME COURT OF PAKISTAN\nC.P. No. 3 of 2026\n" + _PAD
    )
    a = pls_own_citation(grid, ["PLD 2026 SC 7"], page_text=body)
    b = pls_own_citation(
        "PLD 2026 7" if grid == "2026 PLD 7" else "2026 PLD 7",
        ["PLD 2026 SC 7"],
        page_text=body,
    )
    assert a.citation == b.citation == "PLD 2026 SC 7"
    assert a.quarantine_reason is None and b.quarantine_reason is None


async def test_mutant_m3_decision_date_must_match_for_identity(db):
    from scraper.tasks.promotion import _judgment_case_identity_matches

    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    j = Judgment(
        canonical_citation="PLD 2024 SC 1",
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 1, 1),
    )
    assert not _judgment_case_identity_matches(
        j, court_id=sc.id, docket_number="C.P. 1 of 2024", decision_date=date(2024, 2, 1)
    )


async def test_mutant_m4_same_court_different_docket_is_not_identity(db):
    from scraper.tasks.promotion import _judgment_case_identity_matches

    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    j = Judgment(
        canonical_citation="PLD 2024 SC 1",
        court_id=sc.id,
        docket_number="C.P. 1 of 2024",
        decision_date=date(2024, 1, 1),
    )
    assert not _judgment_case_identity_matches(
        j, court_id=sc.id, docket_number="C.P. 2 of 2024", decision_date=date(2024, 1, 1)
    )


def test_mutant_m6_multi_court_pld_in_header_quarantines():
    body = (
        "PLD 2026 SC 7\nPLD 2026 Lahore 7\nIN THE SUPREME COURT OF PAKISTAN\nC.P. 1 of 2026\n"
        + _PAD
    )
    res = pls_own_citation("2026 PLD 7", ["PLD 2026 SC 7", "PLD 2026 Lahore 7"], page_text=body)
    assert res.quarantine_reason == "pld_court_ambiguous"


async def test_mutant_m8_empty_docket_never_identity_matches(db):
    from scraper.tasks.promotion import _judgment_case_identity_matches

    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    j = Judgment(canonical_citation="x", court_id=sc.id, docket_number="", decision_date=date(2024, 1, 1))
    assert not _judgment_case_identity_matches(j, court_id=sc.id, docket_number="C.P. 1 of 2024", decision_date=date(2024, 1, 1))
    assert not _judgment_case_identity_matches(j, court_id=sc.id, docket_number=None, decision_date=date(2024, 1, 1))
