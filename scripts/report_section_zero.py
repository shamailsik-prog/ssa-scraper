#!/usr/bin/env python3
"""List visible statutes whose section_number is 0, and propose a merge.

Dry-run is the default. It writes a backup JSONL and a rollback SQL file and
does not change the database. ``--apply`` merges one section into the
preceding section only after an id and md5 precheck against that backup.

``--csv`` classifies an evidence export and does not open a database and does
not write a file. Each stored section 0 is one of:

- A: a ``0)`` list clause. The missing letter is not assumed. The next clause
  may be ``(k)``, ``(m)`` or anything else. The line continues the preceding
  section.
- B: a decimal that was split on the point, such as ``0.2 per cent``,
  ``0.5%`` or ``0.1%``.
- C: ``0.`` citation or OCR junk that is not a decimal, such as ``0. No.``
  or ``0. [)isputc``.
- unknown: a section 0 whose text matches none of those prefixes.

A merge proposal names the preceding section. ``before_md5`` is the MD5 of
that section's stored text. ``after_md5`` is the MD5 of the text after the
section 0 body is appended. The CSV has only the section 0 row, so those two
digests stay empty until a database dry-run can see the preceding section.

Do not point this at a database until a person has read the dry-run. This
script does not unquarantine, does not deploy, and does not fetch source PDFs.

    python scripts/report_section_zero.py --csv tests/fixtures/statute_quality/section_zero_fs.csv --csv tests/fixtures/statute_quality/section_zero_main.csv
    python scripts/report_section_zero.py --out section_zero_reports
    python scripts/report_section_zero.py --apply --id <section-uuid> --md5 <md5> --out section_zero_reports

``--md5`` is the MD5 (32 hex characters) of the section 0 text, UTF-8, as
printed in the dry-run and stored in the backup JSONL. A mismatch refuses
the write.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.corpus_visibility import statute_servable
from scraper.database import SessionLocal, run_async
from scraper.fetchers import sha256_text
from scraper.models import (
    EmbeddingQueue,
    InstrumentSectionRelation,
    Statute,
    StatuteSection,
    StatuteSectionReextractPrior,
    StatuteSectionVersion,
)
from scraper.statute_quality.year_section_merge import source_host

BACKUP_NAME = "section_zero_backup.jsonl"
ROLLBACK_NAME = "section_zero_rollback.sql"
_MD5_RE = re.compile(r"[0-9a-f]{32}")


class SectionZeroPrecheckError(RuntimeError):
    """The live row does not match the id and md5 from the dry-run backup."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report visible statutes with section_number 0 and propose merging "
            "each one into the preceding section. Default is dry-run. "
            "--apply writes one merge only when --id and --md5 match the backup."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Merge one section. Requires --id and --md5. Omit for a dry-run.",
    )
    parser.add_argument("--id", default="", help="Section id of the section_number 0 row to merge.")
    parser.add_argument("--md5", default="", help="MD5 hex of that section's current text.")
    parser.add_argument(
        "--out",
        default="section_zero_reports",
        help="Directory for the backup JSONL and rollback SQL (default: section_zero_reports).",
    )
    parser.add_argument(
        "--backup",
        default="",
        help="Backup JSONL to precheck on --apply. Default: <out>/section_zero_backup.jsonl.",
    )
    parser.add_argument(
        "--csv",
        action="append",
        default=[],
        help=(
            "Classify section 0 rows in this evidence CSV. Repeat for each file. "
            "Does not open a database and does not write a file."
        ),
    )
    return parser


def section_text_md5(text: str) -> str:
    """MD5 of the section text as stored, UTF-8, with no whitespace rewrite."""
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


# Prefixes of the three stored shapes. Each is linear and anchored at the start.
_CLASS_A_RE = re.compile(r"0\)")
_CLASS_B_RE = re.compile(r"0\.\d")
_CLASS_C_RE = re.compile(r"0\s*\.(?!\d)")
SECTION_ZERO_CLASSES = ("A", "B", "C", "unknown")


