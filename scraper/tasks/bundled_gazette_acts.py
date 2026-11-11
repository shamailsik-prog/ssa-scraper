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
    python -m scraper.tasks.bundled_gazette_acts --apply-reextract --statute-id <uuid>
    python -m scraper.tasks.bundled_gazette_acts --lift-quarantine --statute-id <uuid>
    python -m scraper.tasks.bundled_gazette_acts --rollback RUN_ID
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import delete, or_, select, text
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

# Source-verified rows. ``--preview-reextract`` reads these and writes nothing.
CONFIRMED_BLEED_STATUTE_IDS = (
    UUID("df10d129-fd7b-4c25-9a38-828878466e6a"),
    UUID("b535370b-a524-4d2b-8e72-562984904397"),
    UUID("92aeaf99-7603-41d6-a541-f2c38fe27d04"),
    UUID("5636b724-6a47-42a7-93f5-4d78e07ddea8"),
    UUID("12cf06c5-d90a-4ccb-92e6-30c8c0d7f7c3"),
    UUID("925a267d-a608-419e-9a02-6efba375a7bc"),
    UUID("21f60373-b62a-4df8-9f0f-947dcf8a352e"),
    UUID("06a5058e-511e-4039-befa-947f1fcdfd68"),
    UUID("a6583612-bc3a-4aaf-8584-bbb65b884596"),
    UUID("a8803342-bb11-45d2-8651-bc2efd158476"),
    UUID("018fa065-d60f-4332-bbe0-d731fc560961"),
    UUID("e81dbcfa-910c-4065-8f3c-4d28b1c36c9f"),
    UUID("6c73a686-c038-4611-8f05-8a429168c8a7"),
    UUID("10cc6efa-f845-42e5-83ee-11eeabd0bc63"),
    UUID("0181ad58-1a79-479c-bdd8-dad584cfd683"),
    UUID("7f0a008c-6775-4a44-87b0-4e0e5c1b5b02"),
    UUID("bbc0bfca-6a0e-4eef-b771-532f4f3a4041"),
    UUID("337584d6-c37a-4cfb-99bd-6e933f5b195b"),
    UUID("eef57038-13dc-4b4d-b050-14cb7d490e44"),
    UUID("eb8f8ae0-727a-464b-9786-db0247eca896"),
    UUID("d7d3187e-9ebd-4782-aa9a-228b40d2dd96"),
    UUID("f700f160-4bf3-41df-9ec5-b052b5627f77"),
    UUID("17f283f3-b875-4c5d-b3ed-6a235d9ebd21"),
    UUID("df206e70-1114-4676-8eec-c77fa051f268"),
    UUID("65c5b7b3-674c-4726-8372-b068527cea90"),
    UUID("768e086d-329e-42d9-ade3-01900badfb8e"),
    # The Antiquities Act, 1976 (Act VII of 1976), MAIN and FREE-SITES.
    UUID("a125355e-f78f-4421-81ef-b79fc16a4eb5"),
    UUID("2ceea487-53ab-46a6-b8b2-406cf1146a11"),
    # The Copyright (Amendment) Act, 1973 (Act II of 1973), MAIN and FREE-SITES.
    UUID("ec757be6-77f2-4061-bedb-ee0d4d84c6df"),
    UUID("1dd4940a-ede1-4e0d-a1d5-8df59d3431b4"),
)

