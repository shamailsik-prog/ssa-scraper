"""F1: one test per servable surface; each maps to a specific judgment_servable/statute_servable filter site."""

from __future__ import annotations

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
    Treatment,
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


def _quarantine(db, doc_type: str, doc_id) -> None:
    db.add(
        DocumentQuarantine(
            doc_type=doc_type,
            doc_id=doc_id,
            reason="test:quarantine",
            run_id="test-run",
        )
    )


@pytest.mark.asyncio
async def test_corpus_read_citation_join_hides_quarantined(db, client):
    """Filter at corpus_read.py _judgment_for citation join (~line 131)."""
    j = Judgment(
        canonical_citation="CANON 2020 SC 1",
        case_title="Alias Target",
        court_name="Supreme Court of Pakistan",
        year=2020,
        full_text="The court hears the matter at length and disposes of the appeal with detailed reasons on every issue raised.",
        access_method="public",
    )
    db.add(j)
    await db.flush()
    db.add(Citation(judgment_id=j.id, citation_string="PLD 2020 SC ALIAS", is_primary=False))
    _quarantine(db, "judgment", j.id)
    await db.commit()

    assert client.get("/corpus/v1/judgments/by-citation", params={"citation": "PLD 2020 SC ALIAS"}, headers=H).status_code == 404


@pytest.mark.asyncio
async def test_corpus_read_citing_judgments_hides_quarantined_citing(db, client):
    """Filter at corpus_read.py citing list (~line 245)."""
    cited = Judgment(
        canonical_citation="PLD 2019 SC 10",
        case_title="Cited Case",
        court_name="Supreme Court of Pakistan",
        year=2019,
        full_text="The court hears the matter at length and disposes of the appeal with detailed reasons on every issue raised.",
        access_method="public",
    )
    citing = Judgment(
        canonical_citation="PLD 2020 SC 20",
        case_title="Quarantined Citing",
        court_name="Supreme Court of Pakistan",
        year=2020,
        full_text="The court hears the matter at length and disposes of the appeal with detailed reasons on every issue raised.",
        access_method="public",
    )
    db.add_all([cited, citing])
    await db.flush()
    db.add(
        Treatment(
            citing_judgment_id=citing.id,
            cited_citation="PLD 2019 SC 10",
            cited_judgment_id=cited.id,
            label="followed",
            confidence=0.9,
            method="deterministic",
            evidence_passage="The court followed the earlier ruling.",
        )
    )
    _quarantine(db, "judgment", citing.id)
    await db.commit()

    body = client.get("/corpus/v1/judgments/citing", params={"citation": "PLD 2019 SC 10"}, headers=H).json()
    assert all(row["citation"] != "PLD 2020 SC 20" for row in body.get("citing", []))


@pytest.mark.asyncio
async def test_corpus_read_statute_short_name_hides_quarantined(db, client):
    """Filter at corpus_read.py short_name match (~line 279)."""
    st = Statute(name="Short Name Act, 2020", short_name="QSNA", year_enacted=2020)
    db.add(st)
    await db.flush()
    sec = StatuteSection(statute_id=st.id, section_number="1", section_title="Intro")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="Text.", text_hash="h1")
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    _quarantine(db, "statute", st.id)
    await db.commit()

    assert client.get("/corpus/v1/statutes/section", params={"statute": "QSNA", "section": "1"}, headers=H).status_code == 404


@pytest.mark.asyncio
async def test_corpus_read_statute_name_search_hides_quarantined(db, client):
    """Filter at corpus_read.py name ilike fallback (~line 286)."""
    st = Statute(name="Unique Quarantine Long Act Title", short_name=None, year_enacted=2020)
    db.add(st)
    await db.flush()
    sec = StatuteSection(statute_id=st.id, section_number="2", section_title="Body")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="Section body.", text_hash="h2")
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    _quarantine(db, "statute", st.id)
    await db.commit()

    assert (
        client.get(
            "/corpus/v1/statutes/section",
            params={"statute": "Unique Quarantine", "section": "2"},
            headers=H,
        ).status_code
        == 404
    )


@pytest.mark.asyncio
async def test_export_statutes_hides_quarantined(db, client, admin_headers):
    """Filter at export.py statutes export (~line 100)."""
    st = Statute(name="Export Hidden Act", short_name="EHA", year_enacted=2020)
    db.add(st)
    await db.flush()
    sec = StatuteSection(statute_id=st.id, section_number="9", section_title="Nine")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="Nine.", text_hash="h9")
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    _quarantine(db, "statute", st.id)
    await db.commit()

    text = client.get("/export/statutes?format=jsonl", headers=admin_headers).text
    assert "Export Hidden Act" not in text


@pytest.mark.asyncio
async def test_api_judgment_by_id_hides_quarantined(db, client, admin_headers):
    """Filter at search.py /judgments/{id} (~line 34)."""
    j = Judgment(
        canonical_citation="PLD 2021 SC 77",
        access_method="public",
        full_text="The court hears the matter at length and disposes of the appeal with detailed reasons on every issue raised.",
    )
    db.add(j)
    await db.flush()
    _quarantine(db, "judgment", j.id)
    await db.commit()

    assert client.get(f"/api/judgments/{j.id}", headers=admin_headers).status_code == 404


@pytest.mark.asyncio
async def test_api_statute_sections_hides_quarantined_parent(db, client, admin_headers):
    """Filter at search.py sections parent (~line 50)."""
    st = Statute(name="API Sections Act", short_name="ASA", year_enacted=2021)
    db.add(st)
    await db.flush()
    sec = StatuteSection(statute_id=st.id, section_number="1", section_title="One")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="One.", text_hash="h1a")
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    _quarantine(db, "statute", st.id)
    await db.commit()

    assert client.get(f"/api/statutes/{st.id}/sections", headers=admin_headers).status_code == 404


@pytest.mark.asyncio
async def test_embeddings_statute_section_skips_quarantined_parent(db, monkeypatch):
    """Filter at embeddings.py statute parent lookup (~line 99)."""
    st = Statute(name="Embed Parent Act", short_name="EPA", year_enacted=2022)
    db.add(st)
    await db.flush()
    sec = StatuteSection(statute_id=st.id, section_number="3", section_title="Three")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="Three.", text_hash="h3")
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    db.add(EmbeddingQueue(record_id=sec.id, table_name="statute_section", access_method="public"))
    _quarantine(db, "statute", st.id)
    await db.commit()

    async def _noop_embed(texts, **kwargs):
        return [[0.0] * settings.EMBEDDING_DIM for _ in texts]

    async def _identity_ok():
        return True

    monkeypatch.setattr("scraper.tasks.embeddings.embed_texts", _noop_embed)
    monkeypatch.setattr("scraper.tasks.embeddings.embedding_identity_matches", _identity_ok)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    counts = await process_embedding_queue(limit=5)
    assert counts["skipped_quarantined"] >= 1
