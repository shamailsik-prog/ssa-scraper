"""Allowlisted re-extract of one bundled-gazette statute.

``--apply`` on the sweep only inserts quarantine. This module is the separate
apply path: an explicit UUID list, the pinned local PDF, the same splitter as
preview, a JSONL backup, and ``--rollback``. It does not fetch, does not delete
the statute, and does not lift quarantine unless ``--lift-quarantine`` is passed
on its own.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.database import SessionLocal
from scraper.fetchers import sha256_text
from scraper.models import (
    CorpusMetadata,
    DocumentQuarantine,
    InstrumentSectionRelation,
    Statute,
    StatuteSection,
    StatuteSectionReextractPrior,
    StatuteSectionVersion,
)
from scraper.tasks.senate_statute_integrity import recorded_pdf_bytes

_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SECTION_NUMBER_RE = re.compile(r"^\d+[A-Za-z]?$")
_BACKUP_KEY_PREFIX = "bundled_reextract_bak:"
_RUN_KEY_PREFIX = "bundled_reextract_run:"
_TITLE_LIMIT = 1000

_STATUTE_FIELDS = (
    "name",
    "canonical_name_key",
    "short_name",
    "jurisdiction",
    "statute_type",
    "year_enacted",
    "is_repealed",
    "repealed_by",
    "source_name",
    "source_url",
    "needs_section_refetch",
    "quality_flag_no_year",
    "quality_flag_duplicate_name",
)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def backup_key(run_id: str) -> str:
    return f"{_BACKUP_KEY_PREFIX}{run_id}"


def run_key(run_id: str) -> str:
    return f"{_RUN_KEY_PREFIX}{run_id}"


def parse_explicit_statute_id(value: str) -> UUID:
    """A full hyphenated UUID. An 8-hex prefix, or a 32-hex string, is refused."""
    raw = (value or "").strip()
    if not _CANONICAL_UUID_RE.fullmatch(raw):
        raise ValueError(f"statute id must be a full UUID with hyphens, not a prefix: {raw[:80]}")
    return UUID(raw)


def confirmed_pin(statute_id: UUID) -> Optional[str]:
    """The pinned sha, or None when this id is not an allowlisted authentic file."""
    from scraper.tasks.bundled_gazette_acts import CONFIRMED_BLEED_STATUTE_IDS, CONFIRMED_PDF_SHA256

    if statute_id not in CONFIRMED_BLEED_STATUTE_IDS:
        return None
    pin = CONFIRMED_PDF_SHA256.get(statute_id) or ""
    pin = pin.strip().lower()
    if len(pin) != 64 or any(char not in "0123456789abcdef" for char in pin):
        return None
    return pin


def require_confirmed_ids(statute_ids: Sequence[UUID]) -> dict[UUID, str]:
    """Refuse the whole call when any id is missing from the allowlist or has no pin."""
    if not statute_ids:
        raise ValueError("pass at least one full statute UUID")
    pins: dict[UUID, str] = {}
    for statute_id in statute_ids:
        pin = confirmed_pin(statute_id)
        if pin is None:
            raise ValueError(
                f"statute id is not an allowlisted pinned bundled-gazette row: {statute_id}"
            )
        pins[statute_id] = pin
    return pins


def parse_expected_sections(items: Optional[Sequence[str]]) -> dict[UUID, list[str]]:
    """``UUID=1,2,3`` assertions. The id must be a full UUID, listed once."""
    expected: dict[UUID, list[str]] = {}
    for item in items or []:
        if "=" not in (item or ""):
            raise ValueError("expected-sections must be UUID=1,2,3")
        raw_id, raw_numbers = item.split("=", 1)
        statute_id = parse_explicit_statute_id(raw_id.strip())
        numbers = [part.strip() for part in raw_numbers.split(",") if part.strip()]
        if statute_id in expected or not numbers:
            raise ValueError("expected-sections needs a unique id and at least one section number")
        if any(_SECTION_NUMBER_RE.fullmatch(number) is None for number in numbers):
            raise ValueError("expected section numbers must be plain section numbers")
        expected[statute_id] = numbers
    return expected


def marked_act_preview(statute: Statute, pdf_text: Optional[str], *, origin: str, available: bool) -> dict:
    """The same in-memory extract ``--preview-reextract`` prints. Writes nothing."""
    from scraper.extractors.deterministic import extract_statute_deterministic
    from scraper.statute_quality.bundled_gazette import gazette_bundle_hold_reason

    base = {
        "statute_id": str(statute.id),
        "found": True,
        "stored_name": statute.name,
        "source_url": statute.source_url,
        "pdf_origin": origin,
        "writes": 0,
    }
    if not available:
        base.update(
            {
                "section_count": 0,
                "section_numbers": [],
                "sections": [],
                "hold_reason": "recorded_sha_mismatch" if origin == "sha_mismatch" else "pdf_unavailable",
            }
        )
        return base
    extracted = extract_statute_deterministic(
        html=None,
        text=pdf_text,
        source_meta={
            "source_name": statute.source_name,
            "url": statute.source_url,
            "act_title": statute.name,
            "jurisdiction": statute.jurisdiction,
        },
    )
    extracted_sections = list(extracted.get("sections") or [])
    evidence = extracted.get("field_evidence") or {}
    hold = gazette_bundle_hold_reason(
        statute_name=str(extracted.get("statute_name") or ""),
        year_enacted=extracted.get("year_enacted"),
        jurisdiction=extracted.get("jurisdiction") or statute.jurisdiction,
        source_url=statute.source_url,
        source_name=statute.source_name,
        sections=extracted_sections,
    )
    if not hold and evidence.get("bundled_gazette_marker_absent"):
        hold = "bundled_gazette_marker_absent"
    elif not hold and evidence.get("bundled_gazette_unresolved"):
        hold = "bundled_gazette_unresolved"
    published_name = extracted.get("statute_name")
    if hold:
        extracted_sections = []
        published_name = statute.name
    base.update(
        {
            "statute_name": published_name,
            "year_enacted": extracted.get("year_enacted"),
            "section_count": len(extracted_sections),
            "section_numbers": [section.get("section_number") for section in extracted_sections],
            "hold_reason": hold,
            "marker_absent": bool(evidence.get("bundled_gazette_marker_absent")),
            "sections": extracted_sections,
        }
    )
    return base


def public_preview_row(row: dict) -> dict:
    """Drop section bodies from the preview printout. The command writes nothing."""
    published = dict(row)
    published.pop("sections", None)
    return published


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _embedding(value: Any) -> Optional[list[float]]:
    if value is None:
        return None
    return [float(item) for item in list(value)]


def _title(value: Optional[str]) -> str:
    return (value or "")[:_TITLE_LIMIT]


def _proposed_signature(sections: Sequence[dict]) -> list[tuple]:
    signed = []
    for index, section in enumerate(sections, start=1):
        signed.append(
            (
                str(section.get("section_number") or ""),
                _title(section.get("section_title")),
                section.get("section_text") or "",
                index,
            )
        )
    return signed


async def _snapshot_statute(db: AsyncSession, statute: Statute) -> dict:
    sections = (
        await db.execute(
            select(StatuteSection)
            .where(StatuteSection.statute_id == statute.id)
            .order_by(StatuteSection.sort_key, StatuteSection.section_number)
        )
    ).scalars().all()
    section_ids = [section.id for section in sections]
    version_rows = []
    if section_ids:
        version_rows = (
            await db.execute(
                select(StatuteSectionVersion).where(StatuteSectionVersion.section_id.in_(section_ids))
            )
        ).scalars().all()
    versions_by: dict[UUID, list] = {}
    for version in version_rows:
        versions_by.setdefault(version.section_id, []).append(version)
    quarantine_rows = (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id == statute.id,
            )
        )
    ).scalars().all()
    priors = (
        await db.execute(
            select(StatuteSectionReextractPrior).where(StatuteSectionReextractPrior.statute_id == statute.id)
        )
    ).scalars().all()
    relations = []
    if section_ids:
        relations = (
            await db.execute(
                select(InstrumentSectionRelation).where(
                    InstrumentSectionRelation.target_statute_section_id.in_(section_ids)
                )
            )
        ).scalars().all()
    section_payload = []
    for section in sections:
        current_id = str(section.current_version_id) if section.current_version_id else None
        version_payload = []
        current_text = ""
        for version in versions_by.get(section.id, []):
            version_payload.append(
                {
                    "id": str(version.id),
                    "version_no": version.version_no,
                    "section_text": version.section_text,
                    "text_hash": version.text_hash,
                    "effective_from": version.effective_from.isoformat() if version.effective_from else None,
                    "effective_to": version.effective_to.isoformat() if version.effective_to else None,
                    "amending_instrument_id": str(version.amending_instrument_id) if version.amending_instrument_id else None,
                    "amending_instrument_text": version.amending_instrument_text,
                    "version_confidence": version.version_confidence,
                    "source_provenance_id": str(version.source_provenance_id) if version.source_provenance_id else None,
                }
            )
            if current_id and str(version.id) == current_id:
                current_text = version.section_text or ""
        section_payload.append(
            {
                "id": str(section.id),
                "section_number": section.section_number,
                "section_title": section.section_title,
                "chapter": section.chapter,
                "part": section.part,
                "sort_key": section.sort_key,
                "embedding": _embedding(section.embedding),
                "current_version_id": current_id,
                "section_text": current_text,
                "versions": version_payload,
            }
        )
    return {
        "statute_id": str(statute.id),
        "statute": {field: getattr(statute, field) for field in _STATUTE_FIELDS},
        "sections": section_payload,
        "quarantine": [
            {
                "doc_type": row.doc_type,
                "doc_id": str(row.doc_id),
                "reason": row.reason,
                "run_id": row.run_id,
                "quarantined_at": _iso(row.quarantined_at),
            }
            for row in quarantine_rows
        ],
        "priors": [
            {
                "section_id": str(row.section_id),
                "statute_id": str(row.statute_id),
                "section_number": row.section_number,
                "section_text": row.section_text,
                "sort_key": row.sort_key,
                "captured_at": _iso(row.captured_at),
            }
            for row in priors
        ],
        "section_relations": [
            {"id": str(row.id), "target_statute_section_id": str(row.target_statute_section_id)}
            for row in relations
            if row.target_statute_section_id is not None
        ],
    }


def _live_signature(snapshot: dict) -> list[tuple]:
    rows = []
    for section in snapshot["sections"]:
        rows.append(
            (
                str(section.get("section_number") or ""),
                _title(section.get("section_title")),
                section.get("section_text") or "",
                section.get("sort_key"),
            )
        )
    return rows


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _rewrite_jsonl(path: Path, payloads: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n" for payload in payloads)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(path)


async def _store_run_record(db: AsyncSession, *, run_id: str, kind: str, backup_path: str, snapshots: Sequence[dict]) -> None:
    record = {
        "run_id": run_id,
        "kind": kind,
        "backup_path": backup_path,
        "statutes": list(snapshots),
    }
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    for key in (backup_key(run_id), run_key(run_id)):
        row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == key))).scalars().first()
        if row is None:
            db.add(CorpusMetadata(key=key, value=encoded))
        else:
            row.value = encoded
    await db.flush()


async def _load_run_record(db: AsyncSession, run_id: str) -> Optional[dict]:
    row = (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == backup_key(run_id)))).scalars().first()
    if row is None or not row.value:
        return None
    return json.loads(row.value)


async def _replace_sections(db: AsyncSession, statute_id: UUID, sections: Sequence[dict]) -> None:
    await db.execute(delete(StatuteSection).where(StatuteSection.statute_id == statute_id))
    await db.flush()
    for index, section in enumerate(sections, start=1):
        title = _title(section.get("section_title")) or None
        row = StatuteSection(
            statute_id=statute_id,
            section_number=str(section.get("section_number") or "")[:100],
            section_title=title,
            sort_key=index,
        )
        db.add(row)
        await db.flush()
        text = section.get("section_text") or ""
        version = StatuteSectionVersion(
            section_id=row.id,
            version_no=1,
            section_text=text,
            text_hash=sha256_text(text),
            version_confidence=0.9 if len(text) > 20 else 0.5,
        )
        db.add(version)
        await db.flush()
        row.current_version_id = version.id
    await db.flush()


async def _restore_snapshot(db: AsyncSession, snapshot: dict) -> None:
    statute_id = UUID(snapshot["statute_id"])
    statute = (await db.execute(select(Statute).where(Statute.id == statute_id))).scalars().first()
    if statute is None:
        raise ValueError(f"rollback cannot recreate a statute row: {statute_id}")
    for field in _STATUTE_FIELDS:
        if field in snapshot.get("statute") or {}:
            current = getattr(statute, field)
            backed = snapshot["statute"][field]
            if current != backed:
                setattr(statute, field, backed)
    await db.execute(delete(StatuteSection).where(StatuteSection.statute_id == statute_id))
    await db.flush()
    await db.execute(delete(StatuteSectionReextractPrior).where(StatuteSectionReextractPrior.statute_id == statute_id))
    await db.execute(
        delete(DocumentQuarantine).where(
            DocumentQuarantine.doc_type == "statute",
            DocumentQuarantine.doc_id == statute_id,
        )
    )
    await db.flush()
    for section in snapshot.get("sections") or []:
        row = StatuteSection(
            id=UUID(section["id"]),
            statute_id=statute_id,
            section_number=section["section_number"],
            section_title=section.get("section_title"),
            chapter=section.get("chapter"),
            part=section.get("part"),
            sort_key=section.get("sort_key"),
            embedding=section.get("embedding"),
        )
        db.add(row)
        await db.flush()
        current_id = UUID(section["current_version_id"]) if section.get("current_version_id") else None
        for version in section.get("versions") or []:
            db.add(
                StatuteSectionVersion(
                    id=UUID(version["id"]),
                    section_id=row.id,
                    version_no=int(version["version_no"]),
                    section_text=version.get("section_text") or "",
                    text_hash=version["text_hash"],
                    effective_from=_parse_date(version.get("effective_from")),
                    effective_to=_parse_date(version.get("effective_to")),
                    amending_instrument_id=UUID(version["amending_instrument_id"]) if version.get("amending_instrument_id") else None,
                    amending_instrument_text=version.get("amending_instrument_text"),
                    version_confidence=float(version.get("version_confidence") if version.get("version_confidence") is not None else 0.5),
                    source_provenance_id=UUID(version["source_provenance_id"]) if version.get("source_provenance_id") else None,
                )
            )
        await db.flush()
        row.current_version_id = current_id
    for prior in snapshot.get("priors") or []:
        db.add(
            StatuteSectionReextractPrior(
                section_id=UUID(prior["section_id"]),
                statute_id=UUID(prior["statute_id"]),
                section_number=prior["section_number"],
                section_text=prior.get("section_text") or "",
                sort_key=prior.get("sort_key"),
                captured_at=_parse_dt(prior.get("captured_at")) or datetime.now(timezone.utc),
            )
        )
    for quarantine in snapshot.get("quarantine") or []:
        db.add(
            DocumentQuarantine(
                doc_type=quarantine["doc_type"],
                doc_id=UUID(quarantine["doc_id"]),
                reason=quarantine["reason"],
                run_id=quarantine["run_id"],
                quarantined_at=_parse_dt(quarantine.get("quarantined_at")) or datetime.now(timezone.utc),
            )
        )
    await db.flush()
    for relation in snapshot.get("section_relations") or []:
        target = UUID(relation["target_statute_section_id"]) if relation.get("target_statute_section_id") else None
        row = (
            await db.execute(select(InstrumentSectionRelation).where(InstrumentSectionRelation.id == UUID(relation["id"])))
        ).scalars().first()
        if row is not None:
            row.target_statute_section_id = target
    await db.flush()


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _snapshot_matches_live(snapshot: dict, live: dict) -> bool:
    """True when the stored statute, sections, and quarantine already equal this backup."""
    if _live_signature(snapshot) != _live_signature(live):
        return False
    backed_q = [
        (row["doc_type"], row["doc_id"], row["reason"], row["run_id"])
        for row in snapshot.get("quarantine") or []
    ]
    live_q = [
        (row["doc_type"], row["doc_id"], row["reason"], row["run_id"])
        for row in live.get("quarantine") or []
    ]
    if backed_q != live_q:
        return False
    backed_statute = snapshot.get("statute") or {}
    live_statute = live.get("statute") or {}
    return all(backed_statute.get(field) == live_statute.get(field) for field in _STATUTE_FIELDS)


async def _open_recorded_pdf(
    db: AsyncSession,
    fetch_url: str,
    *,
    pin: str,
    recorded_loader,
) -> tuple[Optional[bytes], str]:
    """Local recorded bytes only. A missing file or a sha mismatch is a refusal. Never fetches."""
    if recorded_loader is not None:
        data = await _maybe_await(recorded_loader(fetch_url))
    else:
        data = await recorded_pdf_bytes(db, fetch_url)
    if not data or not bytes(data).startswith(b"%PDF"):
        return None, "pdf_missing"
    digest = _sha256(bytes(data))
    if digest.lower() != pin.lower():
        return None, "sha_mismatch"
    return bytes(data), "recorded"


def _deciding_preview(preview: dict, expected: Optional[Sequence[str]]) -> tuple[str, str]:
    """``ready`` or a skip/refusal status, plus the reason to print."""
    if preview.get("hold_reason"):
        return "skipped", str(preview["hold_reason"])
    sections = list(preview.get("sections") or [])
    if not sections:
        return "skipped", "empty_sections"
    numbers = [str(section.get("section_number") or "") for section in sections]
    if expected is not None and numbers != list(expected):
        return "skipped", "expected_sections_mismatch"
    return "ready", ""


async def apply_confirmed_reextract(
    *,
    statute_ids: Sequence[UUID],
    expected_sections: Optional[dict[UUID, list[str]]] = None,
    backup_dir: Optional[Path] = None,
    recorded_loader=None,
    extract_text=None,
) -> dict:
    """Replace sections for allowlisted ids whose preview is clean. One transaction each.

    A sha mismatch refuses that id and does not fetch. A hold, an empty section
    list, or an expected-section miss skips that id. A second run with the same
    sections writes nothing.
    """
    from scraper.fetchers import pdf_text_layer
    from scraper.statute_quality.senate_integrity import split_statute_source_url

    pins = require_confirmed_ids(statute_ids)
    expected_sections = expected_sections or {}
    unknown = [str(statute_id) for statute_id in expected_sections if statute_id not in pins]
    if unknown:
        raise ValueError("expected-sections names an id that was not passed: " + ", ".join(unknown))
    text_fn = extract_text or pdf_text_layer
    directory = Path(backup_dir) if backup_dir is not None else Path("var/bundled_gazette_reextract")
    rows: list[dict] = []
    ready: list[dict] = []
    async with SessionLocal() as db:
        for statute_id in statute_ids:
            statute = (await db.execute(select(Statute).where(Statute.id == statute_id))).scalars().first()
            if statute is None:
                rows.append(
                    {
                        "statute_id": str(statute_id),
                        "status": "refused",
                        "reason": "not_found",
                        "writes": 0,
                    }
                )
                continue
            fetch_url, _label = split_statute_source_url(statute.source_url)
            data, origin = await _open_recorded_pdf(
                db, fetch_url, pin=pins[statute_id], recorded_loader=recorded_loader
            )
            if data is None:
                rows.append(
                    {
                        "statute_id": str(statute_id),
                        "status": "refused",
                        "reason": origin,
                        "writes": 0,
                    }
                )
                continue
            try:
                pdf_text = text_fn(data) or ""
            except Exception:
                pdf_text = ""
            preview = marked_act_preview(
                statute,
                pdf_text,
                origin="recorded",
                available=bool(str(pdf_text).strip()),
            )
            status, reason = _deciding_preview(preview, expected_sections.get(statute_id))
            if status != "ready":
                rows.append(
                    {
                        "statute_id": str(statute_id),
                        "status": status,
                        "reason": reason,
                        "section_numbers": preview.get("section_numbers") or [],
                        "hold_reason": preview.get("hold_reason"),
                        "writes": 0,
                    }
                )
                continue
            snapshot = await _snapshot_statute(db, statute)
            proposed = _proposed_signature(preview["sections"])
            if _live_signature(snapshot) == [
                (number, title, text, sort_key) for number, title, text, sort_key in proposed
            ]:
                rows.append(
                    {
                        "statute_id": str(statute_id),
                        "status": "unchanged",
                        "reason": "",
                        "section_numbers": preview.get("section_numbers") or [],
                        "writes": 0,
                    }
                )
                continue
            ready.append({"statute": statute, "preview": preview, "snapshot": snapshot, "statute_id": statute_id})
        if not ready:
            refused = any(row["status"] == "refused" for row in rows)
            return {
                "dry_run": False,
                "apply_reextract": True,
                "writes": 0,
                "refused": refused,
                "run_id": None,
                "backup_path": None,
                "rows": rows,
            }
        run_id = uuid.uuid4().hex
        path = directory / f"{run_id}.jsonl"
        committed: list[dict] = []
        for item in ready:
            payload = {
                "run_id": run_id,
                "kind": "apply",
                "statute_id": str(item["statute_id"]),
                "snapshot": item["snapshot"],
            }
            _append_jsonl(path, payload)
            appended = False
            try:
                async with SessionLocal() as write_db:
                    await _replace_sections(write_db, item["statute_id"], item["preview"]["sections"])
                    committed.append(payload)
                    appended = True
                    await _store_run_record(
                        write_db,
                        run_id=run_id,
                        kind="apply",
                        backup_path=str(path),
                        snapshots=[entry["snapshot"] for entry in committed],
                    )
                    await write_db.commit()
            except Exception:
                if appended:
                    committed.pop()
                _rewrite_jsonl(path, committed)
                raise
        return {
            "dry_run": False,
            "apply_reextract": True,
            "writes": len(committed),
            "refused": any(row["status"] == "refused" for row in rows),
            "run_id": run_id,
            "backup_path": str(path),
            "rollback_command": f"python -m scraper.tasks.bundled_gazette_acts --rollback {run_id}",
            "rows": rows
            + [
                {
                    "statute_id": str(item["statute_id"]),
                    "status": "applied",
                    "reason": "",
                    "section_numbers": item["preview"].get("section_numbers") or [],
                    "writes": 1,
                }
                for item in ready
            ],
        }


async def lift_confirmed_quarantine(
    *,
    statute_ids: Sequence[UUID],
    backup_dir: Optional[Path] = None,
) -> dict:
    """Delete quarantine rows for allowlisted ids, whatever run inserted them.

    Sections are not rewritten. A second call, when the row is already gone,
    writes nothing.
    """
    require_confirmed_ids(statute_ids)
    directory = Path(backup_dir) if backup_dir is not None else Path("var/bundled_gazette_reextract")
    rows: list[dict] = []
    ready: list[dict] = []
    async with SessionLocal() as db:
        for statute_id in statute_ids:
            statute = (await db.execute(select(Statute).where(Statute.id == statute_id))).scalars().first()
            if statute is None:
                rows.append({"statute_id": str(statute_id), "status": "refused", "reason": "not_found", "writes": 0})
                continue
            snapshot = await _snapshot_statute(db, statute)
            if not snapshot["quarantine"]:
                rows.append({"statute_id": str(statute_id), "status": "unchanged", "reason": "", "writes": 0})
                continue
            ready.append({"statute_id": statute_id, "snapshot": snapshot})
        if not ready:
            return {
                "dry_run": False,
                "lift_quarantine": True,
                "writes": 0,
                "refused": any(row["status"] == "refused" for row in rows),
                "run_id": None,
                "backup_path": None,
                "rows": rows,
            }
        run_id = uuid.uuid4().hex
        path = directory / f"{run_id}.jsonl"
        committed: list[dict] = []
        for item in ready:
            payload = {
                "run_id": run_id,
                "kind": "lift",
                "statute_id": str(item["statute_id"]),
                "snapshot": item["snapshot"],
            }
            _append_jsonl(path, payload)
            appended = False
            try:
                async with SessionLocal() as write_db:
                    await write_db.execute(
                        delete(DocumentQuarantine).where(
                            DocumentQuarantine.doc_type == "statute",
                            DocumentQuarantine.doc_id == item["statute_id"],
                        )
                    )
                    committed.append(payload)
                    appended = True
                    await _store_run_record(
                        write_db,
                        run_id=run_id,
                        kind="lift",
                        backup_path=str(path),
                        snapshots=[entry["snapshot"] for entry in committed],
                    )
                    await write_db.commit()
            except Exception:
                if appended:
                    committed.pop()
                _rewrite_jsonl(path, committed)
                raise
        return {
            "dry_run": False,
            "lift_quarantine": True,
            "writes": len(committed),
            "refused": any(row["status"] == "refused" for row in rows),
            "run_id": run_id,
            "backup_path": str(path),
            "rollback_command": f"python -m scraper.tasks.bundled_gazette_acts --rollback {run_id}",
            "rows": rows
            + [
                {"statute_id": str(item["statute_id"]), "status": "lifted", "reason": "", "writes": 1}
                for item in ready
            ],
        }


async def rollback_reextract(run_id: str) -> dict:
    """Put back the statute, sections, and quarantine rows stored for ``run_id``.

    A second rollback of the same run writes nothing once the rows already match
    the backup. Other statutes are not read for update.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", run_id or ""):
        raise ValueError("rollback run_id must be the 32-hex id printed by apply or lift")
    async with SessionLocal() as db:
        record = await _load_run_record(db, run_id)
        if record is None:
            raise ValueError(f"no re-extract backup for run_id {run_id}")
        restored = []
        unchanged = []
        for snapshot in record.get("statutes") or []:
            statute_id = UUID(snapshot["statute_id"])
            statute = (await db.execute(select(Statute).where(Statute.id == statute_id))).scalars().first()
            if statute is None:
                raise ValueError(f"rollback cannot recreate a statute row: {statute_id}")
            live = await _snapshot_statute(db, statute)
            if _snapshot_matches_live(snapshot, live):
                unchanged.append(str(statute_id))
                continue
            await _restore_snapshot(db, snapshot)
            restored.append(str(statute_id))
        await db.commit()
    return {
        "dry_run": False,
        "rollback": True,
        "run_id": run_id,
        "restored": restored,
        "unchanged": unchanged,
        "writes": len(restored),
    }