# Apply and lift. Preview still reads the full CONFIRMED_BLEED_STATUTE_IDS table.
# The first eight are the Auditor-approved pairs (MAIN and FREE-SITES): Legal
# Advisers, Travel Agencies, Pat Feeder, and Withdrawal of Remission. The next
# pair is The Antiquities Act, 1976, then The Copyright (Amendment) Act, 1973.
# Apply still refuses every other id, and still refuses an id whose pin is
# missing or not 64 hex characters.
APPROVED_REEXTRACT_IDS = frozenset(
    {
        UUID("5636b724-6a47-42a7-93f5-4d78e07ddea8"),
        UUID("92aeaf99-7603-41d6-a541-f2c38fe27d04"),
        UUID("21f60373-b62a-4df8-9f0f-947dcf8a352e"),
        UUID("06a5058e-511e-4039-befa-947f1fcdfd68"),
        UUID("6c73a686-c038-4611-8f05-8a429168c8a7"),
        UUID("10cc6efa-f845-42e5-83ee-11eeabd0bc63"),
        UUID("0181ad58-1a79-479c-bdd8-dad584cfd683"),
        UUID("7f0a008c-6775-4a44-87b0-4e0e5c1b5b02"),
        UUID("a125355e-f78f-4421-81ef-b79fc16a4eb5"),
        UUID("2ceea487-53ab-46a6-b8b2-406cf1146a11"),
        UUID("ec757be6-77f2-4061-bedb-ee0d4d84c6df"),
        UUID("1dd4940a-ede1-4e0d-a1d5-8df59d3431b4"),
    }
)

