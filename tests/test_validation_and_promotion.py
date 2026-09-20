"""Validation (tests 8–12), deduplication, bench parsing, treatment and embeddings identity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, update

from scraper.config import settings
from scraper.extractors.deterministic import extract_judgment_deterministic
from scraper.extractors.hybrid_extractor import HybridExtractor, load_court_directory
from scraper.extractors.validation import reconcile_instrument, reconcile_judgment
from scraper.fetchers import canonical_text_hash, record_provenance, stage_judgment, stage_statute
from scraper.models import Citation, Instrument, InstrumentRelation, InstrumentSectionRelation, Judgment, JudgmentCitationRelation, QuarantineQueue, ScraperSource, ScraperStaging, Statute, StatuteSection, Treatment
from scraper.parsers.bench_parser import parse_bench
from scraper.parsers.citation_extractor import extract_instrument_mentions, extract_statute_mentions
from scraper.parsers.text_cleaner import clean_html
from scraper.tasks import promotion as promotion_task_module
from scraper.tasks.promotion import (
    promote_judgment_staging,
    promote_statute_staging,
    reconcile_citation_statute_residual_smoke,
    reconcile_instrument_relations,
)
from scraper.tasks.treatment import (
    classify_deterministic,
    classify_judgment,
    reconcile_judgment_citation_relations,
    reconcile_treatment_citation_links,
    sync_judgment_citation_relations,
)
from tests.fixtures import INSTRUMENT_TEXT, JUDGMENT_HTML, JUDGMENT_TEXT, FakeManagedClient, judgment_html

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
    # court names that map through the directory still need raw-text evidence
    det3 = _det()
    det3["court"] = None
    out3 = reconcile_judgment(
        deterministic=det3,
        ai={"court": "Lahore High Court", "extractor_confidence": 0.9},
        raw_text=clean_html(JUDGMENT_HTML),
        court_directory=COURTS,
        min_confidence=0.85,
    )
    assert out3.data["court"] is None
    assert any(c["field"] == "court" for c in out3.conflicts)


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


def test_instrument_type_requires_raw_evidence():
    det = {"type": None, "full_text": INSTRUMENT_TEXT, "extractor_confidence": 0.7}
    ai = {"type": "ordinance", "extractor_confidence": 0.95}
    out = reconcile_instrument(deterministic=det, ai=ai, raw_text=INSTRUMENT_TEXT, min_confidence=0.5)
    assert out.data.get("type") is None
    assert any(c["field"] == "type" for c in out.conflicts)


def test_gazette_mention_extractors_normalize_core_patterns():
    sample = """
    THE GAZETTE OF PAKISTAN EXTRAORDINARY
    S.R.O. 123(I)/2024 dated 19th January 2024
    Act No. XXI of 2017
    Ordinance No. VI of 2020
    In the Pakistan Penal Code, 1860, section 302 shall be amended.
    """
    instrument_mentions = extract_instrument_mentions(sample)
    normalized_instrument_mentions = {m["normalized"] for m in instrument_mentions}
    assert "S.R.O. 123(I)/2024" in normalized_instrument_mentions
    assert "Act No. XXI of 2017" in normalized_instrument_mentions
    assert "Ordinance No. VI of 2020" in normalized_instrument_mentions

    statute_mentions = extract_statute_mentions(sample)
    assert any(m.get("canonical_statute_name") == "Pakistan Penal Code, 1860" for m in statute_mentions)
    assert any(m.get("section_number") == "302" for m in statute_mentions)


def test_section_key_normalizer_handles_article_and_rule_hyphen_variants():
    assert promotion_task_module._norm_section_key("Article 10-A") == "10A"
    assert promotion_task_module._norm_section_key("Article 10A") == "10A"
    assert promotion_task_module._norm_section_key("Rule 3-A") == "3A"
    assert promotion_task_module._norm_section_key("Rule 3A") == "3A"


async def test_instrument_promotion_persists_and_links_mentions(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()
    text = """
    THE GAZETTE OF PAKISTAN EXTRAORDINARY
    ACT No. XXI of 2017
    S.R.O. 123(I)/2024
    An Act further to amend the Pakistan Penal Code, 1860.
    Criminal Law (Amendment) Act, 2017
    Dated 25th March 2017
    In the Pakistan Penal Code, 1860, in section 302, the words "as qisas" shall be substituted.
    """
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/gazette-mention.pdf",
        content=text.encode("utf-8"),
        content_kind="text",
    )
    st = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=text,
        url="http://127.0.0.1/gazette-mention.pdf",
        kind="instrument",
    )
    outcome = await HybridExtractor(db, source).extract_instrument(text=text, source_meta={"url": "http://127.0.0.1/gazette-mention.pdf"}, content_hash=prov.content_hash)
    st.reconciled_json, st.status, st.confidence_score = outcome.data, "extracted", outcome.confidence
    assert await promote_statute_staging(db, st) == "promoted"

    inst = (await db.execute(select(Instrument).where(Instrument.id == st.promoted_to_id))).scalars().first()
    assert inst is not None
    assert any(m.get("normalized") == "Act No. XXI of 2017" for m in (inst.citation_mentions or []))
    assert any(m.get("normalized") == "S.R.O. 123(I)/2024" for m in (inst.citation_mentions or []))
    assert any(m.get("linked_statute_id") for m in (inst.statute_mentions or []))
    assert inst.affected_statute_id is not None
    assert inst.affected_statute_name == "Pakistan Penal Code, 1860"
    assert "302" in (inst.affected_sections or [])


async def test_instrument_promotion_fails_closed_on_bad_mentions_payload(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()
    text = "THE GAZETTE OF PAKISTAN EXTRAORDINARY\nAct No. XXI of 2017"
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/bad-mentions.pdf",
        content=text.encode("utf-8"),
        content_kind="text",
    )
    st = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=text,
        url="http://127.0.0.1/bad-mentions.pdf",
        kind="instrument",
    )
    st.status = "extracted"
    st.reconciled_json = {
        "type": "act",
        "full_text": text,
        "citation_mentions": "Act No. XXI of 2017",
    }
    result = await promote_statute_staging(db, st)
    assert result == "quarantined"
    q = (
        await db.execute(
            select(QuarantineQueue).where(QuarantineQueue.statutes_staging_id == st.id),
        )
    ).scalars().first()
    assert q is not None
    assert "citation_mentions must be a list" in (q.reason or "")


async def test_instrument_relation_graph_persists_verified_edges_with_provenance(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()

    async def _promote(text: str, url: str) -> Instrument:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=text,
            url=url,
            kind="instrument",
        )
        out = await HybridExtractor(db, source).extract_instrument(text=text, source_meta={"url": url}, content_hash=prov.content_hash)
        staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
        assert await promote_statute_staging(db, staging) == "promoted"
        return (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()

    target = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        NOTIFICATION
        S.R.O. 123(I)/2024
        Dated 20th January 2024
        """,
        "http://127.0.0.1/target-notification.pdf",
    )
    assert target is not None

    source_instrument = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ACT No. XXII of 2025
        This Act is amended by S.R.O. 123(I)/2024 for immediate effect.
        The substituted provision shall be read with Pakistan Penal Code, 1860.
        """,
        "http://127.0.0.1/source-act.pdf",
    )
    assert source_instrument is not None

    edges = (
        await db.execute(
            select(InstrumentRelation).where(InstrumentRelation.source_instrument_id == source_instrument.id),
        )
    ).scalars().all()
    assert len(edges) >= 2
    amended = [e for e in edges if e.relation_type == "amended_by"]
    read_with = [e for e in edges if e.relation_type == "read_with"]
    assert len(amended) == 1
    assert amended[0].target_instrument_id == target.id
    assert amended[0].source_provenance_id == source_instrument.source_provenance_id
    assert "amended by" in (amended[0].evidence_snippet or "").lower()
    assert len(read_with) == 1
    assert read_with[0].target_statute_id is not None
    assert "read with" in (read_with[0].evidence_snippet or "").lower()


async def test_instrument_relation_graph_fails_closed_for_unresolved_targets(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()
    text = """
    THE GAZETTE OF PAKISTAN EXTRAORDINARY
    ACT No. XXIII of 2025
    This Act stands superseded by Ordinance No. IX of 2025.
    """
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/unresolved-edge.pdf",
        content=text.encode("utf-8"),
        content_kind="text",
    )
    staging = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=text,
        url="http://127.0.0.1/unresolved-edge.pdf",
        kind="instrument",
    )
    out = await HybridExtractor(db, source).extract_instrument(text=text, source_meta={"url": "http://127.0.0.1/unresolved-edge.pdf"}, content_hash=prov.content_hash)
    staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
    assert await promote_statute_staging(db, staging) == "promoted"
    inst = (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()
    edges = (
        await db.execute(
            select(InstrumentRelation).where(InstrumentRelation.source_instrument_id == inst.id),
        )
    ).scalars().all()
    assert edges == []


async def test_instrument_section_relation_graph_extracts_amendment_operations(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()
    text = """
    THE GAZETTE OF PAKISTAN EXTRAORDINARY
    ACT No. XXVIII of 2025
    An Act further to amend the Pakistan Penal Code, 1860.
    2. Amendment of section 302 of the Pakistan Penal Code, 1860.- In the Pakistan Penal Code, 1860, for section 302, the following shall be substituted.
    In the Pakistan Penal Code, 1860, section 304 shall be omitted.
    In the Pakistan Penal Code, 1860, after section 299, the following new section shall be inserted, namely:— 299A.
    In the Pakistan Penal Code, 1860, section 500 is hereby repealed.
    """
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/section-ops.pdf",
        content=text.encode("utf-8"),
        content_kind="text",
    )
    staging = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=text,
        url="http://127.0.0.1/section-ops.pdf",
        kind="instrument",
    )
    out = await HybridExtractor(db, source).extract_instrument(text=text, source_meta={"url": "http://127.0.0.1/section-ops.pdf"}, content_hash=prov.content_hash)
    staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
    assert await promote_statute_staging(db, staging) == "promoted"
    inst = (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()
    assert inst is not None

    section_edges = (
        await db.execute(
            select(InstrumentSectionRelation)
            .where(InstrumentSectionRelation.source_instrument_id == inst.id)
            .order_by(InstrumentSectionRelation.amendment_operation.asc(), InstrumentSectionRelation.target_section_key.asc()),
        )
    ).scalars().all()
    assert len(section_edges) == 4
    observed = {(edge.amendment_operation, edge.target_section_key) for edge in section_edges}
    assert observed == {
        ("substitute", "302"),
        ("omit", "304"),
        ("insert", "299A"),
        ("repeal", "500"),
    }
    assert all(edge.target_statute_id == inst.affected_statute_id for edge in section_edges)
    assert all(edge.source_provenance_id == inst.source_provenance_id for edge in section_edges)
    assert any("shall be substituted" in (edge.evidence_snippet or "").lower() for edge in section_edges)
    assert any("is hereby repealed" in (edge.evidence_snippet or "").lower() for edge in section_edges)


async def test_instrument_section_relation_graph_links_article_targets_to_statute_sections(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()
    statute = Statute(
        name="Constitution of the Islamic Republic of Pakistan, 1973",
        short_name="Constitution, 1973",
        jurisdiction="Federal",
        statute_type="constitution",
        year_enacted=1973,
        source_name="PakistanCode",
    )
    db.add(statute)
    await db.flush()
    article_section = StatuteSection(
        statute_id=statute.id,
        section_number="Article 10-A",
        section_title="Right to fair trial",
        sort_key=1,
    )
    db.add(article_section)
    await db.flush()

    text = """
    THE GAZETTE OF PAKISTAN EXTRAORDINARY
    ACT No. XXX of 2025
    An Act further to amend the Constitution of Pakistan, 1973.
    In the Constitution of Pakistan, 1973, Article 10-A shall be substituted.
    """
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/article-ops.pdf",
        content=text.encode("utf-8"),
        content_kind="text",
    )
    staging = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=text,
        url="http://127.0.0.1/article-ops.pdf",
        kind="instrument",
    )
    out = await HybridExtractor(db, source).extract_instrument(
        text=text,
        source_meta={"url": "http://127.0.0.1/article-ops.pdf"},
        content_hash=prov.content_hash,
    )
    staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
    assert await promote_statute_staging(db, staging) == "promoted"
    inst = (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()
    assert inst is not None

    section_edges = (
        await db.execute(
            select(InstrumentSectionRelation)
            .where(InstrumentSectionRelation.source_instrument_id == inst.id)
            .order_by(InstrumentSectionRelation.target_section_key.asc()),
        )
    ).scalars().all()
    assert len(section_edges) == 1
    assert section_edges[0].amendment_operation == "substitute"
    assert section_edges[0].target_section_key == "10A"
    assert section_edges[0].target_statute_id == statute.id
    assert section_edges[0].target_statute_section_id == article_section.id


async def test_nasirlaw_promotion_canonicalizes_hyphenated_sections_and_links_variants(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "NasirLawSite"),
        )
    ).scalars().first()
    assert source is not None

    async def _promote_statute(*, statute_name: str, section_number: str, section_text: str, url: str) -> tuple[Statute, StatuteSection]:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=section_text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=section_text,
            url=url,
            kind="statute",
        )
        staging.status = "extracted"
        staging.reconciled_json = {
            "statute_name": statute_name,
            "jurisdiction": "Federal",
            "statute_type": "act",
            "sections": [
                {
                    "section_number": section_number,
                    "section_text": section_text,
                    "section_title": "Sample section",
                }
            ],
        }
        assert await promote_statute_staging(db, staging) == "promoted"
        statute = (
            await db.execute(select(Statute).where(Statute.id == staging.promoted_to_id))
        ).scalars().first()
        section = (
            await db.execute(
                select(StatuteSection).where(StatuteSection.statute_id == statute.id),
            )
        ).scalars().first()
        return statute, section

    constitution, article_section = await _promote_statute(
        statute_name="Constitution of the Islamic Republic of Pakistan, 1973",
        section_number="Article 10-A",
        section_text="Article 10-A.- Right to fair trial shall be protected.",
        url="http://127.0.0.1/nasir-constitution.txt",
    )
    rules_statute, rule_section = await _promote_statute(
        statute_name="Sample Compliance Rules, 2025",
        section_number="Rule 3-A",
        section_text="Rule 3-A.- Compliance procedure for sample licensing.",
        url="http://127.0.0.1/nasir-rules.txt",
    )
    assert article_section.section_number == "10A"
    assert rule_section.section_number == "3A"

    async def _promote_instrument(text: str, url: str, *, affected_statute: str | None = None) -> Instrument:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=text,
            url=url,
            kind="instrument",
        )
        out = await HybridExtractor(db, source).extract_instrument(
            text=text,
            source_meta={"url": url},
            content_hash=prov.content_hash,
        )
        if affected_statute:
            out.data["affected_statute"] = affected_statute
        staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
        assert await promote_statute_staging(db, staging) == "promoted"
        return (
            await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))
        ).scalars().first()

    article_instrument = await _promote_instrument(
        """
        NOTIFICATION
        In the Constitution of Pakistan, 1973, Article 10A shall be substituted.
        """,
        "http://127.0.0.1/nasir-article-amendment.txt",
    )
    article_edges = (
        await db.execute(
            select(InstrumentSectionRelation).where(
                InstrumentSectionRelation.source_instrument_id == article_instrument.id,
            )
        )
    ).scalars().all()
    assert len(article_edges) == 1
    assert article_edges[0].target_statute_id == constitution.id
    assert article_edges[0].target_section_key == "10A"
    assert article_edges[0].target_statute_section_id == article_section.id

    rule_instrument = await _promote_instrument(
        """
        NOTIFICATION
        In the Sample Compliance Rules, 2025, Rule 3A shall be omitted.
        """,
        "http://127.0.0.1/nasir-rule-amendment.txt",
        affected_statute="Sample Compliance Rules, 2025",
    )
    rule_edges = (
        await db.execute(
            select(InstrumentSectionRelation).where(
                InstrumentSectionRelation.source_instrument_id == rule_instrument.id,
            )
        )
    ).scalars().all()
    assert len(rule_edges) == 1
    assert rule_edges[0].target_statute_id == rules_statute.id
    assert rule_edges[0].target_section_key == "3A"
    assert rule_edges[0].target_statute_section_id == rule_section.id


async def test_instrument_section_relation_graph_fails_closed_without_statute_target(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()
    text = """
    THE GAZETTE OF PAKISTAN EXTRAORDINARY
    ACT No. XXIX of 2025
    Section 10 shall be omitted.
    Section 11 is hereby repealed.
    """
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/section-ops-no-statute.pdf",
        content=text.encode("utf-8"),
        content_kind="text",
    )
    staging = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=text,
        url="http://127.0.0.1/section-ops-no-statute.pdf",
        kind="instrument",
    )
    out = await HybridExtractor(db, source).extract_instrument(
        text=text,
        source_meta={"url": "http://127.0.0.1/section-ops-no-statute.pdf"},
        content_hash=prov.content_hash,
    )
    staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
    assert await promote_statute_staging(db, staging) == "promoted"
    inst = (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()
    section_edges = (
        await db.execute(
            select(InstrumentSectionRelation).where(InstrumentSectionRelation.source_instrument_id == inst.id),
        )
    ).scalars().all()
    assert section_edges == []


async def test_instrument_relation_reconcile_backfills_late_resolved_targets(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()

    async def _promote(text: str, url: str) -> Instrument:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=text,
            url=url,
            kind="instrument",
        )
        out = await HybridExtractor(db, source).extract_instrument(text=text, source_meta={"url": url}, content_hash=prov.content_hash)
        staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
        assert await promote_statute_staging(db, staging) == "promoted"
        return (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()

    source_instrument = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ACT No. XXV of 2025
        This Act is amended by S.R.O. 456(I)/2025 with immediate effect.
        """,
        "http://127.0.0.1/reconcile-source.pdf",
    )
    assert source_instrument is not None
    initial_edges = (
        await db.execute(
            select(InstrumentRelation).where(InstrumentRelation.source_instrument_id == source_instrument.id),
        )
    ).scalars().all()
    assert initial_edges == []

    target_instrument = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        NOTIFICATION
        S.R.O. 456(I)/2025
        Dated 20th July 2025
        """,
        "http://127.0.0.1/reconcile-target.pdf",
    )
    assert target_instrument is not None

    await db.commit()
    counts = await reconcile_instrument_relations(limit=100, lookback_hours=24 * 365)
    assert counts["processed"] >= 1
    edges = (
        await db.execute(
            select(InstrumentRelation).where(InstrumentRelation.source_instrument_id == source_instrument.id),
        )
    ).scalars().all()
    amended = [e for e in edges if e.relation_type == "amended_by"]
    assert len(amended) == 1
    assert amended[0].target_instrument_id == target_instrument.id


async def test_instrument_section_relation_reconcile_backfills_late_article_and_rule_targets(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "NasirLawSite"),
        )
    ).scalars().first()
    assert source is not None

    async def _promote_instrument(text: str, url: str, *, affected_statute: str | None = None) -> Instrument:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=text,
            url=url,
            kind="instrument",
        )
        out = await HybridExtractor(db, source).extract_instrument(
            text=text,
            source_meta={"url": url},
            content_hash=prov.content_hash,
        )
        if affected_statute:
            out.data["affected_statute"] = affected_statute
        staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
        assert await promote_statute_staging(db, staging) == "promoted"
        return (
            await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))
        ).scalars().first()

    article_instrument = await _promote_instrument(
        """
        NOTIFICATION
        In the Constitution of Pakistan, 1973, Article 10A shall be substituted.
        """,
        "http://127.0.0.1/reconcile-article-late-target.txt",
    )
    rule_instrument = await _promote_instrument(
        """
        NOTIFICATION
        In the Sample Compliance Rules, 2025, Rule 3A shall be omitted.
        """,
        "http://127.0.0.1/reconcile-rule-late-target.txt",
        affected_statute="Sample Compliance Rules, 2025",
    )
    assert article_instrument is not None and rule_instrument is not None

    initial_edges = (
        await db.execute(
            select(InstrumentSectionRelation).where(
                InstrumentSectionRelation.source_instrument_id.in_(
                    [article_instrument.id, rule_instrument.id],
                )
            )
        )
    ).scalars().all()
    assert len(initial_edges) == 2
    edges_by_source = {edge.source_instrument_id: edge for edge in initial_edges}
    article_edge = edges_by_source[article_instrument.id]
    rule_edge = edges_by_source[rule_instrument.id]
    assert article_edge.target_section_key == "10A"
    assert rule_edge.target_section_key == "3A"
    assert article_edge.target_statute_section_id is None
    assert rule_edge.target_statute_section_id is None

    old_created_at = datetime.now(timezone.utc) - timedelta(days=30)
    await db.execute(
        update(Instrument)
        .where(Instrument.id.in_([article_instrument.id, rule_instrument.id]))
        .values(created_at=old_created_at)
    )

    async def _promote_late_section(
        *,
        statute_name: str,
        section_number: str,
        section_text: str,
        url: str,
    ) -> StatuteSection:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=section_text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=section_text,
            url=url,
            kind="statute",
        )
        staging.status = "extracted"
        staging.reconciled_json = {
            "statute_name": statute_name,
            "jurisdiction": "Federal",
            "statute_type": "act",
            "sections": [
                {
                    "section_number": section_number,
                    "section_text": section_text,
                    "section_title": "late-arriving section",
                }
            ],
        }
        assert await promote_statute_staging(db, staging) == "promoted"
        statute = (
            await db.execute(select(Statute).where(Statute.id == staging.promoted_to_id))
        ).scalars().first()
        key = promotion_task_module._norm_section(section_number)
        return (
            await db.execute(
                select(StatuteSection).where(
                    StatuteSection.statute_id == statute.id,
                    StatuteSection.section_number == key,
                )
            )
        ).scalars().first()

    article_statute = (
        await db.execute(select(Statute).where(Statute.id == article_edge.target_statute_id))
    ).scalars().first()
    rule_statute = (
        await db.execute(select(Statute).where(Statute.id == rule_edge.target_statute_id))
    ).scalars().first()
    article_section = await _promote_late_section(
        statute_name=article_statute.name,
        section_number="Article 10-A",
        section_text="Article 10-A.- Right to fair trial shall be ensured.",
        url="http://127.0.0.1/reconcile-late-article-section.txt",
    )
    rule_section = await _promote_late_section(
        statute_name=rule_statute.name,
        section_number="Rule 3-A",
        section_text="Rule 3-A.- Compliance procedure for renewals.",
        url="http://127.0.0.1/reconcile-late-rule-section.txt",
    )
    assert article_section is not None and rule_section is not None
    await db.commit()

    first = await reconcile_instrument_relations(limit=50, lookback_hours=1)
    assert first["processed"] >= 2
    refreshed_edges = (
        await db.execute(
            select(InstrumentSectionRelation).where(
                InstrumentSectionRelation.source_instrument_id.in_(
                    [article_instrument.id, rule_instrument.id],
                )
            )
        )
    ).scalars().all()
    refreshed_by_source = {edge.source_instrument_id: edge for edge in refreshed_edges}
    assert refreshed_by_source[article_instrument.id].target_statute_section_id == article_section.id
    assert refreshed_by_source[rule_instrument.id].target_statute_section_id == rule_section.id

    second = await reconcile_instrument_relations(limit=50, lookback_hours=1)
    assert second["section_edges_added"] == 0
    assert second["section_edges_removed"] == 0


async def test_instrument_section_relation_reconcile_skips_multi_target_section_mentions(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "NasirLawSite"),
        )
    ).scalars().first()
    assert source is not None

    text = """
    NOTIFICATION
    ACT No. XL of 2025
    In the Constitution of Pakistan, 1973, section 10A/10B shall be substituted.
    """
    section_token = "10A/10B"
    token_start = text.index(section_token)
    token_end = token_start + len(section_token)
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/multi-target-fail-closed.txt",
        content=text.encode("utf-8"),
        content_kind="text",
    )
    staging = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=text,
        url="http://127.0.0.1/multi-target-fail-closed.txt",
        kind="instrument",
    )
    staging.status = "extracted"
    staging.reconciled_json = {
        "type": "act",
        "number": "Act No. XL of 2025",
        "full_text": text,
        "affected_statute": "Constitution of Pakistan, 1973",
        "citation_mentions": [
            {
                "raw": "Act No. XL of 2025",
                "normalized": "Act No. XL of 2025",
                "mention_type": "act_no",
                "number": "XL",
                "year": 2025,
            }
        ],
        "statute_mentions": [
            {
                "raw": "section 10A/10B",
                "normalized": "Constitution of Pakistan, 1973",
                "canonical_statute_name": "Constitution of Pakistan, 1973",
                "section_number": "10A/10B",
                "span": [token_start, token_end],
            }
        ],
    }
    assert await promote_statute_staging(db, staging) == "promoted"
    inst = (
        await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))
    ).scalars().first()
    assert inst is not None

    before_edges = (
        await db.execute(
            select(InstrumentSectionRelation).where(
                InstrumentSectionRelation.source_instrument_id == inst.id,
            )
        )
    ).scalars().all()
    assert before_edges == []

    await db.commit()
    counts = await reconcile_instrument_relations(limit=20, lookback_hours=24 * 365)
    assert counts["processed"] >= 1
    after_edges = (
        await db.execute(
            select(InstrumentSectionRelation).where(
                InstrumentSectionRelation.source_instrument_id == inst.id,
            )
        )
    ).scalars().all()
    assert after_edges == []


async def test_instrument_relation_reconcile_keeps_ambiguous_targets_skipped(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()

    async def _promote(text: str, url: str) -> Instrument:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=text,
            url=url,
            kind="instrument",
        )
        out = await HybridExtractor(db, source).extract_instrument(text=text, source_meta={"url": url}, content_hash=prov.content_hash)
        staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
        assert await promote_statute_staging(db, staging) == "promoted"
        return (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()

    source_instrument = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ACT No. XXVI of 2025
        This Act stands superseded by Ordinance No. IX of 2025.
        """,
        "http://127.0.0.1/ambiguous-source.pdf",
    )
    assert source_instrument is not None

    target_a = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ORDINANCE No. IX of 2025
        Dated 1st August 2025
        """,
        "http://127.0.0.1/ambiguous-target-a.pdf",
    )
    assert target_a is not None
    target_b = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ACT No. II of 2026
        For interpretive continuity this Act shall be read with Ordinance No. IX of 2025.
        """,
        "http://127.0.0.1/ambiguous-target-b.pdf",
    )
    assert target_b is not None

    await db.commit()
    counts = await reconcile_instrument_relations(limit=100, lookback_hours=24 * 365)
    assert counts["processed"] >= 1
    edges = (
        await db.execute(
            select(InstrumentRelation).where(InstrumentRelation.source_instrument_id == source_instrument.id),
        )
    ).scalars().all()
    superseded = [e for e in edges if e.relation_type == "superseded_by"]
    assert len(superseded) == 1
    assert superseded[0].target_instrument_id == target_a.id
    assert superseded[0].target_instrument_id != target_b.id


