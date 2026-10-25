"""
/corpus/v1 — the read-only corpus API for the Sikander legal research and drafting agent
(cursor-init docs/agents/sikander-legal-agent/agent_spec.md).

Five reads the agent's tools map onto: judgment search, a judgment by citation (full text), citation
verification, the judgments citing a judgment with their treatment, and the current text of a statute
section. Nothing here writes.

* Its own key: X-Corpus-Key = CORPUS_API_KEY, never the admin key, so the agent holds no admin power.
  Repeated wrong keys from one address are refused for a while, counted apart from /admin's.
* Its own login: every query runs as the corpus_writer role in a read-only transaction with a statement
  time limit (the sikander_reader role is column-limited for direct SQL and cannot read full text).
"""

from __future__ import annotations

import hmac
import re
import time
from collections import defaultdict
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from scraper.config import settings
from scraper.corpus_visibility import judgment_servable, statute_servable
from scraper.models import Citation, Judgment, Statute, StatuteSection, StatuteSectionVersion, Treatment
from scraper.parsers.citation_extractor import normalise_citation
from scraper.routers.auth import _client_ip

STATEMENT_TIMEOUT_MS = 15000
SNIPPET_CHARS = 300
# Wrong corpus keys are counted apart from wrong admin keys: a misconfigured agent must not lock the operator
# out of /admin, nor admin mistakes lock the agent out.
_KEY_FAILS: dict[str, list[float]] = defaultdict(list)
_KEY_FAIL_WINDOW_SECONDS = 300
_KEY_FAIL_LIMIT = 20

_reader_engine: Optional[AsyncEngine] = None