# Empty string: the URL no longer serves the harvested file, and no authentic
# sha is on record. Any bytes are a mismatch.
PIN_UNTRUSTED = ""
_S217 = "d669e63a3a69670fa808c620672861ff3f616ca7986c217c0bb0b27415e5b114"
_S710 = "02380046aded041db4be54b644b96afbc14e921f8f978756eb56d88f966b1ede"
_S476 = "a169a902a17a3c38916c8bab05f27859d38989ba5ee63b02afde02b6262fdd01"
_S441 = "320370bfdbe1631332ab60457c17ab1f79fed7e57102269413fe27e5963b51a7"
_S605 = "5bc250481cb702b5565b7091e1b5720d4b657567bfcb60af1aec9a076d0afd34"
_S478 = "d90008a3b35e0ad5b9d2056f0b69462e5fc55d6fb3428049e348fc06754e90a4"
_S171 = "6e9dbba7f60a4265543521a4084565d150c9c29747cb6d860a993d6a1bc37636"
_S685 = "dbdcb76b792e4a127987d275347cf989ed37bb158438455a6ea6c0971dfa3e22"
_S133 = "03cee95b52dbd3ae1f96058937f90c945e7c48634f120da64222f4e4a1c8c616"
_S678 = "f9df01dc3d558ac22b082e634a1e36973d1a7e84e59ac8735e109c9ab94d8c4d"
_S402 = "478655ccc6f6fd1eee8f911ed1d4bedd00950b3855ba6f17ee094a7c818d2bfe"
# senate.gov.pk/uploads/documents/1660631634_471.pdf. Operator-confirmed on both hosts.
_S471 = "b6eee4aa83fcb53b7b7e07a33e25131f7c4e4cf98e1037b753e4ae71a915151c"
# senate.gov.pk/uploads/documents/1660540218_920.pdf.
_S920 = "87ec6471fa9086fbdf0b0a3166671851f480c026b174e7eaf40884621ce69801"
CONFIRMED_PDF_SHA256 = {
    UUID("df10d129-fd7b-4c25-9a38-828878466e6a"): _S217,
    UUID("b535370b-a524-4d2b-8e72-562984904397"): _S217,
    UUID("92aeaf99-7603-41d6-a541-f2c38fe27d04"): _S710,
    UUID("5636b724-6a47-42a7-93f5-4d78e07ddea8"): _S710,
    UUID("12cf06c5-d90a-4ccb-92e6-30c8c0d7f7c3"): _S476,
    UUID("925a267d-a608-419e-9a02-6efba375a7bc"): _S476,
    UUID("21f60373-b62a-4df8-9f0f-947dcf8a352e"): _S441,
    UUID("06a5058e-511e-4039-befa-947f1fcdfd68"): _S441,
    UUID("a6583612-bc3a-4aaf-8584-bbb65b884596"): PIN_UNTRUSTED,
    UUID("a8803342-bb11-45d2-8651-bc2efd158476"): PIN_UNTRUSTED,
    UUID("018fa065-d60f-4332-bbe0-d731fc560961"): _S605,
    UUID("e81dbcfa-910c-4065-8f3c-4d28b1c36c9f"): _S605,
    UUID("6c73a686-c038-4611-8f05-8a429168c8a7"): _S478,
    UUID("10cc6efa-f845-42e5-83ee-11eeabd0bc63"): _S478,
    UUID("0181ad58-1a79-479c-bdd8-dad584cfd683"): _S171,
    UUID("7f0a008c-6775-4a44-87b0-4e0e5c1b5b02"): _S171,
    UUID("bbc0bfca-6a0e-4eef-b771-532f4f3a4041"): PIN_UNTRUSTED,
    UUID("337584d6-c37a-4cfb-99bd-6e933f5b195b"): PIN_UNTRUSTED,
    UUID("eef57038-13dc-4b4d-b050-14cb7d490e44"): _S685,
    UUID("eb8f8ae0-727a-464b-9786-db0247eca896"): _S685,
    UUID("d7d3187e-9ebd-4782-aa9a-228b40d2dd96"): _S133,
    UUID("f700f160-4bf3-41df-9ec5-b052b5627f77"): _S133,
    UUID("17f283f3-b875-4c5d-b3ed-6a235d9ebd21"): _S678,
    UUID("df206e70-1114-4676-8eec-c77fa051f268"): _S678,
    UUID("65c5b7b3-674c-4726-8372-b068527cea90"): _S402,
    UUID("768e086d-329e-42d9-ade3-01900badfb8e"): _S402,
    UUID("a125355e-f78f-4421-81ef-b79fc16a4eb5"): _S471,
    UUID("2ceea487-53ab-46a6-b8b2-406cf1146a11"): _S471,
    UUID("ec757be6-77f2-4061-bedb-ee0d4d84c6df"): _S920,
    UUID("1dd4940a-ede1-4e0d-a1d5-8df59d3431b4"): _S920,
}
LIVE_NA_320_SHA = "83236f6eeedc4bcfd96d3beea0402e3a147ca56393c053fda9fa3a34c238b19d"
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


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def accept_pinned_pdf(
    candidates: Sequence[tuple[str, Optional[bytes]]],
    *,
    pinned_sha: Optional[str] = None,
    recorded_sha: Optional[str] = None,
) -> tuple[Optional[bytes], str]:
    """Keep bytes only when every supplied sha matches. A missing pin fails closed.

    ``pinned_sha=""`` means this row has no authentic file. The live PDF is
    not adopted, even when a stored hash was overwritten to that live file.
    When no sha was supplied and the fetched bytes differ from the recorded
    bytes, that is also a mismatch.
    """
    if pinned_sha == PIN_UNTRUSTED:
        return None, "sha_mismatch"
    pdfs = [(origin, bytes(blob)) for origin, blob in candidates if blob and bytes(blob).startswith(b"%PDF")]
    pins = [item.lower() for item in (pinned_sha, recorded_sha) if item]
    if not pins:
        if len(pdfs) >= 2 and _sha256_hex(pdfs[0][1]) != _sha256_hex(pdfs[1][1]):
            return None, "sha_mismatch"
        if pdfs:
            # (origin, blob) in, (blob, origin) out. Swapping these makes
            # extract_text see a str and the preview reports pdf_unavailable.
            origin, blob = pdfs[0]
            return blob, origin
        return None, "unavailable"
    for origin, blob in pdfs:
        digest = _sha256_hex(blob)
        if all(digest == pin for pin in pins):
            return blob, origin
    return None, "sha_mismatch"


