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
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from celery import shared_task
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.fetchers import canonical_text_hash, sha256_text
from scraper.models import (
    Citation,
    Court,
    EmbeddingQueue,
    Instrument,
    InstrumentRelation,
    Judge,
    Judgment,
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
    if st.status == "quarantined" and not force:
        await _quarantine(db, st, st.quarantine_reason or "below confidence threshold", "judgment")
        return "quarantined"
    cits = [normalise_citation(c) for c in (data.get("citations") or []) if c]
    cits = [c for c in dict.fromkeys(cits) if c]
    if not cits:
        await _quarantine(db, st, "no citation supported by source", "judgment")
        return "quarantined"
    full_text = st.raw_text or ""
    text_hash = canonical_text_hash(full_text)
    if st.raw_text_hash and text_hash != st.raw_text_hash:
        await _quarantine(db, st, "full_text hash changed between staging and promotion", "judgment")
        return "quarantined"
    canonical = cits[0]
    prov = (await db.execute(select(SourceProvenance).where(SourceProvenance.id == st.provenance_id))).scalars().first()
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
    court = await _court_by_name(db, data.get("court_canonical") or data.get("court"))
    dd = data.get("decision_date")
    decision_date = date.fromisoformat(dd) if isinstance(dd, str) and dd else None
    parts = _parse_citation_parts(canonical)
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
    return re.sub(r"\s+", " ", (n or "").strip()).rstrip(".")


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
    for mention in row.citation_mentions or []:
        if not isinstance(mention, dict):
            continue
        for field in ("normalized", "raw"):
            key = _instrument_reference_key(str(mention.get(field) or ""))
            if key:
                keys.add(key)
    return keys


def _instrument_row_signatures(row: Instrument) -> set[tuple[str, str, int]]:
    signatures: set[tuple[str, str, int]] = set()
    direct = _instrument_signature(row.number)
    if direct:
        signatures.add(direct)
    for mention in row.citation_mentions or []:
        if not isinstance(mention, dict):
            continue
        sig = _mention_signature(mention)
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
    await db.flush()


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
        aff = None
        linked_statute_mentions: list[Dict[str, Any]] = []
        collected_sections = [str(s).strip() for s in (data.get("affected_sections") or []) if str(s).strip()]
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
            section_number = row.get("section_number")
            if section_number:
                collected_sections.append(str(section_number))
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
    if not name or not sections:
        await _quarantine(db, st, "statute name or sections missing", "statute")
        return "quarantined"
    statute = (await db.execute(select(Statute).where(Statute.name == name))).scalars().first()
    if statute is None:
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
    if prov is not None:
        prov.promoted_table, prov.promoted_id = "statute", statute.id
    st.status = "promoted" if new_versions or order else "duplicate"
    st.promoted_to_id = statute.id
    await db.flush()
    return st.status


# --------------------------------------------------------------------------- batch entry points
async def promote_staging_records(limit: int = 200) -> Dict[str, int]:
    counts = {"promoted": 0, "duplicate": 0, "quarantined": 0, "statutes_promoted": 0, "statutes_duplicate": 0, "statutes_quarantined": 0}
    async with SessionLocal() as db:
        rows = (await db.execute(select(ScraperStaging).where(ScraperStaging.status.in_(["extracted", "quarantined"]), ScraperStaging.promoted_to_id.is_(None)).order_by(ScraperStaging.created_at).limit(limit))).scalars().all()
        for st in rows:
            if st.status == "quarantined":
                exists = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == st.id))).scalars().first()
                if exists is None:
                    await _quarantine(db, st, st.quarantine_reason or "below threshold", "judgment")
                    counts["quarantined"] += 1
                continue
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
        srows = (await db.execute(select(StatutesStaging).where(StatutesStaging.status.in_(["extracted", "quarantined"]), StatutesStaging.promoted_to_id.is_(None)).order_by(StatutesStaging.created_at).limit(limit))).scalars().all()
        for st in srows:
            if st.status == "quarantined":
                exists = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.statutes_staging_id == st.id))).scalars().first()
                if exists is None:
                    await _quarantine(db, st, st.quarantine_reason or "below threshold", st.kind)
                    counts["statutes_quarantined"] += 1
                continue
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