def _engine() -> AsyncEngine:
    """The reader's own engine, built on first use. NullPool: the API is low-traffic and a pooled asyncpg
    connection must never cross event loops."""
    global _reader_engine
    if _reader_engine is None:
        password = settings.CORPUS_WRITER_PASSWORD.get_secret_value()
        if not password:
            raise HTTPException(status_code=503, detail="corpus API not configured: CORPUS_WRITER_PASSWORD is blank")
        url = make_url(settings.DATABASE_URL).set(username=settings.CORPUS_WRITER_ROLE, password=password)
        _reader_engine = create_async_engine(
            url,
            poolclass=NullPool,
            connect_args={
                "server_settings": {
                    "default_transaction_read_only": "on",
                    "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                }
            },
        )
    return _reader_engine


async def reader_session() -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(_engine(), expire_on_commit=False, class_=AsyncSession)() as session:
        yield session


def _key_failures(ip: str) -> int:
    now = time.time()
    kept = [stamp for stamp in _KEY_FAILS.get(ip, []) if now - stamp < _KEY_FAIL_WINDOW_SECONDS]
    if kept:
        _KEY_FAILS[ip] = kept
    else:
        _KEY_FAILS.pop(ip, None)
    return len(kept)


def require_corpus_key(request: Request, x_corpus_key: Optional[str] = Header(default=None)) -> None:
    if not settings.CORPUS_API_KEY:
        raise HTTPException(status_code=503, detail="corpus API not configured: CORPUS_API_KEY is blank")
    ip = _client_ip(request)
    if _key_failures(ip) >= _KEY_FAIL_LIMIT:
        raise HTTPException(status_code=429, detail="Too many invalid corpus-key attempts")
    if x_corpus_key is None or not hmac.compare_digest(x_corpus_key, settings.CORPUS_API_KEY):
        _KEY_FAILS[ip].append(time.time())
        raise HTTPException(status_code=401, detail="Invalid key")


router = APIRouter(prefix="/corpus/v1", tags=["corpus-read"], dependencies=[Depends(require_corpus_key)])


def _like(term: str) -> str:
    """An ILIKE pattern that matches the term literally (its own % and _ are escaped)."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _snippet(text: Optional[str], words: List[str]) -> str:
    body = text or ""
    lower = body.lower()
    at = min((i for i in (lower.find(w.lower()) for w in words) if i >= 0), default=0)
    start = max(0, at - SNIPPET_CHARS // 3)
    piece = " ".join(body[start : start + SNIPPET_CHARS].split())
    return ("…" if start > 0 else "") + piece + ("…" if start + SNIPPET_CHARS < len(body) else "")


def _citation_variants(citation: str) -> List[str]:
    raw = " ".join((citation or "").split())
    out = [raw]
    normalised = normalise_citation(raw) if raw else ""
    if normalised and normalised not in out:
        out.append(normalised)
    return out


async def _judgment_for(db: AsyncSession, citation: str) -> Optional[Judgment]:
    variants = _citation_variants(citation)
    j = (
        await db.execute(select(Judgment).where(Judgment.canonical_citation.in_(variants), judgment_servable()))
    ).scalars().first()
    if j is not None:
        return j
    return (
        await db.execute(
            select(Judgment)
            .join(Citation, Citation.judgment_id == Judgment.id)
            .where(Citation.citation_string.in_(variants), judgment_servable())
        )
    ).scalars().first()


@router.get("/judgments/search")
async def search_judgments(
    q: str = Query(..., min_length=2, max_length=300),
    court: Optional[str] = Query(default=None, max_length=200),
    year_from: Optional[int] = Query(default=None, ge=1947, le=2100),
    year_to: Optional[int] = Query(default=None, ge=1947, le=2100),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(reader_session),
) -> Dict[str, Any]:
    """Judgments whose citation, title, headnotes or full text contain every word of q, newest first."""
    words = [w for w in re.split(r"\s+", q.strip()) if len(w) >= 2][:12]
    if not words:
        raise HTTPException(status_code=422, detail="q needs at least one word of two or more characters")
    haystack = func.concat_ws(" ", Judgment.canonical_citation, Judgment.case_title, Judgment.headnotes, Judgment.full_text)
    conditions = [haystack.ilike(_like(w), escape="\\") for w in words]
    if court:
        conditions.append(Judgment.court_name.ilike(_like(court), escape="\\"))
    if year_from is not None:
        conditions.append(Judgment.year >= year_from)
    if year_to is not None:
        conditions.append(Judgment.year <= year_to)
    rows = (
        await db.execute(
            select(Judgment)
            .where(and_(*conditions, judgment_servable()))
            .order_by(Judgment.year.desc().nulls_last(), Judgment.decision_date.desc().nulls_last(), Judgment.canonical_citation)
            .limit(limit)
        )
    ).scalars().all()
    return {
        "query": q,
        "results": [
            {
                "citation": j.canonical_citation,
                "case_title": j.case_title,
                "court": j.court_name,
                "year": j.year,
                "decision_date": j.decision_date.isoformat() if j.decision_date else None,
                "judges": list(j.judge_names or []),
                "snippet": _snippet(j.full_text or j.headnotes or j.case_title, words),
            }
            for j in rows
        ],
    }


@router.get("/judgments/by-citation")
async def judgment_by_citation(
    citation: str = Query(..., min_length=3, max_length=200),
    db: AsyncSession = Depends(reader_session),
) -> Dict[str, Any]:
    j = await _judgment_for(db, citation)
    if j is None:
        raise HTTPException(status_code=404, detail=f"no judgment with citation {citation!r} in the corpus")
    return {
        "citation": j.canonical_citation,
        "case_title": j.case_title,
        "court": j.court_name,
        "year": j.year,
        "decision_date": j.decision_date.isoformat() if j.decision_date else None,
        "judges": list(j.judge_names or []),
        "bench_size": j.bench_size,
        "bench_type": j.bench_type,
        "headnotes": j.headnotes,
        "full_text": j.full_text,
        "source_url": j.source_url,
    }


@router.get("/citations/verify")
async def verify_citation(
    citation: str = Query(..., min_length=3, max_length=200),
    db: AsyncSession = Depends(reader_session),
) -> Dict[str, Any]:
    """Whether the citation is a judgment held in the corpus. Never a guess: found=false when it is not held."""
    j = await _judgment_for(db, citation)
    if j is None:
        return {"citation": citation, "found": False, "canonical_citation": None, "case_title": None, "court": None, "year": None, "alternative_citations": []}
    others = (
        await db.execute(select(Citation.citation_string).where(Citation.judgment_id == j.id, Citation.citation_string != j.canonical_citation))
    ).scalars().all()
    return {
        "citation": citation,
        "found": True,
        "canonical_citation": j.canonical_citation,
        "case_title": j.case_title,
        "court": j.court_name,
        "year": j.year,
        "alternative_citations": sorted(set(others)),
    }


@router.get("/judgments/citing")
async def citing_judgments(
    citation: str = Query(..., min_length=3, max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(reader_session),
) -> Dict[str, Any]:
    """Later judgments that cite this one, with the treatment recorded for each and its evidence passage. An
    empty list means no treatment is recorded in the corpus, not that the judgment is good law."""
    cited = await _judgment_for(db, citation)
    variants = _citation_variants(citation)
    match = Treatment.cited_citation.in_(variants)
    if cited is not None:
        match = or_(match, Treatment.cited_judgment_id == cited.id)
    rows = (
        await db.execute(
            select(Treatment, Judgment)
            .join(Judgment, Judgment.id == Treatment.citing_judgment_id)
            .where(match, judgment_servable())
            .order_by(Judgment.year.desc().nulls_last(), Judgment.canonical_citation, Treatment.label)
            .limit(limit)
        )
    ).all()
    return {
        "citation": cited.canonical_citation if cited is not None else citation,
        "found": cited is not None,
        "citing": [
            {
                "citation": j.canonical_citation,
                "case_title": j.case_title,
                "court": j.court_name,
                "year": j.year,
                "treatment": t.label,
                "confidence": t.confidence,
                "evidence": t.evidence_passage,
                "reviewed": t.reviewed,
            }
            for t, j in rows
        ],
    }


@router.get("/statutes/section")
async def statute_section(
    statute: str = Query(..., min_length=2, max_length=300),
    section: str = Query(..., min_length=1, max_length=100),
    db: AsyncSession = Depends(reader_session),
) -> Dict[str, Any]:
    """The current text of one section. The statute is matched by its short name exactly (case-insensitive),
    else by its name containing the words given; the section number must match exactly."""
    name = " ".join(statute.split())
    s = (
        await db.execute(select(Statute).where(func.lower(Statute.short_name) == name.lower(), statute_servable()))
    ).scalars().first()
    if s is None:
        words = [w for w in name.split() if w]
        candidates = (
            await db.execute(
                select(Statute)
                .where(and_(*[Statute.name.ilike(_like(w), escape="\\") for w in words], statute_servable()))
                .order_by(func.length(Statute.name))
                .limit(1)
            )
        ).scalars().all()
        s = candidates[0] if candidates else None
    if s is None:
        raise HTTPException(status_code=404, detail=f"no statute matching {statute!r} in the corpus")
    row = (
        await db.execute(
            select(StatuteSection, StatuteSectionVersion)
            .outerjoin(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id == s.id, StatuteSection.section_number == section.strip())
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no section {section!r} of {s.name} in the corpus")
    sec, ver = row
    return {
        "statute_name": s.name,
        "short_name": s.short_name,
        "is_repealed": s.is_repealed,
        "section_number": sec.section_number,
        "section_title": sec.section_title,
        "text": ver.section_text if ver else None,
        "version_no": ver.version_no if ver else None,
        "effective_from": ver.effective_from.isoformat() if ver and ver.effective_from else None,
        "version_confidence": ver.version_confidence if ver else None,
        "source_url": s.source_url,
    }