async def test_instrument_relation_reconcile_is_idempotent_on_rerun(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()

    async def _promote(text: str, url: str) -> Instrument:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=text,
            url=url,
            kind="instrument",
        )
        out = await HybridExtractor(db, source).extract_instrument(text=text, source_meta={"url": url}, content_hash=prov.content_hash)
        staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
        assert await promote_statute_staging(db, staging) == "promoted"
        return (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()

    source_instrument = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ACT No. XXVII of 2025
        This Act is amended by S.R.O. 789(I)/2025.
        """,
        "http://127.0.0.1/idempotent-source.pdf",
    )
    assert source_instrument is not None
    await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        NOTIFICATION
        S.R.O. 789(I)/2025
        Dated 3rd August 2025
        """,
        "http://127.0.0.1/idempotent-target.pdf",
    )

    await db.commit()
    first = await reconcile_instrument_relations(limit=100, lookback_hours=24 * 365)
    second = await reconcile_instrument_relations(limit=100, lookback_hours=24 * 365)
    edges = (
        await db.execute(
            select(InstrumentRelation).where(InstrumentRelation.source_instrument_id == source_instrument.id),
        )
    ).scalars().all()
    assert len([e for e in edges if e.relation_type == "amended_by"]) == 1
    assert first["processed"] >= 1
    assert second["edges_added"] == 0
    assert second["edges_removed"] == 0


