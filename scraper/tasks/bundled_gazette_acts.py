"""Bundled-gazette act bleed sweep. Dry-run by default.

Classifies Senate, Gazette (pcp.gov.pk), law.gok.pk, and National Assembly
statutes whose PDF prints more than one ``ACT No.`` act. ``--apply`` inserts
``document_quarantine`` for a visible row whose stored sections come from more
than one of those acts, or entirely from a different act than the pipe label
or stored title. It does not rewrite section text and it does not set
``needs_section_refetch``.

A title-heading bleed with no second ``ACT No.`` line, including AJK
Conciliation Courts ``86215821``, stays on ``python -m scraper.tasks.gok_multi_act_bleed``.

    python -m scraper.tasks.bundled_gazette_acts
    python -m scraper.tasks.bundled_gazette_acts --statute-id <uuid>
    python -m scraper.tasks.bundled_gazette_acts --apply
    python -m scraper.tasks.bundled_gazette_acts --undo RUN_ID
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.fetchers import pdf_text_layer
from scraper.models import CorpusMetadata, DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.bundled_gazette import (
    ACTION_QUARANTINE,
    VERDICT_FETCH_FAIL,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_NOT_BUNDLED,
    VERDICT_SKIPPED_EMPTY,
    BundledGazetteVerdict,
    classify_bundled_gazette,
)
from scraper.statute_quality.gok_multi_act_bleed import is_law_gok_source
from scraper.statute_quality.senate_integrity import is_pdf_document_url, is_senate_sourced, split_statute_source_url
from scraper.tasks.senate_statute_integrity import recorded_pdf_bytes

logger = logging.getLogger(__name__)

LATEST_RUN_KEY = "bundled_gazette_act_latest_run_id"
_FETCH_HOSTS = frozenset(
    {
        "senate.gov.pk",
        "www.senate.gov.pk",
        "pcp.gov.pk",
        "www.pcp.gov.pk",
        "law.gok.pk",
        "www.law.gok.pk",
        "na.gov.pk",
        "www.na.gov.pk",
    }
)
_NA_HOSTS = frozenset({"na.gov.pk", "www.na.gov.pk"})

FetchPdf = Callable[[str], Awaitable[Optional[bytes]]]
ExtractText = Callable[[bytes], str]
RecordedLoader = Callable[[str], Any]


def _undo_prior_latest_key(run_id: str) -> str:
    return f"bundled_gazette_act_undo:{run_id}:prior_latest"


def _empty_counts() -> Dict[str, int]:
    return {
        VERDICT_MATCH: 0,
        VERDICT_MISMATCH: 0,
        VERDICT_NOT_BUNDLED: 0,
        VERDICT_FETCH_FAIL: 0,
        VERDICT_SKIPPED_EMPTY: 0,
    }


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _in_scope(statute: Statute) -> bool:
    if is_senate_sourced(statute.source_url, statute.source_name):
        return True
    if is_law_gok_source(statute.source_url):
        return True
    fetch_url, _label = split_statute_source_url(statute.source_url)
    return _host(fetch_url) in _NA_HOSTS


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def fetch_gazette_pdf(url: str) -> Optional[bytes]:
    """Public GET of one gazette PDF. No cookies and no login session.

    Returns None on any failure, including a redirect off the allow-list.
    """
    if not url.lower().startswith("https://") and not url.lower().startswith("http://"):
        return None
    if _host(url) not in _FETCH_HOSTS:
        return None
    limit = max(1, int(settings.PDF_MAX_SIZE_MB)) * 1024 * 1024
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={"User-Agent": settings.SCRAPER_USER_AGENT, "Accept": "application/pdf,*/*"},
        ) as client:
            response = await client.get(url)
        final_url = str(response.url)
        if _host(final_url) not in _FETCH_HOSTS:
            logger.warning("bundled gazette fetch left allow-list: %s", final_url)
            return None
        if response.status_code != 200:
            logger.warning("bundled gazette fetch status %s for %s", response.status_code, url)
            return None
        data = response.content
        if len(data) > limit or not data.startswith(b"%PDF"):
            return None
        return data
    except Exception as exc:
        logger.warning("bundled gazette fetch failed for %s: %s", url, exc)
        return None


async def _load_candidates(db: AsyncSession, statute_id: Optional[UUID]) -> List[Statute]:
    stmt = select(Statute).where(
        or_(
            Statute.source_url.ilike("%senate.gov.pk%"),
            Statute.source_url.ilike("%law.gok.pk%"),
            Statute.source_url.ilike("%na.gov.pk%"),
            (
                Statute.source_url.ilike("%pcp.gov.pk%")
                & Statute.source_name.in_(["Senate", "GazetteOfPakistan"])
            ),
        )
    )
    if statute_id is not None:
        stmt = stmt.where(Statute.id == statute_id)
    stmt = stmt.order_by(Statute.id)
    rows = (await db.execute(stmt)).scalars().all()
    return [row for row in rows if _in_scope(row)]


async def _sections_by_statute(db: AsyncSession, statute_ids: Sequence[UUID]) -> Dict[UUID, List[dict]]:
    if not statute_ids:
        return {}
    rows = (
        await db.execute(
            select(
                StatuteSection.statute_id,
                StatuteSection.section_number,
                StatuteSectionVersion.section_text,
            )
            .join(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id.in_(list(statute_ids)))
        )
    ).all()
    out: Dict[UUID, List[dict]] = {}
    for sid, number, text in rows:
        out.setdefault(sid, []).append({"section_number": str(number), "section_text": text or ""})
    return out


async def _quarantined_statute_ids(db: AsyncSession, statute_ids: Sequence[UUID]) -> set[UUID]:
    if not statute_ids:
        return set()
    rows = (
        await db.execute(
            select(DocumentQuarantine.doc_id).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id.in_(list(statute_ids)),
            )
        )
    ).scalars().all()
    return set(rows)


def _row_payload(
    statute: Statute,
    verdict: BundledGazetteVerdict,
    *,
    pdf_origin: str,
    already_quarantined: bool,
) -> Dict[str, Any]:
    return {
        "statute_id": str(statute.id),
        "stored_name": statute.name,
        "source_url": statute.source_url,
        "pdf_origin": pdf_origin,
        "verdict": verdict.verdict,
        "action": verdict.action,
        "reason": verdict.reason,
        "signals": list(verdict.signals),
        "segment_count": verdict.segment_count,
        "already_quarantined": already_quarantined,
    }


async def _resolve_pdf_text(
    fetch_url: str,
    *,
    db: AsyncSession,
    fetch_pdf: Optional[FetchPdf],
    recorded_loader: Optional[RecordedLoader],
    extract_text: ExtractText,
    skip_fetch: bool,
) -> tuple[Optional[str], str, bool]:
    data: Optional[bytes] = None
    origin = "unavailable"
    if not skip_fetch and fetch_pdf is not None:
        try:
            data = await _maybe_await(fetch_pdf(fetch_url))
        except Exception as exc:
            logger.warning("bundled gazette fetch callback failed for %s: %s", fetch_url, exc)
            data = None
    if data and bytes(data).startswith(b"%PDF"):
        origin = "fetched"
    else:
        recorded = None
        if recorded_loader is not None:
            recorded = await _maybe_await(recorded_loader(fetch_url))
        else:
            recorded = await recorded_pdf_bytes(db, fetch_url)
        if recorded and bytes(recorded).startswith(b"%PDF"):
            data = bytes(recorded)
            origin = "recorded"
        else:
            data = None
    if not data:
        return None, "unavailable", False
    try:
        text = extract_text(data) or ""
    except Exception as exc:
        logger.warning("bundled gazette text extract failed for %s: %s", fetch_url, exc)
        text = ""
    if not str(text).strip():
        return "", origin, False
    return text, origin, True


async def _insert_quarantine(
    db: AsyncSession,
    statute: Statute,
    *,
    reason: str,
    run_id: str,
    now: datetime,
) -> bool:
    exists = (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id == statute.id,
            )
        )
    ).scalars().first()
    if exists is not None:
        return False
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=statute.id,
            reason=reason,
            run_id=run_id,
            quarantined_at=now,
        )
    )
    return True


async def undo_run(db: AsyncSession, run_id: str) -> Dict[str, int]:
    result = await db.execute(
        delete(DocumentQuarantine).where(
            DocumentQuarantine.run_id == run_id,
            DocumentQuarantine.reason.like("bundled_gazette_act_%"),
        )
    )
    removed = int(result.rowcount or 0)
    prior_row = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_prior_latest_key(run_id)))
    ).scalars().first()
    prior_latest = prior_row.value if prior_row is not None else None
    latest = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))).scalars().first()
    if latest is not None and latest.value == run_id:
        if prior_latest:
            latest.value = prior_latest
        else:
            await db.delete(latest)
    if prior_row is not None:
        await db.delete(prior_row)
    await db.commit()
    return {"quarantine_rows_removed": removed}


async def run_bundled_gazette_acts(
    *,
    apply: bool = False,
    undo: Optional[str] = None,
    limit: Optional[int] = None,
    statute_id: Optional[UUID] = None,
    skip_fetch: bool = False,
    include_quarantined: bool = False,
    fetch_pdf: Optional[FetchPdf] = None,
    extract_text: Optional[ExtractText] = None,
    recorded_loader: Optional[RecordedLoader] = None,
) -> Dict[str, Any]:
    """Dry-run unless ``apply`` is true. ``undo`` removes one apply run's quarantine rows."""
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    text_fn = extract_text or pdf_text_layer
    fetcher = fetch_pdf if fetch_pdf is not None else fetch_gazette_pdf
    async with SessionLocal() as db:
        if undo:
            undone = await undo_run(db, undo)
            return {
                "dry_run": False,
                "applied": None,
                "undone": undone,
                "counts": _empty_counts(),
                "rows": [],
                "deferred_quarantined": 0,
                "population": 0,
            }
        candidates = await _load_candidates(db, statute_id)
        if limit is not None:
            candidates = candidates[: max(0, limit)]
        ids = [row.id for row in candidates]
        sections_map = await _sections_by_statute(db, ids)
        quarantined = await _quarantined_statute_ids(db, ids)
        counts = _empty_counts()
        rows: List[Dict[str, Any]] = []
        deferred = 0
        pending: List[tuple[Statute, BundledGazetteVerdict]] = []
        for statute in candidates:
            sections = sections_map.get(statute.id, [])
            already = statute.id in quarantined
            if not sections:
                counts[VERDICT_SKIPPED_EMPTY] += 1
                continue
            if already and not include_quarantined:
                deferred += 1
                continue
            fetch_url, _pipe_label = split_statute_source_url(statute.source_url)
            if not is_pdf_document_url(fetch_url):
                verdict = classify_bundled_gazette(
                    stored_name=statute.name,
                    source_url=statute.source_url,
                    sections=sections,
                    pdf_text=None,
                    pdf_available=False,
                )
                origin = "unavailable"
            else:
                pdf_text, origin, available = await _resolve_pdf_text(
                    fetch_url,
                    db=db,
                    fetch_pdf=fetcher,
                    recorded_loader=recorded_loader,
                    extract_text=text_fn,
                    skip_fetch=skip_fetch,
                )
                verdict = classify_bundled_gazette(
                    stored_name=statute.name,
                    source_url=statute.source_url,
                    sections=sections,
                    pdf_text=pdf_text,
                    pdf_available=available,
                )
            counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
            rows.append(_row_payload(statute, verdict, pdf_origin=origin, already_quarantined=already))
            if apply and not already and verdict.action == ACTION_QUARANTINE and verdict.reason:
                pending.append((statute, verdict))

        applied: Optional[Dict[str, Any]] = None
        if apply:
            run_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc)
            inserted = 0
            for statute, verdict in pending:
                if await _insert_quarantine(db, statute, reason=str(verdict.reason), run_id=run_id, now=now):
                    inserted += 1
            latest = (
                await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))
            ).scalars().first()
            prior_latest = latest.value if latest is not None else ""
            db.add(CorpusMetadata(key=_undo_prior_latest_key(run_id), value=prior_latest))
            if latest is None:
                db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
            else:
                latest.value = run_id
            await db.commit()
            applied = {"run_id": run_id, "quarantined": inserted}
        return {
            "dry_run": not apply,
            "population": len(candidates),
            "deferred_quarantined": deferred,
            "counts": counts,
            "rows": rows,
            "applied": applied,
        }


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Find statutes whose sections were taken from more than one act in a bundled "
            "gazette PDF. Dry-run by default. --apply quarantines visible mismatches and "
            "does not rewrite section text."
        )
    )
    parser.add_argument("--apply", action="store_true", help="Insert document_quarantine rows for visible mismatches")
    parser.add_argument("--undo", metavar="RUN_ID", help="Remove quarantine rows inserted by one --apply run")
    parser.add_argument("--limit", type=int, default=None, help="Classify at most this many in-scope rows")
    parser.add_argument("--statute-id", default=None, help="Classify one statute UUID")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Do not HTTP-fetch. Use recorded public PDF bytes only; missing bytes are FETCH_FAIL",
    )
    parser.add_argument(
        "--include-quarantined",
        action="store_true",
        help="Also classify already-quarantined rows. Apply still does not change an existing quarantine row",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    statute_id = UUID(args.statute_id) if args.statute_id else None
    out = run_async(
        run_bundled_gazette_acts(
            apply=args.apply,
            undo=args.undo,
            limit=args.limit,
            statute_id=statute_id,
            skip_fetch=args.skip_fetch,
            include_quarantined=args.include_quarantined,
        )
    )
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
