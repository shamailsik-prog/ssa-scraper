"""B7/R1 K1: document_quarantine rows hidden from search, corpus API, export, embeddings."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.models import (
    Citation,
    DocumentQuarantine,
    EmbeddingQueue,
    Judgment,
    Statute,
    StatuteSection,
    StatuteSectionVersion,
)
from scraper.tasks.embeddings import process_embedding_queue

KEY = "test-corpus-key"
H = {"X-Corpus-Key": KEY}


@pytest.fixture(autouse=True)
def _corpus_key(monkeypatch):
    from scraper.routers import corpus_read

    monkeypatch.setattr(settings, "CORPUS_API_KEY", KEY)
    corpus_read._KEY_FAILS.clear()
    yield
    corpus_read._KEY_FAILS.clear()


async def _seed_visible_and_quarantined(db):
    visible = Judgment(
        canonical_citation="PLD 2020 SC 1",
        case_title="Visible Case",
        court_name="Supreme Court of Pakistan",
        year=2020,
        full_text="The petitioner seeks bail and the court grants relief after full hearing on merits.",
        access_method="public",
    )
    hidden = Judgment(
        canonical_citation="PLD 2020 SC 99",
        case_title="Quarantined Case",
        court_name="Supreme Court of Pakistan",
        year=2020,
        full_text="Hidden judgment body text that should not appear in search or export.",
        access_method="public",
    )
    db.add_all([visible, hidden])
    await db.flush()
    db.add(Citation(judgment_id=visible.id, citation_string="PLD 2020 SC 1", is_primary=True))
    db.add(EmbeddingQueue(record_id=visible.id, table_name="judgment", access_method="public"))
    db.add(EmbeddingQueue(record_id=hidden.id, table_name="judgment", access_method="public"))
    db.add(
        DocumentQuarantine(
            doc_type="judgment",
            doc_id=hidden.id,
            reason="b7_bad_document:empty_or_short_text",
            run_id="seed-run",
        )
    )

    crpc = Statute(name="Code of Criminal Procedure, 1898", short_name="CrPC", year_enacted=1898)
    bad_statute = Statute(name="Empty Quarantined Act", short_name="EQA", year_enacted=2020)
    db.add_all([crpc, bad_statute])
    await db.flush()
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=bad_statute.id,
            reason="b7_bad_document:no_parseable_sections",
            run_id="seed-run",
        )
    )
    sec = StatuteSection(statute_id=crpc.id, section_number="497", section_title="Bail")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="Bail may be granted.", text_hash="h497")
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    db.add(
        EmbeddingQueue(record_id=sec.id, table_name="statute_section", access_method="public")
    )
    await db.commit()


@pytest.mark.asyncio
async def test_quarantined_judgments_absent_from_all_surfaces(db, client, admin_headers, monkeypatch):
    await _seed_visible_and_quarantined(db)

    hits = client.get("/corpus/v1/judgments/search", params={"q": "Quarantined"}, headers=H).json()["results"]
    assert all("99" not in (h.get("citation") or "") for h in hits)
    assert client.get("/corpus/v1/judgments/by-citation", params={"citation": "PLD 2020 SC 99"}, headers=H).status_code == 404

    api_hits = client.get("/api/search", params={"q": "Quarantined"}, headers=admin_headers).json()
    assert all(r["citation"] != "PLD 2020 SC 99" for r in api_hits)

    export_lines = client.get("/export/judgments?format=jsonl", headers=admin_headers).text.strip().splitlines()
    citations = {json.loads(line)["canonical_citation"] for line in export_lines if line}
    assert "PLD 2020 SC 99" not in citations
    assert "PLD 2020 SC 1" in citations

    async def _noop_embed(texts, **kwargs):
        return [[0.0] * settings.EMBEDDING_DIM for _ in texts]

    async def _identity_ok():
        return True

    monkeypatch.setattr("scraper.tasks.embeddings.embed_texts", _noop_embed)
    monkeypatch.setattr("scraper.tasks.embeddings.embedding_identity_matches", _identity_ok)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    counts = await process_embedding_queue(limit=10)
    assert counts["skipped_quarantined"] >= 1
    assert counts["embedded"] >= 1

    from scraper.database import SessionLocal

    async with SessionLocal() as session:
        hidden = (
            await session.execute(select(Judgment).where(Judgment.canonical_citation == "PLD 2020 SC 99"))
        ).scalars().first()
        assert hidden is not None
        q_rows = (
            await session.execute(
                select(EmbeddingQueue).where(EmbeddingQueue.record_id == hidden.id, EmbeddingQueue.table_name == "judgment")
            )
        ).scalars().all()
        assert any(r.status == "skipped" for r in q_rows)


@pytest.mark.asyncio
async def test_quarantined_statutes_absent_from_export_and_section_api(db, client, admin_headers):
    await _seed_visible_and_quarantined(db)

    statutes = client.get("/api/statutes", params={"statute_name": "Quarantined"}, headers=admin_headers).json()
    assert all(s["name"] != "Empty Quarantined Act" for s in statutes)

    export_text = client.get("/export/statutes?format=jsonl", headers=admin_headers).text
    assert "Empty Quarantined Act" not in export_text
    assert "Code of Criminal Procedure" in export_text

    assert client.get("/corpus/v1/statutes/section", params={"statute": "EQA", "section": "1"}, headers=H).status_code == 404
    sec = client.get("/corpus/v1/statutes/section", params={"statute": "CrPC", "section": "497"}, headers=H).json()
    assert "Bail" in (sec.get("text") or "")
