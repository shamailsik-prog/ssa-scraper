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

A second dry-run refuses to replace recovery files that are already in
``--out``. Pass a new directory. ``--apply`` locks the section 0 row and the
preceding section, rereads them, and writes only when their version pointers
still match the backup. The rollback restores every backed-up version and
queues both sections for embedding. It deletes a merged version only when
that version did not already exist.

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

# `python scripts/report_section_zero.py` puts scripts/ on sys.path, not the
# repository root, so `import scraper` fails in an image that does not install
# the project. The root is this file's parent.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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


def _sql_date(value: Optional[str]) -> str:
    if not value:
        return "NULL"
    raw = str(value)[:10]
    if not _ISO_DATE_RE.fullmatch(raw):
        raise SectionZeroPrecheckError("version date is not YYYY-MM-DD")
    return f"DATE '{raw}'"


def _sql_uuid_or_null(value: Optional[str]) -> str:
    if not value:
        return "NULL"
    return _sql_uuid(value)


def _version_rows(zero: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Every version the backup can restore. A lone current version is the old shape."""
    versions = zero.get("versions")
    if isinstance(versions, list):
        return list(versions)
    if not zero.get("version_id"):
        return []
    return [
        {
            "id": zero.get("version_id"),
            "version_no": zero.get("version_no") or 1,
            "section_text": zero.get("section_text") or "",
            "text_hash": zero.get("text_hash") or "",
            "effective_from": None,
            "effective_to": None,
            "amending_instrument_id": None,
            "amending_instrument_text": None,
            "version_confidence": zero.get("version_confidence"),
            "source_provenance_id": None,
        }
    ]


def _embedding_requeue_sql(section_id_sql: str) -> str:
    """Ask the worker to rebuild the vector. The script does not copy vectors."""
    model = _dollar_quote(str(settings.EMBEDDING_MODEL or ""))
    dimensions = int(settings.EMBEDDING_DIM or 0)
    return (
        "INSERT INTO embedding_queue "
        "(id, record_id, table_name, access_method, status, attempts, max_attempts, "
        "embedding_model, embedding_dimensions) "
        f"VALUES (gen_random_uuid(), {section_id_sql}, 'statute_section', 'public', "
        f"'pending', 0, 3, {model}, {dimensions}) "
        "ON CONFLICT ON CONSTRAINT uq_embedding_record DO UPDATE "
        "SET status = 'pending', attempts = 0, error_message = NULL, updated_at = now();"
    )


def render_rollback_sql(rows: Sequence[Mapping[str, Any]]) -> str:
    """SQL an operator can run to put merged rows back. This script never runs it."""
    lines = [
        "-- section-zero rollback. report_section_zero.py does not execute this file.",
        "-- Run one block only for a merge that --apply wrote, after reading it.",
        "-- Each block restores the preceding section's version pointer, deletes the",
        "-- merged version only when this repair created it, and inserts every",
        "-- backed-up version of section 0, including dates and provenance.",
        "-- Both sections are queued so their embeddings are rebuilt.",
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
        merged_hash = str(row.get("merged_text_hash") or "")
        merged_existed = bool(row.get("merged_version_id"))
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
        if merged_hash and into.get("version_id") and not merged_existed:
            lines.append(
                "DELETE FROM statute_section_version "
                f"WHERE section_id = {into_id} "
                f"AND text_hash = {_dollar_quote(merged_hash)} "
                f"AND id <> {old_version};"
            )
        lines.append(
            "INSERT INTO statute_section "
            "(id, statute_id, section_number, section_title, chapter, part, sort_key, current_version_id) "
            f"VALUES ({zero_id}, {statute_id}, '0', {title_sql}, {chapter_sql}, {part_sql}, {sort_sql}, NULL);"
        )
        for version in _version_rows(zero):
            confidence = version.get("version_confidence")
            confidence_sql = "0.5" if confidence is None else str(float(confidence))
            lines.append(
                "INSERT INTO statute_section_version "
                "(id, section_id, version_no, section_text, text_hash, effective_from, effective_to, "
                "amending_instrument_id, amending_instrument_text, version_confidence, source_provenance_id) "
                f"VALUES ({_sql_uuid(version['id'])}, {zero_id}, {int(version.get('version_no') or 1)}, "
                f"{_dollar_quote(str(version.get('section_text') or ''))}, "
                f"{_dollar_quote(str(version.get('text_hash') or ''))}, "
                f"{_sql_date(version.get('effective_from'))}, {_sql_date(version.get('effective_to'))}, "
                f"{_sql_uuid_or_null(version.get('amending_instrument_id'))}, "
                f"{_dollar_quote(str(version.get('amending_instrument_text') or '')) if version.get('amending_instrument_text') else 'NULL'}, "
                f"{confidence_sql}, {_sql_uuid_or_null(version.get('source_provenance_id'))});"
            )
        if zero.get("version_id"):
            lines.append(
                f"UPDATE statute_section SET current_version_id = {_sql_uuid(zero['version_id'])} "
                f"WHERE id = {zero_id};"
            )
        if relation_ids:
            id_list = ", ".join(_sql_uuid(item) for item in relation_ids)
            lines.append(
                "UPDATE instrument_section_relation "
                f"SET target_statute_section_id = {zero_id} WHERE id IN ({id_list});"
            )
        lines.append(f"DELETE FROM statute_section_reextract_prior WHERE section_id = {zero_id};")
        lines.append(_embedding_requeue_sql(zero_id))
        lines.append(_embedding_requeue_sql(into_id))
        lines.append("COMMIT;")
        lines.append("")
    return "\n".join(lines)


def write_report_files(out_dir: Path, rows: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    backup = out_dir / BACKUP_NAME
    rollback = out_dir / ROLLBACK_NAME
    if backup.exists() or rollback.exists():
        raise SectionZeroPrecheckError(
            f"recovery files already exist in {out_dir}; pass a fresh --out directory"
        )
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
    live_version_ids: Sequence[str] = (),
    live_merged_version_id: Optional[str] = None,
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
    if str(live_zero.get("version_id") or "") != str(backed_zero.get("version_id") or ""):
        raise SectionZeroPrecheckError("section 0 version pointer changed since the backup")
    if str(live_into.get("version_id") or "") != str(backed_into.get("version_id") or ""):
        raise SectionZeroPrecheckError("preceding section version pointer changed since the backup")
    backed_versions = backed_zero.get("versions")
    if not isinstance(backed_versions, list):
        raise SectionZeroPrecheckError("backup has no restorable version history")
    backed_ids: List[str] = []
    for version in backed_versions:
        version_id = str(version.get("id") or "")
        if not version_id or version.get("section_text") is None or not version.get("text_hash"):
            raise SectionZeroPrecheckError("backup version is missing id, text, or hash")
        if version.get("version_no") is None:
            raise SectionZeroPrecheckError("backup version is missing a version number")
        for field in ("effective_from", "effective_to"):
            if version.get(field):
                _sql_date(version.get(field))
        backed_ids.append(version_id)
    current = str(backed_zero.get("version_id") or "")
    if current and current not in backed_ids:
        raise SectionZeroPrecheckError("backup version history does not include the current version")
    if not current and backed_ids:
        raise SectionZeroPrecheckError("backup version history does not match the current pointer")
    if sorted(str(item) for item in live_version_ids) != sorted(backed_ids):
        raise SectionZeroPrecheckError("section 0 version history changed since the backup")
    backed_merged = backup_row.get("merged_version_id") or None
    if (live_merged_version_id or None) != backed_merged:
        raise SectionZeroPrecheckError("merged version identity changed since the backup")


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
        "versions": list(section.get("versions") or []),
    }


def _backup_row(
    statute: Mapping[str, Any],
    sections: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    relation_ids: Sequence[str],
    merged_version_id: Optional[str] = None,
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
        "merged_version_id": merged_version_id,
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


def _version_payload(row: StatuteSectionVersion) -> Dict[str, Any]:
    return {
        "id": str(row.id),
        "version_no": row.version_no,
        "section_text": row.section_text,
        "text_hash": row.text_hash,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "amending_instrument_id": str(row.amending_instrument_id) if row.amending_instrument_id else None,
        "amending_instrument_text": row.amending_instrument_text,
        "version_confidence": row.version_confidence,
        "source_provenance_id": str(row.source_provenance_id) if row.source_provenance_id else None,
    }


async def _load_version_history(
    db: AsyncSession, section_ids: Sequence[UUID]
) -> Dict[UUID, List[Dict[str, Any]]]:
    if not section_ids:
        return {}
    rows = (
        await db.execute(
            select(StatuteSectionVersion)
            .where(StatuteSectionVersion.section_id.in_(list(section_ids)))
            .order_by(StatuteSectionVersion.version_no, StatuteSectionVersion.id)
        )
    ).scalars().all()
    grouped: Dict[UUID, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.section_id, []).append(_version_payload(row))
    return grouped


async def _version_id_for_hash(db: AsyncSession, section_id: UUID, text_hash: str) -> Optional[str]:
    found = (
        await db.execute(
            select(StatuteSectionVersion.id).where(
                StatuteSectionVersion.section_id == section_id,
                StatuteSectionVersion.text_hash == text_hash,
            )
        )
    ).scalar_one_or_none()
    return str(found) if found else None


async def _lock_sections(db: AsyncSession, section_ids: Sequence[UUID]) -> None:
    ids = sorted(set(section_ids), key=str)
    if not ids:
        return
    await db.execute(
        select(StatuteSection.id)
        .where(StatuteSection.id.in_(ids))
        .order_by(StatuteSection.id)
        .with_for_update()
    )


async def _lock_relation_ids(db: AsyncSession, section_id: UUID) -> List[str]:
    rows = (
        await db.execute(
            select(InstrumentSectionRelation.id)
            .where(InstrumentSectionRelation.target_statute_section_id == section_id)
            .order_by(InstrumentSectionRelation.id)
            .with_for_update()
        )
    ).scalars().all()
    return sorted(str(item) for item in rows)


async def _apply_merge(
    db: AsyncSession,
    item: Mapping[str, Any],
    *,
    expected_into_version: Optional[UUID],
    expected_zero_version: Optional[UUID],
    reuse_version_id: Optional[UUID],
) -> None:
    target_id = UUID(str(item["into_section_id"]))
    zero_id = UUID(str(item["zero_section_id"]))
    merged_text = str(item["merged_text"])
    text_hash = merged_text_hash(merged_text)
    if reuse_version_id is None:
        current_no = (
            await db.execute(
                select(func.max(StatuteSectionVersion.version_no)).where(
                    StatuteSectionVersion.section_id == target_id
                )
            )
        ).scalar() or 0
        created = StatuteSectionVersion(
            section_id=target_id,
            version_no=int(current_no) + 1,
            section_text=merged_text,
            text_hash=text_hash,
            version_confidence=0.5,
        )
        db.add(created)
        await db.flush()
        new_version_id = created.id
    else:
        new_version_id = reuse_version_id
    into_clause = (
        StatuteSection.current_version_id.is_(None)
        if expected_into_version is None
        else StatuteSection.current_version_id == expected_into_version
    )
    pointed = await db.execute(
        update(StatuteSection)
        .where(StatuteSection.id == target_id, into_clause)
        .values(current_version_id=new_version_id)
    )
    if pointed.rowcount != 1:
        raise SectionZeroPrecheckError("preceding section version pointer changed before the write")
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
    zero_clause = (
        StatuteSection.current_version_id.is_(None)
        if expected_zero_version is None
        else StatuteSection.current_version_id == expected_zero_version
    )
    removed = await db.execute(
        delete(StatuteSection).where(StatuteSection.id == zero_id, zero_clause)
    )
    if removed.rowcount != 1:
        raise SectionZeroPrecheckError("section 0 version pointer changed before the delete")
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
        zero_section_ids = [
            section["id"]
            for sections in grouped.values()
            for section in sections
            if _is_section_zero(str(section.get("section_number") or ""))
        ]
        history = await _load_version_history(db, zero_section_ids)
        for sections in grouped.values():
            for section in sections:
                if section["id"] in history:
                    section["versions"] = history[section["id"]]
        rows: List[Dict[str, Any]] = []
        for statute in sorted(statutes, key=lambda item: str(item.id)):
            sections = grouped.get(statute.id, [])
            plan = plan_section_zero_merges(sections)
            zero_ids = [
                item["zero_section_id"]
                for item in plan["merged"] + plan["skipped_no_previous"] + plan["skipped_empty"]
            ]
            relation_ids: List[str] = []
            if zero_ids:
                relation_ids = await _relation_ids(db, UUID(zero_ids[0]))
            merged_version_id = None
            if plan["merged"]:
                merged_version_id = await _version_id_for_hash(
                    db,
                    UUID(plan["merged"][0]["into_section_id"]),
                    merged_text_hash(plan["merged"][0]["merged_text"]),
                )
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
                    merged_version_id,
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
        into_id = (match.get("preceding_section") or {}).get("id")
        lock_ids = [UUID(wanted)]
        if into_id:
            lock_ids.append(UUID(str(into_id)))
        await _lock_sections(db, lock_ids)
        relation_ids = await _lock_relation_ids(db, UUID(wanted))
        fresh = await _load_sections(db, [statute_id])
        fresh_history = await _load_version_history(db, [UUID(wanted)])
        by_id = {str(section["id"]): section for section in fresh.get(statute_id, [])}
        live_zero = by_id.get(wanted) or {}
        live_into = by_id.get(str(into_id)) if into_id else None
        live_versions = fresh_history.get(UUID(wanted), [])
        if live_zero:
            live_zero["versions"] = live_versions
        locked_merge = ""
        if live_into is not None and str(live_zero.get("text") or "").strip():
            locked_merge = str(live_into.get("text") or "").rstrip() + "\n" + str(live_zero.get("text") or "").lstrip()
        reuse_id = None
        if live_into is not None and locked_merge:
            reuse_id = await _version_id_for_hash(
                db, UUID(str(live_into["id"])), merged_text_hash(locked_merge)
            )
        try:
            precheck_section_zero(
                claimed_id=wanted,
                claimed_md5=section_md5,
                backup_row=backup[wanted],
                live_zero=live_zero,
                live_into=live_into,
                live_relation_ids=relation_ids,
                live_version_ids=[str(version["id"]) for version in live_versions],
                live_merged_version_id=reuse_id,
            )
        except SectionZeroPrecheckError:
            await db.rollback()
            raise
        await _apply_merge(
            db,
            {
                "into_section_id": str(live_into["id"]),
                "zero_section_id": wanted,
                "merged_text": locked_merge,
            },
            expected_into_version=UUID(str(live_into["version_id"])) if live_into and live_into.get("version_id") else None,
            expected_zero_version=UUID(str(live_zero["version_id"])) if live_zero.get("version_id") else None,
            reuse_version_id=UUID(reuse_id) if reuse_id else None,
        )
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
