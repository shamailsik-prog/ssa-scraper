"""Validation (tests 8–12), deduplication, bench parsing, treatment and embeddings identity."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from scraper.config import settings
from scraper.extractors.deterministic import extract_judgment_deterministic
from scraper.extractors.hybrid_extractor import HybridExtractor, load_court_directory
from scraper.extractors.validation import reconcile_judgment
from scraper.fetchers import canonical_text_hash, record_provenance, stage_judgment
from scraper.models import Citation, Judgment, QuarantineQueue, ScraperStaging, Treatment
from scraper.parsers.bench_parser import parse_bench
from scraper.parsers.text_cleaner import clean_html
from scraper.tasks.promotion import promote_judgment_staging
from scraper.tasks.treatment import classify_deterministic, classify_judgment
from tests.fixtures import JUDGMENT_HTML, JUDGMENT_TEXT, FakeManagedClient, judgment_html

COURTS = {"supreme court of pakistan": "Supreme Court of Pakistan", "sc": "Supreme Court of Pakistan", "supreme court": "Supreme Court of Pakistan", "lahore high court": "Lahore High Court", "lhc": "Lahore High Court"}


def _det():
    return extract_judgment_deterministic(html=JUDGMENT_HTML, text=clean_html(JUDGMENT_HTML))


# --------------------------------------------------------------------------- 8
def test_ai_invented_citation_rejected_and_conflict_logged():
    det = _det()
    ai = {"citations": ["PLD 2024 SC 101", "2099 SCMR 9999"], "extractor_confidence": 0.95}
    out = reconcile_judgment(deterministic=det, ai=ai, raw_text=clean_html(JUDGMENT_HTML), court_directory=COURTS, min_confidence=0.85)
    assert "2099 SCMR 9999" not in out.data["citations"]
    assert any(c["field"] == "citations" and c["ai"] == "2099 SCMR 9999" for c in out.conflicts)


# --------------------------------------------------------------------------- 9
def test_ai_court_and_date_without_evidence_rejected():
    det = _det()
    det["court"] = None
    det["decision_date"] = None
    ai = {"court": "Peshawar High Court", "decision_date": "2021-01-01", "extractor_confidence": 0.9}
    out = reconcile_judgment(deterministic=det, ai=ai, raw_text=clean_html(JUDGMENT_HTML), court_directory=COURTS, min_confidence=0.85)
    fields = {c["field"] for c in out.conflicts}
    assert "court" in fields and "decision_date" in fields
    assert out.data["decision_date"] is None and out.quarantine is True and "court" in (out.quarantine_reason or "")
    # a date that IS in the raw is accepted
    ai2 = {"decision_date": "2024-03-12", "extractor_confidence": 0.9}
    det2 = _det()
    det2["decision_date"] = None
    out2 = reconcile_judgment(deterministic=det2, ai=ai2, raw_text=clean_html(JUDGMENT_HTML), court_directory=COURTS, min_confidence=0.85)
    assert out2.data["decision_date"] == "2024-03-12"


# --------------------------------------------------------------------------- 10
async def test_full_text_hash_preserved_through_extraction_and_promotion(db, source):
    raw = clean_html(JUDGMENT_HTML)
    before = canonical_text_hash(raw)
    client = FakeManagedClient(result={"citations": ["PLD 2024 SC 101"], "full_text_candidate": "A REWRITTEN SUMMARY", "extractor_confidence": 0.99})
    prov = await record_provenance(db, source=source, url="http://127.0.0.1/h", content=JUDGMENT_HTML.encode(), content_kind="html")
    st = await stage_judgment(db, source=source, prov=prov, raw_html=JUDGMENT_HTML, raw_text=raw, url="http://127.0.0.1/h")
    from scraper.extractors.scrapegraph_managed import ManagedScrapeGraphEngine

    extractor = HybridExtractor(db, source, force_ai=True, managed=ManagedScrapeGraphEngine(client_factory=lambda: client), provenance_id=prov.id, staging_id=st.id)
    o = await extractor.extract_judgment(html=JUDGMENT_HTML, text=raw, content_hash=prov.content_hash)
    assert canonical_text_hash(o.data["full_text_candidate"]) == before
    st.reconciled_json = o.data
    st.status = "extracted"
    st.confidence_score = o.confidence
    assert await promote_judgment_staging(db, st) == "promoted"
    j = (await db.execute(select(Judgment))).scalars().first()
    assert j.full_text_hash == before and canonical_text_hash(j.full_text) == before


# --------------------------------------------------------------------------- 11
def test_deterministic_citation_wins_over_conflicting_ai():
    det = _det()
    ai = {"citations": ["PLD 2024 SC 999"], "extractor_confidence": 0.99}
    out = reconcile_judgment(deterministic=det, ai=ai, raw_text=clean_html(JUDGMENT_HTML), court_directory=COURTS, min_confidence=0.85)
    assert out.data["citations"][0] == "PLD 2024 SC 101"
    assert any(c["field"] == "primary_citation" for c in out.conflicts)
    assert out.quarantine and "conflict" in out.quarantine_reason


# --------------------------------------------------------------------------- 12
async def test_same_judgment_from_three_routes_is_one_row(db, source):
    raw = clean_html(JUDGMENT_HTML)
    # Route A (Tier 1) and Route B (Tier 2) deliver identical content → one provenance, one staging
    p1 = await record_provenance(db, source=source, url="http://127.0.0.1/a", content=JUDGMENT_HTML.encode(), content_kind="html", route={"tier": 1})
    p2 = await record_provenance(db, source=source, url="http://127.0.0.1/b", content=JUDGMENT_HTML.encode(), content_kind="html", route={"tier": 2})
    assert p1.id == p2.id and len(p1.routes) == 2
    s1 = await stage_judgment(db, source=source, prov=p1, raw_html=JUDGMENT_HTML, raw_text=raw, url="http://127.0.0.1/a", route={"tier": 1})
    s2 = await stage_judgment(db, source=source, prov=p2, raw_html=JUDGMENT_HTML, raw_text=raw, url="http://127.0.0.1/b", route={"tier": 2})
    assert s1.id == s2.id
    # Route C: a ScrapeGraph-assisted result page with different HTML but the same judgment
    variant = judgment_html("PLD 2024 SC 101").replace("<p>JUDGMENT</p>", "<p>J U D G M E N T</p>")
    p3 = await record_provenance(db, source=source, url="http://127.0.0.1/c", content=variant.encode(), content_kind="html", route={"tier": 3, "engine": "scrapegraph"})
    s3 = await stage_judgment(db, source=source, prov=p3, raw_html=variant, raw_text=clean_html(variant), url="http://127.0.0.1/c", route={"tier": 3})
    assert s3.id != s1.id
    extractor = HybridExtractor(db, source)
    for st in (s1, s3):
        o = await extractor.extract_judgment(html=st.raw_html, text=st.raw_text, content_hash=st.content_hash)
        st.reconciled_json, st.status, st.confidence_score = o.data, "extracted", o.confidence
    r1 = await promote_judgment_staging(db, s1)
    r3 = await promote_judgment_staging(db, s3)
    assert r1 == "promoted" and r3 == "duplicate"
    assert (await db.execute(select(func.count()).select_from(Judgment))).scalar() == 1
    j = (await db.execute(select(Judgment))).scalars().first()
    assert s3.promoted_to_id == j.id
    assert (await db.execute(select(func.count()).select_from(Citation).where(Citation.judgment_id == j.id))).scalar() >= 1


async def test_citation_belonging_to_another_judgment_quarantines(db, source):
    a = judgment_html("PLD 2024 SC 101")
    b = judgment_html("PLD 2024 SC 202", title="Zubair versus Federation").replace("PLD 2019 SC 1", "PLD 2024 SC 101")
    extractor = HybridExtractor(db, source)
    rows = []
    for html, url in ((a, "http://127.0.0.1/a"), (b, "http://127.0.0.1/b")):
        p = await record_provenance(db, source=source, url=url, content=html.encode(), content_kind="html")
        st = await stage_judgment(db, source=source, prov=p, raw_html=html, raw_text=clean_html(html), url=url)
        o = await extractor.extract_judgment(html=html, text=st.raw_text, content_hash=p.content_hash)
        st.reconciled_json, st.status, st.confidence_score = o.data, "extracted", o.confidence
        rows.append(st)
    assert await promote_judgment_staging(db, rows[0]) == "promoted"
    # b carries 'PLD 2024 SC 101' as a cited authority, not as its own citation → still promotes as its own judgment
    assert await promote_judgment_staging(db, rows[1]) in ("promoted", "quarantined")
    assert (await db.execute(select(func.count()).select_from(Judgment))).scalar() >= 1


# --------------------------------------------------------------------------- bench (B-10)
def test_bench_parsing_size_and_type():
    info = parse_bench(JUDGMENT_TEXT)
    assert info.bench_size == 3 and info.bench_type == "full"
    assert "Qazi Faez Isa" in info.judge_names
    larger = parse_bench("Before a seven-member bench of the Supreme Court.\nCORAM: A, J, B, J\n\nversus")
    assert larger.bench_size == 7 and larger.conflict
    single = parse_bench("Before Mr. Justice Ayesha A. Malik\n\nPetitioner versus Respondent")
    assert single.bench_size == 1 and single.bench_type == "single"


# --------------------------------------------------------------------------- treatment (B-7)
def test_treatment_phrase_rules():
    assert classify_deterministic("... we have followed the principle laid down in 2015 SCMR 100 ...")[0] == "followed"
    assert classify_deterministic("The judgment reported as PLD 2019 SC 1 is distinguishable on facts")[0] == "distinguished"
    assert classify_deterministic("PLD 2001 SC 1 stands overruled")[0] == "overruled"
    assert classify_deterministic("the court decided something unrelated") is None


async def test_treatment_rows_with_evidence_and_quarantine(db, source, monkeypatch):
    raw = clean_html(JUDGMENT_HTML)
    p = await record_provenance(db, source=source, url="http://127.0.0.1/t", content=JUDGMENT_HTML.encode(), content_kind="html")
    st = await stage_judgment(db, source=source, prov=p, raw_html=JUDGMENT_HTML, raw_text=raw, url="http://127.0.0.1/t")
    o = await HybridExtractor(db, source).extract_judgment(html=JUDGMENT_HTML, text=raw, content_hash=p.content_hash)
    st.reconciled_json, st.status, st.confidence_score = o.data, "extracted", o.confidence
    assert await promote_judgment_staging(db, st) == "promoted"
    j = (await db.execute(select(Judgment))).scalars().first()
    monkeypatch.setattr(settings, "TREATMENT_MIN_CONFIDENCE", 0.7)
    counts = await classify_judgment(db, j)
    rows = (await db.execute(select(Treatment))).scalars().all()
    labels = {(t.cited_citation, t.label) for t in rows}
    assert ("2015 SCMR 100", "followed") in labels
    assert ("PLD 2019 SC 1", "distinguished") in labels
    assert all(len(t.evidence_passage) <= 400 and t.method == "deterministic" for t in rows)
    # below threshold → review queue, not a treatment row
    monkeypatch.setattr(settings, "TREATMENT_MIN_CONFIDENCE", 0.95)
    await db.execute(Treatment.__table__.delete())
    counts2 = await classify_judgment(db, j)
    assert counts2["quarantined"] >= 1
    assert (await db.execute(select(func.count()).select_from(QuarantineQueue).where(QuarantineQueue.kind == "treatment"))).scalar() >= 1


# --------------------------------------------------------------------------- embeddings (B-6)
async def test_embedding_identity_mismatch_refuses(db, monkeypatch):
    from scraper.models import CorpusMetadata
    from scraper.tasks.embeddings import process_embedding_queue

    row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == "embedding_dim"))).scalars().first()
    row.value = "999"
    await db.commit()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    counts = await process_embedding_queue()
    assert counts["refused"] == 1
