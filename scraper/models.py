"""
SQLAlchemy 2.0 ORM for the SIKANDER AI corpus service — PostgreSQL 16 + pgvector + pg_trgm.

Two groups of tables:

CONTRACT TABLES (Layer 17 Annex A; the only tables `sikander_reader` may SELECT):
    court, judge, judgment, citation, treatment, statute, statute_section,
    statute_section_version, instrument, source_provenance, corpus_metadata

INTERNAL TABLES (never granted to the reader role):
    scraper_sources, scraper_jobs, scraper_staging, statutes_staging, quarantine_queue,
    embedding_queue, browser_session_slots, search_form_map, crawl_frontier, crawl_coverage,
    extraction_audit, scrapegraph_cache, instrument_relation, instrument_section_relation, archive_targets, archive_objects, sgai_usage_daily,
    notifications, schema_migrations

The vector dimension is read from EMBEDDING_DIM (Annex B-6); the model/dimension pair is
written to corpus_metadata at start-up and the embedding worker refuses to run on a mismatch.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import List, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from scraper.config import settings
from scraper.database import Base

EMBEDDING_DIM = settings.EMBEDDING_DIM

CONTRACT_TABLES = (
    "court",
    "judge",
    "judgment",
    "citation",
    "treatment",
    "statute",
    "statute_section",
    "statute_section_version",
    "instrument",
    "source_provenance",
    "corpus_metadata",
)

ACCESS_METHODS = ("public", "login_session", "licensed_api", "import_folder")
SLOT_STATES = ("EMPTY", "ACTIVE", "NEEDS_HUMAN_LOGIN", "PAUSED", "HALTED")
SOURCE_STATES = ("ACTIVE", "PAUSED", "HALTED", "DISABLED")
BENCH_TYPES = ("single", "division", "full", "larger")
TREATMENT_LABELS = (
    "followed",
    "relied_upon",
    "approved",
    "distinguished",
    "not_followed",
    "overruled",
    "dissented_from",
    "per_incuriam",
    "referred",
)
STAGING_STATES = ("pending", "extracted", "promoted", "quarantined", "duplicate", "failed")


def _uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _created():
    return mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# =============================================================================
# CONTRACT TABLES
# =============================================================================
class Court(Base):
    __tablename__ = "court"
    __table_args__ = (
        UniqueConstraint("name", name="uq_court_name"),
        UniqueConstraint("short_code", name="uq_court_short_code"),
        Index("ix_court_level", "court_level"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    short_code: Mapped[str] = mapped_column(String(50), nullable=False)
    court_level: Mapped[str] = mapped_column(String(50), nullable=False)
    jurisdiction_province: Mapped[Optional[str]] = mapped_column(String(100))
    city: Mapped[Optional[str]] = mapped_column(String(100))
    parent_court_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("court.id", ondelete="SET NULL"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    established_year: Mapped[Optional[int]] = mapped_column(Integer)
    website_url: Mapped[Optional[str]] = mapped_column(String(500))
    aliases: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Judge(Base):
    __tablename__ = "judge"
    __table_args__ = (UniqueConstraint("normalized_name", name="uq_judge_normalized_name"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    court_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("court.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class SourceProvenance(Base):
    """One row per preserved fetch. Written BEFORE any extraction (raw-first)."""

    __tablename__ = "source_provenance"
    __table_args__ = (
        Index("ix_prov_content_hash", "content_hash"),
        Index("ix_prov_source_name", "source_name"),
        Index("ix_prov_fetched_at", "fetched_at"),
        Index("ix_prov_promoted", "promoted_table", "promoted_id"),
        CheckConstraint("access_method IN ('public','login_session','licensed_api','import_folder')", name="ck_prov_access_method"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    access_method: Mapped[str] = mapped_column(String(30), nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    route_json: Mapped[Optional[dict]] = mapped_column(JSONB, comment="How the page was reached: tier, query, page, slot")
    routes: Mapped[Optional[list]] = mapped_column(JSONB, comment="Every route that reached the same content hash")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="SHA-256 of the raw bytes")
    content_kind: Mapped[str] = mapped_column(String(20), nullable=False, comment="html|text|pdf|json")
    raw_ref: Mapped[Optional[str]] = mapped_column(String(1000), comment="Path of the preserved raw bytes under RAW_STORAGE_PATH")
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = _created()
    is_original_document: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    document_kind: Mapped[str] = mapped_column(String(30), default="html_text", server_default=text("'html_text'"), nullable=False, comment="original_pdf|html_text|rendered_copy|json")
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"))
    promoted_table: Mapped[Optional[str]] = mapped_column(String(50))
    promoted_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()


class Judgment(Base):
    __tablename__ = "judgment"
    __table_args__ = (
        UniqueConstraint("canonical_citation", name="uq_judgment_canonical_citation"),
        UniqueConstraint("full_text_hash", name="uq_judgment_full_text_hash"),
        Index("ix_judgment_court_id", "court_id"),
        Index("ix_judgment_year", "year"),
        Index("ix_judgment_reporter", "reporter"),
        Index("ix_judgment_decision_date", "decision_date"),
        Index("ix_judgment_access_method", "access_method"),
        Index("ix_judgment_title_trgm", "case_title", postgresql_using="gin", postgresql_ops={"case_title": "gin_trgm_ops"}),
        Index("ix_judgment_citation_trgm", "canonical_citation", postgresql_using="gin", postgresql_ops={"canonical_citation": "gin_trgm_ops"}),
        Index(
            "ix_judgment_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        CheckConstraint("year IS NULL OR (year >= 1947 AND year <= 2100)", name="ck_judgment_year"),
        CheckConstraint("confidence_score >= 0 AND confidence_score <= 1", name="ck_judgment_confidence"),
        CheckConstraint("bench_type IS NULL OR bench_type IN ('single','division','full','larger')", name="ck_judgment_bench_type"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    canonical_citation: Mapped[str] = mapped_column(String(200), nullable=False)
    case_title: Mapped[Optional[str]] = mapped_column(String(2000))
    court_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("court.id", ondelete="SET NULL"))
    court_name: Mapped[Optional[str]] = mapped_column(String(255))
    judge_names: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text))
    bench_size: Mapped[Optional[int]] = mapped_column(Integer)
    bench_type: Mapped[Optional[str]] = mapped_column(String(20))
    decision_date: Mapped[Optional[date]] = mapped_column(Date)
    year: Mapped[Optional[int]] = mapped_column(Integer)
    reporter: Mapped[Optional[str]] = mapped_column(String(20))
    volume: Mapped[Optional[str]] = mapped_column(String(50))
    page_number: Mapped[Optional[int]] = mapped_column(Integer)
    docket_number: Mapped[Optional[str]] = mapped_column(String(300))
    petitioner: Mapped[Optional[str]] = mapped_column(String(1000))
    respondent: Mapped[Optional[str]] = mapped_column(String(1000))
    full_text: Mapped[Optional[str]] = mapped_column(Text)
    full_text_hash: Mapped[Optional[str]] = mapped_column(String(64))
    headnotes: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    statutes_cited: Mapped[Optional[list]] = mapped_column(JSONB, comment="[{statute_name, section_number}]")
    citations_cited: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text))
    access_method: Mapped[str] = mapped_column(String(30), nullable=False, default="public", server_default=text("'public'"))
    source_name: Mapped[Optional[str]] = mapped_column(String(100))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    source_provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"))
    original_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"), comment="Provenance row of the original PDF when one exists")
    has_original_pdf: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    language: Mapped[str] = mapped_column(String(20), default="en", server_default=text("'en'"), nullable=False)
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(EMBEDDING_DIM))
    citation_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False, comment="Derived statistic only")
    confidence_score: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"), nullable=False)
    extraction_engine: Mapped[Optional[str]] = mapped_column(String(40), comment="deterministic|hybrid|scrapegraph_managed|scrapegraph_local")
    promoted_at: Mapped[datetime] = _created()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    citations: Mapped[List["Citation"]] = relationship("Citation", back_populates="judgment", cascade="all, delete-orphan")


class Citation(Base):
    __tablename__ = "citation"
    __table_args__ = (
        UniqueConstraint("citation_string", name="uq_citation_string"),
        Index("ix_citation_judgment_id", "judgment_id"),
        Index("ix_citation_reporter_year", "reporter", "year"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    judgment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("judgment.id", ondelete="CASCADE"), nullable=False)
    citation_string: Mapped[str] = mapped_column(String(200), nullable=False, comment="Normalised, e.g. 'PLD 2019 SC 1'")
    raw_string: Mapped[Optional[str]] = mapped_column(String(300))
    reporter: Mapped[Optional[str]] = mapped_column(String(20))
    year: Mapped[Optional[int]] = mapped_column(Integer)
    volume: Mapped[Optional[str]] = mapped_column(String(50))
    page: Mapped[Optional[int]] = mapped_column(Integer)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    source_evidence: Mapped[Optional[str]] = mapped_column(String(500), comment="Raw-text snippet proving the citation")
    created_at: Mapped[datetime] = _created()

    judgment: Mapped["Judgment"] = relationship("Judgment", back_populates="citations")


class Treatment(Base):
    __tablename__ = "treatment"
    __table_args__ = (
        Index("ix_treatment_citing", "citing_judgment_id"),
        Index("ix_treatment_cited", "cited_judgment_id"),
        Index("ix_treatment_cited_citation", "cited_citation"),
        UniqueConstraint("citing_judgment_id", "cited_citation", "label", name="uq_treatment_triplet"),
        CheckConstraint(
            "label IN ('followed','relied_upon','approved','distinguished','not_followed','overruled','dissented_from','per_incuriam','referred')",
            name="ck_treatment_label",
        ),
        CheckConstraint("char_length(evidence_passage) <= 400", name="ck_treatment_evidence_400"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    citing_judgment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("judgment.id", ondelete="CASCADE"), nullable=False)
    cited_judgment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("judgment.id", ondelete="SET NULL"))
    cited_citation: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_passage: Mapped[str] = mapped_column(String(400), nullable=False)
    method: Mapped[str] = mapped_column(String(30), nullable=False, comment="deterministic|model|local_model")
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = _created()


class Statute(Base):
    __tablename__ = "statute"
    __table_args__ = (
        UniqueConstraint("name", name="uq_statute_name"),
        Index("ix_statute_short_name", "short_name"),
        Index("ix_statute_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(1000), nullable=False)
    short_name: Mapped[Optional[str]] = mapped_column(String(100))
    jurisdiction: Mapped[str] = mapped_column(String(50), default="Federal", server_default=text("'Federal'"), nullable=False)
    statute_type: Mapped[Optional[str]] = mapped_column(String(40), comment="act|ordinance|constitution|rules|regulations|order")
    year_enacted: Mapped[Optional[int]] = mapped_column(Integer)
    is_repealed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    repealed_by: Mapped[Optional[str]] = mapped_column(String(500))
    source_name: Mapped[Optional[str]] = mapped_column(String(100))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    sections: Mapped[List["StatuteSection"]] = relationship("StatuteSection", back_populates="statute", cascade="all, delete-orphan")


class StatuteSection(Base):
    __tablename__ = "statute_section"
    __table_args__ = (
        UniqueConstraint("statute_id", "section_number", name="uq_statute_section"),
        Index("ix_statute_section_statute_id", "statute_id"),
        Index("ix_statute_section_number", "section_number"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    statute_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("statute.id", ondelete="CASCADE"), nullable=False)
    section_number: Mapped[str] = mapped_column(String(100), nullable=False)
    section_title: Mapped[Optional[str]] = mapped_column(String(1000))
    chapter: Mapped[Optional[str]] = mapped_column(String(255))
    part: Mapped[Optional[str]] = mapped_column(String(255))
    sort_key: Mapped[Optional[int]] = mapped_column(Integer)
    current_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    statute: Mapped["Statute"] = relationship("Statute", back_populates="sections")
    versions: Mapped[List["StatuteSectionVersion"]] = relationship("StatuteSectionVersion", back_populates="section", cascade="all, delete-orphan")


class StatuteSectionVersion(Base):
    """One row per text version (Annex B-5). version_confidence 0.5 where only current text exists."""

    __tablename__ = "statute_section_version"
    __table_args__ = (
        UniqueConstraint("section_id", "text_hash", name="uq_section_version_hash"),
        Index("ix_section_version_section_id", "section_id"),
        Index("ix_section_version_effective", "effective_from", "effective_to"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    section_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("statute_section.id", ondelete="CASCADE"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    section_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[Optional[date]] = mapped_column(Date)
    effective_to: Mapped[Optional[date]] = mapped_column(Date)
    amending_instrument_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("instrument.id", ondelete="SET NULL"))
    amending_instrument_text: Mapped[Optional[str]] = mapped_column(String(500))
    version_confidence: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"), nullable=False)
    source_provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()

    section: Mapped["StatuteSection"] = relationship("StatuteSection", back_populates="versions")


class Instrument(Base):
    __tablename__ = "instrument"
    __table_args__ = (
        UniqueConstraint("full_text_hash", name="uq_instrument_text_hash"),
        Index("ix_instrument_type_date", "type", "date"),
        Index("ix_instrument_affected_statute", "affected_statute_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    type: Mapped[str] = mapped_column(String(40), nullable=False, comment="act|ordinance|amendment|notification|rules|gazette_notice|bill")
    number: Mapped[Optional[str]] = mapped_column(String(100))
    date: Mapped[Optional[date]] = mapped_column(Date)
    title: Mapped[Optional[str]] = mapped_column(String(1000))
    gazette_ref: Mapped[Optional[str]] = mapped_column(String(300))
    full_text: Mapped[Optional[str]] = mapped_column(Text)
    full_text_hash: Mapped[Optional[str]] = mapped_column(String(64))
    affected_statute_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("statute.id", ondelete="SET NULL"))
    affected_statute_name: Mapped[Optional[str]] = mapped_column(String(1000))
    affected_sections: Mapped[Optional[list]] = mapped_column(JSONB)
    citation_mentions: Mapped[Optional[list]] = mapped_column(JSONB, comment="[{raw, normalized, mention_type, number, year, span, source_snippet}]")
    statute_mentions: Mapped[Optional[list]] = mapped_column(JSONB, comment="[{raw, canonical_statute_name, section_number, year, linked_statute_id}]")
    jurisdiction: Mapped[Optional[str]] = mapped_column(String(50))
    source_name: Mapped[Optional[str]] = mapped_column(String(100))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    source_provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class CorpusMetadata(Base):
    __tablename__ = "corpus_metadata"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = _updated()


# =============================================================================
# INTERNAL TABLES
# =============================================================================
class InstrumentRelation(Base):
    __tablename__ = "instrument_relation"
    __table_args__ = (
        Index("ix_instrument_relation_source", "source_instrument_id"),
        Index("ix_instrument_relation_target_instrument", "target_instrument_id"),
        Index("ix_instrument_relation_target_statute", "target_statute_id"),
        UniqueConstraint(
            "source_instrument_id",
            "relation_type",
            "target_instrument_id",
            "target_statute_id",
            "span_start",
            "span_end",
            name="uq_instrument_relation_edge",
        ),
        CheckConstraint(
            "target_instrument_id IS NOT NULL OR target_statute_id IS NOT NULL",
            name="ck_instrument_relation_target_present",
        ),
        CheckConstraint("char_length(evidence_snippet) <= 500", name="ck_instrument_relation_evidence_500"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("instrument.id", ondelete="CASCADE"), nullable=False)
    target_instrument_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("instrument.id", ondelete="SET NULL"))
    target_statute_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("statute.id", ondelete="SET NULL"))
    relation_type: Mapped[str] = mapped_column(String(40), nullable=False, comment="amended_by|superseded_by|read_with")
    relation_phrase: Mapped[str] = mapped_column(String(120), nullable=False)
    target_mention_raw: Mapped[str] = mapped_column(String(300), nullable=False)
    target_mention_normalized: Mapped[str] = mapped_column(String(300), nullable=False)
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_snippet: Mapped[str] = mapped_column(String(500), nullable=False)
    source_provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class InstrumentSectionRelation(Base):
    __tablename__ = "instrument_section_relation"
    __table_args__ = (
        Index("ix_instrument_section_relation_source", "source_instrument_id"),
        Index("ix_instrument_section_relation_statute", "target_statute_id"),
        Index("ix_instrument_section_relation_section", "target_section_key"),
        UniqueConstraint(
            "source_instrument_id",
            "amendment_operation",
            "target_statute_id",
            "target_section_key",
            name="uq_instrument_section_relation_edge",
        ),
        CheckConstraint(
            "amendment_operation IN ('insert','substitute','omit','repeal')",
            name="ck_instrument_section_relation_operation",
        ),
        CheckConstraint("span_start >= 0 AND span_end > span_start", name="ck_instrument_section_relation_span"),
        CheckConstraint("char_length(evidence_snippet) <= 500", name="ck_instrument_section_relation_evidence_500"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("instrument.id", ondelete="CASCADE"), nullable=False)
    target_statute_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("statute.id", ondelete="CASCADE"), nullable=False)
    target_statute_section_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("statute_section.id", ondelete="SET NULL"))
    target_section_key: Mapped[str] = mapped_column(String(120), nullable=False)
    amendment_operation: Mapped[str] = mapped_column(String(20), nullable=False, comment="insert|substitute|omit|repeal")
    relation_phrase: Mapped[str] = mapped_column(String(120), nullable=False)
    target_mention_raw: Mapped[str] = mapped_column(String(300), nullable=False)
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_snippet: Mapped[str] = mapped_column(String(500), nullable=False)
    source_provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class JudgmentCitationRelation(Base):
    __tablename__ = "judgment_citation_relation"
    __table_args__ = (
        Index("ix_judgment_citation_relation_source", "source_judgment_id"),
        Index("ix_judgment_citation_relation_target_judgment", "target_judgment_id"),
        Index("ix_judgment_citation_relation_target_key", "target_citation_key"),
        UniqueConstraint(
            "source_judgment_id",
            "target_citation_key",
            "span_start",
            "span_end",
            name="uq_judgment_citation_relation_edge",
        ),
        CheckConstraint(
            "resolution_status IN ('linked','ambiguous','unresolved')",
            name="ck_judgment_citation_relation_status",
        ),
        CheckConstraint("span_start >= 0 AND span_end > span_start", name="ck_judgment_citation_relation_span"),
        CheckConstraint("char_length(evidence_snippet) <= 500", name="ck_judgment_citation_relation_evidence_500"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_judgment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("judgment.id", ondelete="CASCADE"), nullable=False)
    target_judgment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("judgment.id", ondelete="SET NULL"))
    target_citation_raw: Mapped[str] = mapped_column(String(300), nullable=False)
    target_citation_normalized: Mapped[str] = mapped_column(String(200), nullable=False)
    target_citation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    resolution_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unresolved", server_default=text("'unresolved'"))
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_snippet: Mapped[str] = mapped_column(String(500), nullable=False)
    source_provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class ScraperSource(Base):
    __tablename__ = "scraper_sources"
    __table_args__ = (
        UniqueConstraint("source_name", name="uq_scraper_sources_source_name"),
        CheckConstraint("access_method IN ('public','login_session','licensed_api','import_folder')", name="ck_source_access_method"),
        CheckConstraint("extraction_mode IN ('deterministic','hybrid','scrapegraph_managed','scrapegraph_local')", name="ck_source_extraction_mode"),
        CheckConstraint("state IN ('ACTIVE','PAUSED','HALTED','DISABLED')", name="ck_source_state"),
        CheckConstraint("extraction_min_confidence >= 0 AND extraction_min_confidence <= 1", name="ck_source_min_conf"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    access_method: Mapped[str] = mapped_column(String(30), nullable=False, default="public", server_default=text("'public'"))
    allow_list: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"), comment="Permitted hostnames")
    document_cdn_hosts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="ACTIVE", server_default=text("'ACTIVE'"), nullable=False)
    state_reason: Mapped[Optional[str]] = mapped_column(String(1000))
    state_changed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    requires_admin_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    scrape_case_law: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    scrape_statutes: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    scrape_instruments: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    requires_login: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    respect_robots: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    trust_level: Mapped[str] = mapped_column(String(20), default="trusted", server_default=text("'trusted'"), nullable=False)
    scrape_frequency_hours: Mapped[int] = mapped_column(Integer, default=24, server_default=text("24"), nullable=False)
    request_delay_min_ms: Mapped[int] = mapped_column(Integer, default=1200, server_default=text("1200"), nullable=False)
    request_delay_max_ms: Mapped[int] = mapped_column(Integer, default=2500, server_default=text("2500"), nullable=False)
    # ScrapeGraph per-source policy (Amendment §7)
    ai_extract_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    extraction_mode: Mapped[str] = mapped_column(String(30), default="hybrid", server_default=text("'hybrid'"), nullable=False)
    extraction_min_confidence: Mapped[float] = mapped_column(Float, default=0.85, server_default=text("0.85"), nullable=False)
    scrapegraph_schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    crawl_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    crawl_max_depth: Mapped[int] = mapped_column(Integer, default=2, server_default=text("2"), nullable=False)
    crawl_max_pages: Mapped[int] = mapped_column(Integer, default=50, server_default=text("50"), nullable=False)
    # stats
    total_pages_scraped: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)
    total_records_extracted: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)
    last_scraped_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    last_success_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(String(2000))
    last_ai_error: Mapped[Optional[str]] = mapped_column(String(2000))
    next_scrape_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    jobs: Mapped[List["ScraperJob"]] = relationship("ScraperJob", back_populates="source", cascade="all, delete-orphan")


class ScraperJob(Base):
    __tablename__ = "scraper_jobs"
    __table_args__ = (Index("ix_jobs_source_created", "source_name", "created_at"), Index("ix_jobs_status", "status"))
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("scraper_sources.id", ondelete="SET NULL"))
    source_name: Mapped[Optional[str]] = mapped_column(String(100))
    job_type: Mapped[Optional[str]] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), nullable=False)
    celery_task_id: Mapped[Optional[str]] = mapped_column(String(100))
    worker_hostname: Mapped[Optional[str]] = mapped_column(String(200))
    pages_scraped: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    records_extracted: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    records_promoted: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    records_quarantined: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    records_failed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    job_params: Mapped[Optional[dict]] = mapped_column(JSONB)
    result_summary: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    source: Mapped[Optional["ScraperSource"]] = relationship("ScraperSource", back_populates="jobs")


class ScraperStaging(Base):
    """Raw-first judgment staging row. Provenance exists before this row; extraction follows it."""

    __tablename__ = "scraper_staging"
    __table_args__ = (
        Index("ix_staging_source", "source_name"),
        Index("ix_staging_content_hash", "content_hash"),
        Index("ix_staging_status", "status"),
        Index("ix_staging_created", "created_at"),
        CheckConstraint("confidence_score >= 0 AND confidence_score <= 1", name="ck_staging_confidence"),
        CheckConstraint("status IN ('pending','extracted','promoted','quarantined','duplicate','failed')", name="ck_staging_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("scraper_sources.id", ondelete="SET NULL"))
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    access_method: Mapped[str] = mapped_column(String(30), nullable=False, default="public", server_default=text("'public'"))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    provenance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"), nullable=False)
    job_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("scraper_jobs.id", ondelete="SET NULL"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_ref: Mapped[Optional[str]] = mapped_column(String(1000))
    raw_html: Mapped[Optional[str]] = mapped_column(Text)
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    raw_text_hash: Mapped[Optional[str]] = mapped_column(String(64))
    pdf_provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"))
    ocr_applied: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    route_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    # extraction outputs
    extracted_citation: Mapped[Optional[str]] = mapped_column(String(200))
    extracted_title: Mapped[Optional[str]] = mapped_column(String(2000))
    extracted_court: Mapped[Optional[str]] = mapped_column(String(255))
    extracted_year: Mapped[Optional[int]] = mapped_column(Integer)
    extracted_decision_date: Mapped[Optional[date]] = mapped_column(Date)
    deterministic_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    ai_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    reconciled_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    extraction_engine: Mapped[Optional[str]] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), nullable=False)
    promoted_to_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    quarantine_reason: Mapped[Optional[str]] = mapped_column(String(1000))
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"), nullable=False)
    validation_errors: Mapped[Optional[list]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class StatutesStaging(Base):
    __tablename__ = "statutes_staging"
    __table_args__ = (
        Index("ix_sstaging_source", "source_name"),
        Index("ix_sstaging_content_hash", "content_hash"),
        Index("ix_sstaging_status", "status"),
        CheckConstraint("status IN ('pending','extracted','promoted','quarantined','duplicate','failed')", name="ck_sstaging_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("scraper_sources.id", ondelete="SET NULL"))
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    access_method: Mapped[str] = mapped_column(String(30), nullable=False, default="public", server_default=text("'public'"))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    provenance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"), nullable=False)
    job_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("scraper_jobs.id", ondelete="SET NULL"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_ref: Mapped[Optional[str]] = mapped_column(String(1000))
    raw_html: Mapped[Optional[str]] = mapped_column(Text)
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default="statute", server_default=text("'statute'"), nullable=False, comment="statute|instrument")
    deterministic_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    ai_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    reconciled_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    extraction_engine: Mapped[Optional[str]] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), nullable=False)
    promoted_to_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    quarantine_reason: Mapped[Optional[str]] = mapped_column(String(1000))
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"), nullable=False)
    validation_errors: Mapped[Optional[list]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class QuarantineQueue(Base):
    """The review queue."""

    __tablename__ = "quarantine_queue"
    __table_args__ = (Index("ix_quarantine_reviewed", "reviewed"), Index("ix_quarantine_created", "created_at"))
    id: Mapped[uuid.UUID] = _uuid_pk()
    staging_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("scraper_staging.id", ondelete="CASCADE"))
    statutes_staging_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("statutes_staging.id", ondelete="CASCADE"))
    treatment_candidate: Mapped[Optional[dict]] = mapped_column(JSONB)
    source_name: Mapped[Optional[str]] = mapped_column(String(100))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="judgment", server_default=text("'judgment'"))
    extracted_citation: Mapped[Optional[str]] = mapped_column(String(200))
    extracted_title: Mapped[Optional[str]] = mapped_column(String(2000))
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSONB)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(200))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    resolution: Mapped[Optional[str]] = mapped_column(String(30), comment="promoted|rejected|remapped")
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class EmbeddingQueue(Base):
    __tablename__ = "embedding_queue"
    __table_args__ = (Index("ix_embedding_queue_status", "status"), UniqueConstraint("table_name", "record_id", name="uq_embedding_record"))
    id: Mapped[uuid.UUID] = _uuid_pk()
    record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    table_name: Mapped[str] = mapped_column(String(50), nullable=False)
    access_method: Mapped[str] = mapped_column(String(30), nullable=False, default="public", server_default=text("'public'"))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"), nullable=False)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    embedding_model: Mapped[Optional[str]] = mapped_column(String(100))
    embedding_dimensions: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class BrowserSessionSlot(Base):
    """Encrypted Playwright storage state per continuity slot (Amendment §9)."""

    __tablename__ = "browser_session_slots"
    __table_args__ = (
        UniqueConstraint("source_name", "slot_number", name="uq_session_slot"),
        CheckConstraint("slot_number IN (1,2)", name="ck_slot_number"),
        CheckConstraint("state IN ('EMPTY','ACTIVE','NEEDS_HUMAN_LOGIN','PAUSED','HALTED')", name="ck_slot_state"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="primary", server_default=text("'primary'"), comment="primary|alternate")
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="EMPTY", server_default=text("'EMPTY'"))
    state_reason: Mapped[Optional[str]] = mapped_column(String(1000))
    storage_state_encrypted: Mapped[Optional[str]] = mapped_column(Text, comment="Fernet-encrypted Playwright storage_state JSON")
    storage_state_hash: Mapped[Optional[str]] = mapped_column(String(64))
    login_username_encrypted: Mapped[Optional[str]] = mapped_column(Text, comment="Fernet-encrypted PakistanLawSite username for this slot (operator decision of 24 September 2026)")
    login_password_encrypted: Mapped[Optional[str]] = mapped_column(Text, comment="Fernet-encrypted PakistanLawSite password for this slot")
    login_credentials_updated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    login_credentials_updated_by: Mapped[Optional[str]] = mapped_column(String(200))
    logged_in_by: Mapped[Optional[str]] = mapped_column(String(200))
    logged_in_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    last_used_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    halted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    reconnect_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class SearchFormMap(Base):
    __tablename__ = "search_form_map"
    __table_args__ = (Index("ix_search_map_source_active", "source_name", "is_active"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    map_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result_layout: Mapped[dict] = mapped_column(JSONB, nullable=False)
    page_size: Mapped[Optional[int]] = mapped_column(Integer)
    pagination: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    detail_layout: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    limits: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    dom_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mapped_at: Mapped[datetime] = _created()
    mapped_by: Mapped[str] = mapped_column(String(40), nullable=False, default="deterministic", comment="deterministic|deterministic+local_ai")
    verified_against_dom: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    consecutive_parse_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    created_at: Mapped[datetime] = _created()


class CrawlFrontier(Base):
    """The truth for progress. One row per (source, tier, query_key)."""

    __tablename__ = "crawl_frontier"
    __table_args__ = (
        UniqueConstraint("source_name", "tier", "query_key", name="uq_frontier_query"),
        Index("ix_frontier_status", "source_name", "status", "priority"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    query_key: Mapped[str] = mapped_column(String(500), nullable=False)
    query_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cursor_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"), comment="page, row_index, last_citation_no")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default=text("'pending'"), comment="pending|in_progress|done|retired|stale")
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"), nullable=False)
    yield_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    slot_number: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(String(1000))
    last_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    next_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class CrawlCoverage(Base):
    """Tier 1 coverage ledger: one row per reporter volume (reporter, year)."""

    __tablename__ = "crawl_coverage"
    __table_args__ = (UniqueConstraint("source_name", "reporter", "year", name="uq_coverage_volume"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    reporter: Mapped[str] = mapped_column(String(20), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    volume_state: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default=text("'open'"), comment="open|closed|stale")
    highest_page_seen: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    next_page_to_probe: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    consecutive_misses: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    judgments_found: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    routes_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    closed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class ExtractionAudit(Base):
    __tablename__ = "extraction_audit"
    __table_args__ = (Index("ix_audit_staging", "staging_id"), Index("ix_audit_created", "created_at"), Index("ix_audit_status", "status"))
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_provenance.id", ondelete="SET NULL"))
    staging_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    source_name: Mapped[Optional[str]] = mapped_column(String(100))
    extractor: Mapped[str] = mapped_column(String(40), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    ai_mode: Mapped[Optional[str]] = mapped_column(String(30), comment="none|managed|local|cache")
    deterministic_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    ai_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    reconciled_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    conflicts_json: Mapped[Optional[list]] = mapped_column(JSONB)
    validation_errors_json: Mapped[Optional[list]] = mapped_column(JSONB)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    credits_or_cost: Mapped[Optional[float]] = mapped_column(Float)
    elapsed_ms: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), nullable=False, comment="ok|ai_failed|ai_skipped|cache_hit|budget_exhausted|circuit_open|invalid_json|privacy_blocked")
    created_at: Mapped[datetime] = _created()


class ScrapegraphCache(Base):
    __tablename__ = "scrapegraph_cache"
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    extraction_type: Mapped[str] = mapped_column(String(30), primary_key=True)
    engine_mode: Mapped[str] = mapped_column(String(30), primary_key=True)
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()


class ArchiveTarget(Base):
    __tablename__ = "archive_targets"
    __table_args__ = (
        UniqueConstraint("name", name="uq_archive_target_name"),
        CheckConstraint("target_type IN ('google_drive','dropbox','onedrive','s3_compatible','sftp','smb','local_path')", name="ck_archive_target_type"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(30), nullable=False)
    config_encrypted: Mapped[Optional[str]] = mapped_column(Text, comment="Fernet-encrypted JSON of adapter configuration")
    root_path: Mapped[str] = mapped_column(String(1000), nullable=False, default="", server_default=text("''"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    mirror_login_session_rows: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    last_ok_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(String(2000))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    objects_written: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)
    bytes_written: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class ArchiveObject(Base):
    __tablename__ = "archive_objects"
    __table_args__ = (
        UniqueConstraint("target_id", "object_key", name="uq_archive_object"),
        Index("ix_archive_objects_status", "target_id", "status"),
        Index("ix_archive_objects_judgment", "judgment_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("archive_targets.id", ondelete="CASCADE"), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    document_kind: Mapped[str] = mapped_column(String(30), nullable=False, comment="original_pdf|rendered_copy|index_csv|text")
    judgment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    source_provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    access_method: Mapped[str] = mapped_column(String(30), nullable=False, default="public", server_default=text("'public'"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="written", server_default=text("'written'"), comment="written|failed|missing|mismatch")
    error: Mapped[Optional[str]] = mapped_column(String(2000))
    written_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    verified_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = _created()


class SgaiUsageDaily(Base):
    __tablename__ = "sgai_usage_daily"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    engine_mode: Mapped[str] = mapped_column(String(30), primary_key=True)
    source_name: Mapped[str] = mapped_column(String(100), primary_key=True, default="*")
    calls: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    credits: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"), nullable=False)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    conflicts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    successes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    updated_at: Mapped[datetime] = _updated()


class Notification(Base):
    """Operator notifications shown on the dashboard (login needed, block, budget exhausted)."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_unacked", "acknowledged", "created_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    level: Mapped[str] = mapped_column(String(10), nullable=False, comment="info|warning|error|critical")
    source_name: Mapped[Optional[str]] = mapped_column(String(100))
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    message: Mapped[str] = mapped_column(String(2000), nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSONB)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = _created()


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    version: Mapped[str] = mapped_column(String(100), primary_key=True)
    applied_at: Mapped[datetime] = _created()


INTERNAL_TABLES = tuple(t for t in Base.metadata.tables.keys() if t not in CONTRACT_TABLES)
