"""Senate-sourced statute identity sweep. Dry-run by default.

Classifies statutes whose source_url is a senate.gov.pk upload, or a
pcp.gov.pk Gazette PDF on a Senate / GazetteOfPakistan row. ``--apply``
quarantines visible mismatches and flags visible partial identity rows for
re-extract. It does not overwrite statute names or section text, and it does
not relabel already-quarantined bill shells.

    python -m scraper.tasks.senate_statute_integrity
    python -m scraper.tasks.senate_statute_integrity --apply
    python -m scraper.tasks.senate_statute_integrity --undo RUN_ID
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
from uuid import UUID
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.fetchers import pdf_text_layer, read_raw
from scraper.models import CorpusMetadata, DocumentQuarantine, SourceProvenance, Statute, StatuteSection, StatuteSectionVersion
from scraper.statute_quality.senate_integrity import (
    VERDICT_FETCH_FAIL,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_PARTIAL,
    VERDICT_SKIPPED_SHELL,
    ACTION_NEEDS_REEXTRACT,
    ACTION_QUARANTINE,
    SenateIntegrityVerdict,
    classify_senate_identity,
    is_pdf_document_url,
    is_quarantined_bill_shell,
    is_senate_sourced,
    split_statute_source_url,
)

logger = logging.getLogger(__name__)

LATEST_RUN_KEY = "senate_integrity_latest_run_id"
FetchPdf = Callable[[str], Awaitable[Optional[bytes]]]
ExtractText = Callable[[bytes], str]
RecordedLoader = Callable[[str], Any]


def _undo_prior_latest_key(run_id: str) -> str:
    return f"senate_integrity_undo:{run_id}:prior_latest"


def _undo_refetch_key(run_id: str) -> str:
    return f"senate_integrity_undo:{run_id}:refetch"


def _empty_counts() -> Dict[str, int]:
    return {
        VERDICT_MATCH: 0,
        VERDICT_MISMATCH: 0,
        VERDICT_PARTIAL: 0,
        VERDICT_FETCH_FAIL: 0,
        VERDICT_SKIPPED_SHELL: 0,
    }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _host_allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in {"senate.gov.pk", "www.senate.gov.pk", "pcp.gov.pk", "www.pcp.gov.pk"}


async def fetch_senate_pdf(url: str) -> Optional[bytes]:
    """Public GET of one Senate or Gazette PDF. No cookies and no login session.

    Returns None on any failure, including a redirect off the Senate/Gazette hosts.
    """
    if not url.lower().startswith("https://") and not url.lower().startswith("http://"):
        return None
    if not _host_allowed(url):
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
        if not _host_allowed(final_url):
            logger.warning("senate integrity fetch left allow-list: %s", final_url)
            return None
        if response.status_code != 200:
            logger.warning("senate integrity fetch status %s for %s", response.status_code, url)
            return None
        data = response.content
        if len(data) > limit or not data.startswith(b"%PDF"):
            return None
        return data
    except Exception as exc:
        logger.warning("senate integrity fetch failed for %s: %s", url, exc)
        return None


async def recorded_pdf_bytes(db: AsyncSession, fetch_url: str) -> Optional[bytes]:
    """Latest preserved PDF bytes for this fetch URL, if the raw file is still readable."""
    prov = (
        await db.execute(
            select(SourceProvenance)
            .where(
                SourceProvenance.source_url == fetch_url,
                SourceProvenance.content_kind == "pdf",
                SourceProvenance.access_method == "public",
                SourceProvenance.raw_ref.isnot(None),
            )
            .order_by(SourceProvenance.fetched_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if prov is None or not prov.raw_ref:
        return None
    try:
        data = read_raw(prov.raw_ref)
    except OSError as exc:
        logger.warning("senate integrity recorded bytes unreadable for %s: %s", fetch_url, exc)
        return None
    if not data.startswith(b"%PDF"):
        return None
    return data


async def _load_candidates(db: AsyncSession, statute_id: Optional[UUID]) -> List[Statute]:
    stmt = select(Statute).where(
        or_(
            Statute.source_url.ilike("%senate.gov.pk%"),
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
    return [row for row in rows if is_senate_sourced(row.source_url, row.source_name)]


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
    verdict: SenateIntegrityVerdict,
    *,
    pdf_origin: str,
    already_quarantined: bool,
) -> Dict[str, Any]:
    return {
        "statute_id": str(statute.id),
        "stored_name": statute.name,
        "source_url": statute.source_url,
        "fetch_url": verdict.fetch_url,
        "pipe_label": verdict.pipe_label,
        "pdf_title": verdict.pdf_title,
        "pdf_origin": pdf_origin,
        "verdict": verdict.verdict,
        "action": verdict.action,
        "reason": verdict.reason,
        "signals": list(verdict.signals),
        "needs_reextract": verdict.needs_reextract,
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
            data = await fetch_pdf(fetch_url)
        except Exception as exc:
            logger.warning("senate integrity fetch callback failed for %s: %s", fetch_url, exc)
            data = None
    if data and data.startswith(b"%PDF"):
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
        logger.warning("senate integrity text extract failed for %s: %s", fetch_url, exc)
        text = ""
    return text, origin, True


async def _apply_verdict(
    db: AsyncSession,
    statute: Statute,
    verdict: SenateIntegrityVerdict,
    *,
    run_id: str,
    now: datetime,
    refetch_snapshot: List[Dict[str, Any]],
) -> str:
    """Quarantine a visible mismatch, or flag a repairable partial. Returns the write kind.

    Repairable PARTIAL rows stay servable. ``needs_section_refetch`` is only a marker;
    this sweep does not rewrite the name or the section text.
    """
    if verdict.action == ACTION_NEEDS_REEXTRACT:
        refetch_snapshot.append(
            {
                "id": str(statute.id),
                "needs_section_refetch": bool(statute.needs_section_refetch),
            }
        )
        statute.needs_section_refetch = True
        return "reextract"
    if verdict.action != ACTION_QUARANTINE or not verdict.reason:
        return "unchanged"
    exists = (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id == statute.id,
            )
        )
    ).scalars().first()
    if exists is None:
        db.add(
            DocumentQuarantine(
                doc_type="statute",
                doc_id=statute.id,
                reason=verdict.reason,
                run_id=run_id,
                quarantined_at=now,
            )
        )
    return "quarantine"


async def undo_run(db: AsyncSession, run_id: str) -> Dict[str, int]:
    result = await db.execute(
        delete(DocumentQuarantine).where(
            DocumentQuarantine.run_id == run_id,
            or_(
                DocumentQuarantine.reason.like("senate_integrity:%"),
                DocumentQuarantine.reason == "name_s1_mismatch",
            ),
        )
    )
    removed = int(result.rowcount or 0)
    restored = 0
    snap = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == _undo_refetch_key(run_id)))
    ).scalars().first()
    if snap is not None and snap.value:
        for item in json.loads(snap.value):
            sid = UUID(item["id"])
            statute = (await db.execute(select(Statute).where(Statute.id == sid))).scalars().first()
            if statute is not None:
                statute.needs_section_refetch = bool(item.get("needs_section_refetch"))
                restored += 1
        await db.delete(snap)
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
    return {"quarantine_rows_removed": removed, "refetch_flags_restored": restored}


async def run_senate_statute_integrity(
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
    """Dry-run unless ``apply`` is true. ``undo`` removes one apply run's quarantine rows and refetch flags."""
    if undo and apply:
        raise ValueError("pass only one of --apply or --undo")
    text_fn = extract_text or pdf_text_layer
    fetcher = fetch_pdf if fetch_pdf is not None else fetch_senate_pdf
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
        pending: List[tuple[Statute, SenateIntegrityVerdict]] = []
        for statute in candidates:
            sections = sections_map.get(statute.id, [])
            already = statute.id in quarantined
            fetch_url, pipe_label = split_statute_source_url(statute.source_url)
            if is_quarantined_bill_shell(
                already_quarantined=already,
                stored_name=statute.name,
                pipe_label=pipe_label,
                section_count=len(sections),
            ):
                verdict = classify_senate_identity(
                    stored_name=statute.name,
                    year_enacted=statute.year_enacted,
                    source_url=statute.source_url,
                    sections=sections,
                    pdf_text=None,
                    pdf_available=False,
                    already_quarantined=True,
                    section_count=0,
                )
                counts[VERDICT_SKIPPED_SHELL] += 1
                rows.append(_row_payload(statute, verdict, pdf_origin="not_fetched", already_quarantined=True))
                continue
            if already and not include_quarantined:
                deferred += 1
                continue
            if not is_pdf_document_url(fetch_url):
                verdict = classify_senate_identity(
                    stored_name=statute.name,
                    year_enacted=statute.year_enacted,
                    source_url=statute.source_url,
                    sections=sections,
                    pdf_available=False,
                    already_quarantined=already,
                    section_count=len(sections),
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
                verdict = classify_senate_identity(
                    stored_name=statute.name,
                    year_enacted=statute.year_enacted,
                    source_url=statute.source_url,
                    sections=sections,
                    pdf_text=pdf_text,
                    pdf_available=available,
                    already_quarantined=already,
                    section_count=len(sections),
                )
            counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
            rows.append(_row_payload(statute, verdict, pdf_origin=origin, already_quarantined=already))
            if apply and not already:
                pending.append((statute, verdict))

        applied: Optional[Dict[str, Any]] = None
        if apply:
            run_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc)
            refetch_snapshot: List[Dict[str, Any]] = []
            quarantined_n = 0
            reextract_n = 0
            for statute, verdict in pending:
                kind = await _apply_verdict(
                    db,
                    statute,
                    verdict,
                    run_id=run_id,
                    now=now,
                    refetch_snapshot=refetch_snapshot,
                )
                if kind == "quarantine":
                    quarantined_n += 1
                elif kind == "reextract":
                    reextract_n += 1
            latest = (
                await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == LATEST_RUN_KEY))
            ).scalars().first()
            prior_latest = latest.value if latest is not None else ""
            db.add(CorpusMetadata(key=_undo_prior_latest_key(run_id), value=prior_latest))
            db.add(CorpusMetadata(key=_undo_refetch_key(run_id), value=json.dumps(refetch_snapshot)))
            if latest is None:
                db.add(CorpusMetadata(key=LATEST_RUN_KEY, value=run_id))
            else:
                latest.value = run_id
            await db.commit()
            applied = {
                "run_id": run_id,
                "quarantined": quarantined_n,
                "flagged_reextract": reextract_n,
            }
        return {
            "dry_run": not apply,
            "population": len(candidates),
            "deferred_quarantined": deferred,
            "counts": counts,
            "rows": rows,
            "applied": applied,
        }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Classify Senate-sourced statutes against the PDF at source_url. "
            "Dry-run by default. --apply quarantines visible mismatches and flags "
            "visible partial identity rows for re-extract without overwriting text."
        )
    )
    parser.add_argument("--apply", action="store_true", help="Write quarantine rows and re-extract flags")
    parser.add_argument("--undo", metavar="RUN_ID", help="Remove quarantine rows and refetch flags from one --apply run")
    parser.add_argument("--limit", type=int, default=None, help="Classify at most this many Senate-sourced rows")
    parser.add_argument("--statute-id", default=None, help="Classify one statute UUID")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Do not HTTP-fetch. Use recorded PDF bytes only; missing bytes are FETCH_FAIL",
    )
    parser.add_argument(
        "--include-quarantined",
        action="store_true",
        help="Also fetch and classify already-quarantined non-shell rows. Apply still does not relabel them",
    )
    args = parser.parse_args(argv)
    statute_id = UUID(args.statute_id) if args.statute_id else None
    out = run_async(
        run_senate_statute_integrity(
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