async def _resolve_pdf_text(
    fetch_url: str,
    *,
    db: AsyncSession,
    fetch_pdf: Optional[FetchPdf],
    recorded_loader: Optional[RecordedLoader],
    extract_text: ExtractText,
    skip_fetch: bool,
    pinned_sha: Optional[str] = None,
    recorded_sha: Optional[str] = None,
) -> tuple[Optional[str], str, bool]:
    fetched: Optional[bytes] = None
    if not skip_fetch and fetch_pdf is not None:
        try:
            fetched = await _maybe_await(fetch_pdf(fetch_url))
        except Exception as exc:
            logger.warning("bundled gazette fetch callback failed for %s: %s", fetch_url, exc)
            fetched = None
    recorded = None
    if recorded_loader is not None:
        recorded = await _maybe_await(recorded_loader(fetch_url))
    elif pinned_sha is not None or recorded_sha is not None:
        recorded = await recorded_pdf_bytes(db, fetch_url)
    elif fetched is None or not bytes(fetched or b"").startswith(b"%PDF"):
        recorded = await recorded_pdf_bytes(db, fetch_url)
    data, origin = accept_pinned_pdf(
        (("fetched", fetched), ("recorded", recorded)),
        pinned_sha=pinned_sha,
        recorded_sha=recorded_sha,
    )
    if origin == "sha_mismatch" or not data:
        return None, origin, False
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


async def preview_confirmed_reextract(
    *,
    statute_id: Optional[UUID] = None,
    skip_fetch: bool = False,
    fetch_pdf: Optional[FetchPdf] = None,
    extract_text: Optional[ExtractText] = None,
    recorded_loader: Optional[RecordedLoader] = None,
) -> Dict[str, Any]:
    """Re-extract confirmed bundled-gazette rows in memory. Writes nothing.

    A marker whose Act is not in the current PDF yields no sections. This does
    not insert, update, or delete statute, section, or quarantine rows.
    """
    ids = [statute_id] if statute_id is not None else list(CONFIRMED_BLEED_STATUTE_IDS)
    text_fn = extract_text or pdf_text_layer
    fetcher = fetch_pdf if fetch_pdf is not None else fetch_gazette_pdf
    rows: List[Dict[str, Any]] = []
    async with SessionLocal() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        for sid in ids:
            statute = (await db.execute(select(Statute).where(Statute.id == sid))).scalars().first()
            if statute is None:
                rows.append({"statute_id": str(sid), "found": False, "section_count": 0, "writes": 0})
                continue
            fetch_url, _label = split_statute_source_url(statute.source_url)
            pinned = CONFIRMED_PDF_SHA256.get(statute.id) if statute.id in CONFIRMED_PDF_SHA256 else None
            pdf_text, origin, available = await _resolve_pdf_text(
                fetch_url,
                db=db,
                fetch_pdf=None if skip_fetch else fetcher,
                recorded_loader=recorded_loader,
                extract_text=text_fn,
                skip_fetch=skip_fetch,
                pinned_sha=pinned,
            )
            from scraper.tasks.bundled_gazette_reextract import marked_act_preview, public_preview_row

            rows.append(
                public_preview_row(
                    marked_act_preview(statute, pdf_text, origin=origin, available=available)
                )
            )
    return {"dry_run": True, "writes": 0, "preview_reextract": True, "rows": rows}


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
    parser.add_argument(
        "--preview-reextract",
        action="store_true",
        help=(
            "Re-extract the confirmed bundled-gazette rows, or one --statute-id, and print the result. "
            "Writes nothing. A marker whose Act is not in the PDF produces no sections."
        ),
    )
    parser.add_argument(
        "--apply-reextract",
        action="store_true",
        help=(
            "Replace sections for explicit allowlisted statute UUIDs from the pinned local PDF. "
            "Does not fetch, does not delete the statute, and does not lift quarantine."
        ),
    )
    parser.add_argument(
        "--lift-quarantine",
        action="store_true",
        help=(
            "Delete quarantine rows for explicit allowlisted statute UUIDs, whatever run inserted them. "
            "Does not rewrite sections."
        ),
    )
    parser.add_argument("--undo", metavar="RUN_ID", help="Remove quarantine rows inserted by one --apply run")
    parser.add_argument(
        "--rollback",
        metavar="RUN_ID",
        help="Restore the statute, sections, and quarantine rows backed up by one --apply-reextract or --lift-quarantine run",
    )
    parser.add_argument("--limit", type=int, default=None, help="Classify at most this many in-scope rows")
    parser.add_argument(
        "--statute-id",
        action="append",
        default=None,
        help="Full statute UUID. Repeat for --apply-reextract and --lift-quarantine. Prefixes are refused.",
    )
    parser.add_argument(
        "--expected-sections",
        action="append",
        default=None,
        help="Optional per-id assertion UUID=1,2,3. Apply skips that id when the previewed numbers differ.",
    )
    parser.add_argument(
        "--backup-dir",
        default=None,
        help="Directory for the JSONL backup written before an apply or lift. Default: var/bundled_gazette_reextract",
    )
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