def classify_section_zero_text(text: str) -> str:
    """Sort a section 0 body into A, B, C, or unknown.

    A is a ``0)`` list clause, whatever letter it stood for. B is a decimal
    (``0.2 per cent``, ``0.5%``, ``0.1%``, ``0.1 million``, ``0.09``). C is
    ``0.`` junk that is not a decimal (``0. No.``, ``0. [)isputc``, ``0. 1993``,
    ``0 . iby``, ``0..1033``).
    """
    raw = (text or "").lstrip()
    if _CLASS_A_RE.match(raw):
        return "A"
    if _CLASS_B_RE.match(raw):
        return "B"
    if _CLASS_C_RE.match(raw):
        return "C"
    return "unknown"


def propose_section_zero_merge(
    zero_text: str,
    preceding: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Name the preceding section and the before/after MD5s of a merge.

    ``before_md5`` is the preceding section as stored. ``after_md5`` is that
    text with the section 0 body appended. Both stay empty when there is no
    preceding section. This function does not write.
    """
    text = zero_text or ""
    proposal: Dict[str, Any] = {
        "section_zero_class": classify_section_zero_text(text),
        "zero_md5": section_text_md5(text) if text else None,
        "into_section_id": None,
        "into_section_number": None,
        "before_md5": None,
        "after_md5": None,
        "merged_text": "",
        "proposal": "skip",
        "skip_reason": "empty" if not text.strip() else "no_preceding_section",
    }
    if not text.strip() or preceding is None:
        return proposal
    base = str(preceding.get("text") or "")
    merged = base.rstrip() + "\n" + text.lstrip()
    proposal.update(
        {
            "into_section_id": str(preceding.get("id")) if preceding.get("id") is not None else None,
            "into_section_number": str(preceding.get("section_number") or "").strip() or None,
            "before_md5": section_text_md5(base),
            "after_md5": section_text_md5(merged),
            "merged_text": merged,
            "proposal": "merge",
            "skip_reason": None,
        }
    )
    return proposal


def classify_section_zero_csv(path: Path) -> List[Dict[str, Any]]:
    """Classify every section 0 row in an evidence CSV. No database and no writes.

    The CSV carries the section 0 text only, so the merge target is the
    preceding section when ``sort_key`` is after the first row, and the
    before/after MD5s stay empty.
    """
    rows: List[Dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            text = record.get("first80") or ""
            sort_raw = (record.get("sort_key") or "").strip()
            try:
                sort_key = int(sort_raw)
            except ValueError:
                sort_key = None
            has_previous = sort_key is not None and sort_key > 1
            kind = classify_section_zero_text(text)
            rows.append(
                {
                    "source_csv": path.name,
                    "statute_id": record.get("statute_id") or "",
                    "statute_name": record.get("name") or "",
                    "section_number": (record.get("section_number") or "").strip(),
                    "sort_key": sort_key,
                    "section_zero_class": kind,
                    "zero_md5": (record.get("md5") or "").strip().lower(),
                    "snippet_md5": section_text_md5(text),
                    "proposed_merge_target": "preceding section" if has_previous else None,
                    "into_section_id": None,
                    "into_section_number": None,
                    "before_md5": None,
                    "after_md5": None,
                    "proposal": "merge" if has_previous else "skip",
                    "skip_reason": None if has_previous else "no_preceding_section",
                }
            )
    return rows


def _order_key(section: Mapping[str, Any]) -> tuple:
    sort_key = section.get("sort_key")
    created = section.get("created_at")
    return (
        sort_key is None,
        sort_key if sort_key is not None else 0,
        str(created or ""),
        str(section.get("id") or ""),
    )


def _is_section_zero(number: str) -> bool:
    return (number or "").strip() == "0"


def plan_section_zero_merges(sections: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Propose a merge of each section_number ``0`` into the preceding section.

    Order is ``sort_key``, then ``created_at``, then ``id``. A leading ``0``
    has no preceding section and is left in place. Empty text is left in place.
    """
    ordered = sorted(sections, key=_order_key)
    merged: List[Dict[str, Any]] = []
    skipped_no_previous: List[Dict[str, Any]] = []
    skipped_empty: List[Dict[str, Any]] = []
    merged_text_by_target: Dict[str, str] = {}
    last_real: Optional[Mapping[str, Any]] = None

    for section in ordered:
        number = str(section.get("section_number") or "")
        if not _is_section_zero(number):
            last_real = section
            continue
        entry = {
            "zero_section_id": str(section.get("id")),
            "zero_section_number": number.strip(),
        }
        text = str(section.get("text") or "")
        if not text.strip():
            entry.update(propose_section_zero_merge(text, None))
            skipped_empty.append(entry)
            continue
        if last_real is None:
            entry.update(propose_section_zero_merge(text, None))
            skipped_no_previous.append(entry)
            continue
        target_id = str(last_real.get("id"))
        base = merged_text_by_target.get(target_id, str(last_real.get("text") or ""))
        preceding = {"id": target_id, "section_number": last_real.get("section_number"), "text": base}
        proposed = propose_section_zero_merge(text, preceding)
        merged_text = proposed["merged_text"]
        merged_text_by_target[target_id] = merged_text
        merged.append({**entry, **proposed})
    return {
        "merged": merged,
        "skipped_no_previous": skipped_no_previous,
        "skipped_empty": skipped_empty,
        "merged_text_by_target": merged_text_by_target,
    }


def merged_text_hash(merged_text: str) -> str:
    """Same whitespace-normalised SHA-256 the section version row stores."""
    return sha256_text(" ".join((merged_text or "").split()))


def _dollar_quote(text: str) -> str:
    tag = "sz"
    while f"${tag}$" in (text or ""):
        tag += "x"
    return f"${tag}${text or ''}${tag}$"


def _sql_uuid(value: str) -> str:
    return "'" + str(UUID(str(value))) + "'"


def render_rollback_sql(rows: Sequence[Mapping[str, Any]]) -> str:
    """SQL an operator can run to put merged rows back. This script never runs it."""
    lines = [
        "-- section-zero rollback. report_section_zero.py does not execute this file.",
        "-- Run one block only for a merge that --apply wrote, after reading it.",
        "-- Each block restores the preceding section's version pointer, removes the",
        "-- merged version, and inserts the section_number 0 row from the backup.",
        "",
    ]
    merges = [row for row in rows if row.get("proposal") == "merge"]
    if not merges:
        lines.append("-- no merges proposed")
        lines.append("")
        return "\n".join(lines)

    for row in merges:
        zero = row["zero_section"]
        into = row["preceding_section"]
        zero_id = _sql_uuid(zero["id"])
        into_id = _sql_uuid(into["id"])
        statute_id = _sql_uuid(row["statute_id"])
        old_version = _sql_uuid(into["version_id"]) if into.get("version_id") else "NULL"
        zero_version = _sql_uuid(zero["version_id"]) if zero.get("version_id") else None
        merged_hash = str(row.get("merged_text_hash") or "")
        old_version_no = int(into.get("version_no") or 0)
        relation_ids = [str(UUID(item)) for item in row.get("relation_ids") or []]
        title_sql = "NULL" if zero.get("section_title") is None else _dollar_quote(str(zero.get("section_title")))
        chapter_sql = "NULL" if not zero.get("chapter") else _dollar_quote(str(zero.get("chapter")))
        part_sql = "NULL" if not zero.get("part") else _dollar_quote(str(zero.get("part")))
        sort_sql = "NULL" if zero.get("sort_key") is None else str(int(zero["sort_key"]))
        lines.append("BEGIN;")
        lines.append(
            f"UPDATE statute_section SET current_version_id = {old_version} "
            f"WHERE id = {into_id};"
        )
        if merged_hash and into.get("version_id"):
            lines.append(
                "DELETE FROM statute_section_version "
                f"WHERE section_id = {into_id} "
                f"AND text_hash = {_dollar_quote(merged_hash)} "
                f"AND id <> {old_version} "
                f"AND version_no > {old_version_no};"
            )
        lines.append(
            "INSERT INTO statute_section "
            "(id, statute_id, section_number, section_title, chapter, part, sort_key, current_version_id) "
            f"VALUES ({zero_id}, {statute_id}, '0', {title_sql}, {chapter_sql}, {part_sql}, {sort_sql}, NULL);"
        )
        if zero_version is not None:
            lines.append(
                "INSERT INTO statute_section_version "
                "(id, section_id, version_no, section_text, text_hash, version_confidence) "
                f"VALUES ({zero_version}, {zero_id}, {int(zero.get('version_no') or 1)}, "
                f"{_dollar_quote(str(zero.get('section_text') or ''))}, "
                f"{_dollar_quote(str(zero.get('text_hash') or ''))}, "
                f"{float(zero.get('version_confidence') if zero.get('version_confidence') is not None else 0.5)});"
            )
            lines.append(
                f"UPDATE statute_section SET current_version_id = {zero_version} WHERE id = {zero_id};"
            )
        if relation_ids:
            id_list = ", ".join(_sql_uuid(item) for item in relation_ids)
            lines.append(
                "UPDATE instrument_section_relation "
                f"SET target_statute_section_id = {zero_id} WHERE id IN ({id_list});"
            )
        lines.append(f"DELETE FROM statute_section_reextract_prior WHERE section_id = {zero_id};")
        lines.append("COMMIT;")
        lines.append("")
    return "\n".join(lines)


def write_report_files(out_dir: Path, rows: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    backup = out_dir / BACKUP_NAME
    rollback = out_dir / ROLLBACK_NAME
    body = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n" for row in rows
    )
    backup.write_text(body, encoding="utf-8")
    rollback.write_text(render_rollback_sql(rows), encoding="utf-8")
    return {"backup_jsonl": str(backup), "rollback_sql": str(rollback)}


def load_backup_rows(path: Path) -> Dict[str, Mapping[str, Any]]:
    if not path.is_file():
        raise SectionZeroPrecheckError(f"backup JSONL not found: {path}")
    found: Dict[str, Mapping[str, Any]] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SectionZeroPrecheckError(f"backup line {line_no} is not JSON") from exc
        zero = row.get("zero_section") or {}
        zero_id = str(zero.get("id") or "")
        if not zero_id:
            raise SectionZeroPrecheckError(f"backup line {line_no} has no section id")
        found[zero_id] = row
    return found


def _norm_md5(value: str) -> str:
    raw = (value or "").strip().lower()
    if not _MD5_RE.fullmatch(raw):
        raise SectionZeroPrecheckError("md5 must be 32 hex characters")
    return raw


def precheck_section_zero(
    *,
    claimed_id: str,
    claimed_md5: str,
    backup_row: Mapping[str, Any],
    live_zero: Mapping[str, Any],
    live_into: Optional[Mapping[str, Any]],
    live_relation_ids: Sequence[str],
) -> None:
    """Refuse the write unless the live rows still match the dry-run backup."""
    try:
        wanted = str(UUID(str(claimed_id)))
    except (ValueError, TypeError) as exc:
        raise SectionZeroPrecheckError("id must be a UUID") from exc
    digest = _norm_md5(claimed_md5)
    zero_id = str(live_zero.get("id") or "")
    if zero_id != wanted:
        raise SectionZeroPrecheckError("live section id does not match --id")
    if not _is_section_zero(str(live_zero.get("section_number") or "")):
        raise SectionZeroPrecheckError("live section_number is not 0")
    live_md5 = section_text_md5(str(live_zero.get("text") or ""))
    if live_md5 != digest:
        raise SectionZeroPrecheckError("live section text md5 does not match --md5")
    if backup_row.get("proposal") != "merge":
        raise SectionZeroPrecheckError("backup does not propose a merge for this id")
    backed_zero = backup_row.get("zero_section") or {}
    if str(backed_zero.get("id") or "") != wanted:
        raise SectionZeroPrecheckError("backup section id does not match --id")
    if _norm_md5(str(backed_zero.get("md5") or "")) != digest:
        raise SectionZeroPrecheckError("backup md5 does not match --md5")
    if live_into is None:
        raise SectionZeroPrecheckError("no preceding section")
    backed_into = backup_row.get("preceding_section") or {}
    if str(live_into.get("id") or "") != str(backed_into.get("id") or ""):
        raise SectionZeroPrecheckError("preceding section id changed since the backup")
    into_md5 = section_text_md5(str(live_into.get("text") or ""))
    if into_md5 != _norm_md5(str(backed_into.get("md5") or "")):
        raise SectionZeroPrecheckError("preceding section text md5 changed since the backup")
    backed_relations = sorted(str(item) for item in backup_row.get("relation_ids") or [])
    if sorted(str(item) for item in live_relation_ids) != backed_relations:
        raise SectionZeroPrecheckError("citation links changed since the backup")


def _section_payload(section: Mapping[str, Any]) -> Dict[str, Any]:
    text = str(section.get("text") or "")
    return {
        "id": str(section.get("id")),
        "section_number": str(section.get("section_number") or "").strip(),
        "section_title": section.get("section_title"),
        "chapter": section.get("chapter"),
        "part": section.get("part"),
        "sort_key": section.get("sort_key"),
        "version_id": str(section["version_id"]) if section.get("version_id") else None,
        "version_no": section.get("version_no"),
        "section_text": text,
        "text_hash": section.get("text_hash"),
        "version_confidence": section.get("version_confidence"),
        "md5": section_text_md5(text),
    }


def _backup_row(
    statute: Mapping[str, Any],
    sections: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    relation_ids: Sequence[str],
) -> Dict[str, Any]:
    by_id = {str(section.get("id")): section for section in sections}
    zero_id = None
    proposal = "skip"
    skip_reason = None
    into_id = None
    merged_text = ""
    item: Mapping[str, Any] = {}
    if plan["merged"]:
        item = plan["merged"][0]
        zero_id = item["zero_section_id"]
        into_id = item["into_section_id"]
        merged_text = item["merged_text"]
        proposal = "merge"
    elif plan["skipped_no_previous"]:
        item = plan["skipped_no_previous"][0]
        zero_id = item["zero_section_id"]
        skip_reason = "no_preceding_section"
    elif plan["skipped_empty"]:
        item = plan["skipped_empty"][0]
        zero_id = item["zero_section_id"]
        skip_reason = "empty"
    zero = by_id.get(str(zero_id)) or {}
    into = by_id.get(str(into_id)) if into_id else None
    return {
        "statute_id": str(statute.get("id")),
        "statute_name": statute.get("name"),
        "source_url": statute.get("source_url"),
        "host": source_host(statute.get("source_url")),
        "proposal": proposal,
        "skip_reason": skip_reason,
        "section_zero_class": item.get("section_zero_class")
        or classify_section_zero_text(str(zero.get("text") or "")),
        "before_md5": item.get("before_md5"),
        "after_md5": item.get("after_md5"),
        "zero_section": _section_payload(zero),
        "preceding_section": _section_payload(into) if into else None,
        "merged_text": merged_text,
        "merged_text_hash": merged_text_hash(merged_text) if merged_text else None,
        "relation_ids": [str(rel_id) for rel_id in relation_ids],
    }


async def _load_visible_zero_statutes(db: AsyncSession) -> List[Statute]:
    rows = (
        await db.execute(
            select(Statute)
            .join(StatuteSection, StatuteSection.statute_id == Statute.id)
            .where(statute_servable())
            .where(func.btrim(StatuteSection.section_number) == "0")
            .distinct()
        )
    ).scalars().all()
    return list(rows)


async def _load_sections(db: AsyncSession, statute_ids: Sequence[UUID]) -> Dict[UUID, List[Dict[str, Any]]]:
    if not statute_ids:
        return {}
    rows = (
        await db.execute(
            select(
                StatuteSection.id,
                StatuteSection.statute_id,
                StatuteSection.section_number,
                StatuteSection.section_title,
                StatuteSection.chapter,
                StatuteSection.part,
                StatuteSection.sort_key,
                StatuteSection.created_at,
                StatuteSectionVersion.id.label("version_id"),
                StatuteSectionVersion.version_no,
                StatuteSectionVersion.section_text,
                StatuteSectionVersion.text_hash,
                StatuteSectionVersion.version_confidence,
            )
            .outerjoin(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id)
            .where(StatuteSection.statute_id.in_(list(statute_ids)))
        )
    ).all()
    grouped: Dict[UUID, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.statute_id, []).append(
            {
                "id": row.id,
                "section_number": row.section_number,
                "section_title": row.section_title,
                "chapter": row.chapter,
                "part": row.part,
                "sort_key": row.sort_key,
                "created_at": row.created_at,
                "version_id": row.version_id,
                "version_no": row.version_no,
                "text": row.section_text or "",
                "text_hash": row.text_hash,
                "version_confidence": row.version_confidence,
            }
        )
    return grouped


async def _relation_ids(db: AsyncSession, section_id: UUID) -> List[str]:
    rows = (
        await db.execute(
            select(InstrumentSectionRelation.id).where(
                InstrumentSectionRelation.target_statute_section_id == section_id
            )
        )
    ).scalars().all()
    return sorted(str(item) for item in rows)


async def _requeue_section_embedding(db: AsyncSession, section_id: UUID) -> None:
    row = (
        await db.execute(
            select(EmbeddingQueue).where(
                EmbeddingQueue.table_name == "statute_section",
                EmbeddingQueue.record_id == section_id,
            )
        )
    ).scalars().first()
    if row is None:
        db.add(
            EmbeddingQueue(
                record_id=section_id,
                table_name="statute_section",
                access_method="public",
                status="pending",
                embedding_model=settings.EMBEDDING_MODEL,
                embedding_dimensions=settings.EMBEDDING_DIM,
            )
        )
        return
    row.status = "pending"
    row.attempts = 0
    row.error_message = None


async def _apply_merge(db: AsyncSession, item: Mapping[str, Any]) -> None:
    target_id = UUID(str(item["into_section_id"]))
    zero_id = UUID(str(item["zero_section_id"]))
    merged_text = str(item["merged_text"])
    text_hash = merged_text_hash(merged_text)
    target = (
        await db.execute(select(StatuteSection).where(StatuteSection.id == target_id))
    ).scalars().one()
    existing = (
        await db.execute(
            select(StatuteSectionVersion).where(
                StatuteSectionVersion.section_id == target.id,
                StatuteSectionVersion.text_hash == text_hash,
            )
        )
    ).scalars().first()
    if existing is None:
        current_no = (
            await db.execute(
                select(func.max(StatuteSectionVersion.version_no)).where(
                    StatuteSectionVersion.section_id == target.id
                )
            )
        ).scalar() or 0
        existing = StatuteSectionVersion(
            section_id=target.id,
            version_no=int(current_no) + 1,
            section_text=merged_text,
            text_hash=text_hash,
            version_confidence=0.5,
        )
        db.add(existing)
        await db.flush()
    target.current_version_id = existing.id
    await db.flush()
    await db.execute(
        update(InstrumentSectionRelation)
        .where(InstrumentSectionRelation.target_statute_section_id == zero_id)
        .values(target_statute_section_id=target_id)
    )
    await db.execute(
        delete(EmbeddingQueue).where(
            EmbeddingQueue.table_name == "statute_section",
            EmbeddingQueue.record_id == zero_id,
        )
    )
    await db.execute(delete(StatuteSection).where(StatuteSection.id == zero_id))
    await db.execute(
        delete(StatuteSectionReextractPrior).where(StatuteSectionReextractPrior.section_id == zero_id)
    )
    await _requeue_section_embedding(db, target_id)


async def run_section_zero_report(
    *,
    apply: bool = False,
    section_id: str = "",
    section_md5: str = "",
    out_dir: Path = Path("section_zero_reports"),
    backup_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """List visible section 0 rows. Write the database only when ``apply`` is true.

    ``apply`` merges the one section named by ``section_id`` after the md5
    precheck. Quarantine rows are never inserted or removed.
    """
    if apply and (not section_id or not section_md5):
        raise SectionZeroPrecheckError("--apply requires --id and --md5")
    async with SessionLocal() as db:
        statutes = await _load_visible_zero_statutes(db)
        statute_ids = [statute.id for statute in statutes]
        grouped = await _load_sections(db, statute_ids)
        rows: List[Dict[str, Any]] = []
        plans: Dict[str, Dict[str, Any]] = {}
        for statute in sorted(statutes, key=lambda item: str(item.id)):
            sections = grouped.get(statute.id, [])
            plan = plan_section_zero_merges(sections)
            plans[str(statute.id)] = plan
            zero_ids = [
                item["zero_section_id"]
                for item in plan["merged"] + plan["skipped_no_previous"] + plan["skipped_empty"]
            ]
            relation_ids: List[str] = []
            if zero_ids:
                relation_ids = await _relation_ids(db, UUID(zero_ids[0]))
            rows.append(
                _backup_row(
                    {
                        "id": statute.id,
                        "name": statute.name,
                        "source_url": statute.source_url,
                    },
                    sections,
                    plan,
                    relation_ids,
                )
            )
        summary = {
            "dry_run": not apply,
            "apply": apply,
            "statutes": len(rows),
            "merged": sum(1 for row in rows if row["proposal"] == "merge"),
            "skipped_no_previous": sum(1 for row in rows if row["skip_reason"] == "no_preceding_section"),
            "skipped_empty": sum(1 for row in rows if row["skip_reason"] == "empty"),
            "rows": [
                {
                    "statute_id": row["statute_id"],
                    "statute_name": row["statute_name"],
                    "host": row["host"],
                    "proposal": row["proposal"],
                    "skip_reason": row["skip_reason"],
                    "zero_section_id": row["zero_section"]["id"],
                    "zero_md5": row["zero_section"]["md5"],
                    "section_zero_class": row["section_zero_class"],
                    "into_section_id": (row["preceding_section"] or {}).get("id"),
                    "into_section_number": (row["preceding_section"] or {}).get("section_number"),
                    "before_md5": row["before_md5"],
                    "after_md5": row["after_md5"],
                }
                for row in rows
            ],
        }
        if not apply:
            paths = write_report_files(out_dir, rows)
            summary.update(paths)
            await db.rollback()
            return summary

        # The backup is the dry-run pre-image. Do not rewrite it from the live
        # rows before the check: a row that changed would otherwise match itself.
        backup = load_backup_rows(backup_path or (out_dir / BACKUP_NAME))
        try:
            wanted = str(UUID(str(section_id)))
        except (ValueError, TypeError) as exc:
            await db.rollback()
            raise SectionZeroPrecheckError("id must be a UUID") from exc
        match = next((row for row in rows if row["zero_section"]["id"] == wanted), None)
        if match is None or wanted not in backup:
            await db.rollback()
            raise SectionZeroPrecheckError("id is not a visible section_number 0 row in the backup")
        statute_id = UUID(match["statute_id"])
        sections = grouped.get(statute_id, [])
        by_id = {str(section["id"]): section for section in sections}
        live_zero = by_id.get(wanted)
        into_id = (match.get("preceding_section") or {}).get("id")
        live_into = by_id.get(str(into_id)) if into_id else None
        relation_ids = await _relation_ids(db, UUID(wanted))
        try:
            precheck_section_zero(
                claimed_id=wanted,
                claimed_md5=section_md5,
                backup_row=backup[wanted],
                live_zero=live_zero or {},
                live_into=live_into,
                live_relation_ids=relation_ids,
            )
        except SectionZeroPrecheckError:
            await db.rollback()
            raise
        plan = plans[str(statute_id)]
        item = next(entry for entry in plan["merged"] if entry["zero_section_id"] == wanted)
        await _apply_merge(db, item)
        await db.commit()
        summary["applied_section_id"] = wanted
        summary["dry_run"] = False
        return summary


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.csv:
        if args.apply:
            print(
                json.dumps({"apply": False, "error": "--csv does not write and cannot be combined with --apply"}),
                file=sys.stderr,
            )
            return 2
        rows: List[Dict[str, Any]] = []
        for raw in args.csv:
            rows.extend(classify_section_zero_csv(Path(raw)))
        counts = {kind: 0 for kind in SECTION_ZERO_CLASSES}
        for row in rows:
            counts[row["section_zero_class"]] = counts.get(row["section_zero_class"], 0) + 1
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "writes": False,
                    "counts": counts,
                    "note": (
                        "Each row is class A, B, C, or unknown. "
                        "proposed_merge_target is the preceding section when sort_key is after 1. "
                        "before_md5 and after_md5 need that section's stored text, which this CSV does not carry. "
                        "A database dry-run fills them. This command writes nothing."
                    ),
                    "rows": rows,
                },
                indent=2,
            )
        )
        return 0
    out_dir = Path(args.out)
    backup = Path(args.backup) if args.backup else None
    if args.apply and (not args.id or not args.md5):
        print(
            json.dumps({"apply": False, "error": "--apply requires --id and --md5"}),
            file=sys.stderr,
        )
        return 2
    try:
        out = run_async(
            run_section_zero_report(
                apply=args.apply,
                section_id=args.id,
                section_md5=args.md5,
                out_dir=out_dir,
                backup_path=backup,
            )
        )
    except SectionZeroPrecheckError as exc:
        print(json.dumps({"apply": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