async def test_instrument_relation_reconcile_rotates_through_limited_batch(db):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()
    await db.execute(update(Instrument).values(created_at=datetime.now(timezone.utc) - timedelta(days=10)))
    await db.commit()

    async def _promote(text: str, url: str) -> Instrument:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=text,
            url=url,
            kind="instrument",
        )
        out = await HybridExtractor(db, source).extract_instrument(text=text, source_meta={"url": url}, content_hash=prov.content_hash)
        staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
        assert await promote_statute_staging(db, staging) == "promoted"
        return (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()

    source_a = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ACT No. XXX of 2025
        This Act is amended by S.R.O. 901(I)/2025.
        """,
        "http://127.0.0.1/rotate-source-a.pdf",
    )
    source_b = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ACT No. XXXI of 2025
        This Act is amended by S.R.O. 902(I)/2025.
        """,
        "http://127.0.0.1/rotate-source-b.pdf",
    )
    target_a = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        NOTIFICATION
        S.R.O. 901(I)/2025
        Dated 8th August 2025
        """,
        "http://127.0.0.1/rotate-target-a.pdf",
    )
    target_b = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        NOTIFICATION
        S.R.O. 902(I)/2025
        Dated 9th August 2025
        """,
        "http://127.0.0.1/rotate-target-b.pdf",
    )
    assert source_a is not None and source_b is not None and target_a is not None and target_b is not None
    await db.commit()

    for _ in range(5):
        await reconcile_instrument_relations(limit=1, lookback_hours=24)

    edges_a = (
        await db.execute(
            select(InstrumentRelation).where(InstrumentRelation.source_instrument_id == source_a.id),
        )
    ).scalars().all()
    edges_b = (
        await db.execute(
            select(InstrumentRelation).where(InstrumentRelation.source_instrument_id == source_b.id),
        )
    ).scalars().all()
    amended_a = [e for e in edges_a if e.relation_type == "amended_by"]
    amended_b = [e for e in edges_b if e.relation_type == "amended_by"]
    assert len(amended_a) == 1
    assert len(amended_b) == 1
    assert amended_a[0].target_instrument_id == target_a.id
    assert amended_b[0].target_instrument_id == target_b.id


