"""
Validation → deduplication → promotion into the Annex A contract tables (Amendment §6 steps 8–10,
§12, §13; Cursor command §4 'same judgment by two routes → one row').

Judgments: identity = canonical citation; content identity = canonical full-text hash. A staging
row whose citation or text hash already exists becomes a duplicate pointing at the existing
judgment; its route is retained on the provenance row. Citation strings that already belong to a
different judgment are a conflict → quarantine, never a silent overwrite.

Statutes: statute (by name) → statute_section (by number) → statute_section_version (by text
hash, version_confidence 0.5 unless an effective date is evidenced). Instruments by text hash.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from celery import shared_task
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.extractors.judgment_guards import (
    detect_headnotes_only,
    detect_judgment_stub,
    guard_reason,
    strip_leading_judgment_chrome,
)
from scraper.extractors.login_surface_stub import is_login_surface_stub
from scraper.fetchers import canonical_text_hash, sha256_text
from scraper.models import (
    Citation,
    CorpusMetadata,
    Court,
    EmbeddingQueue,
    Instrument,
    InstrumentRelation,
    InstrumentSectionRelation,
    Judge,
    Judgment,
    JudgmentCitationRelation,
    QuarantineQueue,
    ScraperStaging,
    SourceProvenance,
    Statute,
    StatuteSection,
    StatuteSectionVersion,
    StatutesStaging,
)
from scraper.parsers.bench_parser import normalise_judge_name
from scraper.parsers.citation_extractor import canonicalise_statute_name, extract_citations, normalise_citation
from scraper.parsers.statute_parser import is_short_title_clause, looks_like_fragment_name

logger = logging.getLogger(__name__)


def _parse_citation_parts(c: str) -> Dict[str, Any]:
    hits = extract_citations(c)
    if hits:
        h = hits[0]
        page = h.get("page")
        return {"reporter": h.get("reporter"), "year": h.get("year"), "page": int(re.sub(r"\D", "", page)) if page and re.search(r"\d", page) else None, "court": h.get("court")}
    return {"reporter": None, "year": None, "page": None, "court": None}


async def _court_by_name(db: AsyncSession, name: Optional[str]) -> Optional[Court]:
    if not name:
        return None
    low = name.lower().replace(".", "")
    for c in (await db.execute(select(Court))).scalars().all():
        if c.name.lower() == low or c.short_code.lower() == low or any(a.lower().replace(".", "") == low for a in (c.aliases or [])):
            return c
    return None


async def _quarantine(db: AsyncSession, staging, reason: str, kind: str, details: Optional[Dict[str, Any]] = None) -> None:
    staging.status = "quarantined"
    staging.quarantine_reason = reason[:1000]
    exists = (await db.execute(select(QuarantineQueue).where((QuarantineQueue.staging_id == staging.id) if kind == "judgment" else (QuarantineQueue.statutes_staging_id == staging.id), QuarantineQueue.reviewed.is_(False)))).scalars().first()
    if exists is None:
        db.add(
            QuarantineQueue(
                staging_id=staging.id if kind == "judgment" else None,
                statutes_staging_id=staging.id if kind != "judgment" else None,
                source_name=staging.source_name,
                source_url=staging.source_url,
                kind=kind,
                extracted_citation=getattr(staging, "extracted_citation", None),
                extracted_title=getattr(staging, "extracted_title", None),
                reason=reason[:1000],
                details=details or {"validation_errors": staging.validation_errors},
                confidence_score=staging.confidence_score,
            )
        )
    await db.flush()


async def _ensure_judges(db: AsyncSession, names, court: Optional[Court]) -> None:
    for n in names or []:
        norm = normalise_judge_name(n).lower()
        if not norm:
            continue
        exists = (await db.execute(select(Judge).where(Judge.normalized_name == norm))).scalars().first()
        if exists is None:
            db.add(Judge(name=n, normalized_name=norm, court_id=court.id if court else None))
    await db.flush()


# --------------------------------------------------------------------------- judgments
async def promote_judgment_staging(db: AsyncSession, st: ScraperStaging, *, force: bool = False) -> str:
    """Returns promoted|duplicate|quarantined."""
    data = st.reconciled_json or {}
    document_type = str(data.get("document_type") or "").strip().lower()
    if document_type == "headnote":
        await _quarantine(
            db,
            st,
            "headnote_only: document_type=headnote is not eligible for full_judgment promotion",
            "judgment",
            {
                "document_type": data.get("document_type"),
                "document_type_reason": data.get("document_type_reason"),
                "validation_errors": st.validation_errors,
            },
        )
        return "quarantined"
    if st.source_name == "PakistanLawSite":
        headnote_signal = detect_headnotes_only(raw_text=st.raw_text, raw_html=st.raw_html)
        if headnote_signal is not None and headnote_signal.signal == "notes_on_cases_only":
            await _quarantine(
                db,
                st,
                guard_reason(headnote_signal),
                "judgment",
                {
                    "reason_code": headnote_signal.reason_code,
                    "signal": headnote_signal.signal,
                    "matched_value": headnote_signal.matched_value,
                    "validation_errors": st.validation_errors,
                },
            )
            return "quarantined"
    stub_signal = detect_judgment_stub(
        source_url=st.source_url or data.get("source_url"),
        raw_text=st.raw_text,
        raw_html=st.raw_html,
        judge_names=data.get("judge_names"),
        judge_fields={
            "judge": data.get("judge"),
            "judges": data.get("judges"),
        },
    )
    if stub_signal is not None:
        await _quarantine(
            db,
            st,
            guard_reason(stub_signal),
            "judgment",
            {
                "reason_code": stub_signal.reason_code,
                "signal": stub_signal.signal,
                "matched_value": stub_signal.matched_value,
                "validation_errors": st.validation_errors,
            },
        )
        return "quarantined"
    if is_login_surface_stub(
        source_url=st.source_url or data.get("source_url"),
        raw_text=st.raw_text,
        raw_html=st.raw_html,
        reconciled=data,
        court=data.get("court"),
        judge_names=data.get("judge_names"),
    ):
        await _quarantine(
            db,
            st,
            "login_stub: is_login_surface_stub",
            "judgment",
            {
                "reason_code": "login_stub",
                "signal": "is_login_surface_stub",
                "matched_value": (st.source_url or data.get("source_url") or "")[:500],
                "validation_errors": st.validation_errors,
            },
        )
        return "quarantined"
    if st.status == "quarantined" and not force:
        await _quarantine(db, st, st.quarantine_reason or "below confidence threshold", "judgment")
        return "quarantined"
    cits = [normalise_citation(c) for c in (data.get("citations") or []) if c]
    cits = [c for c in dict.fromkeys(cits) if c]
    if not cits:
        await _quarantine(db, st, "no citation supported by source", "judgment")
        return "quarantined"
    full_text = strip_leading_judgment_chrome(st.raw_text or "")
    if full_text != (st.raw_text or ""):
        st.raw_text = full_text
        st.raw_text_hash = canonical_text_hash(full_text)
    text_hash = canonical_text_hash(full_text)
    if st.raw_text_hash and text_hash != st.raw_text_hash:
        await _quarantine(db, st, "full_text hash changed between staging and promotion", "judgment")
        return "quarantined"
    canonical = cits[0]
    prov = (await db.execute(select(SourceProvenance).where(SourceProvenance.id == st.provenance_id))).scalars().first()
    court = await _court_by_name(db, data.get("court_canonical") or data.get("court"))
    dd = data.get("decision_date")
    decision_date = date.fromisoformat(dd) if isinstance(dd, str) and dd else None
    parts = _parse_citation_parts(canonical)
    # dedupe by identity and by content
    existing = (await db.execute(select(Judgment).where((Judgment.canonical_citation == canonical) | (Judgment.full_text_hash == text_hash)))).scalars().first()
    if existing is None:
        alt = (await db.execute(select(Citation).where(Citation.citation_string.in_(cits)))).scalars().first()
        if alt is not None:
            existing = (await db.execute(select(Judgment).where(Judgment.id == alt.judgment_id))).scalars().first()
            if existing is not None and existing.full_text_hash != text_hash and existing.canonical_citation != canonical:
                await _quarantine(db, st, f"citation {alt.citation_string} already belongs to judgment {existing.canonical_citation}", "judgment", {"conflict_with": str(existing.id)})
                return "quarantined"
    if existing is not None:
        existing_headnote_signal = detect_headnotes_only(raw_text=existing.full_text or "", raw_html=None)
        should_upgrade_existing = (
            st.source_name == "PakistanLawSite"
            and (existing.source_name or "") == "PakistanLawSite"
            and existing.canonical_citation == canonical
            and existing.full_text_hash != text_hash
            and existing_headnote_signal is not None
            and existing_headnote_signal.signal == "notes_on_cases_only"
        )
        if should_upgrade_existing:
            existing.case_title = (data.get("case_title") or existing.case_title)
            existing.court_id = court.id if court else existing.court_id
            existing.court_name = court.name if court else (data.get("court") or existing.court_name)
            existing.judge_names = data.get("judge_names") or existing.judge_names
            existing.bench_size = data.get("bench_size")
            existing.bench_type = data.get("bench_type")
            existing.decision_date = decision_date
            existing.year = data.get("year") or parts["year"] or existing.year
            existing.reporter = parts["reporter"] or existing.reporter
            existing.page_number = parts["page"] or existing.page_number
            existing.full_text = full_text
            existing.full_text_hash = text_hash
            existing.headnotes = data.get("headnotes")
            if data.get("statutes_cited") is not None:
                existing.statutes_cited = data.get("statutes_cited")
            if data.get("citations_cited") is not None:
                existing.citations_cited = data.get("citations_cited")
            existing.access_method = st.access_method
            existing.source_name = st.source_name
            existing.source_url = st.source_url
            existing.source_provenance_id = st.provenance_id
            existing.original_document_id = st.pdf_provenance_id
            existing.has_original_pdf = st.pdf_provenance_id is not None
            existing.confidence_score = st.confidence_score or existing.confidence_score
            existing.extraction_engine = st.extraction_engine
            if prov is not None:
                prov.promoted_table = "judgment"
                prov.promoted_id = existing.id
                routes = list(prov.routes or [])
                if st.route_json and st.route_json not in routes:
                    routes.append(st.route_json)
                    prov.routes = routes
            known = {c.citation_string for c in (await db.execute(select(Citation).where(Citation.judgment_id == existing.id))).scalars().all()}
            for c in cits:
                if c not in known:
                    taken = (await db.execute(select(Citation).where(Citation.citation_string == c))).scalars().first()
                    if taken is None:
                        part = _parse_citation_parts(c)
                        db.add(Citation(judgment_id=existing.id, citation_string=c, raw_string=c, reporter=part["reporter"], year=part["year"], page=part["page"], is_primary=False, source_evidence=(data.get("field_evidence") or {}).get("citations", "")[:500]))
            await _ensure_judges(db, data.get("judge_names"), court)
            st.status = "promoted"
            st.promoted_to_id = existing.id
            queue = (
                await db.execute(
                    select(EmbeddingQueue).where(
                        EmbeddingQueue.table_name == "judgment",
                        EmbeddingQueue.record_id == existing.id,
                    )
                )
            ).scalars().first()
            if queue is None:
                db.add(
                    EmbeddingQueue(
                        record_id=existing.id,
                        table_name="judgment",
                        access_method=st.access_method,
                        embedding_model=settings.EMBEDDING_MODEL,
                        embedding_dimensions=settings.EMBEDDING_DIM,
                    )
                )
            else:
                queue.status = "pending"
                queue.attempts = 0
                queue.error_message = None
            await db.flush()
            return "promoted"
        st.status = "duplicate"
        st.promoted_to_id = existing.id
        if prov is not None:
            prov.promoted_table = "judgment"
            prov.promoted_id = existing.id
            routes = list(prov.routes or [])
            if st.route_json and st.route_json not in routes:
                routes.append(st.route_json)
                prov.routes = routes
        # alternate citations learned from another route
        known = {c.citation_string for c in (await db.execute(select(Citation).where(Citation.judgment_id == existing.id))).scalars().all()}
        for c in cits:
            if c not in known:
                taken = (await db.execute(select(Citation).where(Citation.citation_string == c))).scalars().first()
                if taken is None:
                    parts = _parse_citation_parts(c)
                    db.add(Citation(judgment_id=existing.id, citation_string=c, raw_string=c, reporter=parts["reporter"], year=parts["year"], page=parts["page"], is_primary=False, source_evidence=(data.get("field_evidence") or {}).get("citations", "")[:500]))
        await db.flush()
        return "duplicate"
    j = Judgment(
        canonical_citation=canonical,
        case_title=(data.get("case_title") or None),
        court_id=court.id if court else None,
        court_name=court.name if court else data.get("court"),
        judge_names=data.get("judge_names") or [],
        bench_size=data.get("bench_size"),
        bench_type=data.get("bench_type"),
        decision_date=decision_date,
        year=data.get("year") or parts["year"],
        reporter=parts["reporter"],
        page_number=parts["page"],
        full_text=full_text,
        full_text_hash=text_hash,
        headnotes=data.get("headnotes"),
        statutes_cited=data.get("statutes_cited") or [],
        citations_cited=data.get("citations_cited") or [],
        access_method=st.access_method,
        source_name=st.source_name,
        source_url=st.source_url,
        source_provenance_id=st.provenance_id,
        original_document_id=st.pdf_provenance_id,
        has_original_pdf=st.pdf_provenance_id is not None,
        confidence_score=st.confidence_score or 0.0,
        extraction_engine=st.extraction_engine,
    )
    db.add(j)
    await db.flush()
    for i, c in enumerate(cits):
        taken = (await db.execute(select(Citation).where(Citation.citation_string == c))).scalars().first()
        if taken is not None:
            continue
        p = _parse_citation_parts(c)
        db.add(Citation(judgment_id=j.id, citation_string=c, raw_string=c, reporter=p["reporter"], year=p["year"], page=p["page"], is_primary=(i == 0), source_evidence=(data.get("field_evidence") or {}).get("citations", "")[:500]))
    await db.flush()
    # Keep judgment citation graph in sync at promotion time; late links are reconciled by maintenance.
    from scraper.tasks.treatment import sync_judgment_citation_relations

    await sync_judgment_citation_relations(db, j)
    await _ensure_judges(db, data.get("judge_names"), court)
    if prov is not None:
        prov.promoted_table = "judgment"
        prov.promoted_id = j.id
    st.status = "promoted"
    st.promoted_to_id = j.id
    db.add(EmbeddingQueue(record_id=j.id, table_name="judgment", access_method=st.access_method, embedding_model=settings.EMBEDDING_MODEL, embedding_dimensions=settings.EMBEDDING_DIM))
    await db.flush()
    return "promoted"


# --------------------------------------------------------------------------- statutes / instruments
def _norm_section(n: Optional[str]) -> str:
    raw = re.sub(r"\s+", " ", (n or "").strip()).rstrip(".")
    stable_key = _norm_section_key(raw)
    # Store a stable canonical section identifier when one is available so
    # amendment edges can resolve onto harvested statute sections reliably.
    if stable_key:
        return stable_key
    return raw


_PAKISTANCODE_MIN_SECTION_BODY_CHARS = 80
_PAKISTANCODE_THIN_BODY_RE = re.compile(
    r"(?i)\b(?:substituted|inserted|added|omitted|amended|renumbered|repealed)\s+by\b"
)


def _normalize_section_body_for_quality(section_text: Any) -> str:
    text = str(section_text or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?i)^\s*(?:section|sec\.?|s\.?|article|art\.?|rule|r\.?)\s+\d+[A-Z]?\s*[\.\-:)\]]*\s*", "", text)
    text = re.sub(r"^\s*\d+[A-Z]?\s*[\.\-:)\]]\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_pakistancode_thin_section_body(normalized_body: str) -> bool:
    footnote = _PAKISTANCODE_THIN_BODY_RE.search(normalized_body)
    operative = normalized_body[: footnote.start()] if footnote else normalized_body
    operative = operative.strip(" .,:;-")
    return len(operative) < _PAKISTANCODE_MIN_SECTION_BODY_CHARS


def _collect_pakistancode_thin_sections(sections: Any) -> list[Dict[str, Any]]:
    if not isinstance(sections, list):
        return []
    thin: list[Dict[str, Any]] = []
    for idx, section in enumerate(sections):
        if not isinstance(section, dict):
            thin.append({"index": idx, "section_number": None, "reason": "section_not_object"})
            continue
        section_number = str(section.get("section_number") or "").strip()
        section_text = str(section.get("section_text") or "")
        normalized_body = _normalize_section_body_for_quality(section_text)
        if _is_pakistancode_thin_section_body(normalized_body):
            thin.append(
                {
                    "index": idx,
                    "section_number": section_number or None,
                    "body_chars": len(normalized_body),
                    "snippet": normalized_body[:160],
                }
            )
    return thin


def _validated_mentions_payload(
    value: Any,
    *,
    field_name: str,
    required_keys: tuple[str, ...],
) -> tuple[list[Dict[str, Any]], Optional[str]]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], f"{field_name} must be a list"
    cleaned: list[Dict[str, Any]] = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            return [], f"{field_name} item must be an object"
        missing = [k for k in required_keys if not item.get(k)]
        if missing:
            return [], f"{field_name} item missing keys: {', '.join(missing)}"
        year = item.get("year")
        if year is not None:
            try:
                y = int(year)
            except (TypeError, ValueError):
                return [], f"{field_name} year must be an integer"
            if y < 1800 or y > 2035:
                return [], f"{field_name} year out of accepted range"
            item = {**item, "year": y}
        span = item.get("span")
        if span is not None:
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                return [], f"{field_name} span must be a two-item list"
            try:
                item = {**item, "span": [int(span[0]), int(span[1])]}
            except (TypeError, ValueError):
                return [], f"{field_name} span must contain integers"
        dedupe_key = (
            item.get("mention_type"),
            item.get("normalized"),
            item.get("section_number"),
            item.get("year"),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        cleaned.append(item)
    return cleaned, None


def _instrument_statute_shape(name: str) -> tuple[Optional[str], Optional[int]]:
    stype_match = re.search(r"(?i)\b(act|ordinance|rules|regulations|order|code|constitution)\b", name)
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", name)
    stype = stype_match.group(1).lower() if stype_match else None
    year = int(year_match.group(1)) if year_match else None
    return stype, year


async def _resolve_statute_link(
    db: AsyncSession,
    *,
    canonical_name: str,
    source_name: Optional[str],
    source_url: Optional[str],
    jurisdiction: Optional[str],
) -> Statute:
    statute = (await db.execute(select(Statute).where(Statute.name == canonical_name))).scalars().first()
    if statute is None:
        statute = (await db.execute(select(Statute).where(Statute.name.ilike(f"%{canonical_name[:80]}%")))).scalars().first()
    if statute is None:
        stype, year = _instrument_statute_shape(canonical_name)
        statute = Statute(
            name=canonical_name,
            short_name=canonical_name[:100],
            jurisdiction=jurisdiction or "Federal",
            statute_type=stype,
            year_enacted=year,
            source_name=source_name,
            source_url=source_url,
        )
        db.add(statute)
        await db.flush()
    return statute


RELATION_PATTERNS = (
    ("amended_by", re.compile(r"(?i)\b(?:as\s+)?amended\s+by\b"), {"instrument"}),
    ("superseded_by", re.compile(r"(?i)\b(?:is\s+)?superseded\s+by\b"), {"instrument"}),
    ("read_with", re.compile(r"(?i)\bread\s+with\b"), {"instrument", "statute"}),
)
AMENDMENT_OPERATION_RE = re.compile(
    r"(?i)\b(?:(?:shall|may)\s+be|(?:is|are)\s+hereby|hereby|shall\s+stand\s+)?\s*(?P<lemma>inserted|substituted|omitted|repealed)\b"
)
AMENDMENT_OPERATION_MAP = {
    "inserted": "insert",
    "substituted": "substitute",
    "omitted": "omit",
    "repealed": "repeal",
}
SECTION_TOKEN_PATTERN = r"\d+[A-Z]?(?:\s*[-/]\s*[0-9A-Z]+)?(?:\s*\(\s*[A-Z0-9]+\s*\))*"
SECTION_REFERENCE_RE = re.compile(
    rf"(?i)\b(?:section|sections|article|rule)\s+(?P<section>{SECTION_TOKEN_PATTERN})\b"
)
INSERT_BEFORE_TARGET_RE = re.compile(
    rf"(?i)\b(?:section|article|rule)\s+(?P<section>{SECTION_TOKEN_PATTERN})\s+(?:shall|may)\s+be\s+inserted\b"
)
INSERT_NAMELY_SECTION_RE = re.compile(
    rf"(?i)\bnamely\s*[:\-–—]*\s*(?:the\s+following\s+new\s+section\s+)?(?:section\s+)?(?P<section>{SECTION_TOKEN_PATTERN})\b"
)
MAX_AMENDMENT_SCAN_CHARS = 120_000
MAX_AMENDMENT_SECTION_MENTIONS = 800

SRO_SIGNATURE_RE = re.compile(
    r"(?i)\bS\.?\s*R\.?\s*O\.?\s*(?:No\.?\s*)?(?P<number>[A-Z0-9]+(?:\s*\([A-Z0-9]+\))?)\s*(?:/|of)\s*(?P<year>18\d{2}|19\d{2}|20\d{2})\b"
)
ACT_SIGNATURE_RE = re.compile(
    r"(?i)\bAct\s+No\.?\s*(?P<number>[IVXLCDM]+|\d{1,5}[A-Z]?)\s+of\s+(?P<year>18\d{2}|19\d{2}|20\d{2})\b"
)
ORD_SIGNATURE_RE = re.compile(
    r"(?i)\bOrdinance\s+No\.?\s*(?P<number>[IVXLCDM]+|\d{1,5}[A-Z]?)\s+of\s+(?P<year>18\d{2}|19\d{2}|20\d{2})\b"
)


def _valid_span(payload: Dict[str, Any]) -> Optional[tuple[int, int]]:
    span = payload.get("span")
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None
    try:
        start = int(span[0])
        end = int(span[1])
    except (TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def _norm_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _snippet(text: str, start: int, end: int, window: int = 60) -> str:
    return _norm_ws(text[max(0, start - window) : min(len(text), end + window)])


def _relation_window_end(text: str, start: int, max_chars: int = 260) -> int:
    segment = text[start : start + max_chars]
    boundary = re.search(r"[.;\n]", segment)
    if boundary:
        return start + boundary.start()
    return min(len(text), start + max_chars)


def _relation_window_start(text: str, end: int, max_chars: int = 220) -> int:
    begin = max(0, end - max_chars)
    segment = text[begin:end]
    boundary_index = max(segment.rfind("."), segment.rfind(";"), segment.rfind("\n"))
    if boundary_index >= 0:
        return begin + boundary_index + 1
    return begin


def _norm_section_key(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    # Fail closed on list/range references: one graph edge must resolve to one concrete section.
    if re.search(r"[,&]|\b(?:and|or|to)\b|[0-9A-Z]+\s*/\s*[0-9A-Z]+", raw, flags=re.I):
        return None
    cleaned = raw.replace("–", "-").replace("—", "-")
    cleaned = re.sub(r"(?i)^(?:section|sec\.?|s\.?|article|art\.?|rule|r\.?)\s+", "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned).rstrip(".,;:")
    # Normalize frequent portal variants (e.g. "Article 10-A" vs "10A").
    cleaned = re.sub(r"(?<=\d)-(?=[A-Z])", "", cleaned)
    if not cleaned or not re.search(r"\d", cleaned):
        return None
    return cleaned.upper()


def _collect_amendment_section_candidates(
    *,
    text: str,
    statute_mentions: list[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    bounded_text = text[:MAX_AMENDMENT_SCAN_CHARS]
    if not bounded_text:
        return []
    section_mentions: list[Dict[str, Any]] = []
    for mention in statute_mentions[:MAX_AMENDMENT_SECTION_MENTIONS]:
        if not isinstance(mention, dict):
            continue
        span = _valid_span(mention)
        if span is None or span[1] > len(bounded_text):
            continue
        section_key = _norm_section_key(mention.get("section_number"))
        if not section_key:
            continue
        raw = str(mention.get("raw") or "").strip()
        if not raw:
            continue
        section_mentions.append(
            {
                "span": span,
                "section_key": section_key,
                "raw": raw,
                "linked_statute_id": mention.get("linked_statute_id"),
                "canonical_statute_name": mention.get("canonical_statute_name"),
            }
        )
    linked_mentions = [
        row
        for row in section_mentions
        if row.get("linked_statute_id") or row.get("canonical_statute_name")
    ]

    def _nearest_linked_context(span_start: int) -> tuple[Optional[Any], Optional[Any]]:
        nearest = None
        nearest_distance = None
        for row in linked_mentions:
            distance = abs(int(row["span"][0]) - span_start)
            if nearest_distance is None or distance < nearest_distance:
                nearest_distance = distance
                nearest = row
        if nearest is not None and nearest_distance is not None and nearest_distance <= 260:
            return nearest.get("linked_statute_id"), nearest.get("canonical_statute_name")
        return None, None

    seen_section_spans = {(int(row["span"][0]), int(row["span"][1]), row["section_key"]) for row in section_mentions}

    def _append_synthetic_mention(*, span: tuple[int, int], section_value: str, raw: str) -> None:
        section_key = _norm_section_key(section_value)
        if not section_key:
            return
        dedupe = (int(span[0]), int(span[1]), section_key)
        if dedupe in seen_section_spans:
            return
        linked_statute_id, canonical_statute_name = _nearest_linked_context(span[0])
        section_mentions.append(
            {
                "span": span,
                "section_key": section_key,
                "raw": raw.strip(),
                "linked_statute_id": linked_statute_id,
                "canonical_statute_name": canonical_statute_name,
            }
        )
        seen_section_spans.add(dedupe)

    for match in SECTION_REFERENCE_RE.finditer(bounded_text):
        section_value = str(match.group("section") or "").strip()
        if not section_value:
            continue
        _append_synthetic_mention(
            span=(match.start("section"), match.end("section")),
            section_value=section_value,
            raw=str(match.group(0) or section_value),
        )
    for match in INSERT_NAMELY_SECTION_RE.finditer(bounded_text):
        section_value = str(match.group("section") or "").strip()
        if not section_value:
            continue
        _append_synthetic_mention(
            span=(match.start("section"), match.end("section")),
            section_value=section_value,
            raw=section_value,
        )

    if not section_mentions:
        return []
    section_mentions.sort(key=lambda row: row["span"][0])
    edges: list[Dict[str, Any]] = []
    seen = set()
    for match in AMENDMENT_OPERATION_RE.finditer(bounded_text):
        lemma = str(match.group("lemma") or "").lower()
        operation = AMENDMENT_OPERATION_MAP.get(lemma)
        if not operation:
            continue
        op_start, op_end = match.start(), match.end()
        window_start = _relation_window_start(bounded_text, op_start)
        window_end = _relation_window_end(bounded_text, op_end, max_chars=280)
        before = [
            row
            for row in section_mentions
            if window_start <= row["span"][0] and row["span"][1] <= op_start
        ]
        before.sort(key=lambda row: (op_start - row["span"][1], -(row["span"][1] - row["span"][0])))
        after = [
            row
            for row in section_mentions
            if op_end <= row["span"][0] and row["span"][1] <= window_end
        ]
        after.sort(key=lambda row: (row["span"][0] - op_end, row["span"][1] - row["span"][0]))

        def _is_anchor_reference(row: Dict[str, Any]) -> bool:
            prefix = bounded_text[max(0, row["span"][0] - 20) : row["span"][0]].lower()
            return bool(re.search(r"\b(after|before)\s+(?:sections?|article|rule)?\s*$", prefix))

        chosen = None
        if operation == "insert":
            explicit_section_key = None
            explicit_section_span = None
            before_slice_start = max(0, op_start - 220)
            before_slice_end = min(len(bounded_text), op_end + 20)
            explicit_before = INSERT_BEFORE_TARGET_RE.search(bounded_text[before_slice_start:before_slice_end])
            if explicit_before:
                explicit_section_key = _norm_section_key(explicit_before.group("section"))
                explicit_section_span = (
                    before_slice_start + explicit_before.start("section"),
                    before_slice_start + explicit_before.end("section"),
                )
            else:
                explicit_after = INSERT_NAMELY_SECTION_RE.search(
                    bounded_text[op_end : min(len(bounded_text), op_end + 260)]
                )
                if explicit_after:
                    explicit_section_key = _norm_section_key(explicit_after.group("section"))
                    explicit_section_span = (
                        op_end + explicit_after.start("section"),
                        op_end + explicit_after.end("section"),
                    )
            if explicit_section_key:
                matching_sections = [row for row in section_mentions if row["section_key"] == explicit_section_key]
                if matching_sections:
                    pivot = explicit_section_span[0] if explicit_section_span is not None else op_end
                    matching_sections.sort(
                        key=lambda row: (abs(row["span"][0] - pivot), abs(row["span"][1] - row["span"][0]))
                    )
                    chosen = matching_sections[0]
            if chosen is None:
                for row in after:
                    if (row["span"][0] - op_end) <= 180 and not _is_anchor_reference(row):
                        chosen = row
                        break
            if chosen is None:
                for row in before:
                    if (op_start - row["span"][1]) <= 180 and not _is_anchor_reference(row):
                        chosen = row
                        break

        if chosen is None and before and (op_start - before[0]["span"][1]) <= 180:
            chosen = before[0]
        if chosen is None:
            relaxed_after = [
                row
                for row in section_mentions
                if op_end <= row["span"][0] and (row["span"][0] - op_end) <= 120
            ]
            relaxed_after.sort(key=lambda row: (row["span"][0] - op_end, row["span"][1] - row["span"][0]))
            if relaxed_after:
                chosen = relaxed_after[0]
        if chosen is None:
            relaxed_before = [
                row
                for row in section_mentions
                if row["span"][1] <= op_start and (op_start - row["span"][1]) <= 180
            ]
            relaxed_before.sort(key=lambda row: (op_start - row["span"][1], -(row["span"][1] - row["span"][0])))
            if relaxed_before:
                chosen = relaxed_before[0]
        if chosen is None:
            continue
        span_start = min(chosen["span"][0], op_start)
        span_end = max(chosen["span"][1], op_end)
        row = {
            "amendment_operation": operation,
            "relation_phrase": _norm_ws(match.group(0).lower()),
            "target_mention_raw": chosen["raw"],
            "target_section_key": chosen["section_key"],
            "linked_statute_id": chosen.get("linked_statute_id"),
            "canonical_statute_name": chosen.get("canonical_statute_name"),
            "span_start": span_start,
            "span_end": span_end,
            "evidence_snippet": _snippet(bounded_text, span_start, span_end),
        }
        dedupe_key = (
            row["amendment_operation"],
            row["target_section_key"],
            row["span_start"],
            row["span_end"],
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        edges.append(row)
    return edges


def _instrument_reference_key(value: Optional[str]) -> str:
    if not value:
        return ""
    base = re.sub(r"[^a-z0-9]+", "", value.lower())
    for prefix in ("actno", "ordinanceno", "sro", "notification"):
        if base.startswith(prefix):
            return base[len(prefix) :]
    return base


def _instrument_signature(value: Optional[str]) -> Optional[tuple[str, str, int]]:
    if not value:
        return None
    for kind, pattern in (("sro", SRO_SIGNATURE_RE), ("act_no", ACT_SIGNATURE_RE), ("ordinance_no", ORD_SIGNATURE_RE)):
        match = pattern.search(value)
        if not match:
            continue
        try:
            year = int(match.group("year"))
        except (TypeError, ValueError):
            continue
        number = re.sub(r"\s+", "", match.group("number").upper())
        return kind, number, year
    return None


def _mention_signature(mention: Dict[str, Any]) -> Optional[tuple[str, str, int]]:
    mtype = str(mention.get("mention_type") or "").strip().lower()
    if mtype not in {"sro", "act_no", "ordinance_no"}:
        mtype = ""
    try:
        year = int(mention.get("year"))
    except (TypeError, ValueError):
        year = None
    number = str(mention.get("number") or "").strip()
    if mtype and year and number:
        return mtype, re.sub(r"\s+", "", number.upper()), year
    return _instrument_signature(str(mention.get("normalized") or mention.get("raw") or ""))


def _collect_relation_candidates(
    *,
    text: str,
    citation_mentions: list[Dict[str, Any]],
    statute_mentions: list[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    targets: list[Dict[str, Any]] = []
    for mention in citation_mentions:
        if not isinstance(mention, dict):
            continue
        span = _valid_span(mention)
        normalized = str(mention.get("normalized") or "").strip()
        raw = str(mention.get("raw") or "").strip()
        if span is None or not normalized or not raw:
            continue
        targets.append(
            {
                "target_kind": "instrument",
                "span": span,
                "raw": raw,
                "normalized": normalized,
                "mention_type": mention.get("mention_type"),
                "number": mention.get("number"),
                "year": mention.get("year"),
            }
        )
    for mention in statute_mentions:
        if not isinstance(mention, dict):
            continue
        span = _valid_span(mention)
        normalized = str(mention.get("normalized") or mention.get("canonical_statute_name") or "").strip()
        raw = str(mention.get("raw") or "").strip()
        if span is None or not normalized or not raw:
            continue
        targets.append(
            {
                "target_kind": "statute",
                "span": span,
                "raw": raw,
                "normalized": normalized,
                "linked_statute_id": mention.get("linked_statute_id"),
                "canonical_statute_name": mention.get("canonical_statute_name"),
            }
        )
    targets.sort(key=lambda row: row["span"][0])
    edges: list[Dict[str, Any]] = []
    seen = set()
    for relation_type, pattern, allowed_targets in RELATION_PATTERNS:
        for match in pattern.finditer(text):
            end_limit = _relation_window_end(text, match.end())
            for target in targets:
                start, end = target["span"]
                if target["target_kind"] not in allowed_targets:
                    continue
                if end <= match.end() or start > end_limit:
                    continue
                if target["target_kind"] == "instrument" and start < match.end():
                    continue
                row = {
                    "relation_type": relation_type,
                    "relation_phrase": _norm_ws(match.group(0).lower()),
                    "target_kind": target["target_kind"],
                    "target_mention_raw": target["raw"],
                    "target_mention_normalized": target["normalized"],
                    "target_mention_type": target.get("mention_type"),
                    "target_number": target.get("number"),
                    "target_year": target.get("year"),
                    "linked_statute_id": target.get("linked_statute_id"),
                    "canonical_statute_name": target.get("canonical_statute_name"),
                    "span_start": match.start(),
                    "span_end": end,
                    "evidence_snippet": _snippet(text, match.start(), end),
                }
                dedupe_key = (
                    row["relation_type"],
                    row["target_kind"],
                    row["target_mention_normalized"],
                    row["span_start"],
                    row["span_end"],
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                edges.append(row)
    return edges


def _instrument_row_keys(row: Instrument) -> set[str]:
    keys: set[str] = set()
    if row.number:
        key = _instrument_reference_key(row.number)
        if key:
            keys.add(key)
    if keys:
        return keys
    first_mention = next((m for m in (row.citation_mentions or []) if isinstance(m, dict)), None)
    if first_mention:
        for field in ("normalized", "raw"):
            key = _instrument_reference_key(str(first_mention.get(field) or ""))
            if key:
                keys.add(key)
    return keys


def _instrument_row_signatures(row: Instrument) -> set[tuple[str, str, int]]:
    signatures: set[tuple[str, str, int]] = set()
    direct = _instrument_signature(row.number)
    if direct:
        signatures.add(direct)
        return signatures
    first_mention = next((m for m in (row.citation_mentions or []) if isinstance(m, dict)), None)
    if first_mention:
        sig = _mention_signature(first_mention)
        if sig:
            signatures.add(sig)
    return signatures


async def _resolve_target_instrument(db: AsyncSession, *, source_instrument_id, target: Dict[str, Any]) -> Optional[Instrument]:
    mention_key = _instrument_reference_key(target.get("target_mention_normalized"))
    mention_signature = _mention_signature(
        {
            "mention_type": target.get("target_mention_type"),
            "number": target.get("target_number"),
            "year": target.get("target_year"),
            "normalized": target.get("target_mention_normalized"),
            "raw": target.get("target_mention_raw"),
        }
    )
    candidates = []
    for row in (await db.execute(select(Instrument).where(Instrument.id != source_instrument_id))).scalars().all():
        sig_match = mention_signature and mention_signature in _instrument_row_signatures(row)
        key_match = bool(mention_key and mention_key in _instrument_row_keys(row))
        if sig_match or key_match:
            candidates.append(row)
    if len(candidates) == 1:
        return candidates[0]
    return None


async def _resolve_target_statute_id(db: AsyncSession, target: Dict[str, Any]):
    linked_id = target.get("linked_statute_id")
    if linked_id:
        try:
            parsed = UUID(str(linked_id))
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            statute = (await db.execute(select(Statute).where(Statute.id == parsed))).scalars().first()
            if statute is not None:
                return statute.id
    canonical_name = str(target.get("canonical_statute_name") or "").strip()
    if canonical_name:
        statute = (await db.execute(select(Statute).where(Statute.name == canonical_name))).scalars().first()
        if statute is not None:
            return statute.id
    return None


async def _resolve_target_statute_section_id(
    db: AsyncSession,
    *,
    target_statute_id,
    target_section_key: str,
):
    section = (
        await db.execute(
            select(StatuteSection).where(
                StatuteSection.statute_id == target_statute_id,
                StatuteSection.section_number == target_section_key,
            )
        )
    ).scalars().first()
    if section is not None:
        return section.id
    # Relaxed fallback: spacing/punctuation differences between extractor and stored section.
    for row in (await db.execute(select(StatuteSection).where(StatuteSection.statute_id == target_statute_id))).scalars().all():
        if _norm_section_key(row.section_number) == target_section_key:
            return row.id
    return None


async def _sync_instrument_section_relation_edges(db: AsyncSession, inst: Instrument) -> None:
    text = inst.full_text or ""
    statute_mentions = inst.statute_mentions if isinstance(inst.statute_mentions, list) else []
    candidates = _collect_amendment_section_candidates(text=text, statute_mentions=statute_mentions)
    await db.execute(delete(InstrumentSectionRelation).where(InstrumentSectionRelation.source_instrument_id == inst.id))
    seen = set()
    for edge in candidates:
        target_statute_id = await _resolve_target_statute_id(db, edge)
        if (
            target_statute_id is None
            and inst.affected_statute_id is not None
            and not edge.get("linked_statute_id")
            and not edge.get("canonical_statute_name")
        ):
            target_statute_id = inst.affected_statute_id
        if target_statute_id is None:
            continue  # fail-closed: unresolved statute target.
        target_section_key = str(edge.get("target_section_key") or "").strip().upper()
        if not target_section_key:
            continue
        dedupe_key = (
            str(edge.get("amendment_operation") or ""),
            str(target_statute_id),
            target_section_key,
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        target_statute_section_id = await _resolve_target_statute_section_id(
            db,
            target_statute_id=target_statute_id,
            target_section_key=target_section_key,
        )
        db.add(
            InstrumentSectionRelation(
                source_instrument_id=inst.id,
                target_statute_id=target_statute_id,
                target_statute_section_id=target_statute_section_id,
                target_section_key=target_section_key[:120],
                amendment_operation=str(edge["amendment_operation"])[:20],
                relation_phrase=str(edge["relation_phrase"])[:120],
                target_mention_raw=str(edge["target_mention_raw"])[:300],
                span_start=int(edge["span_start"]),
                span_end=int(edge["span_end"]),
                evidence_snippet=str(edge["evidence_snippet"])[:500],
                source_provenance_id=inst.source_provenance_id,
                source_url=inst.source_url,
            )
        )
    await db.flush()


async def _sync_instrument_relation_edges(db: AsyncSession, inst: Instrument) -> None:
    text = inst.full_text or ""
    citation_mentions = inst.citation_mentions if isinstance(inst.citation_mentions, list) else []
    statute_mentions = inst.statute_mentions if isinstance(inst.statute_mentions, list) else []
    candidates = _collect_relation_candidates(
        text=text,
        citation_mentions=citation_mentions,
        statute_mentions=statute_mentions,
    )
    await db.execute(delete(InstrumentRelation).where(InstrumentRelation.source_instrument_id == inst.id))
    for edge in candidates:
        target_instrument_id = None
        target_statute_id = None
        if edge["target_kind"] == "instrument":
            target_inst = await _resolve_target_instrument(db, source_instrument_id=inst.id, target=edge)
            if target_inst is None or target_inst.id == inst.id:
                continue  # fail-closed: unresolved/ambiguous target instrument
            target_instrument_id = target_inst.id
            target_statute_id = target_inst.affected_statute_id
        else:
            target_statute_id = await _resolve_target_statute_id(db, edge)
            if target_statute_id is None:
                continue  # fail-closed: statute endpoint could not be canonically resolved
        db.add(
            InstrumentRelation(
                source_instrument_id=inst.id,
                target_instrument_id=target_instrument_id,
                target_statute_id=target_statute_id,
                relation_type=str(edge["relation_type"])[:40],
                relation_phrase=str(edge["relation_phrase"])[:120],
                target_mention_raw=str(edge["target_mention_raw"])[:300],
                target_mention_normalized=str(edge["target_mention_normalized"])[:300],
                span_start=int(edge["span_start"]),
                span_end=int(edge["span_end"]),
                evidence_snippet=str(edge["evidence_snippet"])[:500],
                source_provenance_id=inst.source_provenance_id,
                source_url=inst.source_url,
            )
        )
    await _sync_instrument_section_relation_edges(db, inst)
    await db.flush()


# --------------------------------------------------------------------------- relation reconciliation
def _positive_int(value: Any, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


async def reconcile_instrument_relations(
    *,
    limit: Optional[int] = None,
    lookback_hours: Optional[int] = None,
) -> Dict[str, int]:
    effective_limit = _positive_int(limit, fallback=settings.INSTRUMENT_RELATION_RECONCILE_BATCH_SIZE)
    effective_lookback = _positive_int(lookback_hours, fallback=settings.INSTRUMENT_RELATION_RECONCILE_WINDOW_HOURS)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=effective_lookback)
    counts = {
        "scanned": 0,
        "processed": 0,
        "failed": 0,
        "edges_before": 0,
        "edges_after": 0,
        "edges_added": 0,
        "edges_removed": 0,
        "section_edges_before": 0,
        "section_edges_after": 0,
        "section_edges_added": 0,
        "section_edges_removed": 0,
    }
    offset_key = "instrument_relation_reconcile_offset"
    async with SessionLocal() as db:
        base_recent_filter = and_(
            Instrument.created_at >= cutoff,
            or_(Instrument.citation_mentions.isnot(None), Instrument.statute_mentions.isnot(None)),
        )
        # Include unresolved section-link edges regardless of instrument age so late-arriving
        # statute sections can backfill target_statute_section_id on the next reconcile pass.
        unresolved_section_backfill_filter = exists(
            select(InstrumentSectionRelation.id).where(
                InstrumentSectionRelation.source_instrument_id == Instrument.id,
                InstrumentSectionRelation.target_statute_section_id.is_(None),
            )
        )
        filters = (or_(base_recent_filter, unresolved_section_backfill_filter),)
        total = (
            await db.execute(
                select(func.count()).select_from(Instrument).where(*filters)
            )
        ).scalar() or 0
        instrument_ids = []
        if total > 0:
            cursor = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == offset_key))).scalars().first()
            if cursor is None:
                cursor = CorpusMetadata(key=offset_key, value="0")
                db.add(cursor)
                await db.flush()
            try:
                offset = int(cursor.value)
            except (TypeError, ValueError):
                offset = 0
            offset = max(offset, 0) % int(total)
            base_query = (
                select(Instrument.id)
                .where(*filters)
                .order_by(Instrument.created_at.desc(), Instrument.id.desc())
            )
            instrument_ids = (await db.execute(base_query.offset(offset).limit(effective_limit))).scalars().all()
            if len(instrument_ids) < effective_limit and total > len(instrument_ids):
                wrap_ids = (await db.execute(base_query.limit(effective_limit - len(instrument_ids)))).scalars().all()
                instrument_ids.extend(wrap_ids)
            cursor.value = str((offset + len(instrument_ids)) % int(total))
        counts["scanned"] = len(instrument_ids)
        for inst_id in instrument_ids:
            inst = (await db.execute(select(Instrument).where(Instrument.id == inst_id))).scalars().first()
            if inst is None:
                continue
            try:
                before = (
                    await db.execute(
                        select(func.count())
                        .select_from(InstrumentRelation)
                        .where(InstrumentRelation.source_instrument_id == inst.id)
                    )
                ).scalar() or 0
                section_before = (
                    await db.execute(
                        select(func.count())
                        .select_from(InstrumentSectionRelation)
                        .where(InstrumentSectionRelation.source_instrument_id == inst.id)
                    )
                ).scalar() or 0
                await _sync_instrument_relation_edges(db, inst)
                after = (
                    await db.execute(
                        select(func.count())
                        .select_from(InstrumentRelation)
                        .where(InstrumentRelation.source_instrument_id == inst.id)
                    )
                ).scalar() or 0
                section_after = (
                    await db.execute(
                        select(func.count())
                        .select_from(InstrumentSectionRelation)
                        .where(InstrumentSectionRelation.source_instrument_id == inst.id)
                    )
                ).scalar() or 0
                counts["processed"] += 1
                counts["edges_before"] += int(before)
                counts["edges_after"] += int(after)
                if after >= before:
                    counts["edges_added"] += int(after - before)
                else:
                    counts["edges_removed"] += int(before - after)
                counts["section_edges_before"] += int(section_before)
                counts["section_edges_after"] += int(section_after)
                if section_after >= section_before:
                    counts["section_edges_added"] += int(section_after - section_before)
                else:
                    counts["section_edges_removed"] += int(section_before - section_after)
                await db.commit()
            except Exception:
                await db.rollback()
                counts["failed"] += 1
                logger.exception("relation reconcile failed for instrument %s", inst_id)
        await db.commit()
    return counts


async def _unresolved_relation_residual_counts(db: AsyncSession) -> Dict[str, int]:
    judgment_unresolved = (
        await db.execute(
            select(func.count())
            .select_from(JudgmentCitationRelation)
            .where(JudgmentCitationRelation.resolution_status == "unresolved")
        )
    ).scalar() or 0
    statute_section_unresolved = (
        await db.execute(
            select(func.count())
            .select_from(InstrumentSectionRelation)
            .where(InstrumentSectionRelation.target_statute_section_id.is_(None))
        )
    ).scalar() or 0
    return {
        "judgment_citation_unresolved": int(judgment_unresolved),
        "instrument_section_unresolved": int(statute_section_unresolved),
        "total_unresolved": int(judgment_unresolved) + int(statute_section_unresolved),
    }


def _residual_bucket_label(value: Optional[str]) -> str:
    normalized = str(value or "").strip()
    return normalized or "(unknown)"


async def _unresolved_relation_residual_breakdown(db: AsyncSession, *, top_n: int) -> Dict[str, Any]:
    judgment_by_source_rows = (
        await db.execute(
            select(Judgment.source_name, func.count())
            .select_from(JudgmentCitationRelation)
            .join(Judgment, Judgment.id == JudgmentCitationRelation.source_judgment_id)
            .where(JudgmentCitationRelation.resolution_status == "unresolved")
            .group_by(Judgment.source_name)
            .order_by(func.count().desc(), Judgment.source_name.asc().nulls_last())
            .limit(top_n)
        )
    ).all()
    judgment_by_key_rows = (
        await db.execute(
            select(JudgmentCitationRelation.target_citation_key, func.count())
            .select_from(JudgmentCitationRelation)
            .where(JudgmentCitationRelation.resolution_status == "unresolved")
            .group_by(JudgmentCitationRelation.target_citation_key)
            .order_by(func.count().desc(), JudgmentCitationRelation.target_citation_key.asc().nulls_last())
            .limit(top_n)
        )
    ).all()
    instrument_by_source_rows = (
        await db.execute(
            select(Instrument.source_name, func.count())
            .select_from(InstrumentSectionRelation)
            .join(Instrument, Instrument.id == InstrumentSectionRelation.source_instrument_id)
            .where(InstrumentSectionRelation.target_statute_section_id.is_(None))
            .group_by(Instrument.source_name)
            .order_by(func.count().desc(), Instrument.source_name.asc().nulls_last())
            .limit(top_n)
        )
    ).all()
    instrument_by_key_rows = (
        await db.execute(
            select(InstrumentSectionRelation.target_section_key, func.count())
            .select_from(InstrumentSectionRelation)
            .where(InstrumentSectionRelation.target_statute_section_id.is_(None))
            .group_by(InstrumentSectionRelation.target_section_key)
            .order_by(func.count().desc(), InstrumentSectionRelation.target_section_key.asc().nulls_last())
            .limit(top_n)
        )
    ).all()
    return {
        "judgment_citation_unresolved": {
            "by_source_name": [
                {"source_name": _residual_bucket_label(source_name), "count": int(count)}
                for source_name, count in judgment_by_source_rows
            ],
            "by_target_citation_key": [
                {"target_citation_key": _residual_bucket_label(citation_key), "count": int(count)}
                for citation_key, count in judgment_by_key_rows
            ],
        },
        "instrument_section_unresolved": {
            "by_source_name": [
                {"source_name": _residual_bucket_label(source_name), "count": int(count)}
                for source_name, count in instrument_by_source_rows
            ],
            "by_target_section_key": [
                {"target_section_key": _residual_bucket_label(section_key), "count": int(count)}
                for section_key, count in instrument_by_key_rows
            ],
        },
    }


async def reconcile_citation_statute_residual_smoke(
    *,
    lookback_hours: Optional[int] = None,
    instrument_limit: Optional[int] = None,
    judgment_batch_size: Optional[int] = None,
    run_reconcile: bool = False,
    fail_on_increase: bool = True,
    include_unresolved_breakdown: bool = False,
    unresolved_breakdown_top_n: Optional[int] = None,
) -> Dict[str, Any]:
    instrument_batch = _positive_int(
        instrument_limit,
        fallback=settings.INSTRUMENT_RELATION_RECONCILE_BATCH_SIZE,
    )
    instrument_lookback = _positive_int(
        lookback_hours,
        fallback=settings.INSTRUMENT_RELATION_RECONCILE_WINDOW_HOURS,
    )
    judgment_batch = _positive_int(
        judgment_batch_size,
        fallback=settings.JUDGMENT_CITATION_RECONCILE_BATCH_SIZE,
    )
    judgment_lookback = _positive_int(
        lookback_hours,
        fallback=settings.JUDGMENT_CITATION_RECONCILE_LOOKBACK_HOURS,
    )
    breakdown_top_n = _positive_int(unresolved_breakdown_top_n, fallback=5)
    breakdown: Optional[Dict[str, Any]] = None
    async with SessionLocal() as db:
        before = await _unresolved_relation_residual_counts(db)
        if include_unresolved_breakdown:
            breakdown = await _unresolved_relation_residual_breakdown(db, top_n=breakdown_top_n)

    result: Dict[str, Any] = {
        "mode": "apply" if run_reconcile else "dry_run",
        "before": before,
        "after": dict(before),
        "delta": {
            "judgment_citation_unresolved_reduced": 0,
            "instrument_section_unresolved_reduced": 0,
            "total_unresolved_reduced": 0,
        },
        "runs": {},
        "window": {
            "instrument_lookback_hours": instrument_lookback,
            "judgment_lookback_hours": judgment_lookback,
            "instrument_limit": instrument_batch,
            "judgment_batch_size": judgment_batch,
        },
    }
    if run_reconcile:
        instrument_counts = await reconcile_instrument_relations(
            limit=instrument_batch,
            lookback_hours=instrument_lookback,
        )
        from scraper.tasks.treatment import reconcile_judgment_citation_relations

        judgment_counts = await reconcile_judgment_citation_relations(
            lookback_hours=judgment_lookback,
            batch_size=judgment_batch,
        )
        async with SessionLocal() as db:
            after = await _unresolved_relation_residual_counts(db)
        result["runs"] = {
            "reconcile_instrument_relations": instrument_counts,
            "reconcile_judgment_citation_relations": judgment_counts,
        }
        result["after"] = after
        result["delta"] = {
            "judgment_citation_unresolved_reduced": int(before["judgment_citation_unresolved"]) - int(after["judgment_citation_unresolved"]),
            "instrument_section_unresolved_reduced": int(before["instrument_section_unresolved"]) - int(after["instrument_section_unresolved"]),
            "total_unresolved_reduced": int(before["total_unresolved"]) - int(after["total_unresolved"]),
        }
        if include_unresolved_breakdown:
            async with SessionLocal() as db:
                breakdown = await _unresolved_relation_residual_breakdown(db, top_n=breakdown_top_n)
        regressions = {
            key: {"before": int(before[key]), "after": int(after[key])}
            for key in ("judgment_citation_unresolved", "instrument_section_unresolved", "total_unresolved")
            if int(after[key]) > int(before[key])
        }
        if fail_on_increase and regressions:
            raise RuntimeError(f"residual smoke failed: unresolved counts increased {regressions}")
    if include_unresolved_breakdown:
        result["unresolved_breakdown"] = {"top_n": breakdown_top_n, **(breakdown or {})}
    return result


async def promote_statute_staging(db: AsyncSession, st: StatutesStaging, *, force: bool = False) -> str:
    data = st.reconciled_json or {}
    if st.status == "quarantined" and not force:
        await _quarantine(db, st, st.quarantine_reason or "below confidence threshold", st.kind)
        return "quarantined"
    prov = (await db.execute(select(SourceProvenance).where(SourceProvenance.id == st.provenance_id))).scalars().first()
    if st.kind == "instrument":
        text = st.raw_text or data.get("full_text") or ""
        h = sha256_text(" ".join(text.split()))
        existing = (await db.execute(select(Instrument).where(Instrument.full_text_hash == h))).scalars().first()
        if existing is not None:
            st.status = "duplicate"
            st.promoted_to_id = existing.id
            await db.flush()
            return "duplicate"
        if not data.get("type"):
            await _quarantine(db, st, "instrument type unknown", "instrument")
            return "quarantined"
        citation_mentions, mention_error = _validated_mentions_payload(
            data.get("citation_mentions"),
            field_name="citation_mentions",
            required_keys=("raw", "normalized", "mention_type"),
        )
        if mention_error:
            await _quarantine(db, st, mention_error, "instrument")
            return "quarantined"
        statute_mentions, statute_mention_error = _validated_mentions_payload(
            data.get("statute_mentions"),
            field_name="statute_mentions",
            required_keys=("raw", "normalized"),
        )
        if statute_mention_error:
            await _quarantine(db, st, statute_mention_error, "instrument")
            return "quarantined"
        if data.get("affected_sections") is not None and not isinstance(data.get("affected_sections"), list):
            await _quarantine(db, st, "affected_sections must be a list", "instrument")
            return "quarantined"

        def _normalized_section_identifier(value: Any) -> Optional[str]:
            raw = str(value or "").strip()
            if not raw:
                return None
            return _norm_section(raw)

        aff = None
        linked_statute_mentions: list[Dict[str, Any]] = []
        collected_sections: list[str] = []
        for section_value in data.get("affected_sections") or []:
            normalized_section = _normalized_section_identifier(section_value)
            if normalized_section:
                collected_sections.append(normalized_section)
        if data.get("affected_statute"):
            canonical = canonicalise_statute_name(str(data["affected_statute"])) or str(data["affected_statute"]).strip()
            aff = await _resolve_statute_link(
                db,
                canonical_name=canonical,
                source_name=st.source_name,
                source_url=st.source_url,
                jurisdiction=data.get("jurisdiction"),
            )
        for mention in statute_mentions:
            raw_name = str(mention.get("canonical_statute_name") or mention.get("statute_name") or "").strip()
            if not raw_name:
                # Keep section-level mention spans when the instrument already has a resolved
                # affected statute; this enables bounded amendment-op edge extraction later.
                if aff is None or not mention.get("section_number"):
                    continue
                row = dict(mention)
                row["canonical_statute_name"] = aff.name
                row["linked_statute_id"] = str(aff.id)
                linked_statute_mentions.append(row)
                section_number = _normalized_section_identifier(row.get("section_number"))
                if section_number:
                    collected_sections.append(section_number)
                continue
            canonical = canonicalise_statute_name(raw_name, mention.get("year")) or raw_name
            linked = await _resolve_statute_link(
                db,
                canonical_name=canonical,
                source_name=st.source_name,
                source_url=st.source_url,
                jurisdiction=data.get("jurisdiction"),
            )
            row = dict(mention)
            row["canonical_statute_name"] = canonical
            row["linked_statute_id"] = str(linked.id)
            linked_statute_mentions.append(row)
            section_number = _normalized_section_identifier(row.get("section_number"))
            if section_number:
                collected_sections.append(section_number)
            if aff is None:
                aff = linked
        collected_sections = [s for s in dict.fromkeys(collected_sections) if s]
        dd = data.get("date")
        inst = Instrument(
            type=str(data["type"])[:40],
            number=data.get("number"),
            date=date.fromisoformat(dd) if isinstance(dd, str) and dd else None,
            title=data.get("title"),
            gazette_ref=data.get("gazette_ref"),
            full_text=text,
            full_text_hash=h,
            affected_statute_id=aff.id if aff else None,
            affected_statute_name=aff.name if aff else (canonicalise_statute_name(data.get("affected_statute")) if data.get("affected_statute") else None),
            affected_sections=collected_sections,
            citation_mentions=citation_mentions,
            statute_mentions=linked_statute_mentions,
            jurisdiction=data.get("jurisdiction"),
            source_name=st.source_name,
            source_url=st.source_url,
            source_provenance_id=st.provenance_id,
        )
        db.add(inst)
        await db.flush()
        await _sync_instrument_relation_edges(db, inst)
        if prov is not None:
            prov.promoted_table, prov.promoted_id = "instrument", inst.id
        st.status, st.promoted_to_id = "promoted", inst.id
        await db.flush()
        return "promoted"
    # statute
    name = (data.get("statute_name") or "").strip()
    sections = data.get("sections") or []
    evidence = data.get("field_evidence") or {}
    if st.source_name == "PakistanCode":
        if name and is_short_title_clause(name):
            await _quarantine(
                db,
                st,
                "pakistancode_bad_name: statute_name matched short-title clause",
                "statute",
                {
                    "reason_code": "pakistancode_bad_name",
                    "statute_name": name[:280],
                    "validation_errors": st.validation_errors,
                },
            )
            return "quarantined"
        if name and looks_like_fragment_name(name):
            await _quarantine(
                db,
                st,
                "pakistancode_fragment_name: statute_name looks like a URL or filename fragment",
                "statute",
                {
                    "reason_code": "pakistancode_fragment_name",
                    "statute_name": name[:280],
                    "validation_errors": st.validation_errors,
                },
            )
            return "quarantined"
        if evidence.get("listed_title_absent"):
            await _quarantine(
                db,
                st,
                "pakistancode_listed_title_absent: listing title is not supported by document text",
                "statute",
                {
                    "reason_code": "pakistancode_listed_title_absent",
                    "statute_name": name[:280],
                    "listed_title": str(evidence.get("listed_title_absent"))[:280],
                    "validation_errors": st.validation_errors,
                },
            )
            return "quarantined"
        thin_sections = _collect_pakistancode_thin_sections(sections)
        if thin_sections:
            await _quarantine(
                db,
                st,
                "pakistancode_thin_section_body: one or more sections were too thin",
                "statute",
                {
                    "reason_code": "pakistancode_thin_section_body",
                    "min_chars": _PAKISTANCODE_MIN_SECTION_BODY_CHARS,
                    "thin_sections": thin_sections[:25],
                    "validation_errors": st.validation_errors,
                },
            )
            return "quarantined"
    if not name or name == "Unknown Statute" or not sections:
        await _quarantine(db, st, "statute name or sections missing", "statute")
        return "quarantined"
    statute = (await db.execute(select(Statute).where(Statute.name == name))).scalars().first()
    created_new_statute = statute is None
    if created_new_statute:
        statute = Statute(name=name, short_name=data.get("short_name"), jurisdiction=data.get("jurisdiction") or "Federal", statute_type=data.get("statute_type"), year_enacted=data.get("year_enacted"), source_name=st.source_name, source_url=st.source_url)
        db.add(statute)
        await db.flush()
    new_versions = 0
    order = 0
    for s in sections:
        num = _norm_section(s.get("section_number"))
        txt = (s.get("section_text") or "").strip()
        if not num or not txt:
            continue
        order += 1
        sec = (await db.execute(select(StatuteSection).where(StatuteSection.statute_id == statute.id, StatuteSection.section_number == num))).scalars().first()
        if sec is None:
            sec = StatuteSection(statute_id=statute.id, section_number=num, section_title=(s.get("section_title") or None), chapter=s.get("chapter"), sort_key=order)
            db.add(sec)
            await db.flush()
            db.add(EmbeddingQueue(record_id=sec.id, table_name="statute_section", access_method=st.access_method, embedding_model=settings.EMBEDDING_MODEL, embedding_dimensions=settings.EMBEDDING_DIM))
        th = sha256_text(" ".join(txt.split()))
        ver = (await db.execute(select(StatuteSectionVersion).where(StatuteSectionVersion.section_id == sec.id, StatuteSectionVersion.text_hash == th))).scalars().first()
        if ver is None:
            count = (await db.execute(select(func.count()).select_from(StatuteSectionVersion).where(StatuteSectionVersion.section_id == sec.id))).scalar() or 0
            ef = s.get("effective_from")
            et = s.get("effective_to")
            ver = StatuteSectionVersion(
                section_id=sec.id,
                version_no=count + 1,
                section_text=txt,
                text_hash=th,
                effective_from=date.fromisoformat(ef) if isinstance(ef, str) and ef else None,
                effective_to=date.fromisoformat(et) if isinstance(et, str) and et else None,
                amending_instrument_text=s.get("amending_instrument"),
                version_confidence=0.9 if (ef or s.get("amending_instrument")) else 0.5,
                source_provenance_id=st.provenance_id,
            )
            db.add(ver)
            await db.flush()
            new_versions += 1
            if count > 0:
                prev = (await db.execute(select(StatuteSectionVersion).where(StatuteSectionVersion.section_id == sec.id, StatuteSectionVersion.id != ver.id).order_by(StatuteSectionVersion.version_no.desc()))).scalars().first()
                if prev is not None and prev.effective_to is None and ver.effective_from is not None:
                    prev.effective_to = ver.effective_from
        sec.current_version_id = ver.id
    if order == 0:
        if created_new_statute:
            await db.delete(statute)
            await db.flush()
            await _quarantine(
                db,
                st,
                "empty_statute_sections: no parseable section bodies",
                "statute",
                {
                    "reason_code": "empty_statute_sections",
                    "statute_name": name[:280],
                    "validation_errors": st.validation_errors,
                },
            )
            return "quarantined"
        st.status = "duplicate"
        st.promoted_to_id = statute.id
        await db.flush()
        return "duplicate"
    if prov is not None:
        prov.promoted_table, prov.promoted_id = "statute", statute.id
    st.status = "promoted" if new_versions else "duplicate"
    st.promoted_to_id = statute.id
    await db.flush()
    return st.status


# --------------------------------------------------------------------------- batch entry points
async def promote_staging_records(limit: int = 200) -> Dict[str, int]:
    """Promote every `extracted` staging row (bounded per pass) and make sure every `quarantined` row
    has a review-queue entry.

    The two populations are selected separately. A quarantined row keeps status=quarantined and
    promoted_to_id=NULL forever, so one query over both statuses ordered by created_at fills the
    batch with old quarantined rows once enough of them accumulate and newer extracted rows are
    never reached: promotion silently stops while harvesting continues."""
    counts = {"promoted": 0, "duplicate": 0, "quarantined": 0, "statutes_promoted": 0, "statutes_duplicate": 0, "statutes_quarantined": 0}
    async with SessionLocal() as db:
        unqueued = (
            await db.execute(
                select(ScraperStaging)
                .where(
                    ScraperStaging.status == "quarantined",
                    ScraperStaging.promoted_to_id.is_(None),
                    ~exists(select(QuarantineQueue.id).where(QuarantineQueue.staging_id == ScraperStaging.id)),
                )
                .order_by(ScraperStaging.created_at)
                .limit(limit)
            )
        ).scalars().all()
        for st in unqueued:
            await _quarantine(db, st, st.quarantine_reason or "below threshold", "judgment")
            counts["quarantined"] += 1
        await db.commit()
        rows = (await db.execute(select(ScraperStaging).where(ScraperStaging.status == "extracted", ScraperStaging.promoted_to_id.is_(None)).order_by(ScraperStaging.created_at).limit(limit))).scalars().all()
        for st in rows:
            try:
                counts[await promote_judgment_staging(db, st)] += 1
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.exception("promotion failed for staging %s", st.id)
                st2 = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == st.id))).scalars().first()
                if st2 is not None:
                    await _quarantine(db, st2, f"promotion error: {exc}"[:1000], "judgment")
                    await db.commit()
                    counts["quarantined"] += 1
        s_unqueued = (
            await db.execute(
                select(StatutesStaging)
                .where(
                    StatutesStaging.status == "quarantined",
                    StatutesStaging.promoted_to_id.is_(None),
                    ~exists(select(QuarantineQueue.id).where(QuarantineQueue.statutes_staging_id == StatutesStaging.id)),
                )
                .order_by(StatutesStaging.created_at)
                .limit(limit)
            )
        ).scalars().all()
        for st in s_unqueued:
            await _quarantine(db, st, st.quarantine_reason or "below threshold", st.kind)
            counts["statutes_quarantined"] += 1
        await db.commit()
        srows = (await db.execute(select(StatutesStaging).where(StatutesStaging.status == "extracted", StatutesStaging.promoted_to_id.is_(None)).order_by(StatutesStaging.created_at).limit(limit))).scalars().all()
        for st in srows:
            try:
                r = await promote_statute_staging(db, st)
                counts[f"statutes_{r}"] += 1
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.exception("statute promotion failed for staging %s", st.id)
                st2 = (await db.execute(select(StatutesStaging).where(StatutesStaging.id == st.id))).scalars().first()
                if st2 is not None:
                    await _quarantine(db, st2, f"promotion error: {exc}"[:1000], st2.kind)
                    await db.commit()
                    counts["statutes_quarantined"] += 1
        await db.commit()
    return counts


async def resolve_quarantine(db: AsyncSession, item: QuarantineQueue, *, reviewer: str, resolution: str, notes: Optional[str] = None, corrected: Optional[Dict[str, Any]] = None) -> str:
    """Review-queue action: promote (with optional corrected fields) or reject."""
    item.reviewed = True
    item.reviewed_by = reviewer
    item.reviewed_at = datetime.now(timezone.utc)
    item.resolution = resolution
    item.resolution_notes = notes
    if resolution != "promoted":
        if item.staging_id:
            st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == item.staging_id))).scalars().first()
            if st is not None:
                st.status = "failed"
        if item.statutes_staging_id:
            st = (await db.execute(select(StatutesStaging).where(StatutesStaging.id == item.statutes_staging_id))).scalars().first()
            if st is not None:
                st.status = "failed"
        await db.flush()
        return "rejected"
    if item.staging_id:
        st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == item.staging_id))).scalars().first()
        if st is None:
            return "missing"
        if corrected:
            data = dict(st.reconciled_json or {})
            # A reviewer may only correct values that are present in the raw text.
            raw = st.raw_text or ""
            for k, v in corrected.items():
                if k == "citations":
                    vals = [normalise_citation(x) for x in v]
                    ok = [x for x in vals if x and (" ".join(x.split()).lower() in " ".join(raw.split()).lower())]
                    if ok:
                        data["citations"] = ok
                elif k in ("court", "case_title", "year", "decision_date") and v is not None:
                    data[k] = v
                    if k == "court":
                        data["court_canonical"] = None
            st.reconciled_json = data
        st.status = "extracted"
        res = await promote_judgment_staging(db, st, force=True)
        await db.flush()
        return res
    if item.statutes_staging_id:
        st = (await db.execute(select(StatutesStaging).where(StatutesStaging.id == item.statutes_staging_id))).scalars().first()
        if st is None:
            return "missing"
        st.status = "extracted"
        res = await promote_statute_staging(db, st, force=True)
        await db.flush()
        return res
    return "noop"


@shared_task(name="scraper.tasks.promotion.promote_staging_records")
def promote_staging_records_task(limit: int = 200):
    return run_async(promote_staging_records(limit))


@shared_task(name="scraper.tasks.promotion.reconcile_instrument_relations")
def reconcile_instrument_relations_task(limit: Optional[int] = None, lookback_hours: Optional[int] = None):
    return run_async(reconcile_instrument_relations(limit=limit, lookback_hours=lookback_hours))
