"""
The corpus record format, version 1: the one shape in which every scraper hands a document on.

Both the PakistanLawSite scraper and the free-sites harvester save each judgment as a folder

    <prefix>/original.pdf  or  <prefix>/rendered_copy.pdf
    <prefix>/judgment.txt
    <prefix>/metadata.json          <- the record built here

so the ingestion stage (OCR, indexing, learning) reads one format whichever scraper collected the
document. docs/CORPUS_RECORD_FORMAT.md describes every field in plain terms.

Version 1 keeps every key of the earlier, unversioned metadata.json, so a reader that knew that file
still works. A metadata.json without "schema_version" is a version 0 record written before this module.

needs_ocr marks a record whose text is too thin to be the document's real text (a scanned PDF): the
scraper saves it as it is and moves on, and ingestion does the OCR. No scraper reads scanned pages.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from scraper.parsers.pdf_writer import RENDERED_COPY_LABEL

SCHEMA_VERSION = 1

# Text shorter than this (after stripping) is not the document's real text. The same threshold has
# long decided, in scraper/fetchers.py, whether a PDF counts as scanned.
THIN_TEXT_CHARS = 200

RECORD_KINDS = ("judgment", "statute_section")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_JUDGMENT_REQUIRED = (
    "canonical_citation",
    "court",
    "year",
    "source_name",
    "source_url",
    "access_method",
    "full_text_sha256_canonical",
    "has_original_pdf",
    "needs_ocr",
    "files",
)
_STATUTE_REQUIRED = (
    "statute_name",
    "jurisdiction",
    "section_number",
    "version_no",
    "text_sha256",
    "source_name",
    "needs_ocr",
)


def text_is_thin(text: Optional[str]) -> bool:
    return len((text or "").strip()) < THIN_TEXT_CHARS


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return None


def judgment_record(j: Any, *, has_original_pdf: bool) -> Dict[str, Any]:
    """metadata.json for one promoted judgment. `has_original_pdf` says whether the folder carries the
    genuine source PDF (True) or a rendered copy of the text (False)."""
    reported = bool(j.reporter)
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "judgment",
        # keys of the earlier, unversioned metadata.json, unchanged
        "canonical_citation": j.canonical_citation,
        "case_title": j.case_title,
        "court": j.court_name,
        "judge_names": j.judge_names,
        "bench_size": j.bench_size,
        "bench_type": j.bench_type,
        "decision_date": _iso(j.decision_date),
        "year": j.year,
        "source_name": j.source_name,
        "source_url": j.source_url,
        "access_method": j.access_method,
        "full_text_sha256_canonical": j.full_text_hash,
        "has_original_pdf": bool(has_original_pdf),
        "document_label": "ORIGINAL SOURCE PDF" if has_original_pdf else RENDERED_COPY_LABEL,
        "judgment_id": str(j.id),
        # added in version 1
        "reported": reported,
        "reporter": j.reporter,
        "volume": j.volume,
        "page_number": j.page_number,
        "docket_number": j.docket_number,
        "petitioner": j.petitioner,
        "respondent": j.respondent,
        "language": j.language,
        "needs_ocr": text_is_thin(j.full_text),
        "collected_at": _iso(j.promoted_at),
        "files": {
            "document": "original.pdf" if has_original_pdf else "rendered_copy.pdf",
            "text": "judgment.txt",
        },
    }


def statute_section_record(statute: Any, section: Any, version: Any) -> Dict[str, Any]:
    """Record for one version of one statute section (the text itself travels beside it as a .txt)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "statute_section",
        "statute_name": statute.name,
        "short_name": statute.short_name,
        "jurisdiction": statute.jurisdiction,
        "statute_type": statute.statute_type,
        "year_enacted": statute.year_enacted,
        "is_repealed": bool(statute.is_repealed),
        "section_number": section.section_number,
        "section_title": section.section_title,
        "chapter": section.chapter,
        "part": section.part,
        "version_no": version.version_no,
        "effective_from": _iso(version.effective_from),
        "effective_to": _iso(version.effective_to),
        "text_sha256": version.text_hash,
        "source_name": statute.source_name,
        "source_url": statute.source_url,
        # A section can be one line long, so only a section with no readable text at all needs OCR.
        "needs_ocr": not (version.section_text or "").strip(),
    }


def validate_record(rec: Dict[str, Any]) -> List[str]:
    """Problems with a version 1 record, as plain sentences; an empty list means the record is sound.
    The ingestion stage runs this on every record it reads and sets aside any record that fails."""
    problems: List[str] = []
    if not isinstance(rec, dict):
        return ["record is not a JSON object"]
    if rec.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version is {rec.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    kind = rec.get("record_kind")
    if kind not in RECORD_KINDS:
        return problems + [f"record_kind {kind!r} is not one of {', '.join(RECORD_KINDS)}"]
    required = _JUDGMENT_REQUIRED if kind == "judgment" else _STATUTE_REQUIRED
    problems += [f"{k} is missing" for k in required if k not in rec]
    if not isinstance(rec.get("needs_ocr"), bool):
        problems.append("needs_ocr must be true or false")
    if kind == "judgment":
        if not rec.get("canonical_citation"):
            problems.append("canonical_citation is empty")
        year = rec.get("year")
        if year is not None and not (isinstance(year, int) and 1947 <= year <= 2100):
            problems.append(f"year {year!r} is outside 1947-2100")
        digest = rec.get("full_text_sha256_canonical")
        if digest is not None and not (isinstance(digest, str) and _SHA256.match(digest)):
            problems.append("full_text_sha256_canonical is not a sha256 hex digest")
        files = rec.get("files")
        if not isinstance(files, dict) or files.get("document") not in ("original.pdf", "rendered_copy.pdf") or files.get("text") != "judgment.txt":
            problems.append("files must name the document (original.pdf or rendered_copy.pdf) and judgment.txt")
        elif (files.get("document") == "original.pdf") != bool(rec.get("has_original_pdf")):
            problems.append("files.document does not agree with has_original_pdf")
    else:
        if not rec.get("statute_name") or not rec.get("section_number"):
            problems.append("statute_name and section_number must not be empty")
        digest = rec.get("text_sha256")
        if not (isinstance(digest, str) and _SHA256.match(digest)):
            problems.append("text_sha256 is not a sha256 hex digest")
    return problems