def _preflight(message: str) -> None:
    """A refusal before any backup or write. Exit 2, with the reason on stderr."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _exclusive_mode(args) -> str:
    selected = [
        name
        for name, enabled in (
            ("apply", args.apply),
            ("preview-reextract", args.preview_reextract),
            ("apply-reextract", args.apply_reextract),
            ("lift-quarantine", args.lift_quarantine),
            ("undo", bool(args.undo)),
            ("rollback", bool(args.rollback)),
        )
        if enabled
    ]
    if len(selected) > 1:
        _preflight("pass only one of --apply, --preview-reextract, --apply-reextract, --lift-quarantine, --undo, --rollback")
    return selected[0] if selected else ""


def _explicit_ids(raw_ids: Optional[List[str]]) -> list:
    from scraper.tasks.bundled_gazette_reextract import parse_explicit_statute_id

    try:
        return [parse_explicit_statute_id(value) for value in (raw_ids or [])]
    except ValueError as exc:
        _preflight(str(exc))


def main(argv: Optional[List[str]] = None) -> int:
    from pathlib import Path

    from scraper.tasks.bundled_gazette_reextract import (
        apply_confirmed_reextract,
        lift_confirmed_quarantine,
        parse_expected_sections,
        rollback_reextract,
    )

    parser = build_parser()
    args = parser.parse_args(argv)
    mode = _exclusive_mode(args)
    raw_ids = args.statute_id or []
    if mode in {"apply-reextract", "lift-quarantine"}:
        statute_ids = _explicit_ids(raw_ids)
        if not statute_ids:
            _preflight(f"{mode} requires at least one full --statute-id")
        backup_dir = Path(args.backup_dir) if args.backup_dir else None
        try:
            if mode == "apply-reextract":
                expected = parse_expected_sections(args.expected_sections)
                out = run_async(
                    apply_confirmed_reextract(
                        statute_ids=statute_ids,
                        expected_sections=expected,
                        backup_dir=backup_dir,
                    )
                )
            else:
                if args.expected_sections:
                    _preflight("expected-sections is only valid with --apply-reextract")
                out = run_async(lift_confirmed_quarantine(statute_ids=statute_ids, backup_dir=backup_dir))
        except ValueError as exc:
            _preflight(str(exc))
        print(json.dumps(out, indent=2, default=str))
        return 2 if out.get("refused") else 0
    if mode == "rollback":
        if raw_ids or args.expected_sections:
            _preflight("rollback restores the backup for the run and does not take statute ids")
        try:
            out = run_async(rollback_reextract(args.rollback))
        except ValueError as exc:
            _preflight(str(exc))
        print(json.dumps(out, indent=2, default=str))
        return 0
    if len(raw_ids) > 1:
        _preflight("pass one --statute-id unless using --apply-reextract or --lift-quarantine")
    statute_id = _explicit_ids(raw_ids)[0] if raw_ids else None
    if args.expected_sections:
        _preflight("expected-sections is only valid with --apply-reextract")
    if mode == "preview-reextract":
        out = run_async(
            preview_confirmed_reextract(
                statute_id=statute_id,
                skip_fetch=args.skip_fetch,
            )
        )
        print(json.dumps(out, indent=2, default=str))
        return 0
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
