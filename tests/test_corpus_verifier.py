"""Read-only corpus record verifier."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select, text

from scraper.config import settings
from scraper.models import Court, Judgment
from scraper.tasks.corpus_verifier import verify_judgments_batch
from scraper.verification.rules import verify_judgment_row


@pytest.mark.asyncio
async def test_rules_fail_header_citation_mismatch():
    j = Judgment(
        id=uuid4(),
        source_name="PakistanLawSite",
        canonical_citation="PLD 2024 SC 1",
        court_name="Supreme Court of Pakistan",
        full_text="1999 MLD 99\nIN THE SUPREME COURT\nJUDGMENT\n" + ("x" * 600),
        full_text_hash="hdr_mismatch",
        access_method="rendered_copy",
        year=2024,
    )
    out = verify_judgment_row(j, court_row=None, duplicate_citations=set(), duplicate_hashes=set())
    assert out.verdict == "FAIL"
    assert "header_citation_mismatch" in out.reason_codes


@pytest.mark.asyncio
async def test_rules_pass_minimal_pls_judgment():
    j = Judgment(
        id=uuid4(),
        source_name="PakistanLawSite",
        canonical_citation="PLD 2024 SC 1",
        court_name="Supreme Court of Pakistan",
        full_text=(
            "PLD 2024 SC 1\n"
            "IN THE SUPREME COURT OF PAKISTAN\n"
            "JUDGMENT\n"
            "Muhammad Akram versus The State\n"
            + ("x" * 600)
        ),
        full_text_hash="pass_row",
        access_method="rendered_copy",
        year=2024,
    )
    sc = Court(name="Supreme Court of Pakistan", short_code="SC", court_level="supreme_court")
    out = verify_judgment_row(j, court_row=sc, duplicate_citations=set(), duplicate_hashes=set())
    assert out.verdict == "PASS"


@pytest.mark.asyncio
async def test_verifier_batch_writes_results_only_in_verification_schema(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_CORPUS_RECORD_VERIFIER", True)
    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().first()
    jid = uuid4()
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_id=sc.id if sc else None,
            court_name="Supreme Court of Pakistan",
            canonical_citation="PLD 2024 SC 9",
            full_text=(
                "PLD 2024 SC 9\n"
                "IN THE SUPREME COURT OF PAKISTAN\n"
                "JUDGMENT\n"
                "A versus B\n"
                + ("y" * 600)
            ),
            full_text_hash="verifier_batch",
            access_method="rendered_copy",
            year=2024,
        )
    )
    await db.commit()
    before = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().one()
    result = await verify_judgments_batch(limit=50, fingerprint=False)
    assert result.get("judgments_processed", 0) >= 1
    row = (
        await db.execute(
            text(
                "SELECT verdict FROM verification.verification_results "
                "WHERE record_kind = 'judgment' AND record_id = :id"
            ),
            {"id": str(jid)},
        )
    ).first()
    assert row is not None
    after = (await db.execute(select(Judgment).where(Judgment.id == jid))).scalars().one()
    assert after.full_text == before.full_text
    assert after.court_name == before.court_name