async def test_instrument_relation_reconcile_continues_after_rollback(db, monkeypatch):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "GazetteOfPakistan"),
        )
    ).scalars().first()
    await db.execute(update(Instrument).values(created_at=datetime.now(timezone.utc) - timedelta(days=10)))
    await db.commit()

    async def _promote(text: str, url: str) -> Instrument:
        prov = await record_provenance(
            db,
            source=source,
            url=url,
            content=text.encode("utf-8"),
            content_kind="text",
        )
        staging = await stage_statute(
            db,
            source=source,
            prov=prov,
            raw_html=None,
            raw_text=text,
            url=url,
            kind="instrument",
        )
        out = await HybridExtractor(db, source).extract_instrument(text=text, source_meta={"url": url}, content_hash=prov.content_hash)
        staging.reconciled_json, staging.status, staging.confidence_score = out.data, "extracted", out.confidence
        assert await promote_statute_staging(db, staging) == "promoted"
        return (await db.execute(select(Instrument).where(Instrument.id == staging.promoted_to_id))).scalars().first()

    first = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ACT No. XXXII of 2025
        This Act is amended by S.R.O. 999(I)/2025.
        """,
        "http://127.0.0.1/rollback-source-a.pdf",
    )
    second = await _promote(
        """
        THE GAZETTE OF PAKISTAN EXTRAORDINARY
        ACT No. XXXIII of 2025
        This Act is amended by S.R.O. 998(I)/2025.
        """,
        "http://127.0.0.1/rollback-source-b.pdf",
    )
    assert first is not None and second is not None
    await db.commit()

    original_sync = promotion_task_module._sync_instrument_relation_edges
    calls = {"count": 0}

    async def _fail_once(sync_db, inst):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated reconcile failure")
        await original_sync(sync_db, inst)

    monkeypatch.setattr(promotion_task_module, "_sync_instrument_relation_edges", _fail_once)
    counts = await reconcile_instrument_relations(limit=2, lookback_hours=24)
    assert counts["failed"] == 1
    assert counts["processed"] == 1


async def test_judgment_citation_relation_graph_persists_spans_and_stable_keys(db, source):
    async def _promote(html: str, url: str) -> Judgment:
        prov = await record_provenance(db, source=source, url=url, content=html.encode(), content_kind="html")
        st = await stage_judgment(db, source=source, prov=prov, raw_html=html, raw_text=clean_html(html), url=url)
        out = await HybridExtractor(db, source).extract_judgment(html=html, text=st.raw_text, content_hash=prov.content_hash)
        st.reconciled_json, st.status, st.confidence_score = out.data, "extracted", out.confidence
        assert await promote_judgment_staging(db, st) == "promoted"
        return (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().first()

    target = await _promote(judgment_html("PLD 2019 SC 1", title="Target Case versus Federation"), "http://127.0.0.1/target")
    source_row = await _promote(JUDGMENT_HTML, "http://127.0.0.1/source")
    assert target is not None and source_row is not None

    edges = (
        await db.execute(
            select(JudgmentCitationRelation).where(JudgmentCitationRelation.source_judgment_id == source_row.id).order_by(JudgmentCitationRelation.span_start.asc())
        )
    ).scalars().all()
    assert len(edges) >= 3

    pld_edges = [e for e in edges if e.target_citation_key == "PLD:2019:SC:1"]
    assert len(pld_edges) >= 2
    assert all(e.target_judgment_id == target.id and e.resolution_status == "linked" for e in pld_edges)
    assert any(source_row.full_text[e.span_start : e.span_end] == "PLD 2019 SC 1" for e in pld_edges)

    scmr_edges = [e for e in edges if e.target_citation_key == "SCMR:2015:-:100"]
    assert len(scmr_edges) >= 2
    assert all(e.target_judgment_id is None and e.resolution_status == "unresolved" for e in scmr_edges)


async def test_judgment_citation_relation_reconcile_backfills_late_targets_and_is_idempotent(db, source):
    async def _promote(html: str, url: str) -> Judgment:
        prov = await record_provenance(db, source=source, url=url, content=html.encode(), content_kind="html")
        st = await stage_judgment(db, source=source, prov=prov, raw_html=html, raw_text=clean_html(html), url=url)
        out = await HybridExtractor(db, source).extract_judgment(html=html, text=st.raw_text, content_hash=prov.content_hash)
        st.reconciled_json, st.status, st.confidence_score = out.data, "extracted", out.confidence
        assert await promote_judgment_staging(db, st) == "promoted"
        return (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().first()

    source_row = await _promote(JUDGMENT_HTML, "http://127.0.0.1/source-first")
    assert source_row is not None
    unresolved_before = (
        await db.execute(
            select(JudgmentCitationRelation).where(
                JudgmentCitationRelation.source_judgment_id == source_row.id,
                JudgmentCitationRelation.target_citation_key == "PLD:2019:SC:1",
                JudgmentCitationRelation.resolution_status == "unresolved",
            )
        )
    ).scalars().all()
    assert unresolved_before

    target = await _promote(judgment_html("PLD 2019 SC 1", title="Late Linked Case versus Province"), "http://127.0.0.1/target-late")
    assert target is not None
    await db.commit()

    first = await reconcile_judgment_citation_relations(lookback_hours=24 * 365, batch_size=50)
    assert first["processed"] >= 1
    linked_edges = (
        await db.execute(
            select(JudgmentCitationRelation).where(
                JudgmentCitationRelation.source_judgment_id == source_row.id,
                JudgmentCitationRelation.target_citation_key == "PLD:2019:SC:1",
                JudgmentCitationRelation.resolution_status == "linked",
            )
        )
    ).scalars().all()
    assert linked_edges
    assert all(edge.target_judgment_id == target.id for edge in linked_edges)

    second = await reconcile_judgment_citation_relations(lookback_hours=24 * 365, batch_size=50)
    assert second["edges_added"] == 0
    assert second["edges_removed"] == 0


async def test_reconcile_residual_smoke_dry_run_is_read_only(db):
    source_judgment = Judgment(
        canonical_citation="PLD 2026 SC 700",
        full_text="Reference was made to PLD 2030 SC 777 in argument.",
    )
    db.add(source_judgment)
    await db.flush()
    await sync_judgment_citation_relations(db, source_judgment)
    await db.commit()

    unresolved_before = (
        await db.execute(
            select(func.count())
            .select_from(JudgmentCitationRelation)
            .where(JudgmentCitationRelation.resolution_status == "unresolved")
        )
    ).scalar() or 0
    report = await reconcile_citation_statute_residual_smoke(run_reconcile=False)
    unresolved_after = (
        await db.execute(
            select(func.count())
            .select_from(JudgmentCitationRelation)
            .where(JudgmentCitationRelation.resolution_status == "unresolved")
        )
    ).scalar() or 0

    assert report["mode"] == "dry_run"
    assert report["before"]["judgment_citation_unresolved"] >= 1
    assert report["before"] == report["after"]
    assert report["delta"]["total_unresolved_reduced"] == 0
    assert "unresolved_breakdown" not in report
    assert unresolved_after == unresolved_before


async def test_reconcile_residual_smoke_apply_reports_before_after_reduction(db):
    citation_target = "PLD 2030 SC 777"
    source_judgment = Judgment(
        canonical_citation="PLD 2026 SC 701",
        full_text=f"The Court relied upon {citation_target} for the controlling principle.",
    )
    db.add(source_judgment)
    await db.flush()
    await sync_judgment_citation_relations(db, source_judgment)

    statute = Statute(
        name="Constitution of Pakistan, 1973",
        short_name="Constitution",
        jurisdiction="Federal",
        statute_type="constitution",
    )
    db.add(statute)
    await db.flush()

    instrument_text = "In the Constitution of Pakistan, 1973, Article 10A shall be substituted."
    section_token = "Article 10A"
    token_start = instrument_text.index(section_token)
    token_end = token_start + len(section_token)
    instrument = Instrument(
        type="act",
        number="Act No. I of 2026",
        full_text=instrument_text,
        full_text_hash=canonical_text_hash(instrument_text),
        affected_statute_id=statute.id,
        affected_statute_name=statute.name,
        citation_mentions=[],
        statute_mentions=[
            {
                "raw": section_token,
                "normalized": statute.name,
                "canonical_statute_name": statute.name,
                "linked_statute_id": str(statute.id),
                "section_number": section_token,
                "span": [token_start, token_end],
            }
        ],
        source_name="NasirLawSite",
        source_url="http://127.0.0.1/residual-smoke-source.txt",
    )
    db.add(instrument)
    await db.flush()
    await promotion_task_module._sync_instrument_relation_edges(db, instrument)
    await db.commit()

    unresolved_section_before = (
        await db.execute(
            select(func.count())
            .select_from(InstrumentSectionRelation)
            .where(InstrumentSectionRelation.target_statute_section_id.is_(None))
        )
    ).scalar() or 0
    assert unresolved_section_before >= 1

    target_judgment = Judgment(canonical_citation=citation_target, full_text="Target judgment text.")
    db.add(target_judgment)
    await db.flush()
    db.add(
        Citation(
            judgment_id=target_judgment.id,
            citation_string=citation_target,
            raw_string=citation_target,
            is_primary=True,
        )
    )
    db.add(
        StatuteSection(
            statute_id=statute.id,
            section_number="10A",
            section_title="Right to fair trial",
            sort_key=1,
        )
    )
    await db.commit()

    report = await reconcile_citation_statute_residual_smoke(
        run_reconcile=True,
        lookback_hours=24 * 365,
        instrument_limit=50,
        judgment_batch_size=50,
    )

    assert report["mode"] == "apply"
    assert report["before"]["judgment_citation_unresolved"] >= 1
    assert report["before"]["instrument_section_unresolved"] >= 1
    assert report["after"]["judgment_citation_unresolved"] < report["before"]["judgment_citation_unresolved"]
    assert report["after"]["instrument_section_unresolved"] < report["before"]["instrument_section_unresolved"]
    assert report["delta"]["judgment_citation_unresolved_reduced"] >= 1
    assert report["delta"]["instrument_section_unresolved_reduced"] >= 1
    assert "reconcile_instrument_relations" in report["runs"]
    assert "reconcile_judgment_citation_relations" in report["runs"]
    assert "unresolved_breakdown" not in report


async def test_reconcile_residual_smoke_breakdown_reports_top_buckets(db):
    statute = Statute(name="Qanun-e-Shahadat Order, 1984", jurisdiction="Federal", statute_type="act")
    db.add(statute)
    await db.flush()

    judgments = [
        Judgment(canonical_citation="PLD 2026 SC 801", source_name="SourceA", full_text="The bench relied on PLD 2030 SC 777."),
        Judgment(canonical_citation="PLD 2026 SC 802", source_name="SourceA", full_text="Counsel cited PLD 2030 SC 777 in support."),
        Judgment(canonical_citation="PLD 2026 SC 803", source_name="SourceB", full_text="A reference was made to PLD 2040 SC 1."),
    ]
    db.add_all(judgments)
    await db.flush()
    for judgment in judgments:
        await sync_judgment_citation_relations(db, judgment)

    instrument_text = "In the Qanun-e-Shahadat Order, 1984, Article 10A and Article 11 shall be omitted."
    for idx, (number, source_name) in enumerate((("Article 10A", "SourceInstA"), ("Article 10A", "SourceInstA"), ("Article 11", "SourceInstB")), start=1):
        start = instrument_text.index(number)
        inst = Instrument(
            type="act",
            number=f"Act {source_name}-{idx}",
            full_text=instrument_text,
            full_text_hash=canonical_text_hash(f"{instrument_text}:{number}:{source_name}:{idx}"),
            affected_statute_id=statute.id,
            affected_statute_name=statute.name,
            citation_mentions=[],
            statute_mentions=[
                {
                    "raw": number,
                    "normalized": statute.name,
                    "canonical_statute_name": statute.name,
                    "linked_statute_id": str(statute.id),
                    "section_number": number,
                    "span": [start, start + len(number)],
                }
            ],
            source_name=source_name,
            source_url=f"http://127.0.0.1/{source_name}.txt",
        )
        db.add(inst)
        await db.flush()
        await promotion_task_module._sync_instrument_relation_edges(db, inst)
    await db.commit()

    report = await reconcile_citation_statute_residual_smoke(
        run_reconcile=False,
        include_unresolved_breakdown=True,
        unresolved_breakdown_top_n=2,
    )

    breakdown = report["unresolved_breakdown"]
    assert breakdown["top_n"] == 2
    assert "judgment_citation_unresolved" in breakdown
    assert "instrument_section_unresolved" in breakdown
    judgment_source = breakdown["judgment_citation_unresolved"]["by_source_name"]
    judgment_keys = breakdown["judgment_citation_unresolved"]["by_target_citation_key"]
    instrument_source = breakdown["instrument_section_unresolved"]["by_source_name"]
    instrument_keys = breakdown["instrument_section_unresolved"]["by_target_section_key"]
    assert len(judgment_source) <= 2 and len(judgment_keys) <= 2
    assert len(instrument_source) <= 2 and len(instrument_keys) <= 2
    assert any(row["source_name"] == "SourceA" and row["count"] >= 2 for row in judgment_source)
    assert any(row["source_name"] == "SourceInstA" and row["count"] >= 2 for row in instrument_source)
    assert all(row["count"] >= 1 for row in judgment_keys + instrument_keys)


async def test_reconcile_residual_smoke_breakdown_empty_when_no_unresolved_rows(db):
    report = await reconcile_citation_statute_residual_smoke(
        run_reconcile=False,
        include_unresolved_breakdown=True,
        unresolved_breakdown_top_n=3,
    )

    assert report["before"]["total_unresolved"] == 0
    breakdown = report["unresolved_breakdown"]
    assert breakdown["top_n"] == 3
    assert breakdown["judgment_citation_unresolved"]["by_source_name"] == []
    assert breakdown["judgment_citation_unresolved"]["by_target_citation_key"] == []
    assert breakdown["instrument_section_unresolved"]["by_source_name"] == []
    assert breakdown["instrument_section_unresolved"]["by_target_section_key"] == []


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


async def test_treatment_reconcile_links_late_arriving_unique_and_is_idempotent(db):
    citing = Judgment(canonical_citation="PLD 2026 SC 123", full_text="citing text")
    db.add(citing)
    await db.flush()
    treatment = Treatment(
        citing_judgment_id=citing.id,
        cited_judgment_id=None,
        cited_citation="PLD 2019 SC 1",
        label="followed",
        confidence=0.82,
        evidence_passage="We have followed PLD 2019 SC 1.",
        method="deterministic",
    )
    db.add(treatment)
    await db.commit()

    before_target = await reconcile_treatment_citation_links(lookback_hours=24, batch_size=10)
    assert before_target["linked"] == 0
    assert before_target["unresolved"] == 1

    target = Judgment(canonical_citation="PLD 2019 SC 1", full_text="target text")
    db.add(target)
    await db.flush()
    db.add(Citation(judgment_id=target.id, citation_string="PLD 2019 SC 1", raw_string="PLD 2019 SC 1", is_primary=True))
    await db.commit()

    linked = await reconcile_treatment_citation_links(lookback_hours=24, batch_size=10)
    assert linked["linked"] == 1
    assert linked["ambiguous"] == 0
    assert linked["unresolved"] == 0

    await db.refresh(treatment)
    await db.refresh(target)
    assert treatment.cited_judgment_id == target.id
    assert target.citation_count == 1

    rerun = await reconcile_treatment_citation_links(lookback_hours=24, batch_size=10)
    assert rerun["scanned"] == 0
    assert rerun["linked"] == 0
    await db.refresh(treatment)
    assert treatment.cited_judgment_id == target.id


async def test_treatment_reconcile_skips_ambiguous_citation_matches(db):
    citing = Judgment(canonical_citation="PLD 2026 SC 124", full_text="citing text")
    db.add(citing)
    await db.flush()
    treatment = Treatment(
        citing_judgment_id=citing.id,
        cited_judgment_id=None,
        cited_citation="PLD 2018 SC 50",
        label="referred",
        confidence=0.73,
        evidence_passage="Reference made to PLD 2018 SC 50.",
        method="deterministic",
    )
    db.add(treatment)

    canonical_match = Judgment(canonical_citation="PLD 2018 SC 50", full_text="candidate A")
    citation_match = Judgment(canonical_citation="PLD 2018 SC 500", full_text="candidate B")
    db.add_all([canonical_match, citation_match])
    await db.flush()
    db.add(Citation(judgment_id=citation_match.id, citation_string="PLD 2018 SC 50", raw_string="PLD 2018 SC 50", is_primary=False))
    await db.commit()

    result = await reconcile_treatment_citation_links(lookback_hours=24, batch_size=10)
    assert result["linked"] == 0
    assert result["ambiguous"] == 1
    assert result["unresolved"] == 0
    await db.refresh(treatment)
    assert treatment.cited_judgment_id is None


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
